---
name: fm-ingest-inbox
description: Process the local inbox end-to-end with one invocation — propose with the Clinical Interpreter, drive in-chat user verification, persist accepted decisions deterministically, then run required post-ingestion semantic history extraction. Composes fm-inbox-batch-processor, fm-inbox-freeform-processor, fm-inbox-execute, and fm-history-extract.
metadata:
  reasoning_class: medium
  business_logic: true
  minimum_capability: C2
  capability_reference: docs/references/model-capability-matrix.md
  orchestrating: true
---

# When to use
- Use as the user's one invocation for "process my inbox," "process this one file," or "re-process everything from scratch." This is the documented user-facing entry for inbox processing per ADR-014 and ADR-015.
- Use whenever the user expresses an inbox-processing intent without specifying which sub-step they want. The orchestrating skill chooses the right composition.
- Do not use for unrelated workflows (person registration, reference cache management). Each user intent has its own dedicated skill.
- Do not bypass the proposal phase even when the user is in a hurry. Skipping verification is what ADR-001 (AI non-authority) forbids; this skill drives the verification loop, it does not skip it.

# Inputs
- User intent statement: process inbox, process one named file, re-process from scratch (including quarantine), or triage a specific subset.
- Inbox root under `data/`, normally `data/inbox/`. From-scratch mode also reads `data/quarantine/`.
- Targeted-subset runs: an explicit list of source paths (e.g. surfaced by an inbox-feedback re-evaluation query) naming exactly which files to reprocess, scoped to those files only.
- Known people, aliases, and family context available to the current run.
- Active fixture set under `data/manual-testing/` when present, used for proposal-phase calibration.
- Refreshed `medication-conflicts.md` reports for people whose medication facts
  changed during the run.
- **The agent executing this skill is the Clinical Interpreter.** Read source files directly with native tools. There is no model-provider automation layer to invoke; it was retired.

# Procedure
1. Classify the user's intent into a run mode: whole-inbox incremental, whole-inbox from-scratch, single-file, or targeted subset. From-scratch mode must signal the proposal phase to ignore stale ledger paths and re-read sources directly. A targeted subset is an explicit list of source paths to reprocess (e.g. surfaced by an inbox-feedback re-evaluation query); it reprocesses exactly those files, not a fresh scan.
2. Invoke the proposal phase as a single-focused sub-skill:
   - For whole-inbox or from-scratch runs, invoke `fm-inbox-batch-processor` with the matching run mode.
   - For a single explicit file, invoke `fm-inbox-freeform-processor` on that source.
   - For a targeted subset, invoke `fm-inbox-batch-processor` scoped to exactly the given path list, not a fresh whole-inbox scan.
   Pass the inbox root, run mode, and known-person context. Never inline-duplicate the proposal Procedure here.
   For C-CDA/IHE health-summary XML or ZIP contents, require the proposal to identify mixed categories when present (labs/results, medications, vitals, immunizations, diagnoses/procedures/history). Do not pre-place extracted XML under a person `history/` inbox path as a shortcut; that bypasses category fan-out and causes raw retained-source notes to look like completed processing.
3. Drive in-chat user verification for every non-high-confidence outcome the proposal returned. Apply the same sequential, one-file-at-a-time clarifying-question discipline that `fm-inbox-batch-processor` requires:
   - Show the full filesystem path so the user can open and inspect the file.
   - Present the best available candidate with evidence and confidence; avoid open-ended questions when a clear candidate exists.
   - Offer specific labeled decision options (`yes-dummy_alice / no-dummy_bob / dismiss / other`).
   - Wait for the user's response before moving to the next file.
   Distinguish stable identity fields (name, full DOB, MRN — belong on the person profile) from dynamic encounter fields (PCP, referring provider, address, insurance, phone — belong on the encounter record, never on the person profile).
4. Build the accepted decision set from proposal output and user responses. Each accepted decision must include the source path, resolved person and category, any PDF-approval marker the user explicitly provided, and the originating outcome category. Decisions the user dismissed, deferred, or routed to OCR/parser refinement do not enter the accepted set.
5. If no decisions are accepted, stop and report the open items as the final output. Do not invoke the persistence phase to "do something."
6. Invoke the persistence phase as a single-focused sub-skill: call `fm-inbox-execute` with the accepted decision set. Do not duplicate its Procedure here. Relay its per-file outcomes and post-condition results into the orchestrator's summary.
7. If persistence produced any history notes with `verification_status: needs_review`, immediately invoke `fm-history-extract` on those exact note paths before presenting the run as complete. This is the default post-ingestion semantic-processing step for history Layer 2. Do not leave raw `## Narrative Sections` or `## Extracted Text` as the final successful state when high- or medium-confidence structured facts can be extracted. If `fm-history-extract` accepts facts, report the accepted count and verify the note now has `verification_status: accepted`. If it finds no accepted facts, report that as an outstanding review gap rather than a successful semantic import.
   For mixed C-CDA/IHE documents, this post-step must extract all supported fact classes from the retained source: diagnoses/history/procedures, labs/results, medications, vitals, immunizations, and follow-up. Categories that cannot yet be persisted to their canonical category store must be called out as `needs_logic_refinement`; do not summarize the run as complete just because the source was retained.
