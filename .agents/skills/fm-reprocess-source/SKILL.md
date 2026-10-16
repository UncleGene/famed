---
name: fm-reprocess-source
description: Reprocess a retained source file, optionally with an attended source-bound decision bundle, then refresh canonical rows, audit, and affected vault reports.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C1
  capability_reference: docs/references/model-capability-matrix.md
---

# When to use
- Use when the user has manually edited a retained source file under `data/processed/<person_id>/<Category>/` and wants DB rows and vault reports to reflect the updated content.
- Use when the user has explicitly reviewed a source-bound retained decision
  bundle and wants its exact C-CDA Lab resolution applied to working storage.
- Supports lab CSV and PDF files under `data/processed/<person_id>/Labs/`, supplement list files under `data/processed/<person_id>/Medications/`, and history review files (.pdf, .xml, .tif, .tiff) under `data/processed/<person_id>/History/`. PDFs under `Labs/` with the documented bounded radiology, ophthalmology, or cardiology study-name prefixes route to History review while retaining their original source path. Extend the shared registry when other retained-source types need reprocessing.
- Do not use to re-ingest new inbox files — those belong in `fm-ingest-inbox`.
- Do not use to correct individual DB rows in isolation — retained-source reprocessing is the safe, auditable path (source-first authority per ADR-006).

# Inputs
- Path to the retained source file under `data/processed/<person_id>/<Category>/`.
- For a source edit, confirmation from the user that the edit is complete.
- Optional reviewed retained semantic decision bundle. For a v5 C-CDA Lab
  resolution, require explicit user confirmation of its accept/reject/quarantine
  action, source fields, actor, timestamp, and reason before applying it.
- A history source with previously accepted semantic facts requires the
  matching source-bound retained decisions; replay without them fails before
  mutation.
- Local execution environment: `python` available at the repo root with `pip install -e .` already applied; `data/famed.db` and vault writable.
- Previously reviewed archive Lab observations automatically reuse their
  durable checksum/selector/count-bound decisions. Legacy summary-only audit
  records require the existing reviewed bundle; stale, missing, or malformed
  decision coverage stops archive replay before mutation (ADR-039).

# Procedure
1. Verify the source path exists under `data/processed/<person_id>/<Category>/` by direct filesystem check. When reprocessing an edit, confirm the edit is complete before proceeding.
2. Invoke: `python scripts/reprocess_source.py <source_path>` from the repo root.
   When an explicitly reviewed bundle is present, add
   `--retained-decisions <bundle>`. Bundle validation must finish before replay
   mutation; do not fall back to an unbound or retyped value.
3. Capture stdout/stderr. On non-zero exit, surface the error verbatim and stop.
4. Verify post-conditions:
   - `dependent_output_marked_stale` audit events recorded for each affected report.
   - `lab_source_reprocessed` or `medication_source_reprocessed` (category-appropriate) audit event recorded.
   - Affected vault report files are newer than the pre-run modification time.
   - A reprocessed Lab source regenerates only its own source-evidence note,
     preserves its user-authored Notes section, and does not absorb or discard
     another source note.
   - A reprocessed medication source regenerates only its own source-evidence
     note, preserves its user-authored Notes section, and leaves the DB-backed
     `current-medications.md` report as the canonical current view.
   - If exact-source medication replay yields no accepted medication facts, a
     matching stale generated note is retired with
     `medication_source_note_retired`, its index is refreshed, and retained
     archive/History evidence remains. Only source-evidence or compatible
     legacy projections qualify; nonblank user-authored Notes content stops
     standalone or whole-archive replay before mutation for review.
   - A routed domain-study PDF produces a History review note whose
     `source_path` is the original retained `Labs/` path.
   - A History source with accepted semantic facts either replays through the
     supplied source-bound decisions or fails before any vault, DB, report,
     stale-marker, or audit mutation. Deterministic C-CDA archive facts remain
     reproducible without an AI decision bundle.
   - A v5 C-CDA Lab resolution records
     `lab_observation_resolution_recorded`; `accept_verbatim` preserves the
     retained member path, complete `value_text`, and source-provided LOINC,
     while reject/quarantine writes no Lab row.
   - Its resolution audit message also retains the structured exact decision
     for subsequent replay. Replaying unchanged bytes without the original
     bundle preserves accepted text and reject/quarantine dispositions. A
     missing canonical accepted result leaves current review only after the
     exact result is restored; quarantine remains visible.
5. Report the outcome using role-level language. Do not expose script paths, DB paths, or CLI internals in user-visible output.

# Skeptic pass
- Stop if the source path is not under `data/processed/`.
- Stop if the script exits non-zero; do not interpret or retry without diagnosis.
- Stop if an ordinarily fact-bearing standalone source parses empty — it may
  have been emptied or become unparseable. A C-CDA medication member containing
  an explicit negative assertion is the bounded exception in ADR-038: it
  produces no medication fact and may retire an exact-source stale generated
  note after the user-content guard passes.
- Do not broaden the domain-study filename router with clinical prose
  keywords; only stable document-type prefixes belong in deterministic
  routing.
- Stop if any post-condition fails.
- Stop if a decision bundle fails checksum/member/person/selector/count
  preflight. Do not retry against working storage or edit the selector to force
  a match.
- Stop if History replay reports that accepted facts would be overwritten;
  obtain and review the exact source-bound semantic decisions before retrying.

# Output
```md
## Reprocess Result
- Source file:
- Person:
- Rows updated:
- Reports regenerated:
- Stale-marker audit events recorded:
- Reprocess audit event recorded:
- Observation resolution audit event recorded:
```

# Hand-off
- Successful reprocess → Data Steward updated DB rows and regenerated affected reports; audit trail current.
- Parse error or empty result → source file may be malformed or empty; review and correct before retrying.
- Unregistered source type → handler for this category/extension not yet implemented.
- Stale or mismatched attended decision → return to the Human Resolver; working
  storage remains unchanged.
