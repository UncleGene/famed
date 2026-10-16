---
name: fm-record-user-update
description: Record user-authored fact updates for a person on a given date. Persists same-day updates into category-specific dated authoritative notes in the vault; each note carries source-level authority for its recorded facts (REQ-024, ADR-005). Re-imports of the originating source will respect those update notes exactly as they respect explicit corrections.
metadata:
  reasoning_class: medium
  category: business-logic
  business_logic: true
  minimum_capability: C2
  capability_reference: docs/references/model-capability-matrix.md
  status: active
  links: REQ-003, REQ-009, REQ-024, ADR-003, ADR-002, ADR-001, ADR-008, ADR-014, ADR-015, ADR-004, ADR-008, ADR-005
---

# When to use

Use this skill when the user wants to record one or more fact changes that are not captured in an imported source document — for example:

- Starting a new prescription or supplement
- Discontinuing a medication
- Correcting a dose, frequency, or status for the current date
- Recording a lab result from a home test or point-of-care device
- Recording a diagnosis, procedure, or clinical finding that was communicated verbally or in a non-importable format
- Any other user-directed fact update across medication, lab, or history categories

All updates reported on the same calendar date for the same person are consolidated by category into dated update notes. Calling this skill a second time on the same date merges the new facts into the existing same-category note rather than creating a duplicate. Same-day updates to the same medication use snapshot semantics: the later value replaces the earlier one.

Do not use this skill for correcting a specific past imported value in-place (use `fm-correct-fact`), for reprocessing a source file (use `fm-reprocess-source`), or for processing inbox documents (use `fm-ingest-inbox`).

# Inputs

- **User description**: plain-language description of the changes (free-form)
- **Person ID**: the person whose record is affected
- **Date** (optional): ISO 8601 date (YYYY-MM-DD); defaults to today's date

# Procedure

1. **Read primary sources first.** Open `docs/REQUIREMENTS.md` (REQ-024) to confirm the update is within scope. Do not act on a paraphrased prior-turn summary.

2. **Resolve person and date.**
   - Confirm person_id from the user's input (or ask if ambiguous).
   - If no date is given, use today's date (`date.today().isoformat()`).

3. **Interpret the free-form description.** Parse the user's description into structured fact lists by category:

   **Medications** — each fact requires at minimum `medication_name`:

   | Field             | Type   | Notes                                                           |
   |-------------------|--------|-----------------------------------------------------------------|
   | `medication_name` | string | Required                                                        |
   | `medication_type` | string | `prescription`, `supplement`, `otc`, `herbal`, `vaccine`        |
   | `rxnorm_cui`      | string | Optional explicit CUI; otherwise local cache lookup is attempted |
   | `dose`            | string | e.g. `"10 mg"`                                                  |
   | `frequency`       | string | e.g. `"daily"`, `"twice daily"`                                 |
   | `status`          | string | `active`, `discontinued`; default `active` for new starts      |
   | `start_date`      | string | ISO 8601, if mentioned                                          |
   | `end_date`        | string | ISO 8601, if mentioned                                          |

   If the user gives a free-form dose schedule or titration for one medication,
   interpret it into one medication fact per dose period when the dates are clear.
   Each period should repeat the same `medication_name` and carry its own `dose`,
   `frequency`, `start_date`, and `end_date`. If the period boundaries are ambiguous,
   present the ambiguity for confirmation before persisting.

   **Lab observations** — each fact requires at minimum `test_name` and `observed_at`:

   | Field                 | Type   | Notes                                    |
   |-----------------------|--------|------------------------------------------|
   | `test_name`           | string | Required                                 |
   | `observed_at`         | string | ISO 8601 date of observation; required   |
   | `value`               | number | Numeric result                           |
   | `unit`                | string | e.g. `"mg/dL"`, `"mmHg"`                 |
   | `reference_range_low` | number | Lower bound of reference range           |
   | `reference_range_high`| number | Upper bound of reference range           |
   | `loinc_code`          | string | LOINC code if known                      |

   **History facts** — each fact requires at minimum `description`:

   | Field         | Type   | Notes                                          |
   |---------------|--------|------------------------------------------------|
   | `description` | string | Required — the fact being recorded              |
   | `category`    | string | `diagnosis`, `procedure`, `finding`, `other`    |
   | `date`        | string | ISO 8601 when the fact occurred                 |
   | `details`     | string | Additional context                              |

4. **Present proposed facts for user confirmation.** Show:
   - Person ID and date
   - Tables of the structured facts by category
   - Any fields left blank (so the user can add them)

   Do not proceed without explicit user confirmation. The AI never persists facts autonomously (ADR-001).

