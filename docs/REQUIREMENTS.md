# famed — Functional Requirements

The human-editable source of functional truth. Each requirement is
scenario-rich so an agent can implement it without guessing. Decisions that
explain *why* live in `DECISIONS.md`; the current architecture lives in
`SYSTEM.md`.

Status values: `active`, `planned`.

## Index

| ID | Area | Load full body when touching |
|---|---|---|
| REQ-001 | Unified ingestion | Inbox orchestration, source lifecycle, supported formats |
| REQ-002 | Clinical interpretation | AI extraction, confidence, candidate schemas |
| REQ-003 | Canonicalization and coding | Validation, LOINC/RxNorm/ICD/rsID, units, quarantine |
| REQ-004 | Deduplication and conflicts | Duplicate classification, conflicts, reversible links |
| REQ-005 | Human resolver | Ambiguity resolution, review queue |
| REQ-006 | Storage, vault, navigation | Authority, notes, rebuild |
| REQ-007 | Person and family graph | Person identity, relationships |
| REQ-008 | Labs | Lab import, LOINC, lab reports |
| REQ-009 | Medications | Medication import, RxNorm, current list, conflicts |
| REQ-010 | Medical history | History notes, fact extraction, summary |
| REQ-011 | DNA ingestion | Genotype import, providers, VCF, PRS validation |
| REQ-012 | Pharmacogenomics | PGx results, medication-safety alerts |
| REQ-013 | Carrier screening | Carrier status from consumer DNA |
| REQ-014 | Findings and insights | Evidence tiers, ledger, synthesis, genetic flags |
| REQ-015 | Query | `fm-query`, cited answers |
| REQ-016 | Reports and exports | Report refresh, report set, FHIR |
| REQ-017 | Readiness checks | Dependency and cache checks |
| REQ-018 | User procedures | Caregiver docs |
| REQ-019 | Skills and AI guardrails | Skills, capability matrix, change gates |
| REQ-020 | Performance | Local runtime budget |
| REQ-021 | Privacy and security | Local-first, tracked-file privacy, network rules |
| REQ-022 | Success criteria | Product-level acceptance |
| REQ-023 | Fact corrections | User corrections, refutation, revocation |
| REQ-024 | Dated update notes | User-authored updates |
| REQ-025 | Flagged for discussion | Out-of-range and trending labs report |

---

## REQ-001 Unified Ingestion Pipeline
Status: active

All structured and unstructured medical data enters through one pipeline.

**Behavior:**
- Canonical workflow: drop files into `data/inbox/` (preferably
  `data/inbox/<person_id>/`) and invoke `fm-ingest-inbox`. Processing runs end
  to end when person, category, and content are unambiguous; the user is asked
  only about ambiguity, conflict, or low confidence. Structured files are never
  gated behind "would you like to import this?".
- Supported inputs: lab PDFs, CSV/TSV, C-CDA / IHE health-summary ZIP archives,
  medication lists (text, PDF, HTML, images, vendor CSV), free-form history
  (text, PDF, TIF via OCR), person YAML, and consumer DNA files.
- Pipeline: inspect → interpret → identify person → identify category → extract
  → normalize → deduplicate → validate → write → relocate source → refresh
  reports.
- One source may produce several categories. A mixed C-CDA archive fans out into
  labs, medications, vitals, immunizations, and history review notes.
- Retained filenames keep clinically meaningful components; a redundant leading
  date label may be dropped once the canonical date becomes the structured
  prefix.
- Incremental and idempotent: unchanged inputs are skipped; re-runs update the
  same records and notes. A from-scratch run re-reads sources instead of trusting
  stale ledger state.
- Bounded domain-study PDFs retained under `Labs/` (radiology, ophthalmology,
  cardiology study names) route to History review without moving the source.
- Deterministic MyChart parsing accepts comma-grouped reference bounds and
  explicit qualitative `Value` blocks, and fails closed on prose, unit tokens, or
  narrative numbers.

**Key scenarios:** Lab PDF → extract, LOINC-map, deduplicate, persist with
provenance. | CSV export with known person and category → persisted silently,
user notified. | Free-form PDF → the skill proposes facts with evidence; the user
verifies before persistence. | Ambiguous person → Human Resolver, or quarantine
when non-interactive. | Parser upgrade → re-import updates records in place.

