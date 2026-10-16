---
name: fm-history-extract
description: Extract structured facts from retained-source history notes automatically. Auto-accepts high and medium confidence facts without interactive per-fact confirmation. Medium confidence facts are marked for review. Vitals are included. Replaces the raw narrative dump with a clean structured note.
metadata:
  reasoning_class: medium
  business_logic: true
  minimum_capability: C2
  capability_reference: docs/references/model-capability-matrix.md
---

# When to use
- Use to extract structured facts from one or more history notes that have `verification_status: needs_review`.
- Use as the default post-ingestion step after `fm-ingest-inbox` creates history review notes.
- Use when reprocessing an existing history note after `fm-reprocess-source`.
- Use for batch extraction across all `needs_review` notes for a person.
- Do NOT use when you want interactive per-fact confirmation; use `fm-inbox-freeform-processor` step 15 instead.
- Do NOT use when the source is a new document type not yet in the vault; run `fm-ingest-inbox` first.

# Inputs
- Person identifier or note path(s). Accepts: person_id (processes all `needs_review` notes for that person), exact vault note path, or glob pattern under `famed-vault/Records/`.
- Optional: `--dry-run` flag to show what would be accepted without persisting.
- Optional isolated rebuild mode: explicit vault, database, and processed-source
  roots plus a decision-bundle output path. This mode proposes only; it never
  reads working accepted facts or persists directly.

# Procedure
1. Resolve the target. Use the explicit vault root in isolated rebuild mode;
   otherwise use `famed-vault`. If a person_id is given, glob
   `<vault_root>/Records/<person_id>/History/*.md` excluding `_index.md`. If a
   note path is given, verify it exists. Filter to notes with
   `verification_status: needs_review` in their YAML front matter.
2. For each resolved note, read its YAML front matter to obtain `source_path`, `person_id`, `source_file_name`, and `event_date`.
3. Read source text for extraction:
    - **Primary:** resolve canonical `data/processed/...` provenance beneath the
      explicit processed root in isolated rebuild mode; otherwise read
      `<source_path>` relative to the repo root. Archive-member provenance uses
      the named member from the retained ZIP.
      - For `.pdf`: run `python -c "from famed.import_labs import extract_pdf_text; from pathlib import Path; print(extract_pdf_text(Path('<source_path>')))"` to get plain text.
      - For `.xml` (C-CDA): run `python -c "from famed.history_sync import parse_ccda_review_document; from pathlib import Path; doc = parse_ccda_review_document(Path('<source_path>')); [print(s.title + ':\n' + s.text) for s in doc.sections]"` to get structured section text.
      - For `.tif` or `.tiff`: run `python -c "from famed.ocr import ocr_image_file; from pathlib import Path; print(ocr_image_file(Path('<source_path>')))"` to get OCR-extracted text (requires Tesseract).
   - **Fallback:** if the retained source is not readable, read the note body for any `## Narrative Sections` or `## Extracted Text` block and use that text. If neither is available, skip this note and record it as `skipped: source_unavailable`.
4. Extract structured candidate facts from the source text using your own AI capability. Extract all of:
   - **Diagnoses** — condition name, ICD-10 code if present, recorded date (encounter date if condition-level date absent), evidence snippet.
   - **Procedures** — procedure name, CPT/SNOMED if visible, date, evidence snippet.
   - **Medication changes** — medication name, change type (started/stopped/dose changed/continued), recorded date, evidence snippet. Assign `medication_type` (prescription/otc/supplement) where determinable.
   - **Follow-up orders** — description, due date if stated, evidence snippet.
   - **Vitals** — one fact per measurement (systolic BP, diastolic BP, weight, height, BMI, temperature, O2 saturation, pulse). Include value, unit, and recorded date.
   - **Key clinical findings** — lab results embedded in visit notes, imaging findings, pathology results.
   - **Immunizations and screenings** — vaccine names/dates and explicit screening encounters or orders.
   For C-CDA/IHE health-summary documents, assume the source is mixed-category unless section evidence proves otherwise. Do not stop after diagnoses/procedures; scan medication, results, vital-sign, immunization, encounter, and plan sections.
