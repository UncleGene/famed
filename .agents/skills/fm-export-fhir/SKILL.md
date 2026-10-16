---
name: fm-export-fhir
description: Export or validate a deterministic FHIR R4 JSON bundle for one person from current local canonical records.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-016
  related_decisions:
    - ADR-014
    - ADR-015
---

# When to use
- Use when the user wants a local FHIR R4 export for one person.
- Use when the user wants to validate the existing local FHIR R4 export for one person.
- Use after ingestion and report refresh have produced current canonical rows.
- Do not use to ingest source files, create facts, interpret medication safety, infer diagnoses, or generate risk scores.

# Inputs
- `person_id` for the export target.
- Local repo with `famed-vault/` available and writable.
- Local SQLite DB at `data/famed.db`.
- Existing export at `Records/<person_id>/Reports/fhir-export.json` when validating only.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`.
2. Invoke the internal helper from the repo root:

   ```bash
   python scripts/export_fhir.py --person-id <person_id>
   ```

3. To validate an existing export without regenerating it, invoke:

   ```bash
   python scripts/export_fhir.py --person-id <person_id> --validate-only
   ```

4. Inspect `Records/<person_id>/Reports/fhir-export.json`.
5. Report:
   - Export path
   - Validation result
   - Observation resources included
   - MedicationStatement resources included
   - DiagnosticReport resources included
   - Condition resources included
   - FamilyMemberHistory resources included
   - Immunization resources included
   - Scope limitations
6. Keep boundaries sharp. This FHIR slice exports and validates a deterministic collection bundle with `Patient`, lab `Observation`, medication `MedicationStatement`, lab `DiagnosticReport`, accepted-history `Condition`, family-graph `FamilyMemberHistory`, and canonical `Immunization` resources only.

# Skeptic pass
- Stop if the person note or database does not exist.
- Stop if the helper exits non-zero.
- Do not claim the export is a complete clinical record. External-profile certification and inferred conditions remain future work.
- Do not export unreviewed history candidates or raw narrative text as Condition resources.
- Do not silently infer missing coded values; uncoded observations must remain text-coded.
- Preserve local provenance extensions for source path and source type.
- Export only canonical immunization rows; preserve readable vaccine text and
  every retained source path without inferring missing dates or codes.
- Treat validation errors as blocking before sharing the export.

# Output
```md
## FHIR Export
- Person:
- Export path:
- Validation:
- Observation resources included:
- MedicationStatement resources included:
- DiagnosticReport resources included:
- Condition resources included:
- FamilyMemberHistory resources included:
- Immunization resources included:
- Scope:
```

# Hand-off
- User wants inferred or unreviewed conditions in FHIR -> future interpreted evidence workflow.
- User wants conservative medication-safety review alerts -> `fm-generate-medication-safety-alerts`.
- User wants a doctor-facing visit summary -> `fm-prepare-doctor-visit`.
- User wants PGx phenotype/dosing interpretation, interaction severity scoring, or disease probability scoring -> future interpreted report workflow.
- Export helper reports structural validation errors -> fix canonical rows or exporter rules before sharing.
