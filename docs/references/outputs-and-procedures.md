# Reference — Outputs, Procedures, and User-Facing Requirements

## Report Types

| Report | Description |
| :--- | :--- |
| Pending Review | Current person-scoped review items (medication contradictions, History notes awaiting review, Lab rejection coverage, on-disk inbox/quarantine files) with source, reason, and resolving action; unassigned files appear only in `fm-review-pending` output |
| Lab Results / Flagged for Discussion | One lab report grouped by LOINC-derived sections with trend charts, plus a focused report of out-of-range (including older results not re-checked since), trending, recently normalized, and coronary-calcium findings (ADR-030) |
| Current Medications / Conflicts | Deduplicated current list grouped by type and schedule; per-axis source contradictions (ADR-042) |
| Medical History Summary | Fact-oriented history with a Digest of chronic conditions and major events (ADR-029) |
| Immunizations | Canonical immunization list with CVX codes and lot numbers when known |
| Genetics | DNA source, gene-centric PGx table that doubles as a wallet card, carrier screening when a carrier is found, genetic-flag disposition (ADR-031); no dosing recommendations |
| Doctor Visit Packet | Printable two-page summary: allergies, problem list, medications, ranked labs, coronary calcium, vitals, PGx results, immunizations, record status, blood-relative family history (ADR-031); no generated questions |
| Medication Safety Alerts | Conservative review prompts composed from current reports and active findings; no dosing advice, severity scores, or new facts |
| Health Advisory | Non-authoritative advisory: ClinVar genetic risk flags with zygosity, inheritance refinement, disposition counts, and optional gnomAD frequency; annotated screening reminders; lab observations; blood-relative family history; discussion prompts; optional live-agent synthesis |
| PRS Validation | On-demand model/input compatibility for a pinned PRS model package; aggregate counts only; no score |

All reports use probabilistic language. High-risk findings display: `Contact a healthcare professional`.

## FHIR R4 Export

Target: FHIR R4. Current deterministic collection bundle includes Patient, lab
Observation, medication MedicationStatement, lab DiagnosticReport,
accepted-history Condition, family-graph FamilyMemberHistory, and canonical
Immunization resources;
local source-path/source-type provenance extensions; LOINC coding when
available; RxNorm coding when available; `valueString` for canonical textual
lab results; and data-absent markers only when both numeric and textual results
are absent. Uncoded operational assay metadata such as lot identifiers,
expiration dates, internal controls, and performing locations is not a
canonical lab result and therefore does not produce an Observation.
DiagnosticReports group lab Observations by local source,
observation date, and source type. Conditions are limited to
accepted structured diagnosis/finding facts from reviewed history notes;
unreviewed history candidates and raw narrative text are not exported as
conditions. FamilyMemberHistory rows are derived from local family links and
export relationship and relative name fields. Immunizations are limited to
canonical rows and retain readable vaccine text, optional CVX coding, explicit
occurrence, status, lot number when present, and every retained source path. The
helper validates the existing deterministic bundle as a
standalone action and after generation, with actionable errors for missing local
references, unsupported resource types, missing provenance, invalid
Observation value/data-absent state, unreadable medication text/status, and
DiagnosticReport result mismatches, and invalid Immunization fields. The export
refreshes automatically after upstream lab, medication, immunization, and
accepted-history changes when the person has both
a vault identity note and DB person row. External FHIR profile certification
remains future scope.

### Immunization tracer contract