8. Inspect the refreshed `medication-conflicts.md` for every person whose
   medication facts changed. For each report with
   `verification_status: needs_review`, drive one source-bound medication at a
   time through the Human Resolver:
   - Show every disagreeing axis for that medication, its candidate values, and
     retained source paths exactly as the report records them.
   - Offer the source-stated candidate values, a user-provided value, or defer
     for each axis. Do not recommend a medical dose or frequency.
   - Because medication authority is row-scoped, collect an explicit value for
     every pending axis before invoking `fm-correct-fact` once for that
     medication. If the user defers any axis, leave the whole medication
     contradiction pending; never let a dose choice silently resolve a frequency
     disagreement or vice versa.
   - Treat the complete value set plus the user's stated reason as confirmation
     to invoke `fm-correct-fact`. The correction skill remains the only writer
     of the authoritative value.
   - Verify `medication_conflict_resolution_recorded` was appended, the refreshed
     conflict report no longer lists that contradiction, and a repeat import will
     not ask again. A deferred choice remains persisted, pending, and
     report-visible (ADR-042).
9. After persistence, required post-ingestion extraction, and medication-conflict
   review complete, present a single end-of-run summary that combines:
   - Counts by outcome from the proposal phase.
   - Counts by outcome from the persistence phase.
   - History extraction outcomes: accepted, skipped with no high/medium-confidence facts, or blocked.
   - Medication contradiction outcomes: resolved during this run or left pending,
     with no medical recommendation by the agent.
   - Outstanding human-resolution items (deferred medication contradictions,
     ambiguous identity/category, OCR-needed, fixture/parser refinement requests).
   - Reports refreshed (per ADR-014).
10. Record skill-tuning feedback by calling `scripts/record_inbox_feedback.py --run-id <ISO-timestamp> --records-json '[...]'` with one object per processed file. Each object must include `source_path`, `confidence_tier` (high/medium/low/unknown), `proposal_outcome`, and any optional fields (`person_id`, `category`, `user_decision`, `vault_note_path`, `uncertainty_reason`, `reviewer_note`). `user_decision` must be one of: accepted/edited/rejected/dismissed/deferred (or omit for null). Quarantine and person-assignment outcomes are system outcomes, not user decisions — encode them in `proposal_outcome` (e.g. `"quarantine_recommended"`, `"person_assigned"`) and leave `user_decision` null. Records are appended to `data/inbox-run-feedback.jsonl` — never overwritten — so run history accumulates and can be queried to identify systematic uncertainty patterns.
11. Keep boundaries sharp. This skill orchestrates; it does not interpret content (that is the proposal or `fm-history-extract` phase's job) and it does not write canonical truth (that is the persistence phase, `fm-correct-fact`, or `accept_history_candidates` Data Steward helper's job).

# Skeptic pass
- Stop if the run mode cannot be classified from the user's intent. Ask for clarification with concrete options ("incremental / from-scratch / single-file / targeted subset") rather than guessing.
- Stop if a targeted-subset path no longer exists on disk and the orchestrator is about to drop it silently instead of relaying the proposal phase's `source_missing` outcome for that file.
- Stop if the proposal phase reports unknown-person, ambiguous-source, or OCR-needed outcomes and they are not surfaced to the user as sequential clarifying questions before any persistence step.
- Stop if the accepted decision set would let `fm-inbox-execute` persist a candidate the user did not explicitly accept.
- Stop if the orchestrator is about to inline the body of a sub-skill instead of invoking it. Composition is the point of an orchestrating skill; duplicating sub-skill Procedures defeats it.
- Stop if a history note was persisted and the orchestrator is about to declare the inbox run complete without either running `fm-history-extract` or explicitly reporting semantic extraction as blocked/skipped with a reason.
- Stop if a mixed C-CDA/IHE source containing labs or medications is routed as history-only without an explicit category split and post-ingestion extraction outcome.
- Stop if a pending medication contradiction is omitted from the end-of-run
  Human Resolver loop, or if the agent recommends which medical value to choose.
- Stop if a selected contradiction value is treated as authoritative without
  invoking `fm-correct-fact` and verifying the linked resolution audit event.
- Stop if the user-visible output references CLI flags, script paths, ledger internals, or any implementation detail that belongs inside a sub-skill's Procedure.
- Stop if the persistence phase reports a post-condition failure and the orchestrator is about to retry without escalating to the responsible role.

# Output
```md
## Inbox Run Summary
- Run mode:
- Inbox root:
- Source count:
- Proposal outcome counts:
- Persistence outcome counts:
- History extraction outcomes:
- Medication contradiction outcomes:
- Reports refreshed:
- Outstanding human-resolution items:
- Skill-tuning feedback recorded:

## Per-File Result
- Source:
- Resolved person/category:
- Proposal outcome:
- Persistence outcome:
- Vault note path (if persisted):
- Required follow-up (if any):

## Hand-off
- Persisted: Data Steward closed the loop; reports refreshed where applicable.
- Quarantined: cite the responsible role and the deterministic reason recorded in audit.
- Outstanding decisions: role-level next step (Human Resolver, fixture/parser refinement, OCR pipeline, person-record update).
- Linked REQ / ADR: REQ-004, REQ-005, REQ-006; ADR-042
```

# Hand-off
- Successful persistence → Data Steward; downstream report refresh confirmed.
- Pending medication contradiction → Human Resolver selects or supplies a value,
  then `fm-correct-fact` records the authoritative correction; defer leaves both
  source facts visible.
- Targeted-subset path reported `source_missing` → surfaced to the user as an outstanding item; not retried automatically within this run.
- Deterministic rejection during persistence → Canonicalizer / Deduplication Engine quarantined the candidate; route to Human Resolver if the user wants to override.
- Unknown person → Human Resolver for dismiss / assign / update matching data / refine matching logic, then re-run this orchestrator.
- Logic, fixture, preprocessing, OCR, or parser refinement requests → corresponding pipeline owner; re-run this orchestrator after refinement lands.
- Schema or contract change discovered during the run → `data-contract-designer` → host-provided validation planning and phase-quality review, then re-run.
