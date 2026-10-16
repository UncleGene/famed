# Reference — Coding Standards and Clinical Data Categories

## Person Records

- Profile per family member: optional name, birth year, sex, family role.
- Relationships: parent, child, sibling, spouse, other.
- Identifier: stable human-readable `person_id`, filename-safe, user chosen.

## Coding Standards

| Domain | Standard | Notes |
| :--- | :--- | :--- |
| Lab tests | LOINC | Required for structured results where available. User downloads LOINC files manually, places them under `data/reference/loinc/`, and runs `python scripts/cache.py --dataset loinc refresh` to parse `Loinc.csv`; no UTS/UMLS API integration required. |
| Medications | RxNorm CUI | Required for PGx matching where available. Cached at `data/reference/rxnorm/rxnorm_concepts.jsonl` (RxNav API, no credentials). |
| Diagnoses | ICD-10 | Optional; auto-extracted by NLP and manually correctable. |
| Genetic variants | rsID | Reports reference rsIDs only; no bulk genotype dumps in reports. |

## Lab Requirements

- Test name, value, unit, reference range, date.
- LOINC-coded where standard exists.
- Unit-sensitive identities use distinct reviewed concepts for percentage/ratio,
  absolute-count, and fL measurements when the analyte label alone is ambiguous.
- Stored LOINC provenance distinguishes source-provided codes from famed
  inference. Reviewed corrective backfills may select only exact inferred-code
  matches and must preserve source-provided disagreements for review.
- Longitudinal trend per person/test.
- Import formats: PDF lab reports, C-CDA archives, CSV/TSV, manual vault entry, dated user updates.
- Distinguish lab-verified and patient-reported results.

## Medication Requirements

- Current and historical medications: dose, frequency, start/end dates.
- RxNorm-coded where available.
- Active vs discontinued status.
- Adverse reaction notes.

## Medical History Requirements

- Visit notes, hospitalization summaries, diagnoses, surgical history, vaccination records, allergy lists.
- Diagnoses optionally ICD-10-coded.
- NLP-assisted extraction from PDF/text/doc-like formats.
- Extracted metadata requires human review before being authoritative.
