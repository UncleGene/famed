---
name: fm-generate-medication-safety-alerts
description: Generate conservative medication-safety review alerts for one person by composing existing vault reports and active source-cited findings.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-009
    - REQ-014
    - REQ-016
  related_decisions:
    - ADR-014
    - ADR-015
    - ADR-020
    - ADR-021
    - ADR-027
---

# When to use
- Use when the user wants conservative medication-safety review prompts for one person from reports already present in the vault.
- Use after ingestion and report refresh have produced current medication, medication-conflict, PGx, lab, history, DNA, and findings outputs where available.
- Do not use to ingest source files, create canonical facts, recommend dosing,
  score interaction severity, or replace clinician/pharmacist review. PGx
  metabolizer or transporter-Function prompts may be surfaced only when they
  already appear in current PGx reports.

# Inputs
- `person_id` for the report target.
- Local repo with `famed-vault/` available and writable.
- Optional `ledger/findings.jsonl`; missing ledger is allowed and renders as zero active medication/PGx findings.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`.
2. Invoke the internal helper from the repo root:

   ```bash
   python scripts/generate_medication_safety_alerts.py --person-id <person_id>
   ```

3. Inspect `Records/<person_id>/Reports/medication-safety-alerts.md`.
4. Report:
   - Report path
   - Medication/PGx findings included
   - Missing source reports
   - Stale or unreviewed source reports
   - Whether medication or lab conflicts were surfaced
5. Keep boundaries sharp. This skill writes a derived review-alert report only. It composes current vault reports and active findings; it does not read raw DB rows as a parallel evidence surface.

# Skeptic pass
- Stop if the person note does not exist.
- Stop if the helper exits non-zero.
- Do not treat generated alerts as medical advice. They are caregiver-prepared review prompts for clinician/pharmacist discussion and must include the professional-contact warning.
- Do not infer diagnoses, interaction severity, dosing recommendations, or risk
  scores from source reports. Do not invent PGx results; only repeat
  conservative source-backed prompts already present in PGx coverage.
- Missing, stale, unreviewed, or conflicting source reports must remain visible in the output instead of being silently ignored.
- Active findings must remain source-cited and evidence tier/check/confidence must remain visible.

# Output
```md
## Medication Safety Alerts
- Person:
- Report path:
- Medication/PGx findings included:
- Missing source reports:
- Stale source reports:
- Unreviewed source reports:
- Conflicts surfaced:
```

# Hand-off
- Missing or stale source reports -> regenerate the relevant upstream report or rerun the source workflow before sharing alerts.
- Conflicting labs or medications -> resolve with `fm-correct-fact` or retained-source reprocessing before relying on a specific value or dose.
- User wants a clinician-facing visit summary -> `fm-prepare-doctor-visit`.
- User wants DNA-based medication gene results without safety alerts -> `fm-genetics`.
- User wants non-authoritative genetic risk flags and screening reminders -> `fm-generate-health-advisory`.