**Acceptance:** Every medical source ends `processed`, `quarantined`,
`awaiting_user_decision`, or `needs_user_verification`. Hidden OS/editor files
are ignored. Notes link retained sources, never inbox, quarantine, tmp, or
absolute paths. Zero duplicate canonical records on re-import. Zero confirmation
steps for unambiguous files.

**Links:** ADR-001, ADR-002, ADR-003, ADR-006, ADR-013, ADR-014, ADR-015, ADR-016, ADR-017, ADR-018, ADR-038, ADR-039

---

## REQ-002 Clinical Interpretation Layer
Status: active

A constrained AI-assisted Clinical Interpreter handles heterogeneous documents,
person inference, category assignment, and candidate fact extraction.

**Behavior:**
- Produce schema-shaped candidate facts with field-level confidence and raw
  evidence (snippets or source coordinates).
- Record facts as written; clinical applicability, normalization, and
  persistence are downstream decisions.
- Low confidence produces an actionable outcome: verify/edit, clarify, refine
  logic or fixtures, or quarantine.
- Never write to canonical storage.
- Deterministic handlers may bypass the interpreter for regular sources, within
  ADR-018.
- AI behavior is specified as business logic skills with fixtures and manual
  verification flows; the live agent running the skill does the interpretation.

**Key scenarios:** Glucose in a visit note → candidate `glucose = 102 mg/dL` with
date, source, person, confidence; the Canonicalizer decides. | Fasting vs random
unclear → ask or route to review, never silently store.

**Acceptance:** No interpreter output bypasses canonicalization. Every candidate
has a source reference and confidence. Invalid schema is rejected before
persistence. Replay decision bundles reject wrong person, stale checksum, missing
evidence, duplicate source/category, unsupported category, and below-threshold
payloads (ADR-039). Whole-inbox processing reports high-confidence,
low-confidence, ambiguous, unsupported, and needs-verification outcomes
distinctly.

**Links:** ADR-001, ADR-002, ADR-013, ADR-016, ADR-018, ADR-039

---

## REQ-003 Canonicalization and Coding
Status: active

Convert candidate facts into validated canonical records with standard codes.

**Behavior:**
- Validate candidates against domain schemas; reject or quarantine invalid data
  with a human-readable reason.
- Map labs to LOINC, medications to RxNorm CUI, diagnoses to ICD-10 where
  possible; store variants by rsID.
- Normalize units and reference ranges through the reviewed lanes in ADR-035.
- Infer LOINC by name and unit class (ADR-034); record whether each code was
  source-provided or inferred.
- A reviewed repair may change an *inferred* code by exact pattern; a
  source-provided code changes only through an individually reviewed row-ID
  override.
- LOINC files are downloaded manually by the user; no UTS/UMLS API.
- `fm-correctness-audit` may cross-check persisted coding against public sources
  with scrubbed-generic web queries. It only records findings (ADR-033).

**Key scenarios:** Sodium reported as `140 mg` → unit mismatch, quarantined with
reason. | Missing LOINC cache → ingestion continues with visibly unmapped codes. |
A reviewed-wrong inferred code → exact DB row and vault cell repaired and
audited; source untouched. | Same mismatch but source-provided → preserved for
review.

**Acceptance:** Accepted records are schema-valid. Quarantine keeps context for
correction. Repairs are exact, idempotent, mirrored to vault cells, and audited.
Audit findings cite both the local record and the external source, are never
auto-applied, and use no identifying query text.

**Links:** ADR-001, ADR-008, ADR-018, ADR-033, ADR-034, ADR-035, ADR-041 | REFERENCES: coding-standards.md

---

## REQ-004 Deduplication and Conflict Handling
Status: active

Detect duplicate, updated, and conflicting facts across sources without
destructive merges.

**Behavior:**
- Deterministic pre-filters (person, date, category, code, source, value); AI
  scoring only as an aid.
- Classify facts as `new`, `updated`, `redundant`, `conflicting`, `needs_review`.
- The same fact from several sources links every source.
- Suppressed redundant facts stay recoverable: if the owning source stops
  stating a fact, it is restored under a source that does, unless user authority
  governs it (ADR-006).
- An archive lab restatement and a same-day PDF may collapse when only unit or
  reference metadata is missing on one side and test identity, LOINC code,
  value, and date agree.
