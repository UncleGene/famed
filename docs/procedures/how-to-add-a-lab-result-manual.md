# How to Add a Lab Result

## Goal

Record lab results in the vault and the local SQLite index.

One imported lab set maps to one Markdown note, even when it contains multiple tests. Imported panel tests are written as an editable Markdown table in the note body. Re-importing the same source file updates the same note (idempotent), rather than creating duplicates. After a successful CSV or PDF import, the accepted source is retained under `data/processed/<person_id>/Labs/` and the generated note links to that processed path. Each ingestion event refreshes `lab-results.md`, `lab-conflicts.md`, `flagged-for-discussion.md`, the FHIR export, and existing composite reports that consume those lab outputs (ADR-014, ADR-025). The conflict report is empty when no same-test/same-date values disagree across retained sources.

## User-facing path: drop and invoke a skill

Per ADR-015 the user-facing interface is a skill. One invocation handles proposal, verification of anything ambiguous, and deterministic persistence.

1. Drop one or more lab files into the inbox:
   - Person-scoped inbox: `data/inbox/<person_id>/` (preferred — person is unambiguous from the path and category is inferred automatically)
   - Or root inbox: `data/inbox/` (the skill will ask you to confirm the person if the filename or content does not match a unique known person)
   - Supported formats: CSV, PDF, plain-text lab cards
2. In your AI CLI in this repo, ask:

   ```
   Use fm-ingest-inbox to process my inbox.
   ```

3. The skill proposes person, category, dates, and per-test candidate facts with confidence and evidence. For lab PDFs, it surfaces the extracted values for your approval before anything is persisted. Answer the targeted questions it asks (one file at a time).
4. The skill then runs deterministic persistence: notes appear in `famed-vault/Records/<person_id>/Labs/`, lab rows land in `famed.db`, the source moves to `data/processed/<person_id>/Labs/`, and the complete lab-dependent report cascade refreshes before the command succeeds.

If you only want the narrower step — proposal-only triage, single-file proposal, or persistence-only — invoke the corresponding single-focused skill (`fm-inbox-batch-processor`, `fm-inbox-freeform-processor`, `fm-inbox-execute`). The orchestrating skill is the default.

## Required fields

For inbox imports, `person_id` is inferred from the folder path `data/inbox/<person_id>/`. Files in the root inbox are matched by filename and content against known person notes; the skill asks for confirmation when matching is ambiguous. Legacy `data/inbox/<person_id>/labs/` paths still work, but they are no longer the preferred layout.

Required fields per row:

- `test_name`
- `observed_at` (ISO date such as `2026-04-22` or datetime)

Optional fields:

- `loinc_code`
- `value`
- `unit`
- `reference_range_low`
- `reference_range_high`
- `source_type`

For multi-test notes, the `## Results` Markdown table uses columns: `test_name`, `loinc_code`, `value`, `unit`, `reference_range_low`, `reference_range_high`, `observed_at`. Qualitative values such as `NEGATIVE` are kept in the `value` cell.

## Direct vault editing (fallback only)

Manual Obsidian editing remains a fallback for review, correction, and human-authored notes. Create a note in `famed-vault/Records/<person_id>/Labs/` from the lab template at `famed-vault/Records/_person_id/Labs/_template.md`, fill the front matter, then re-run `fm-ingest-inbox` (or its persistence step) so the change reaches the DB and the reports refresh. Direct DB inspection:

```bash
sqlite3 data/famed.db "SELECT person_id, test_name, value, unit, observed_at FROM lab_results ORDER BY observed_at DESC LIMIT 20;"
```

## Obsidian views

Open `famed-vault/Persons/_index.md` for:

- Latest Labs By Person
- Out-of-Range Lab Flags

These views read lab note front matter and update after each ingestion event refreshes the reports. For cross-source disagreements, open `famed-vault/Records/<person_id>/Reports/lab-conflicts.md` and compare the listed retained source paths before correcting or dismissing a value.

## Developer / internal entrypoints

The user-facing path is the skill above. The commands below are implementation detail invoked by skills; they are not the documented user interface (ADR-015). They are listed here for development, debugging, and CI use only.

```bash
# Deterministic CSV/PDF ingestion entry (called by fm-inbox-execute)
python scripts/import_labs.py
python scripts/import_labs.py --pdf-file data/inbox/<person_id>/example-lab-report.pdf
python scripts/import_labs.py --review-only        # parse without writing
python scripts/import_labs.py --approve-pdf        # skip the per-PDF approval prompt

# Direct vault sync after manual note edits
python scripts/sync_vault.py
```
