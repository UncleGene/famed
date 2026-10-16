# How to Correct an Auto-Imported Value

## Goal

Safely override a wrong persisted lab or medication fact, or refute an unsupported history diagnosis, while preserving provenance and re-import protection.

## Use this when

- A lab value or unit was imported incorrectly.
- A medication dose or frequency is wrong in the current record.
- You want the corrected value to remain authoritative across future re-imports of the same source.
- A source asserts a diagnosis with no basis (refute it by label and date; see ADR-004).

## User-facing path

Ask your AI CLI:

```text
Use fm-correct-fact to correct dummy_alice's Glucose value on 2024-01-15 to 88 mg/dL because the PDF was misread.
```

```text
Use fm-correct-fact to refute dummy_alice's "Synthetic condition" dated 2024-01-15 because no evidence supports it.
```

The skill should show the current persisted value, the proposed replacement, and the reason before it applies anything.

## What happens

- The corrected row becomes `source_type: user_correction`.
- Affected reports refresh automatically.
- Future re-imports of the original source keep the corrected value unless you revoke it.
- An audit event is recorded.
- When the correction resolves a pending medication contradiction, a linked
  `medication_conflict_resolution_recorded` event is also appended and the
  contradiction leaves `medication-conflicts.md`.
- If both dose and frequency are pending for the same medication, provide an
  explicit selection for both in one correction. Row-scoped authority rejects a
  partial choice so one axis cannot silently resolve the other.

## Do not use this for

- Reprocessing an edited retained source file. Use `fm-reprocess-source`.
- Adding a new caregiver-authored fact for today. Use `fm-record-user-update`.
- Editing a rendered report directly.

## Related procedures

- `docs/procedures/how-to-revoke-manual-override-and-rerun-inference.md`
- `docs/procedures/how-to-recalculate-after-data-correction.md`