5. Assign confidence to each extracted fact:
   - `high` — explicitly stated with an unambiguous date and value/status; ICD/code included when present. No inference required.
   - `medium` — stated but date is inferred from context, code is inferred, value is approximate, or source sections contain an apparent contradiction.
   - `low` — derived by implication, no direct textual support, or contradictory evidence.
6. Apply the acceptance policy:
   - `high` confidence → accept silently.
   - `medium` confidence → accept and set `confidence: medium` in the fact row. These will appear in the note and report with the confidence label so the caregiver can spot-check.
   - `low` confidence → exclude from persistence. Record in the output summary only.
7. Build the `accepted_rows` list for all accepted facts. Each row must have at minimum `description`. Include `category`, `icd_10_code`, `recorded_date`, `value`, `unit`, and `confidence` where available. For vitals, set `category: vital_sign`, `value: <numeric>`, `unit: <unit>`.
   Use category values that downstream reports already understand where possible: `diagnosis`, `procedure`, `medication`, `vital_sign`, `lab`, `immunization`, `screening`, `imaging_finding`, `follow_up`, or `other`.
8. In isolated decision-bundle mode, stop before persistence. Write
   `famed.history-extraction-decisions.v1` JSON with one decision per source:
   canonical `source_path`, SHA-256 of the isolated retained file (the containing
   ZIP for an archive member), and a complete
   `clinical_interpreter.fact_extraction.v1` extraction payload. Use numeric
   confidence, include source evidence, exclude below-threshold proposals, and
   do not read or copy facts from the working vault. Hand the bundle to
   `fm-rebuild-vault`.
9. If `--dry-run` is specified, stop here and output the candidate table. Do not call `accept_history_candidates`.
10. If accepted_rows is non-empty, call `accept_history_candidates` via:
   ```
   python -c "
   import json, sqlite3
   from pathlib import Path
   from famed.history_sync import accept_history_candidates
   rows = json.loads(open('/tmp/famed_history_extract_rows.json').read())
   conn = sqlite3.connect('data/famed.db')
   accept_history_candidates(
       conn=conn,
       vault_root=Path('famed-vault'),
       note_path=Path('<note_path>'),
       accepted_rows=rows,
       person_id='<person_id>',
       source_path='<source_path>',
       extraction_mode='clinical_interpreter',
   )
   conn.commit()
   conn.close()
   "
   ```
   Write the `accepted_rows` list as JSON to `/tmp/famed_history_extract_rows.json` before running the command. Remove the temp file after the call succeeds.
11. `accept_history_candidates` only updates the vault note's front matter/body — it does not write to any SQL table. If any accepted fact has `category: immunization`, also persist it into the `immunizations` table via `persist_immunization_rows`, the same Data Steward gate a structured immunization import uses:
    ```
    python -c "
    import sqlite3
    from pathlib import Path
    from famed.lab_sync import source_file_checksum
    from famed.immunization_sync import persist_immunization_rows
    conn = sqlite3.connect('data/famed.db')
    source_path = Path('<source_path>')
    persist_immunization_rows(
        conn=conn,
        vault_root=Path('famed-vault'),
        person_id='<person_id>',
        source_file=source_path,
        processed_source_path='<source_path>',
        checksum=source_file_checksum(source_path),
        source_type='history_extract',
        rows=[
            {
                'vaccine_name': '<vaccine name from the fact description>',
                'cvx_code': '<CVX code if stated in source, else None>',
                'administered_date': '<recorded_date>',
                'status': 'completed',
                'lot_number': '<lot number if stated in source, else None>',
                'evidence_snippet': '<evidence>',
            },
            # one dict per accepted immunization-category fact from this note
        ],
        document_date='<event_date>',
        retained_source=True,
    )
    conn.commit()
    conn.close()
    "
    ```
    Pass every accepted immunization-category fact from this note in one `rows` list (not one call per fact). Skip this step entirely when no accepted fact has `category: immunization`.
