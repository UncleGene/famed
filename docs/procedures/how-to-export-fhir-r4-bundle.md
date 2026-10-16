# How to Export a FHIR R4 Bundle

## Goal

Generate or validate the deterministic local FHIR R4 export for one person.

## Generate the export

Ask your AI CLI:

```text
Use fm-export-fhir for dummy_alice.
```

Output:

```text
Records/<person_id>/Reports/fhir-export.json
```

## Validate the existing export

Ask your AI CLI to validate the existing export without regenerating it:

```text
Use fm-export-fhir to validate dummy_alice's existing export.
```

## Current export scope

- `Patient`
- Lab `Observation`
- Medication `MedicationStatement`
- Lab `DiagnosticReport`
- Accepted-history `Condition`
- Family-graph `FamilyMemberHistory`
- Canonical `Immunization`

## Notes

- Provenance extensions preserve local source path and source type;
  Immunizations retain every converged source path.
- Immunizations preserve readable vaccine text, add CVX coding when present,
  use an explicit unknown-date occurrence when the canonical date is absent,
  and never infer missing clinical fields.
- The current export is a deterministic tracer bundle, not a certified external profile.
- Wider FHIR resource coverage remains future work.
