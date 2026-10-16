---
name: fm-correct-fact
description: Apply or revoke a user-directed correction on a persisted lab result or medication row, or refute an unsupported history fact (diagnosis/finding) by label and date. The correction becomes the highest-authority value (ADR-004) and is preserved across all subsequent re-imports of the originating source. Revocation restores source authority.
metadata:
  reasoning_class: medium
  category: business-logic
  business_logic: true
  minimum_capability: C1
  capability_reference: docs/references/model-capability-matrix.md
  status: active
  links: REQ-004, REQ-005, REQ-023, ADR-004, ADR-001, ADR-014, ADR-015, ADR-042
---

# When to use

Use this skill when the user wants to:

- Correct an imported lab value, unit, medication dose, or frequency that is wrong in the vault or DB
- Refute a history fact (e.g. a diagnosis a source asserts without basis) so it
  leaves the history summary, doctor-visit packet, advisory, and FHIR export
- Revoke a previously applied correction or refutation to restore source authority
- Confirm that a user-corrected value survives subsequent re-imports of the originating source
- Resolve a source-cited medication dose/frequency contradiction surfaced during
  `fm-ingest-inbox`

Do not use this skill for reprocessing a retained source file (use `fm-reprocess-source`), for deleting records, or for making derived-report edits directly.

# Inputs

- **User instruction**: which fact to correct, new value(s), reason
- **Person ID**: the person whose record is affected
- **Category**: `labs`, `medications`, or `history`
- **Fact identity**:
  - Labs: `test_name` + `observed_at` (ISO 8601)
  - Medications: `medication_name` + `medication_type`
  - History: exact fact `description` + `recorded_date` (YYYY-MM-DD) as shown in
    the history-review note; a refutation matches that label and date from any
    source, case- and whitespace-insensitive (ADR-004)
- **Corrected values**:
  - Labs: `value` (numeric), optionally `unit`
  - Medications: `dose` and/or `frequency` (at least one required)
  - History: none — a history fact can only be refuted, not edited
- **Reason**: plain-language explanation, required for audit trail
- **Pending contradiction evidence** when applicable: disagreeing axis, candidate
  values, and retained source paths from `medication-conflicts.md`

# Procedure

1. **Read primary sources first.** Open `docs/REQUIREMENTS.md` (REQ-023) and `docs/DECISIONS.md` (ADR-004) to verify the correction is within scope; for history also ADR-004. Do not act on a paraphrased prior-turn summary.

2. **Identify the current persisted value.** Query the DB or read the vault note to find:
   - Labs: the row for `(person_id, test_name, observed_at)` — show current value, unit, source_type
   - Medications: the row for `(person_id, medication_name, medication_type)` — show current dose, frequency, source_type
   - History: every accepted fact in `Records/<person_id>/History/*.md` with the
     same label and date — list each source note it appears in, plus any variant
     labels for the same condition (each label needs its own refutation)

3. **Present the proposed change to the user for confirmation.** Show:
   - Record identity (who, what, when)
   - Current persisted value(s)
   - Proposed corrected value(s)
   - Whether an existing `user_correction` will be updated (if source_type is already `user_correction`)
   - Reason that will be recorded

   Do not proceed without explicit user confirmation. The AI never applies corrections autonomously (ADR-001).

4. **Apply or revoke via the internal helper.** After user confirms, invoke `scripts/correct_fact.py`:

   ```bash
   # Apply a lab correction
   python scripts/correct_fact.py \
     --category labs --action apply \
     --person-id <person_id> \
     --test-name "<test_name>" \
     --observed-at "<observed_at>" \
     --value <new_value> \
     --unit "<unit>" \
     --reason "<reason>"

   # Apply a medication correction
   python scripts/correct_fact.py \
     --category medications --action apply \
     --person-id <person_id> \
     --medication-name "<name>" \
     --medication-type <type> \
     --dose "<new_dose>" \
     --reason "<reason>"

   # Revoke a lab correction
   python scripts/correct_fact.py \
     --category labs --action revoke \
     --person-id <person_id> \
     --test-name "<test_name>" \
     --observed-at "<observed_at>" \
     --reason "<reason>"

   # Refute (or, with --action revoke, restore) a history fact
   python scripts/correct_fact.py \
     --category history --action apply \
     --person-id <person_id> \
     --description "<exact label>" \
     --recorded-date <YYYY-MM-DD> \
     --reason "<reason>"

   # Revoke a medication correction
   python scripts/correct_fact.py \
     --category medications --action revoke \
     --person-id <person_id> \
     --medication-name "<name>" \
     --medication-type <type> \
     --reason "<reason>"
   ```

5. **For a pending medication contradiction, verify resolution linkage.** Confirm
   every pending axis for that medication has an explicit user-selected value
   before applying the row-scoped correction; otherwise stop and leave all axes
   pending. Then confirm
   the correction appended `medication_conflict_resolution_recorded` for the
   pending conflict identity, and that regenerated `medication-conflicts.md` no
   longer lists that contradiction. Do not treat an unrelated correction as a
   resolution.

6. **Report the outcome.** Show the user:
   - Confirmation of old → new values (or revocation of old correction)
   - Which reports were regenerated (`lab-results.md`, `lab-conflicts.md`, or `current-medications.md`, `medication-conflicts.md`, or for history `medical-history-summary.md`, history dependents, and `fhir-export.json`); history refutations also report how many notes moved the fact to `refuted_facts`
   - The audit event that was logged (`user_fact_corrected` or `user_correction_revoked`)

7. **Inform user about re-import behaviour.** After apply: "The corrected value will be preserved if the originating source is re-imported; any conflicting import value will be skipped with a `skipped_user_correction` audit event." For a history refutation: "Any source asserting this label on this date, including future exports, is kept out of accepted facts and reports; it stays visible under `Refuted by User` and in `History/history-corrections.md`. Existing doctor-visit packets are dated snapshots — generate a new one." After revoke: "Source authority is restored; the next re-import of the originating source will apply its value normally and may reopen a source contradiction."

# Skeptic pass

- Is the correction within scope (raw persisted fact row, not a derived report cell)? If not, reject with explanation.
- Is there an existing `user_correction` row? If so, confirm whether the user wants to update it or this is a mistake.
- Does the reason string meet minimum quality (not empty, not a single word)? Prompt for elaboration if needed.
- Is the person_id a known person in the vault? If not, flag and stop.
- For a history refutation: the user's judgement that a fact is unsupported is
  the authority — but surface any local evidence that does support it (e.g. the
  diagnosis-support review in `medical-history-summary.md`) before confirming.
- If this is resolving an import-time contradiction, does the chosen value match a
  displayed source candidate or an explicitly user-supplied replacement? Never
  infer a medical value.

# Output

After `scripts/correct_fact.py` succeeds:

```
Correction applied:
  Person:    <person_id>
  Category:  <labs | medications>
  Fact:      <test_name>/<medication_name> at <observed_at>/<medication_type>
  Old value: <old>
  New value: <new>
  Audit:     user_fact_corrected logged
  Conflict:  medication_conflict_resolution_recorded logged (when applicable)

Reports regenerated:
  - Records/<person_id>/Reports/<report1>.md
  - Records/<person_id>/Reports/<report2>.md

Note: Corrected value will be preserved across re-imports. Use `fm-correct-fact revoke` to restore source authority.
```

# Hand-off

If the correction triggers a broader re-evaluation (e.g. a lab value correction changes a conflict report that was shared across sources), hand off to `fm-reprocess-source` to re-derive affected outputs from the updated state. If the user asks to process more inbox files after correction, hand off to `fm-ingest-inbox`.
