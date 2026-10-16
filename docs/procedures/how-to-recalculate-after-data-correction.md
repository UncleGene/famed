# How to Recalculate After a Data Correction

## Goal

Refresh the derived reports that depend on corrected or edited authoritative data.

## Choose the right path

### You corrected a specific persisted lab or medication value

Use:

```text
Use fm-correct-fact ...
```

That skill applies the correction and refreshes the affected reports automatically.

### You edited a retained source file under `data/processed/`

Use:

```text
Use fm-reprocess-source on data/processed/<person_id>/<Category>/<filename>.
```

That re-parses the source, replaces the rows derived from it, marks dependent outputs stale, and regenerates the affected reports.

### You are recording a new caregiver-authored update

Use:

```text
Use fm-record-user-update ...
```

That writes the dated update note and refreshes the relevant medication/lab outputs.

## Notes

- Do not edit generated reports directly as a substitute for recalculation.
- If a correction changes the evidence surface for the doctor-visit packet, PGx, or FHIR outputs, regenerate those focused outputs after the authoritative change is complete.