- Medication cross-source dedup treats only an exact dose / frequency / duration
  / start / end / status period as redundant.
- Never silently discard conflicting values. A medication contradiction keeps
  both facts pending resolution (ADR-042).

**Key scenarios:** Same lab panel in CSV and PDF → one canonical set, both
sources linked. | Conflicting dose across two sources → both kept, Human Resolver
asked during an attended import, report-visible otherwise.

**Acceptance:** Duplicate detection tested per category. Links are reversible
and audited. When an exact lower-authority medication row exists and the user
later records the same period, persistence converges on the `user_update` row.

**Links:** ADR-003, ADR-006, ADR-018, ADR-042

---

## REQ-005 Human-in-the-Loop Resolution
Status: active

One Human Resolver resolves ambiguity without having to maintain internal state.

**Behavior:**
- Ask only when ambiguity affects truth, safety, or persistence. Show candidate
  values, sources, dates, confidence, and consequences.
- Allow choosing a value, supplying one, rejecting, or quarantining.
- Unknown person: dismiss as wrong person, assign to an existing person, create
  a person (with relationships and reciprocal closure), or request
  matching-logic refinement. Show extracted text alongside the file path. Works
  with zero configured persons.
- Record every resolution with actor, timestamp, reason, and rejected values.
- `pending-review.md` per person and `fm-review-pending` expose the current
  review set (ADR-043).
- Rejected source-coded C-CDA Lab observations are resolved by exact,
  checksum-bound decisions that are reused on replay (ADR-039).
- Non-interactive runs quarantine ambiguity that requires choosing a value.

**Acceptance:** No ambiguous conflict is resolved silently. Every resolution is
audited. Quarantine is a first-class outcome. The review set is derived from
current evidence; resolved items leave it while their events remain.

**Links:** ADR-001, ADR-003, ADR-039, ADR-042, ADR-043 | See also: REQ-023

---

## REQ-006 Storage Authority, Vault, and Navigation
Status: active

A source-first, vault-readable, DB-indexed storage model with navigable notes.

**Behavior:**
- Sources are retained locally after processing until the user deletes them.
- Authority and divergence follow ADR-003. Note contract follows ADR-009. Layout
  follows ADR-010.
- Headings and back-links use `display_name`; `person_id` appears only in file
  stems, front matter, DB rows, and paths.
- Fields are duplicated across layers only for an explicit UX, navigation,
  provenance, or performance reason. Reports, indexes, and stale markers are
  derived outputs.
- Lab and medication source notes are source-scoped evidence; the canonical
  current views are the Lab index, `lab-results.md`, and
  `current-medications.md` (ADR-038).
- New category handlers declare generated vs user-authored sections before
  writing notes.
- `fm-rebuild-vault` replays retained sources into isolated storage as a
  read-only differential check of the working store (ADR-040).

**Key scenarios:** Person hub → labs, medications, history, genes, insights,
reports in one or two clicks. | Re-import → same note updated, no sibling
duplicate. | Two sources report the same observation → two evidence notes, one
canonical row in current views. | Regeneration → generated sections replaced,
user sections kept.

**Acceptance:** No orphan notes. No JSON blobs in front matter. Rebuilding from
the authoritative layer reproduces derived artifacts. Source-note identity does
not depend on arrival order. A rebuild never mutates working storage, rolls back
a failed source completely, and reports every source outcome.

**Links:** ADR-003, ADR-005, ADR-008, ADR-009, ADR-010, ADR-017, ADR-025, ADR-038, ADR-039, ADR-040, ADR-043 | See also: REQ-023, REQ-024

---

## REQ-007 Person and Family Graph
Status: active

Manage family members, stable identifiers, and relationships.

**Behavior:**
- One profile per person: display name, optional birth year, sex, family role.
- Stable human-readable `person_id` used in filenames, metadata, and DB rows.
- Adding a relationship on one person creates the inverse on the other.
- Person facts from YAML, notes, or documents converge on one identity.

**Acceptance:** Relationship updates are bidirectional and idempotent.

**Links:** ADR-010

---

## REQ-008 Labs
Status: active

Store, normalize, trend, and present lab results.

**Behavior:**
- Capture test name, numeric value when available, source text
  (`value_text`), unit, reference range, date, source, verification status.
