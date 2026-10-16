---
name: fm-inbox-batch-processor
description: Process all eligible local inbox files through proposal-only free-form interpretation, with optional from-scratch reprocessing and human-resolution outcomes.
metadata:
  reasoning_class: medium
  business_logic: true
  minimum_capability: C2
  capability_reference: docs/references/model-capability-matrix.md
---

# When to use
- Use for whole-inbox processing or re-processing of local medical files under `data/inbox/` when the user wants high-confidence source-backed proposals before canonical persistence.
- Use when the user asks to process every inbox file, re-process everything from scratch, triage shared/unresolved inbox files, or identify gaps blocking confident fact recording.
- Use before live model-backed whole-inbox execution; fixture-backed manual verification remains the release gate.
- Do not use for a single explicitly named source file; use `fm-inbox-freeform-processor` for that.
- Do not use to persist canonical medical truth directly; this skill only coordinates Clinical Interpreter proposals and human-resolution outcomes.

# Inputs
- Inbox root under `data/`, normally `data/inbox/`.
- Run mode: incremental scan, from-scratch reprocessing, or an explicit path list scoping the run to a named subset of source files. From-scratch mode must re-read local source files and must not trust stale ledger paths, prior route states, or prior quarantine reasons as current truth. An explicit path list reprocesses exactly those files, without scanning the rest of the inbox root.
- Known people, aliases, family context, and any person-matching data available to the current run.
- Active category vocabulary and candidate schemas for labs, medications, history, procedures, imaging/pathology placeholders, unsupported content, and future categories.
- Existing local fixture expectations and verification feedback, when available, such as `data/manual-testing/freeform-inbox-fixtures.yaml`.
- Minimum model capability reference: `docs/references/model-capability-matrix.md` level C2 before live invocation, and C3 when cross-document ambiguity or conflicting evidence requires careful decomposition. **Exception: when a live AI agent is executing this skill, the capability gate is already satisfied — see the Live AI Agent Sessions section of the capability matrix.**

# Procedure
1. Register the batch without clinical interpretation: inbox root, run mode, source count, file types, parser/OCR availability, fixture set used, and whether stale ledgers are ignored.
2. Enumerate existing source files from the filesystem under the inbox root. Exclude directories and non-medical control files. Preserve original filenames. In from-scratch mode, treat ledgers only as historical hints, never as source-of-truth for file existence, filename spelling, person, category, or prior outcome. When an explicit path list is given, enumerate exactly those paths instead of scanning the inbox root; a listed path that no longer exists on the filesystem gets its own `source_missing` outcome rather than being silently dropped from the batch.
3. Classify each source into an initial processing lane: deterministic handler candidate, free-form text/PDF candidate, scanned/TIF/OCR-needed candidate, structured XML/CSV/TSV/HL7 candidate, unsupported content, or needs human routing. C-CDA/IHE health-summary XML is a mixed clinical document when it contains medication, result, vital-sign, procedure, encounter, or diagnosis sections; do not force it into a history-only lane just because the retained-source fallback is a history review note.
4. For each eligible free-form text/PDF source, apply `fm-inbox-freeform-processor` semantics: source metadata, person/category/date/type proposal, candidate facts, field confidence, overall confidence, and evidence snippets or coordinates. Keep single-file processing output proposal-only. **Read each source file directly using native tools; the agent executing this skill is the interpreter.**
5. For deterministic handler candidates, report the intended deterministic handler and reason. Do not duplicate structured-handler work inside this skill unless the current run explicitly asks for manual proposal review.
6. For scanned/TIF/OCR-needed sources, report `needs_review` or `unsupported_content` unless a local OCR/parser helper is available and evidence snippets can be produced.
7. Assign each file exactly one batch outcome: `high_confidence_ready_for_canonicalizer`, `needs_user_verification`, `needs_person_resolution`, `needs_category_resolution`, `needs_logic_refinement`, `needs_ocr_or_parser`, `unsupported_content`, `deterministic_handler_candidate`, `quarantine_recommended`, or (explicit path list runs only) `source_missing` for a listed path no longer on disk.
8. For non-high-confidence outcomes, include concrete user actions:
   - Verify, approve, edit, reject, or quarantine candidate facts.
   - Clarify missing context needed to improve confidence.
   - Dismiss a wrong-person/not-relevant document.
   - Assign an existing person.
   - Provide or update person aliases/family context/matching data.
   - Request refinement of skill logic, fixture expectations, preprocessing, OCR, or parser behavior.