5. **Persist via the internal helper.** After user confirms, invoke `scripts/record_user_update.py`:

   ```bash
   python scripts/record_user_update.py \
     --person-id <person_id> \
     --date <YYYY-MM-DD> \
     --medications-json '<json_array>' \
     --labs-json '<json_array>' \
     --history-json '<json_array>'
   ```

   Example with all categories:
   ```bash
   python scripts/record_user_update.py \
     --person-id dummy_alice \
     --date 2024-01-15 \
     --medications-json '[{"medication_name":"Zetia","medication_type":"prescription","dose":"10 mg","frequency":"daily","status":"active"}]' \
     --labs-json '[{"test_name":"Blood Glucose","value":95,"unit":"mg/dL","observed_at":"2024-01-15"}]' \
     --history-json '[{"description":"Diagnosed with seasonal allergies","category":"diagnosis","date":"2024-01-12"}]'
   ```

   Each `--*-json` flag may be omitted when no facts of that category are being recorded.

   For medications, persistence is order-independent across authority layers.
   Before persistence, a blank `rxnorm_cui` is enriched from the local
   reviewed/downloaded RxNorm cache. An explicitly supplied nonblank CUI is
   preserved. A missing cache or unmatched medication remains null and visibly
   `unresolved`; this workflow never performs a live RxNorm request.
   After the dated `user_update` row exists, an exact lower-authority DB copy
   with the same normalized name/type and complete
   dose/frequency/explicit-duration/start/end/status period is superseded. The
   Data Steward first records an append-only audit link containing the removed
   row's stable canonical fields and surviving authoritative row ID. Distinct periods,
   `user_correction` rows, other `user_update` rows, and retained source bytes
   remain unchanged.

6. **Report the outcome.** Show the user:
   - How many new facts were recorded by category
   - The vault note paths, when that category was recorded:
     - Medications: `Records/<person_id>/Medications/<date>-<person_id>-medications.md`
     - Labs: `Records/<person_id>/Labs/<date>-<person_id>-labs.md`
     - History: `Records/<person_id>/History/<date>-<person_id>-history.md`
   - The retained source paths (for re-importability):
     - Medications: `data/processed/<person_id>/Medications/<date>-<person_id>-medications`
     - Labs: `data/processed/<person_id>/Labs/<date>-<person_id>-labs`
     - History: `data/processed/<person_id>/History/<date>-<person_id>-history`
   - Which reports were regenerated (`current-medications.md`, `lab-results.md`)
   - Note: "These updates carry source-level authority, are stored as retained sources, and will be preserved across re-imports and vault rebuilds."

   Rebuild discovers medication and lab update notes from their DB rows. It
   discovers History update notes directly from their canonical dated vault
   paths because History has no DB row. A History note declaring
   `source_document` or `source_path` is retained-source review material and
   must not be treated as a dated user update.

# Skeptic pass

- Is each `medication_name` non-empty and recognisable? Flag ambiguous names.
- Is `medication_type` valid (`prescription`, `supplement`, `otc`, `herbal`, `vaccine`)? Default to `prescription` if not specified.
- Is `status` valid (`active`, `discontinued`)? Default to `active` for new starts.
- For labs: does `test_name` appear to be a valid test? Does `observed_at` parse as a date?
- For history: is `description` non-empty and meaningful?
- For history: verify the dated update note does not declare
  `source_document` or `source_path`; those fields indicate imported-source
  provenance rather than user-authored authority.
- Is the person_id a known person in the vault? Check `Persons/` directory. If not found, stop and ask.
- Does the date string parse as a valid ISO 8601 date (YYYY-MM-DD)? Reject non-conforming dates.
- Is any medication already covered by an existing `user_correction` row for the same person? If so, note this to the user — recording the update may conflict; use `fm-correct-fact` to update the correction value instead.
- Before reporting medication convergence, verify only exact lower-authority
  periods were removed and every removal has a
  `medication_redundant_row_superseded` audit event linked to the surviving
  `user_update` row.

# Output

After `scripts/record_user_update.py` succeeds:

```
User updates recorded:
  Person:  <person_id>
  Date:    <date>
  Vault notes:
    - Records/<person_id>/Medications/<date>-<person_id>-medications.md  (if medications recorded)
    - Records/<person_id>/Labs/<date>-<person_id>-labs.md                (if labs recorded)
    - Records/<person_id>/History/<date>-<person_id>-history.md          (if history recorded)
  Retained sources:
    - data/processed/<person_id>/Medications/<date>-<person_id>-medications  (if medications recorded)
    - data/processed/<person_id>/Labs/<date>-<person_id>-labs                (if labs recorded)
    - data/processed/<person_id>/History/<date>-<person_id>-history          (if history recorded)
  Facts:   <N> medication(s), <M> lab(s), <K> history fact(s)

Artifacts regenerated:
  - Records/<person_id>/Reports/current-medications.md       (if medications recorded)
  - Records/<person_id>/Reports/lab-results.md               (if labs recorded)
  - Records/<person_id>/Medications/_index.md                (if medications recorded)
  - Records/<person_id>/Labs/_index.md                       (if labs recorded)
  - Records/<person_id>/History/_index.md                    (if history recorded)

Note: These updates carry source-level authority, are stored as retained sources,
and will be preserved across re-imports and vault rebuilds.
```

# Hand-off

If the user wants to correct a specific past imported fact rather than record a current-date update, hand off to `fm-correct-fact`. If the user wants to process inbox documents after recording the update, hand off to `fm-ingest-inbox`.