- LOINC-code where a standard code exists (ADR-034).
- Import from PDF, CSV/TSV, C-CDA, manual entry, and dated user updates.
- C-CDA specifics:
  - an `ST` value with one numeric token plus one unit token is split into value
    and unit; compound text stays unparsed for review;
  - the SNOMED 246514001 "units" companion supplies its parent's unit and is not
    a result;
  - uncoded operational rows (lot, expiration, internal control, performing
    location) are metadata, not results;
  - textual forms follow ADR-036; every failed observation is an audited review
    item.
- Vendor unit spellings resolve through reviewed lanes; `mEq/L` = `mmol/L` only
  for sodium, potassium, chloride (ADR-035).
- Reviewed display-name aliases share one series while each reading keeps its own
  date, unit, and range. Dual-reference-range variants converge (ADR-037).
  Distinct measurements stay separate: free PSA vs percent free PSA; percent vs
  absolute-count immature granulocytes and NRBC; RDW-CV vs RDW-SD.
- `lab-results.md` is the one lab report. Sections follow LOINC: specimen
  (urine, stool) first, then reviewed chemistry panels (lipids, glucose, kidney,
  electrolytes, liver, pancreas, thyroid, hormones, iron and vitamins, heart,
  inflammation, prostate), then LOINC class. Out-of-range results lead each
  section by distance from range, with H/L flags. The newest draw leads; older
  rows are muted; each section has collapsed trend charts and an "All readings"
  table. Without a LOINC cache, one "All tests" section is used. The report
  exists only when the person has labs.
- Distinguish lab-verified and patient-reported results.

**Key scenarios:** `<value xsi:type="ST">88 µm</value>` → value 88, unit µm. |
`&lt;0.1` → textual result, no trend point. | `88 µm. 11 µm.` → not guessed. |
`PSA, Free` and `Free PSA` → one series; `% Free PSA` separate.

**Acceptance:** No duplicate trend points from re-import. Mapping failures are
visible. Operational metadata never appears as a lab fact anywhere. Textual
results reach reports and FHIR `valueString`.

**Links:** ADR-016, ADR-034, ADR-035, ADR-036, ADR-037, ADR-038, ADR-039 | REFERENCES: coding-standards.md | See also: REQ-025

---

## REQ-009 Medications
Status: active

Manage current and historical medications with normalization and safety
support.

**Behavior:**
- Store name, dose, frequency, start/end dates, authorization window, status,
  adverse-reaction notes, source, verification status.
- Every row has `medication_type`: `prescription | otc | supplement | herbal |
  vaccine`. Inferred types below high confidence are confirmed by the user.
- RxNorm-code where possible (reviewed lane, ADR-008). Type classification does
  not depend on RxNorm resolution.
- `current-medications.md` groups non-PRN rows by type and schedule (regular /
  schedule not recorded), then lists every as-needed row in one final section
  with its type visible. Allergies lead the report.
- Refill expiry (`authorized_through`) is distinct from therapy end and shows as
  a renewal action (ADR-041).
- Cross-source dose/frequency contradictions follow ADR-042.
- PGx paths receive the full typed list, including supplements and herbals.
- `fm-generate-medication-safety-alerts` composes existing reports and findings
  into conservative review alerts: no new facts, no severity scores, no dose
  changes.
- Source notes are evidence; "No known medications" is never a medication
  (ADR-038).

**Key scenarios:** Re-imported list → statuses updated, no duplicates. |
Discontinued medication → history kept. | Supplement list → `supplement` rows in
their own sub-list. | "Daily as needed" → final as-needed section.

**Acceptance:** Prescriptions are queryable without supplements mixed in.
`medication_type` is set on every row. Unresolved RxNorm is flagged. Re-imports
update existing notes and regenerate dependent reports. Safety alerts cite
sources, show missing/stale/conflicting inputs, and include the
professional-contact warning.

**Links:** ADR-005, ADR-008, ADR-038, ADR-041, ADR-042 | See also: REQ-023, REQ-024

---

## REQ-010 Free-Form Medical History
Status: active

Manage visit notes, discharge summaries, diagnoses, procedures, immunizations,
allergies, and other free-form history.

**Behavior:**
- Import PDF, text, and TIF (OCR) through a review-first flow; preserve the
  narrative in the note body.