9. Treat unrecognized person as a first-class resolution path. Distinguish evidence strength before deferring:
   - **Weak evidence** (no naming pattern, no test-type signal, no same-visit clustering): output `needs_person_resolution` with available signals only.
   - **Medium evidence** (consistent naming convention such as one suffix for `dummy_bob` and another for `dummy_alice`, test type implying sex or age cohort, same-visit date clustering with a fixture-confirmed file): present a ranked best-guess candidate with confidence and ask a single Y/N confirmation — do not silently defer. Example: "Filename pattern and PSA test suggest person=dummy_alice. Confirm? (yes / no / different person)".
   - **Strong evidence** (person-scoped subdirectory path, accepted fixture): proceed without deferral.
   Always show the source path, candidate person(s), evidence, confidence level, and decision options.
   When the user confirms person identity and opts to update the person record, distinguish field stability:
   - **Stable identity fields** (legal name, full date of birth, system patient identifiers / MRNs): these belong in the person profile (`Persons/<id>.md` and `templates/person-template.yaml`).
   - **Dynamic / time-sensitive fields** (primary care provider, referring provider, address, insurance, phone): these change between encounters and must NOT be added to the person profile. Record them in the individual source document's encounter note or record metadata instead.
10. Preserve fact-recording boundaries. Extract source-labeled facts as written and defer applicability, clinical meaning, unit normalization, coding, deduplication, and persistence to downstream Canonicalizer, Deduplication Engine, Human Resolver, and Data Steward roles.
    - For mixed C-CDA/IHE sources, surface the category split explicitly: history/diagnoses/procedures, labs/results, medications, vitals, immunizations, and other supported categories. If the current deterministic pipeline cannot canonicalize one category from C-CDA yet, report that category as `needs_logic_refinement` instead of silently storing the entire source as a raw history note.
11. Only surface facts that require human judgment. Auto-accept high-confidence unambiguous facts and record them in the batch summary without asking. Ask about a fact only when: confidence is below high, the date is filename-derived only, the value requires an encoding decision, the routing category is ambiguous, or the source has an anomaly (mislabeled file, date discrepancy, new test name).
    - Present **one question at a time** across the entire batch. Wait for the user's response before presenting the next question.
    - Each question must include a clickable file link (`file:///absolute/path/to/file`), the specific fact or decision at issue, evidence from the source, and labeled options (e.g., `accept / edit <field>=<value> / reject / quarantine`).
    - If the user indicates incorrect processing on a multi-fact file, switch to per-fact confirmation for that file: present each disputed fact individually, one at a time, waiting for a response before the next.
    - Do not bundle multiple files or multiple questions into a single message.
    Never batch human action items into a grouped list as the final output. The batch summary (step 12) identifies which files need input; the sequential one-at-a-time question loop resolves them.
12. Produce a batch summary with counts by outcome, then per-file structured results. The report must make it clear which files are ready for deterministic canonicalizer hand-off and which require user action or skill/pipeline improvement.
13. Record verification feedback for skill tuning when the user supplies decisions. Call `scripts/record_inbox_feedback.py --run-id <ISO-timestamp> --records-json '[...]'` with one object per file, including `source_path`, `confidence_tier`, `proposal_outcome`, `user_decision` (accepted/edited/rejected/dismissed/deferred or omit for null), `uncertainty_reason` (if non-high-confidence), and any available `person_id`, `category`, `vault_note_path`, or `reviewer_note`. Records accumulate in `data/inbox-run-feedback.jsonl` across runs. Note: quarantine and person-assignment outcomes are system outcomes, not user decisions — encode them in `proposal_outcome` (e.g. `"quarantine_recommended"`, `"person_assigned"`) and leave `user_decision` null.
14. Keep Data Steward persistence separate. This skill never writes vault notes, DB rows, processed files, quarantine files, or audit events directly.

