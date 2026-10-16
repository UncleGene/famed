---
name: fm-inbox-freeform-processor
description: Process inbox free-form text and PDF medical documents through skill-first interpretation and human verification.
metadata:
  reasoning_class: medium
  business_logic: true
  minimum_capability: C2
  capability_reference: docs/references/model-capability-matrix.md
---

# When to use
- Use for local free-form text and PDF medical documents under `data/` that need person/category proposals, candidate fact extraction, evidence snippets, confidence, and human verification before canonical persistence.
- Use before live model-backed execution for this behavior; manual verification fixtures are the first release target.
- Do not use for structured CSV/TSV/HL7 imports with unambiguous person/category routing; route those to deterministic handlers.
- Do not use to persist canonical medical truth directly; the Clinical Interpreter proposes only.

# Inputs
- Source identifier supplied by the user: exact source file path under `data/`, filename, glob, or filename substring. Structured inbox placement is required only when testing inbox routing or source lifecycle behavior.
- Source text extracted from a `.txt`, Markdown-like text file, PDF parser/OCR helper, or other local extraction tool available to the current run.
- Known people, person aliases, and family context available to the current run.
- Active category vocabulary and candidate schemas for labs, medications, history, and placeholders.
- Minimum model capability reference: `docs/references/model-capability-matrix.md` level C2 before live invocation. **Exception: when a live AI agent is executing this skill, the capability gate is already satisfied — see the Live AI Agent Sessions section of the capability matrix.**

# Procedure
1. Resolve the source identifier before clinical interpretation. Search only under `data/` by exact path, exact filename, glob, case-insensitive filename substring, then punctuation-insensitive normalized filename match where separators such as space, slash, colon, underscore, hyphen, and repeated whitespace compare equivalently. Prefer an existing filesystem path over stale ledger metadata. If direct file search is unavailable or returns nothing, consult local inbox/source ledgers such as `data/inbox_state.tsv` when present. If exactly one existing source is found, continue. If no existing source or multiple plausible existing sources are found, stop and output the structured result with `needs_review` match status and the candidate paths; do not guess.
2. Register source metadata without clinical interpretation: original filename, current data path, matched-by method, checksum if available, detected file type, extraction method, and parser/OCR warnings.
3. Extract source text locally when possible. For PDFs, use available local parser/OCR helpers and preserve page or coordinate references when available. **Read the file directly using the agent's native file-reading tool; the agent executing this skill is the interpreter.** If extraction fails or only filename/ledger metadata is available, report that limitation and do not invent candidate facts.
4. Apply deterministic pre-processing when it reduces interpretation errors: segment lab result cards into analyte name, observed value, unit, reference interval/value, specimen/date, performing lab, and source page; segment vital-sign tables into one candidate fact per measurement, including date/time when visible. If segmentation is not reliable, keep the raw snippet and lower confidence instead of filling fields from nearby text.
5. Produce a Clinical Interpreter proposal only. The proposal may include `person_id`, category list, document date, document type, candidate facts, field-level confidence, overall interpretation confidence, and evidence snippets or source coordinates.
6. For lab candidates, include the observed value and the normal/reference information exactly as represented by the source: range low, range high, comparator value such as `>59`, units, and raw reference text. Do not omit normal ranges or normal values when they are visible in the source.
7. Extract calculated or population-qualified rows faithfully as source-labeled candidate facts. Keep source labels such as `eGFR`, `eGFR If Afr Am`, `eGFR non-African American`, or similar exactly as written, preserve values/units/reference text, and do not collapse them into one canonical fact. Applicability, clinical meaning, and demographic interpretation belong to downstream fact-interpretation/canonicalization modules, not this recording-oriented extraction skill.
8. Extract vital signs as separate source-labeled candidate facts. Do not combine BP, weight, BMI, temperature, pulse, oxygen saturation, height, or other vitals into one bundled value. For blood pressure, keep systolic and diastolic as separately identified fields or one structured blood-pressure candidate with distinct systolic and diastolic values, depending on the active schema.
9. For medication candidates, assign `medication_type` as `prescription`, `otc`, `supplement`, `herbal`, or `vaccine` independently of RxNorm mapping. If the type is inferred but not high confidence, lower the candidate confidence below the persistence threshold and require human verification before canonical persistence.
10. Assign every proposal item one of: `candidate_fact`, `candidate_category`, `candidate_person`, `needs_review`, or `unsupported_content`.
11. Include all identified data in the output: source metadata, parsed document metadata, proposed person/category/date/type, every candidate fact, field-level confidence, overall interpretation confidence, and the evidence supporting each interpretation.
12. Only surface facts that require human judgment. Do not present high-confidence facts for verification — auto-accept them and note them in the run summary. Surface only facts where confidence is below high, the date is filename-derived only, the value requires an encoding decision (e.g., a range stored as a single number), the routing category is ambiguous, or the source has an anomaly (mislabeled file, date discrepancy, new test name). For those items:
    - Present **one question at a time**. Wait for the user's response before presenting the next.
    - Each question must include a clickable file link (`file:///absolute/path/to/file`), the specific fact or decision at issue, evidence from the source, and labeled options (e.g., `accept / edit <field>=<value> / reject / quarantine`).
    - If the user indicates incorrect processing on a multi-fact file, switch to per-fact confirmation for that file: present each disputed fact individually, one at a time, and wait for a response before the next.
