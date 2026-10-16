---
name: fm-rebuild-vault
description: Rebuild famed into isolated storage and audit redriven results against working stores to expose processing gaps.
metadata:
  reasoning_class: medium
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-001
    - REQ-002
    - REQ-003
    - REQ-004
    - REQ-006
    - REQ-007
    - REQ-008
    - REQ-009
    - REQ-011
    - REQ-023
    - REQ-024
  related_decisions:
    - ADR-003
    - ADR-004
    - ADR-008
    - ADR-010
    - ADR-011
    - ADR-015
    - ADR-025
    - ADR-038
    - ADR-039
    - ADR-040
  status: active
---

# When to use
- Use when redriving retained data in isolated storage to discover processing or design gaps.
- The workflow rebuilds identity, retained medical sources, and user-authored
  authority overlays. It adds DNA replay and a complete household differential
  only when the active issue or declared audit task includes DNA.
- Redrive is diagnostic evidence. It does not replace or repair working storage.

# Inputs
- Explicit source vault and source database paths.
- Explicit isolated target vault and target database paths.
- Explicit source and isolated target processed-source roots for medical replay.
- Audit scope: the named non-DNA issue and affected tables/vault paths, or a
  task whose declared scope includes DNA.
- Optional source-bound retained semantic decision bundle generated only from
  the isolated retained evidence and validated against its checksums. Legacy
  v1 is History-only; v2 permits one `history` and one `labs` extraction for a
  bounded retained domain-study PDF; v3 additionally permits one medication
  extraction for a registered extensionless retained Medications list; v4
  additionally permits one medication extraction for an exact date-prefixed
  `visitSummary.pdf` retained under `History/`.
- V5 additionally permits exact Human Resolver accept/reject/quarantine
  decisions for rejected source-coded C-CDA `ST` Lab observations under
  `HealthSummary` archive members.
- Optional explicit working findings-ledger path and audit-output path.

# Procedure
1. Read the active source vault person notes and verify the source database path exists.
2. Confirm source and target paths differ and vault trees do not overlap.
3. For a non-DNA issue, run
   `python scripts/rebuild_vault.py --source-vault <source_vault> --source-db <source_db> --target-vault <target_vault> --target-db <target_db> --replay-medical --replay-authority --source-processed <source_processed> --target-processed <target_processed>`.
   With authority replay requested, the orchestrator validates and materializes
   category-native update snapshots in isolated processed storage before
   ordinary replay. This permits an authority-backed raw narrative to prove
   exact equality on a fresh target; update rows still persist only in the later
   authority-overlay stage.
   Do not add `--replay-dna` merely to obtain a broad audit. After replay, use
   read-only, issue-scoped comparisons for the affected canonical tables and
   vault paths. This scoped result is diagnostic evidence, not a complete
   `rebuild-diff-v1` or replacement-readiness proof.
   Add `--replay-dna --compare` only when the active issue or declared audit
   task touches DNA/genotypes or DNA-dependent report behavior.
   If retained evidence remains unresolved because it requires semantic
   interpretation, compose the category owner in proposal-only decision-bundle
   mode against the isolated target. Then rerun with `--resume-medical` and
   `--retained-decisions <bundle>` so the same retained bytes are replayed through
   the Clinical Interpreter schema and the existing category Canonicalizer and
   Data Steward gates before comparison. V2 is initially limited to `history`
   plus `labs`; Lab decisions require a bounded domain-study PDF under `Labs/`.
   V3 may additionally replace a lossy supplement fallback with one
   source-bound medication proposal under `Medications/`.
   V4 may additionally dispatch one source-bound medication proposal after
   History replay for an exact date-prefixed `visitSummary.pdf`; ordinary
   History PDFs and XML remain ineligible.
   V5 may additionally apply an exact C-CDA Lab observation resolution after
   checksum/member/person/selector/count preflight. Verbatim acceptance
   reconstructs medical fields from retained XML and passes through the
   existing Lab Canonicalizer and Data Steward; reject/quarantine persists no
   Lab row. `--history-decisions` remains a compatibility alias.
4. Verify the reported person/family-link counts, retained-source outcome counts, and foreign-key success.
5. Triage every `failed`, `unsupported`, and `zero_fact` source. `imported` means rows were produced, while `redundant` means the source produced facts already represented canonically.
   A PDF retained under `Labs/` with a bounded radiology, ophthalmology, or
   cardiology study-name prefix routes to History review without moving the
   source or changing its provenance. Verify ordinary lab PDFs still use the
   lab handler.
   A raw multi-dose medication narrative may defer to the authority stage only
   when its strict timeline semantics exactly match the validated same-day
   update snapshot. Verify the ordinary stage writes no duplicate rows and a
   one-field mismatch remains failed.
6. Verify that validated dated-update snapshots are available before ordinary
   replay when needed for an exact deferral proof, that their rows replay only
   after ordinary sources, and that active corrections apply last. Compare
   authority-row counts with the source DB. Medication and lab update notes are
   discovered from their DB rows; category-native History update notes are also
   discovered directly from the source vault because History has no DB row.
