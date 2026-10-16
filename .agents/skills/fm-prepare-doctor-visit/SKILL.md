---
name: fm-prepare-doctor-visit
description: Generate a deterministic doctor-visit packet for one person by composing existing vault reports, surfacing review gaps, and writing a timestamped packet under that person's Reports area.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C1
  capability_reference: docs/references/model-capability-matrix.md
---

# When to use
- Use when the user wants a visit-prep packet for one family member from reports already present in the vault.
- Use after ingestion and report refresh have already produced the current history, medication, and lab reports for that person.
- Do not use to ingest new inbox files or to create canonical medical facts.

# Inputs
- Person ID for the packet target.
- Local repo with `famed-vault/` available and writable.
- Existing per-person reports under `Records/<person_id>/Reports/`.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`.
2. Invoke the internal helper from the repo root:

   ```bash
   python scripts/prepare_doctor_visit.py --person-id <person_id>
   ```

3. On success, inspect the generated packet path under `Records/<person_id>/Reports/doctor-visit-packets/`.
4. Report:
   - Packet path
   - Any missing core reports called out in the packet
   - Any record-status notes (items awaiting review, disagreeing sources); the packet has no generated questions (ADR-031)
   - Whether an allergy-status disagreement was flagged
5. Keep boundaries sharp. This skill writes a derived packet only. It does not ingest inbox files, edit source notes, or treat family context as canonical truth.

# Skeptic pass
- Stop if the person note does not exist.
- Stop if the helper exits non-zero.
- Stop if the packet would be written outside `Records/<person_id>/Reports/doctor-visit-packets/`.
- Stop if the packet body tries to embed raw retained-source text instead of composing existing vault reports.
- Stop if the packet inlines a whole sub-report body; it carries a curated summary only (ADR-031).

# Output
```md
## Doctor Visit Packet
- Person:
- Packet path:
- Missing core reports:
- Conflict reports surfaced:
- Question buckets:
```

# Hand-off
- If core reports are missing, hand off to the relevant upstream workflow (`fm-ingest-inbox`, `fm-history-extract`, medication/lab handlers) before treating the packet as complete.
- If the user wants a retained source corrected first, hand off to `fm-reprocess-source` or `fm-correct-fact` as appropriate.