13. Detect whether manual user steps are needed to verify behavior. Trigger verification only for: confidence below high, filename-only date, encoding ambiguity, routing ambiguity, source anomaly (mislabel, discrepancy), or new fixture type. Do not ask about facts that are unambiguous in the source. Each verification request must include the file link, the specific fact or decision, evidence, and concrete labeled options.
14. Route accepted candidates to deterministic Canonicalizer validation. Route low confidence, invalid schema, ambiguous person, unsupported content, conflicting values, failed extraction, or ambiguous source match to Human Resolver or quarantine according to run mode. Do not send a source-labeled fact to review merely because it is calculated or population-qualified; review is needed only if extraction, schema fit, or source identity is uncertain.
15. **History candidate verification loop.** For history notes with `verification_status: needs_review`, present extracted structured candidates (diagnoses, procedures, medication changes, follow-up items, key findings) to the user for per-fact accept/reject/edit decisions before any persistence. Display: description, category, ICD-10 proposal, recorded date, confidence, and evidence snippet. After the user finalises decisions, pass the accepted set to `accept_history_candidates` (internal helper in `src/famed/history_sync.py`) which updates the note's `accepted_facts` front-matter field, sets `verification_status: accepted`, appends an `## Accepted Structured Facts` section to the note body, and regenerates `medical-history-summary.md`. Rejected facts are discarded without trace. Edited facts replace the candidate value with the user-supplied value before persistence. A note where all facts are rejected retains its source narrative and `verification_status: needs_review`.
16. Preserve narrative context. If structured extraction is incomplete, keep a reviewed placeholder/history note with source provenance rather than dropping content.
17. Record verification feedback for skill tuning: accepted, edited, rejected, quarantined, reviewer note, timestamp, and reason.
18. Keep Data Steward persistence separate. This skill never writes vault notes, DB rows, processed files, quarantine files, or audit events directly — except when delegating to `accept_history_candidates` as documented in step 15.
19. When the user requests manual processing output, return only the structured output block. Do not include progress updates, tool narration, apologies, or extra commentary.

# Skeptic pass
- Stop if any step would let AI or this skill write canonical storage without Canonicalizer and Data Steward gates.
- Stop if the user-provided source identifier cannot be resolved to exactly one local file under `data/`.
- Stop if person identity is inferred without enough evidence and no human verification path is available.
- Stop if evidence snippets are missing for extracted medical facts unless the fact is explicitly marked `needs_review`.
- Stop if a lab candidate omits visible reference interval/value information from the source.
- Stop if visible vital signs are bundled into one combined text fact instead of separate measurement facts.
- Stop if a calculated or population-qualified result is silently converted into another fact, loses the source label, or is interpreted for applicability during extraction.
- Stop if a verification block is presented without a `file:///` link to the source file.
- Stop if more than one uncertain item is presented at the same time without the user having responded to the previous one.
- Stop if a high-confidence, unambiguous fact is presented for user verification instead of being auto-accepted.
- Stop if a mixed document is forced into one category when the evidence supports multiple categories or an unsupported placeholder.
- Stop if asked to route interpretation through a model-provider automation layer. No such layer exists; the agent executing this skill is the interpreter, and reintroducing an unattended provider path requires its own ADR.

# Output
```md
## Free-Form Inbox Processing Result
- Source request:
- Source match:
- File link: file:///absolute/path
- Source metadata:
- Extraction method and warnings:
- Proposed person/category/date/type:
- Overall interpretation confidence and evidence:
- Candidate facts:
  - item:
  - type: `candidate_fact` | `candidate_category` | `candidate_person` | `needs_review` | `unsupported_content`
  - raw label:
  - value:
  - unit:
  - medication_type:
  - reference interval/value:
  - confidence:
  - evidence:
  - verification options: accept / edit / reject / quarantine
- Human verification decision:
- Manual verification request:
- Canonicalizer hand-off:
- Quarantine or resolver outcome:
- Skill-tuning feedback recorded:
- Linked REQ / ADR:
```

# Hand-off
- Accepted candidates → deterministic Canonicalizer, then Deduplication Engine, then Data Steward.
- Ambiguous person, low confidence, conflict, or edited value → Human Resolver before persistence.
- Unsupported or incomplete category → reviewed placeholder/history flow with retained source provenance.
- New or changed candidate schema → `data-contract-designer` → host-provided validation planning and phase-quality review.