7. If an authoritative dated update note lacks its retained snapshot, recover the category-native snapshot from the validated source-vault note and preserve its bytes exactly in the target vault. Exclude any History note declaring `source_document` or `source_path`; retained-source-derived interpretation is review material, not user-authored authority.
8. When DNA is in scope, verify retained DNA source counts, import/genotype
   counts, canonical provenance, source-byte preservation, and the clean
   foreign-key check. The stage must refresh DNA-dependent reports with no
   working findings-ledger path. Otherwise record that DNA replay was omitted
   by scope and do not claim DNA parity.
9. For a full audit, verify final household regeneration excludes the working
   findings ledger, then inspect the versioned JSON audit across processed
   sources, multiplicity-aware semantic DB state, user-authority markers,
   provenance, vault artifacts, reports, ledger inputs, and volatile metadata.
   For a scoped non-DNA audit, inspect only the named tables, source outcomes,
   and vault paths with read-only comparisons and record the exclusions.
10. Triage every discrepancy. `candidate_bug` identifies a structural mismatch, `contract_explained` requires a proved contract rule and remains visible, and `needs_review` must not be silently classified. Generated Lab and medication notes carrying `projection_role: source_evidence` remain reviewable evidence differences rather than canonical-data bugs; verify each note names one retained source. Compatible legacy retained-source notes receive the same review classification. Canonical DB and current-report differences retain their ordinary candidate status; dated user updates and corrections remain authority records rather than source evidence.
11. Fix confirmed implementation bugs through their owning workflow. Reprocess working storage only as a separately reviewed action; comparison itself never mutates it.
12. To rerun after correcting a parser or classification, replace
    `--replay-medical` with `--resume-medical`; the command revalidates identity
    and foreign keys, resets target-derived state to the identity boundary,
    refreshes static target files from the current tracked template, and
    replays from scratch. A target-only recovered update snapshot is refreshed
    from the current validated vault note; a real snapshot present in the
    source processed tree remains higher authority.

# Skeptic pass
- Stop if any relationship targets an unknown person; do not create an inferred identity.
- Stop if source and target storage overlap.
- Never read replay bytes from the working retained-source tree after its isolated copy is created.
- Never coerce an unsupported or miscategorized source through the wrong category parser.
- Permit the documented bounded filename router for domain-study PDFs retained
  under `Labs/`; reject broader clinical keyword classification.
- Replay retained `user_update` snapshots only in the authority-overlay stage, then apply active `user_correction` rows last.
- Preparing an update snapshot before ordinary replay is evidence
  materialization only. Do not persist its rows or copy it into the canonical
  target vault until the authority-overlay stage.
- Replay DNA only from the isolated target `Genes/` tree, never from the working retained-source tree or working findings ledger.
- Do not replay DNA for a non-DNA issue. A full comparator currently requires
  DNA replay; use targeted read-only comparisons instead of broadening scope.
- Permit target-only retained files on resume only when their exact paths derive
  from current source-DB `user_update` references or from canonical dated
  History update notes without retained-clinical-source provenance.
- Never treat a target-only snapshot recovered during an earlier run as source
  authority. Refresh it from the current validated vault note on resume unless
  the same path exists as a real retained snapshot in the source processed tree.
- Never suppress a discrepancy merely because the target was produced by a redrive.
- Never copy working derived state into the target to manufacture parity.
- Never derive an attended retained semantic decision bundle from working
  accepted facts.
  Each decision must name canonical retained provenance, match the isolated
  source checksum and person, contain source evidence and above-threshold
  category-schema-valid proposals, and remain proposal input rather than source
  authority. Reject duplicate source/category pairs. Dispatch each supported
  category through its own Canonicalizer and Data Steward within the same
  source-level savepoint.
- Never infer a medication start date from a document date or “current as of”
  heading. Preserve it as document context unless the retained source explicitly
  states a start.
- Apply later dated updates and active corrections after visit-summary
  medication replay; redrive must not resurrect an older source value over
  current user authority.
- Never mutate working source, DB, vault, or ledger state during comparison.
- Never treat v5 selector fields as replacement medical data. They are exact
  assertions against the retained XML; any mismatch fails before replay.

# Output
- Target paths.
- Person and reciprocal family-link counts.
- Foreign-key validation result.
- Per-outcome retained-source counts and actionable failure details.
- Count of source-bound attended semantic decisions and their resulting
  accepted, recovered, unresolved, or rejected outcomes.
- Dated-update and active-correction overlay counts.
- Retained DNA source, import, genotype, and affected-person counts when DNA is
  in scope; otherwise an explicit DNA-omitted scope statement.
- Versioned deterministic audit path, complete discrepancy counts, bounded
  samples, and triage statuses for a full audit; or named read-only comparison
  evidence and explicit exclusions for a scoped non-DNA audit.
- Working-store immutability proof plus vault and foreign-key validation results.

# Hand-off
- Failed or unsupported retained source → category owner or `fm-reprocess-source` after correcting its retained classification/parser.
- Confirmed comparison bug → owning ingestion, normalization, report, or persistence workflow.
- Working-store reprocessing → explicit review followed by the relevant `fm-reprocess-source` or ingestion workflow.