# Skeptic pass
- Stop if any step would let this skill or AI output write canonical storage without Canonicalizer and Data Steward gates.
- Stop if from-scratch mode relies on `data/inbox_state.tsv` or another stale ledger instead of re-reading current source files.
- Stop if an explicit path list run silently drops a path that no longer exists on disk instead of reporting `source_missing` for it.
- Stop if a file disappears, matches multiple existing sources, or cannot be read and no human-resolution path is shown.
- Stop if low-confidence, unknown-person, unsupported, or OCR-needed files are summarized without concrete user actions.
- Stop if a per-file verification block is presented without a `file:///` link to the source file.
- Stop if more than one uncertain item is presented at the same time without the user having responded to the previous one.
- Stop if a high-confidence, unambiguous fact is presented for user verification instead of being auto-accepted.
- Stop if high-confidence status is assigned without evidence snippets/source coordinates for extracted medical facts.
- Stop if a mixed document is forced into one category when evidence supports multiple categories or an unsupported placeholder.
- Stop if a C-CDA/IHE health summary with labs or medications is reported as successfully processed solely because a history review note was written.
- Stop if asked to route interpretation through a model-provider automation layer. No such layer exists; the agent executing this skill is the interpreter, and reintroducing an unattended provider path requires its own ADR.
- Stop if hand-off or action output references implementation-specific CLI flags, script invocation parameters, or execution notes sourced from a stale ledger or prior run state.
- Stop if a person-profile update proposes adding dynamic / time-sensitive fields (PCP, referring provider, address, insurance, phone) to the person profile instead of to the individual encounter record.

# Output
```md
## Whole-Inbox Processing Result
- Inbox root:
- Run mode:
- Source count:
- Fixture/feedback basis:
- Batch outcome counts:
- High-confidence canonicalizer candidates:
- Human action needed:
- Manual verification requested:
- Logic/parser refinement needed:
- Unsupported or quarantine recommended:

## Per-File Results
- Source:
- File link: file:///absolute/path
- Matched current filesystem path:
- File type and extraction method:
- Proposed person/category/date/type:
- Outcome:
- Confidence summary:
- Candidate facts summary:
- Evidence summary:
- Required user action or hand-off:
- Manual verification step:
- Skill-tuning feedback:

## Reprocessing Notes
- Stale ledger assumptions ignored:
- Explicit-subset paths no longer found on disk:
- Files needing person-data updates:
- Files needing fixture/preprocessing/OCR/parser refinement:
- Linked REQ / ADR:
```

# Hand-off
- High-confidence accepted candidates → deterministic Canonicalizer, then Deduplication Engine, then Data Steward.
- `source_missing` (explicit-subset path no longer on disk) → surfaced to the user as an outstanding item; not retried automatically.
- Unknown person → Human Resolver for dismiss/assign/update matching data/refine matching logic.
- Low-confidence extraction → human verification or skill/fixture/preprocessing refinement before rerun.
- OCR/parser failures → OCR/parser pipeline or reviewed placeholder with retained provenance.
- Unsupported or incomplete category → reviewed placeholder/history flow with retained source provenance.
- New or changed candidate schema → `data-contract-designer` → host-provided validation planning and phase-quality review.
- Hand-off language must use role-level identifiers (e.g., "import_labs handler", "Canonicalizer") and must never reference implementation-specific CLI flags, script parameters, or invocation notes sourced from any stale ledger, prior run, or pipeline internals. Those details are the pipeline's concern, not the human's.