- Extract diagnoses, procedures, medication changes, follow-ups, vitals, and
  immunizations as structured candidates (`fm-history-extract`). High and medium
  confidence facts are accepted automatically; medium ones are marked for review.
- Optionally code diagnoses with ICD-10.
- Deterministic parsing only for stable structure (C-CDA sections, dated rows);
  semantic splitting is AI-assisted (ADR-018).
- `medical-history-summary.md` is fact-oriented, not a visit log: a Digest of
  chronic conditions, cancers, and cerebrovascular events (ADR-029); facts
  grouped by topic with dates; vitals as a latest-value snapshot; visits only as
  provenance or review gaps; diagnosis-support prompts for labels with weak or
  conflicting local support.
- Reprocessing never clears accepted facts without matching replay decisions
  (ADR-039).

**Acceptance:** Unverified metadata never becomes authoritative. Accepted facts
appear as structured fields alongside the narrative. The summary refreshes on
every history change, as does the pending-review report.

**Links:** ADR-004, ADR-013, ADR-018, ADR-029, ADR-039, ADR-043

---

## REQ-011 DNA and Genomics Ingestion
Status: active

Ingest consumer DNA data for PGx, carrier screening, and risk-flag outputs.

**Behavior:**
- Supported: 23andMe, AncestryDNA, MyHeritage, Genotek, Nebula Genomics, generic
  VCF/VCF.GZ, and single-file ZIP wrappers (the ZIP stays the retained source).
- Provider auto-detected from header/content.
- Raw files stay local; nothing is transmitted without explicit user action.
- Coverage states: measured, not on chip, no-call.
- VCF keeps genome build and genotyped vs imputed source; low-quality imputed
  rows are filtered; genotyped calls win over imputed calls.
- Accepted DNA imports regenerate the Genetics report and its dependents.
- `fm-imputation-readiness` prints an aggregate readiness report on demand.
- `fm-prs-validation` validates a pinned PRS model package against imported DNA
  (ADR-024); no scores.

**Key scenarios:** Re-downloaded file → converges on the same records. | Variant
not on chip → `not on chip`, never negative. | Malformed VCF.GZ → rejected with
no partial writes.

**Acceptance:** Detection works for supported formats. Re-import is idempotent.
Failures roll back. Reports never expose bulk genotypes, weights, or source
paths.

**Links:** ADR-008, ADR-011, ADR-020, ADR-024, ADR-025, ADR-031 | REFERENCES: genomics-reference.md

---

## REQ-012 Pharmacogenomics and Medication Safety
Status: active

PGx results grounded in normalized DNA and the current medication list.

**Behavior:**
- Cover CPIC pharmacogenes, including CYP genes and transporters.
- The Genetics report's gene-centric table lists every CPIC gene the chip can
  call, with conservative star-allele calls and exact CPIC results when allele
  definitions, a matching diplotype translation, and enough genotype data exist.
  It doubles as the printable wallet card.
- Results keep their CPIC vocabulary: metabolizer status for metabolism genes,
  Function for transporters (ADR-021). Otherwise an explicit not-callable state.
- Call quality and genotype-to-result predictability are separate axes.
- The report regenerates when medications, DNA, or CPIC reference data change.
- No dosing guidance.

**Acceptance:** Not-callable states are explained. Every result is an exact
match to cached CPIC data. Function and metabolizer results are never ranked
together. Alerts include evidence level and source.

**Links:** ADR-020, ADR-021, ADR-025, ADR-031 | REFERENCES: genomics-reference.md, evidence-model.md

---

## REQ-013 Carrier Screening
Status: active

Carrier screening from consumer DNA for ACMG-recommended genes (CFTR, SMN1,
HEXA, HBB, GJB2).

**Behavior:**
- Report `carrier`, `clear`, or `not callable from this chip`; explain when
  clinical testing is needed.
- Computed from normalized DNA plus the local ClinVar cache. Appears in
  `genetics.md` only when a carrier is found.

**Acceptance:** `Not callable` is never treated as `clear`. Results are
reproducible and refreshed after DNA imports.

**Links:** ADR-020, ADR-031 | REFERENCES: genomics-reference.md

---

## REQ-014 Evidence-Backed Insights and Findings Ledger
Status: active

Evidence-backed insights with an append-only findings ledger.