12. If accepted_rows is empty after semantic review, call `remove_history_review_note_without_facts` so the raw no-use note is deleted while the retained source file and audit trail remain:
   ```
   python -c "
   import sqlite3
   from pathlib import Path
   from famed.history_sync import remove_history_review_note_without_facts
   conn = sqlite3.connect('data/famed.db')
   remove_history_review_note_without_facts(
       conn=conn,
       vault_root=Path('famed-vault'),
       note_path=Path('<note_path>'),
       person_id='<person_id>',
       source_path='<source_path>',
       excluded_low_confidence=<low_confidence_count>,
   )
   conn.commit()
   conn.close()
   "
   ```
   Use this only after reading the source and confirming no high- or medium-confidence structured facts should be accepted. Explicit negative clinical facts, encounter metadata, care-team details, immunizations, medication absence/presence, allergy absence/presence, and active-problem absence/presence count as useful facts when source-backed. If the source could not be read, leave the note at `needs_review` and record `skipped: source_unavailable`.
13. Repeat steps 2–12 for each resolved note.
14. Verify the person's `pending-review.md` was refreshed and the resolved note
    is absent from the current queue. A remaining entry must correspond to a
    still-current `needs_review` note.
15. Output the structured extraction result (see Output section).

# Skeptic pass
- Stop if any accepted fact would be written without going through `accept_history_candidates` (the deterministic Data Steward gate).
- Stop if an accepted `category: immunization` fact is left unpersisted to the `immunizations` table — `accept_history_candidates` alone does not write it there; `persist_immunization_rows` must also be called.
- Stop if a retained source file is not found and there is no narrative text in the note body; do not invent facts or mark the note reviewed.
- Stop if `person_id` in the note front matter does not match the person being processed.
- Stop if a vital sign is bundled as a single multi-measurement string instead of separate facts.
- Stop if a mixed C-CDA/IHE source has labs or medications in the source text but the extraction result only contains generic history narrative.
- Stop if a C-CDA source with encounter details, explicit negative clinical statements, or care-team data would be treated as having no useful facts.
- Stop if the proposed improvement would add deterministic clinical phrase lists or broad keyword inference to split/interpret heterogeneous prose. Per ADR-018, semantic interpretation belongs in this AI-assisted skill output; deterministic code validates and persists the resulting schema-shaped facts.
- Stop if `--dry-run` is not honored when specified.
- Stop if isolated decision-bundle mode reads a working accepted fact, omits
  canonical provenance/checksum/evidence, contains a below-threshold proposal,
  or persists before the rebuild validates the bundle.

# Output
```md
## History Extraction Result
- Notes processed:
- Notes skipped (source unavailable / all low-confidence):
- Per note:
  - Note: <note_path>
  - Source: <source_path>
  - Facts accepted (high confidence):
    | Description | Category | ICD-10 | Date | Value | Unit |
    | ...         | ...      | ...    | ...  | ...   | ...  |
  - Facts accepted (medium confidence — marked for review):
    | Description | Category | ICD-10 | Date | Value | Unit |
    | ...         | ...      | ...    | ...  | ...   | ...  |
  - Facts excluded (low confidence):
    - <description> — reason: <reason>
  - Outcome: accepted <n> facts / skipped / error
```

# Hand-off
- Accepted facts persisted → `accept_history_candidates` updates vault note and regenerates `medical-history-summary.md`; accepted immunization-category facts additionally persist to the `immunizations` table via `persist_immunization_rows`.
- No accepted facts after semantic review → `remove_history_review_note_without_facts` deletes the no-use note, preserves retained source/audit history, and regenerates `medical-history-summary.md`.
- Medium confidence facts → remain in `accepted_facts` with `confidence: medium`; caregiver reviews in vault.
- Low confidence facts → listed in output only; no persistence.
- Source unavailable → note left at `needs_review`; operator must supply source or run `fm-reprocess-source`.
- Isolated decision bundle → `fm-rebuild-vault` validates source/person/checksum
  binding and applies proposals through the normal deterministic gates.
- Schema change needed → `data-contract-designer` → host-provided validation planning.
