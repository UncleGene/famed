---
name: fm-inbox-execute
description: Run deterministic ingestion of accepted inbox decisions through the Canonicalizer, Deduplication Engine, and Data Steward, persisting vault notes, DB rows, processed-source paths, and audit events.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C1
  capability_reference: docs/references/model-capability-matrix.md
---

# When to use
- Use after a proposal-phase skill (`fm-inbox-batch-processor` or `fm-inbox-freeform-processor`) has produced accepted candidates and the user has confirmed any required person/category resolution.
- Use as the deterministic-persistence single-focused skill composed by orchestrating skills such as `fm-ingest-inbox`. May also be invoked directly when the user explicitly wants only the persistence step.
- Do not use to interpret content, propose facts, or resolve ambiguity. If a decision is unresolved, route back to the proposal phase or the Human Resolver.
- Do not use to bypass deterministic validation or quarantine rules. Persistence is decided by the Canonicalizer and Data Steward, not by this skill.

# Inputs
- Inbox root under `data/`, normally `data/inbox/`.
- An accepted decision set: per-file or batch-level decisions made in the proposal phase. Each decision identifies the source path and, where required, the resolved person, category, or PDF-approval flag.
- Active person registry, category vocabulary, and reference cache state required by deterministic handlers.
- Local execution environment: `python` available at the repo root with `pip install -e .` already applied, `data/inbox/`, `data/processed/`, `data/quarantine/`, and `famed.db` writable.

# Procedure
1. Read the accepted decision set. Confirm every decision identifies an existing source under the inbox root by direct filesystem check; do not trust stale ledger paths.
2. For decisions that require resolution (person, category, PDF approval), assemble a resolution table keyed by source path. If two decisions disagree on the same source, stop and request a single decision before proceeding; never silently merge.
3. Invoke the deterministic ingestion entry point. The skill calls `python scripts/inbox_orchestrator.py --execute` from the repo root, threading the assembled resolution table through the entry point's accepted resolution channels (per-source CLI arguments for a single file; a TSV resolution file for a batch). PDF lab approvals are passed through the entry point's PDF-approval channel only when the user has explicitly approved review.
4. Capture the entry point's per-file outcome: persisted, quarantined, awaiting user decision, or relocated as a reviewed placeholder. Do not interpret outcomes — relay them.
5. Verify post-conditions deterministically:
   - Every accepted source is either under `data/processed/<person_id>/<Category>/` or under `data/quarantine/` with an audit reason.
   - Generated vault notes link to retained source paths under `data/processed/`, not inbox or absolute paths.
   - Generated Lab notes are source-scoped evidence projections
     (`projection_role: source_evidence`) and never absorb rows from another
     retained source. The Lab index/current/trend views label and link them.
   - Generated medication source notes are likewise marked
     `projection_role: source_evidence`, remain faithful to one retained
     source, and are labeled in the Medication index.
     `current-medications.md` remains the DB-backed canonical current view and
     links that evidence index; dated user updates and corrections are not
     source-evidence notes.
   - Persisted DB rows reference the same retained source path as their evidence
     note. A redundant equivalent fact may be skipped under a second source,
     but that outcome must remain explicit while its source-evidence note stays
     navigable.
   - Configured per-person and family-level reports affected by the change have refreshed (ADR-014).
   - A cross-source medication dose/frequency contradiction preserves both source
     facts, writes one idempotent `medication_conflict_detected` audit event in
     `awaiting_user_decision`, and appears in `medication-conflicts.md` with
     `verification_status: needs_review`. The persistence skill reports it as an
     outstanding Human Resolver item; it does not choose a value (ADR-042).
6. If any post-condition fails, surface the failure as a deterministic error attributed to the responsible role (Canonicalizer / Deduplication Engine / Data Steward / report-refresh hook) and stop. Do not retry by interpreting content.
7. Produce a per-file outcome list and a batch summary. Use role-level hand-off language. Do not include CLI flag names, script paths, or ledger internals in the user-visible output.

# Skeptic pass
- Stop if any decision in the input set lacks an accepted candidate or required resolution. Persistence requires an explicit accept; this skill does not infer.
- Stop if a source path no longer exists, has changed checksum since the proposal phase, or matches multiple existing files.
- Stop if PDF lab approval is requested without explicit user approval recorded in the decision set.
- Stop if any post-condition (processed relocation, vault note linkage, DB-source-path coherence, report refresh) fails.
- Stop if a medication contradiction is present in the refreshed report but lacks
  its pending audit event, or if repeated report refresh creates duplicate pending
  events for the same candidate values and sources.
- Stop if the deterministic entry point reports a quarantine outcome but the input decision set marked the source as accepted; the conflict means an earlier role rejected the candidate and persistence must not proceed.
- Stop if the user-visible output would reference implementation-specific CLI flags, script paths, or ledger internals.

# Output
```md
## Inbox Execution Result
- Inbox root:
- Decisions processed:
- Persisted (Data Steward):
- Quarantined (Canonicalizer / Deduplication Engine):
- Reviewed placeholders written:
- Reports refreshed:
- Outstanding human-resolution items:

## Per-File Outcome
- Source:
- Resolved person/category:
- Outcome:
- Retained source path:
- Vault note path:
- DB rows touched:
- Audit event recorded:
- Pending medication contradictions:
- Notes for human follow-up:

## Hand-off
- Successful persistence: Data Steward closed the loop; reports refreshed where applicable.
- Quarantined: cite the responsible role and the deterministic reason.
- Outstanding decisions: list role-level next step (Human Resolver, fixture/parser refinement, OCR pipeline).
- Linked REQ / ADR: REQ-004, REQ-005, REQ-006; ADR-042
```

# Hand-off
- Successful persistence → Data Steward has written vault notes, DB rows, processed-source paths, and audit events; downstream report refresh has run.
- Pending medication contradiction → return the source-cited candidate values to
  `fm-ingest-inbox` for sequential Human Resolver review. Both facts remain
  persisted while the decision is pending.
- Deterministic rejection → Canonicalizer or Deduplication Engine quarantined the candidate with a recorded reason; route to Human Resolver if the user wants to override.
- Resolution conflict between proposal and execution → return to the orchestrating skill (typically `fm-ingest-inbox`) for a fresh proposal cycle.
- Post-condition failure → escalate to the responsible role (Data Steward for vault/DB linkage, report pipeline for refresh failure); do not retry without diagnosis.