**Behavior:**
- Every insight carries an evidence tier and confidence.
- New E1/E2 literature claims require an evidence check before surfacing; E3–E5
  claims are labeled unverified.
- Findings are revised or superseded without deleting history; stale findings
  are excluded from current lists (ADR-027).
- Synthesized insights follow ADR-026: live agent only, resolvable citations, at
  least two reviewed domains, tier no higher than the weakest citation, no
  dosing/diagnosis/treatment language.
- Genetic risk flags follow ADR-022 (zygosity, exact-identifier inheritance
  refinement, disposition counts) and may show population frequency from the
  local gnomAD cache (ADR-023).

**Key scenarios:** Source correction → related insights, reports, and exports
marked stale and regenerated. | Heterozygous finding with matching Strong ClinGen
recessive evidence → carrier-compatible observation citing the GenCC record. |
Missing disease identifier → default carrier-or-risk wording.

**Acceptance:** 100% of insights cite sources. The ledger is append-only and
gitignored. Every allele match is counted in exactly one disposition; excluded
variants are never listed. Missing data reads as not evaluated, never as zero.

**Links:** ADR-022, ADR-023, ADR-025, ADR-026, ADR-027, ADR-033 | REFERENCES: evidence-model.md

---

## REQ-015 Query Interface
Status: active

Answer plain-language questions about one person's data.

**Behavior:**
- `fm-query` reads the person's current vault notes and linked sources; the live
  agent does the reasoning, with no separately configured model service.
- Answers cite sources and include confidence; stale artifacts are excluded or
  flagged; unverified notes stay labeled as unverified.

**Acceptance:** Every answer cites current sources. A run succeeds with no model
service installed.

**Links:** ADR-013, ADR-014, ADR-015

---

## REQ-016 Reports and Exports
Status: active

Caregiver and clinician outputs generated from validated data.

**Behavior:**
- Reports refresh automatically on every ingestion event, correction, or
  reference-cache change that affects their inputs, including a relative's
  records (ADR-025). Live-agent synthesis is the one exception (ADR-026).
- Per-person report set (ADR-031), each generated only when its inputs exist:

  | Report | Skill |
  |---|---|
  | `pending-review.md` | `fm-review-pending` |
  | `lab-results.md`, `lab-conflicts.md`, `flagged-for-discussion.md` | refreshed on ingestion |
  | `current-medications.md`, `medication-conflicts.md` | refreshed on ingestion |
  | `medical-history-summary.md`, `immunizations.md` | refreshed on ingestion |
  | `genetics.md` | `fm-genetics` |
  | `medication-safety-alerts.md` | `fm-generate-medication-safety-alerts` |
  | `health-advisory.md` | `fm-generate-health-advisory` |
  | `doctor-visit-packets/<timestamp>.md` | `fm-prepare-doctor-visit` |
  | `prs-validation.md` | `fm-prs-validation` (on request) |
  | FHIR R4 bundle | `fm-export-fhir` |

- The doctor-visit packet is the one doctor-facing artifact (ADR-031).
- The health advisory holds genetic risk flags (ADR-022, ADR-023), preventive
  screening reminders annotated for currency (ADR-019), lab observations from the
  shared flagged filter (ADR-030), blood-relative family history (ADR-028), and
  discussion prompts ranked by distance from range.
- Composite reports link sub-reports rather than inlining them.
- Language is probabilistic and conservative, with the warning "Contact a
  healthcare professional."
- FHIR R4 export per person: Patient, lab Observation, DiagnosticReport,
  MedicationStatement, accepted-history Condition, FamilyMemberHistory,
  Immunization. Exports are validated before replacing the last good file and
  regenerate after corrections.
- Immunization export: stable identity, optional CVX coding without invention,
  explicit unknown-date handling, every retained source path. Clinically
  meaningful same-event disagreements go to human resolution before export.

**Key scenarios:** New lab panel → lab, flagged, advisory, and FHIR outputs
refresh before the command returns. | Reference cache refreshed → Genetics and
composite reports regenerate. | Visit prep → a timestamped packet; canonical
facts untouched.

**Acceptance:** FHIR validates. Reports keep provenance in metadata or links.
Packets label missing or unreviewed inputs. No report contains dosing advice,
diagnoses, or bulk genotype data. No category handler ships without its refresh
hook.