This contract follows the FHIR R4
`Immunization` cardinalities and required status value set documented by
[HL7 FHIR R4](https://hl7.org/fhir/R4/immunization.html).

#### Contract: canonical immunization store → FHIR bundle builder

- **Compatibility:** adding `Immunization` resources is
  backward-compatible for bundle readers. The local validator continues to
  accept an otherwise-valid historical bundle with zero Immunization resources;
  regeneration adds all currently eligible rows.
- **Producer:** the canonical `immunizations` DB table and its source-linked
  `Records/<person_id>/Immunizations/` notes. **Consumer:** the deterministic FHIR
  collection-bundle builder and local validator
  (`persist_immunization_rows` in `src/famed/immunization_sync.py`;
  `_load_immunization_rows` and `_immunization` in `src/famed/fhir_export.py`).
- **Eligibility:** export one resource for every current canonical row belonging
  to the export person. Presence in the canonical table is the reviewed/accepted
  boundary; `needs_review` history facts, placeholders, quarantine records, and
  note-only candidates never enter this contract. Do not add a second review flag
  or infer eligibility from free-form note text.
- **Required canonical inputs:** stable row `id`, matching `person_id`, non-blank
  `vaccine_name`, normalized `status`, non-blank `source_type`, and at least one
  source path from `source_paths` or `source_path`.
- **Optional canonical inputs:** `cvx_code`, `administered_date`, and `lot_number`.
  `evidence_snippet` and `created_at` are not exported because the source links
  and canonical clinical fields already carry the required meaning.
- **FHIR identity and subject:** derive a stable `imm-` resource id from the
  canonical row id; expose that row id as a local identifier; and set `patient`
  to the bundle's sole `Patient/<id>` reference.
- **Status:** accept only the case-normalized FHIR R4 values `completed`,
  `entered-in-error`, and `not-done`. Any other canonical value is a blocking
  export validation error; do not silently reinterpret it.
- **Vaccine:** always set `vaccineCode.text` from `vaccine_name`. When `cvx_code`
  is present, also add coding system `http://hl7.org/fhir/sid/cvx`, the canonical
  code, and the vaccine name as display text. A missing CVX code is valid and
  must remain text-only rather than triggering lookup or inference.
- **Occurrence:** use `occurrenceDateTime` when `administered_date` is present and
  ISO-valid. When it is absent, use `occurrenceString` with the fixed text
  `Administration date not recorded in local canonical row`; this satisfies the
  required occurrence element without inventing a date. An invalid non-blank
  date is a blocking error.
- **Optional output:** copy a non-blank `lot_number` to `lotNumber`. Do not emit
  manufacturer, performer, dose, route, site, `primarySource`, or `reportOrigin`:
  the current producer cannot supply or distinguish those meanings reliably.
- **Provenance:** emit the established local source-type extension and one
  source-path extension for every unique, sorted path in `source_paths`, falling
  back to `source_path`. A non-blank malformed `source_paths` value is a blocking
  error rather than permission to discard retained sources. Provenance is
  required; a row with none blocks export (`_immunization_source_paths` in
  `src/famed/fhir_export.py`).
- **Deduplication and ordering:** canonical row identity remains authoritative.
  Equivalent source events that have already converged on one row produce one
  resource carrying all retained source paths. Sort rows deterministically by
  administration date, vaccine name, primary source path, and row id.
- **Failure behavior:** validate the complete in-memory bundle before replacing
  `fhir-export.json`. Any invalid Immunization resource blocks the write and
  reports the resource label plus failed rule; no row is silently dropped and a
  previously valid export is not overwritten.
- **Refresh behavior:** every successful immunization persistence,
  correction, source reprocessing, or removal path must refresh the FHIR export
  when the person has both a vault identity note and DB person row. Otherwise it
  follows the existing safe skip-until-scaffolded behavior. Repeated generation
  may advance `Bundle.timestamp`, but resource ids, payloads, counts, and ordering
  remain stable for unchanged inputs (`refresh_fhir_dependent_export` and
  `has_fhir_export_surface` in `src/famed/dependent_reports.py`, called from
  `persist_immunization_rows`).
- **Non-goals:** external profile certification, terminology enrichment, inferred
  dates or status, vaccination recommendations, adverse reactions, and changes to
  canonical immunization storage. The exporter does not resolve source conflicts:
  unresolved or low-confidence candidates must remain outside the canonical table
  under the existing human-resolution rules.

#### Contract examples

- **Valid:** a canonical completed row with vaccine text, ISO administration
  date, CVX code, lot number, source type, and two retained source paths produces
  one coded resource with both provenance paths.
- **Low confidence:** an extracted vaccine candidate still marked `needs_review`
  remains note-only and produces no resource.
- **Invalid:** a canonical row with an unsupported status, malformed non-blank
  date, missing vaccine text, malformed retained-path JSON, or no provenance
  blocks bundle replacement with an actionable validation error.
- **Conflict:** two sources that disagree remain in human resolution and produce
  no row. If reviewed values converge on one canonical id, export one resource
  with every retained source path; the exporter never chooses between sources.

#### Immunization validation matrix

| Case | Required assertion |
| :--- | :--- |
| Valid coded row | One Immunization with stable local identifier, CVX coding, Patient reference, occurrenceDateTime, provenance, and optional lot number. |
| Valid uncoded row | `vaccineCode.text` remains readable and no coding is invented. |
| Missing administration date | Resource uses the fixed `occurrenceString`; export remains valid. |
| Invalid non-blank date | Export fails before replacing the existing bundle and names the affected resource/rule. |
| Supported statuses | Each of `completed`, `entered-in-error`, and `not-done` round-trips unchanged. |
| Unsupported status | Export fails; no guessed status and no silent omission. |
| Missing vaccine name or provenance | Export fails with an actionable contract error. |
| Unreviewed or quarantined input | Note-only candidate produces no Immunization because it is outside the canonical producer boundary. |
| Equivalent event from multiple sources | One resource is emitted for the converged canonical id and all retained source paths are present. |
| Conflicting same-event sources | Canonical persistence routes disagreement to human resolution before export; no preferred-string merge or resource is produced. |
| Person isolation | Only canonical rows for the requested person reference that bundle's sole Patient. |
| Repeat export | With fixed generation time, unchanged input produces byte-equivalent JSON; under normal generation only timestamp may change. |
| Mutation refresh | Persistence, correction, reprocessing, and removal regenerate the configured export; missing identity scaffolding skips safely and a later refresh catches up. |
| Mixed bundle validation | Existing Patient, Observation, DiagnosticReport, MedicationStatement, Condition, and FamilyMemberHistory rules still pass alongside Immunization rules. |

## Alerts

- Conservative medication-safety review alerts from existing vault reports and active medication/PGx findings are available through `fm-generate-medication-safety-alerts`.
- Non-authoritative health advisory reports are available through `fm-generate-health-advisory`.
- Dosing alerts, full interaction severity scoring, and clinical-grade genetic interpretation remain future scope.
- Lab value out-of-range flags.
- ClinVar genetic risk flags with zygosity (ADR-022).

## Required User Procedures

Each procedure leads with the skill invocation and documents the manual vault
path where one exists. Procedures live in `docs/procedures/`.

| Procedure | File |
| :--- | :--- |
| Initial vault setup | `initial-vault-setup.md` |
| Add a family member and relationships | `how-to-add-a-family-member.md` |
| Add a lab result | `how-to-add-a-lab-result-manual.md` |
| Add free-form medical history | `how-to-add-medical-history.md` |
| Manually test free-form inbox processing | `manually-test-freeform-inbox-processing.md` |
| Add a medication | `how-to-add-medication.md` |
| Correct an auto-imported value or refute a diagnosis | `how-to-correct-auto-imported-value.md` |
| Review pending items | `how-to-review-pending-items.md` |
| Delete an erroneous record safely | `how-to-delete-erroneous-record-safely.md` |
| Back up and restore | `backup-and-restore-vault-db.md` |
| Import DNA data | `how-to-import-dna-data.md` |
| Generate the Genetics report / PGx wallet card | `how-to-generate-pgx-wallet-card.md` |
| Export a FHIR R4 bundle | `how-to-export-fhir-r4-bundle.md` |
| Recalculate after a correction | `how-to-recalculate-after-data-correction.md` |
| Revoke a correction | `how-to-revoke-manual-override-and-rerun-inference.md` |
| Clean temporary files | `tmp-cleanup.md` |