**Links:** ADR-010, ADR-014, ADR-019, ADR-020, ADR-022, ADR-023, ADR-024, ADR-025, ADR-026, ADR-028, ADR-030, ADR-031, ADR-043 | REFERENCES: outputs-and-procedures.md

---

## REQ-017 Dependency and Readiness Checks
Status: active

**Behavior:**
- `python scripts/setup.py --check` reports each dependency as `OK`, `MISSING`,
  or `OUTDATED` with installed/required version and remediation.
- Required items block; optional tools warn.
- `fm-refresh-reference-cache` reports reference-data status.

**Acceptance:** Operations stop on missing required dependencies; optional tools
never block.

**Links:** ADR-007, ADR-011

---

## REQ-018 User Procedures
Status: active

Plain-language procedures a caregiver can follow without reading code, under
`docs/procedures/`. Each leads with the skill invocation and documents the
manual vault alternative where one exists. A skill's `SKILL.md` is the
authoritative description of its mechanics; procedures link to it.

**Acceptance:** A procedure exists for each user-facing workflow before it is
considered complete.

**Links:** ADR-012, ADR-015, ADR-043 | REFERENCES: outputs-and-procedures.md

---

## REQ-019 Skills and AI Guardrails
Status: active

**Behavior:**
- Skills live once under `.agents/skills/` and are reused by every agent
  (`.claude/skills` symlink, Gemini and Copilot adapters).
- Business logic skills (`fm-*`) encode product behavior, trace to requirements
  and decisions, and cite a capability level from
  `docs/references/model-capability-matrix.md`.
- The runtime ships no model-provider layer (ADR-013).
- Code changes keep docs and traceability current, pass simplification and
  review passes, tests, and Pyrefly type checks.

**Acceptance:** One canonical copy of each skill. An attended skill run needs no
configured model service.

**Links:** ADR-012, ADR-013, ADR-015

---

## REQ-020 Performance and Local Runtime
Status: active

**Behavior:** Interactive query p95 ≤ 10 s for up to 10 people, 100k lab rows,
and one DNA profile per person on a recent Apple Silicon laptop. Imports do not
crash or get killed by memory pressure.

**Links:** ADR-007

---

## REQ-021 Privacy and Security
Status: active

**Behavior:**
- All personal data directories are gitignored.
- No telemetry and no automatic external calls; external services need explicit
  user action.
- Secrets use the OS credential store.
- Reports avoid bulk genotype output.
- Tracked files never name real household members or carry their details
  (ADR-032).
- Live web queries during an agent session must be generic and free of
  identifying values (ADR-033).
- Reference downloads (gnomAD targets, PRS models) are selected only from public
  data, never from household genotypes or other vault state (ADR-023, ADR-024).

**Acceptance:** No personal medical data in tracked paths. External network use
is opt-in and auditable.

**Links:** ADR-010, ADR-011, ADR-022, ADR-023, ADR-024, ADR-032, ADR-033

---

## REQ-022 Success Criteria
Status: active

| Criterion | Target |
|---|---|
| Lab and DNA parsing accuracy | ≥ 95% on supported fixtures |
| Insight traceability | 100% source citation |
| Offline use | No internet after setup except intentional reference downloads |
| FHIR R4 export | Validates without errors |
| Incremental ingestion | Only changed inputs reprocessed; zero duplicates |
| Correction recalculation | 100% of affected outputs refreshed |
| DB–vault consistency | Divergences resolved by authority or surfaced to the user |
| Automated tests | Full suite passes |

**Links:** none (cross-cutting)

---

## REQ-023 Fact Corrections
Status: active

A user's explicit correction outranks every later import of the same fact.

**Behavior:**
- `fm-correct-fact` applies a correction immediately: vault note and DB row
  updated, `source_type: user_correction`, audit event with actor, reason, old
  and new values.
- Re-imports skip user-corrected rows (non-interactive: emit
  `skipped_user_correction`; interactive with a differing value: keep / accept /
  revoke).
- Revocation restores source authority.
- History facts are corrected by refutation of a label + date (ADR-004).
- Conflict and dependent reports regenerate after every correction or
  revocation.
- Corrections apply only to persisted facts, never to derived report cells.

**Key scenarios:**

#### Dose correction
- Given a supplement row imported at `100 mcg`
- When the user corrects it to `200 mcg`
- Then the row and note show `200 mcg` with `source_type: user_correction`, an
  audit event records both values, and re-importing the source does not
  overwrite it.

#### Refute an unsupported diagnosis
- Given a diagnosis repeated with the same visit date in several documents of one
  export
- When the user refutes that label and date
- Then every matching fact leaves the summary, packet, advisory, and FHIR export;
  a later export of the same visit stays refuted; the same label on another date
  still appears; revocation restores it.

#### Non-interactive re-import
- Given a live correction
- When the source is re-imported non-interactively
- Then the import value is skipped and `skipped_user_correction` is recorded.

**Acceptance:** No live correction is overwritten. Every correction, skip, and
revocation is audited. Correction and revocation are idempotent. Regenerated
notes keep `source_type: user_correction`.

**Links:** ADR-001, ADR-003, ADR-004, ADR-025 | See also: REQ-005, REQ-024

---

## REQ-024 Dated Update Notes
Status: active

User-authored updates on a given date are consolidated into dated notes that
carry source-level authority.

**Behavior:**
- `fm-record-user-update` writes one note per person, date, and category
  (medications, labs, history) as described in ADR-005, merging same-date
  updates.
- Notes use their category-native front matter (`record_type: medication`;
  `record_type: lab_panel` with `source_type: user_update`; `record_type:
  user_update` with `category: history`).
- Re-imports respect these rows exactly as they respect `user_correction` rows.
- Rebuild authority replay discovers medication and lab update notes through
  their DB rows and History update notes through their canonical paths. A
  History note that declares `source_document` or `source_path` is
  source-derived review material, not a user update.
- Notes for other dates or categories are never rewritten.

**Key scenarios:**

#### Multiple updates the same day
- Given the user reports on one date that a supplement was stopped and a new
  prescription started
- Then both facts land in
  `Records/dummy_alice/Medications/<date>-dummy_alice-medications.md`.

#### Same-day change
- Given a new prescription recorded at 10 mg earlier today
- When the user reports 20 mg later the same day
- Then the note shows 20 mg only.

#### Multi-category update
- Given a new medication, a home glucose reading, and a diagnosis reported
  together
- Then each is written to its category's dated note.

**Acceptance:** One note per person/date/category. Category-native front matter
validates. Re-imports respect update authority. Relevant reports regenerate.

**Links:** ADR-003, ADR-005 | See also: REQ-006, REQ-009, REQ-023

---

## REQ-025 Flagged-for-Discussion Lab Report
Status: active

A per-person `flagged-for-discussion.md` surfaces lab results worth discussing
with a clinician, with recency filters that suppress stale items.

**Behavior:**
- Sections and rules as defined in ADR-030: Out of Range (with Newly / Other
  split and a Not Re-checked group for out-of-range results 180 days to 2 years
  old that were never re-measured), Trending, Recently Normalized, and Coronary
  Calcium.
- Built from the same `build_lab_trends_from_db` data as `lab-results.md`;
  `lab-results.md` links this report rather than repeating its rows.
- The health advisory's lab observations use the same flagged-row filter.

**Key scenarios:** Glucose 115 mg/dL (ref 70–100) in the latest panel → Out of
Range, not Trending. | LDL from five months before the latest panel → still Out
of Range. | ALT rising 30 → 38 U/L against 0–40 → Trending; falling 39 → 32 →
not. | Upper-only reference 100 with a value of 90 rising → Trending; a value of
60 → not. | Previous out of range, latest in range → Recently Normalized. | A
POCT dipstick visit after a lipid panel → does not move the recency anchor. |
Cholesterol 255 mg/dL against an upper-only bound of 200 → Out of Range. | LDL
above range one year before the latest draw and not measured since → Not
Re-checked, with its date, and a re-check prompt in the advisory. | Test measured
three years ago → excluded. | High "Glucose" one year ago, "Glucose, Fasting"
(same LOINC component, property, and system) measured since → not listed.

**Acceptance:** Exists for every person with labs. Single-sided ranges are
classified by their one bound. Trending never duplicates Out of Range. SVG date
intervals identify the two plotted readings. The advisory's lab observations
match this report's set. Tests cover every rule above.

**Links:** ADR-030, ADR-031, ADR-035, ADR-037 | See also: REQ-008, REQ-016
