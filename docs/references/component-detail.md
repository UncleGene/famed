# Reference — Component Detail

Implementation detail behind `docs/SYSTEM.md`. The owning ADRs and requirements
remain authoritative. Load a section when a task touches that component.

## Ingestion pipeline and report refresh

The canonical user invocation is "drop files in `data/inbox/` and run a single CLI skill" (ADR-014, ADR-015). The orchestrating skill `fm-ingest-inbox` composes proposal (`fm-inbox-batch-processor` or `fm-inbox-freeform-processor`), in-chat verification, deterministic persistence (`fm-inbox-execute`), and required post-ingestion semantic history extraction (`fm-history-extract`) so one user intent maps to one completed invocation. Single-focused skills may be invoked directly when the user explicitly wants the narrower step. The pipeline runs end-to-end automatically wherever person, category, and content are unambiguous; ambiguity, conflict, low confidence, or mixed-source categories that lack canonicalizers route to the Human Resolver or `needs_logic_refinement`. C-CDA/IHE health-summary XML is treated as mixed-category when it contains medication, result, vital-sign, immunization, procedure, diagnosis, or encounter sections; a retained history note alone is not a completed import for those sources. Manual Obsidian entry remains for review, correction, fallback, and human-authored notes. Every successful ingestion event regenerates every derived report affected by the change, in dependency order, before the command returns (ADR-025): DNA ingestion regenerates the Genetics report (PGx, carrier screening) and then every composite that reads it — medication-safety alerts and health advisory; medication persistence cascades through the Genetics report's PGx section and the same composites. The person-scoped `pending-review.md` projection is part of medication and lab refresh cascades, the household refresh, and the immediate path when a resolved-person lab or medication source enters quarantine; DNA-only changes do not regenerate it. Successful reference-cache `download` or `refresh` operations are also dependency changes: when a vault and DB exist, `cache.py` runs the same household cascade with the updated reference DB and fails if the downstream consistency gate fails; failed cache operations do not regenerate outputs. Composite and pending-review regeneration is skipped — not failed — until the person note exists, because medication and lab data can arrive before onboarding writes it; the next ingestion-triggered or cache-triggered refresh catches configured reports up once onboarding completes. Lab, medication, immunization, and accepted-history changes also refresh the FHIR export when the person has both a vault identity note and DB person row. Same-event immunization status disagreements are rejected before canonical or vault writes and route to quarantine/human resolution rather than a preferred-string merge. The vault is not allowed to lag behind canonical state between ingestion runs or successful reference-cache changes.

## Pending-review projection

`pending_review.py` owns the `famed.pending-review-item.v1` in-process contract
and derives the current queue from category-owned evidence (ADR-043). Current
history evidence includes retained History notes whose verification status is
`needs_review`, distinguishing notes with no accepted facts from notes with
accepted facts under unresolved review status (ADR-039). Current medication
contradictions come from canonical medication rows; inbox and
quarantine items exist only while a non-hidden source file remains on disk.
`lab_rejections.py` re-reads retained HealthSummary observations, validates exact
reviewed decisions, and checks canonical textual coverage. Missing accepted
observations, unresolved rejections, quarantine decisions, and inaccessible
evidence enter current review; repeated equivalent statements share an item
with all member sources (ADR-039). Historical audit status is never treated as
current state. Unsupported source families remain explicit coverage warnings.
The skill refreshes every
person's `Records/<person_id>/Reports/pending-review.md`, returns unassigned
files only in its invocation output, and does not persist a cross-person note.

## Inbox orchestrator

The unified inbox orchestrator scans person-scoped, shared, and person-YAML inbox files; skips hidden OS/editor metadata files; routes supported inputs to handlers; marks shared/ambiguous files `awaiting_user_decision`; quarantines unsupported layouts. The preferred person-private inbox layout is a single drop folder per person (`data/inbox/<person_id>/...`) with deterministic category inference for labs, medications, history, DNA, and HealthSummary ZIPs; legacy person/category subfolders remain supported as compatibility input. It defaults to dry-run with `data/inbox_state.tsv` per-file ledger and batch summary CLI. Executed person-YAML routes retain accepted sources under `data/processed/persons/`; executed lab routes own the source lifecycle (accepted→`data/processed/`, invalid→`data/quarantine/`). Deterministic lab parsers (Quest, MyChart, POCT three-line, generic-numeric) are tried first; on failure or zero rows, the source is quarantined for attended interpretation by the agent running the ingest skill (ADR-016). The MyChart parser normalizes comma-grouped numeric reference bounds and accepts bounded explicit qualitative `Value` blocks. Unreferenced results additionally require a title-like multi-token test label and a nonnumeric, non-unit result; prose sentences, punctuation-ended labels, numeric values, and unit tokens fail closed. Embedded narrative numbers remain qualitative, and recognized POCT layouts stay with their dedicated parser. Attended fact extraction can persist high-confidence lab and medication candidates after deterministic schema validation; medication persistence writes unresolved RxNorm mappings as null until deterministic or reviewed mappings are available, while rendered medication notes/reports display those nulls as explicit `unresolved` markers. Reviewed mappings can be curated locally in `data/reference/rxnorm/rxnorm_review_overrides.jsonl`; valid reviewed entries override the downloaded RxNorm cache for the named medication, and an optional reviewed `cpic_drug` alias lets PGx coverage match brand/display names to local CPIC generic drug rows without changing the stored RxNorm CUI. Medication fact extraction accepts `.txt`, `.md`, `.pdf`, `.html`/`.htm`, and image (`.png`, `.jpg`, `.jpeg`) files; person-scoped medication files dropped into the person inbox root or legacy person/category subfolders fall back to reviewed placeholders for attended interpretation, and any resolved-person mismatch quarantines before persistence. Vendor portal CSV exports with non-canonical column names (Drug Name, Strength, Sig, etc.) are normalized to canonical fields before parsing. C-CDA/IHE health-summary XML routed through `history_review` now fans out deterministic structured lab observations, medication rows, vital-sign review notes, and immunization rows into dedicated lab, medication, vital, and immunization stores when those sections are present, while still writing the retained-source history review note for narrative/procedure/problem review. A C-CDA result value encoded as `xsi:type="ST"` is split into value and unit only when its complete text is one scalar quantity; compound and punctuation-ended text fails closed. High-confidence history candidates create retained-source review notes with `verification_status: needs_review` rather than authoritative DB rows. Immature category handlers get reviewed placeholder notes. Shared files can be resolved via CLI args, TSV resolution file, or whole-inbox `fm-` business skill proposals. Whole-inbox skill processing must support a from-scratch run that re-reads local sources instead of trusting stale ledger state. Low-confidence outputs remain unresolved or require human verification. Without `--interactive`, execute quarantines unresolved files. With `--interactive`, unresolved-person files trigger interactive terminal resolution instead of quarantine (REQ-005).

## Retained semantic decision bundles

Cold rebuild accepts legacy History-only attended decisions and the
`famed.retained-semantic-decisions.v2` contract. V2 is bounded to one
`history` and one `labs` extraction per retained source; Lab decisions are
permitted only for bounded domain-study PDFs retained under `Labs/`. Each
extraction revalidates checksum, person, evidence, confidence, and category
schema, then runs through its existing Canonicalizer and Data Steward inside
the source-level savepoint (ADR-039). The bundle is proposal input, never
authority.

`famed.retained-semantic-decisions.v3` additionally accepts one medication
extraction for an exact retained Medications source. When present, that
schema-valid proposal replaces the bounded supplement fallback rather than
merging with its lossy rows, then passes through the existing medication
Canonicalizer, RxNorm lookup, and Data Steward. Document and “current as of”
dates remain provenance/note dates unless the source explicitly states a
medication start (ADR-039).

`famed.retained-semantic-decisions.v4` additionally accepts one medication
extraction for a date-prefixed `visitSummary.pdf` retained under `History/`.
Replay processes History first and then sends the medication proposal through
the same Canonicalizer, reviewed RxNorm lookup, and Data Steward inside the
source savepoint. The bounded route does not extend to ordinary History PDFs or
XML, and later user-update/correction authority still applies after ordinary
source replay (ADR-039).

`famed.retained-semantic-decisions.v5` additionally accepts a Human Resolver
decision for an exact rejected, source-coded C-CDA `ST` Lab observation under a
`HealthSummary` archive member. The bundle carries only selector assertions and
resolution metadata; accepted medical fields are reconstructed from the
retained XML. Bundle loading validates the archive checksum, member, person,
complete selector, and expected occurrence count before replay can mutate DB or
vault state. Accept/reject/quarantine actions are append-only audited; accepted
rows use the existing textual Lab, reversible deduplication, source-evidence,
report, and FHIR paths. Resolution audit messages persist the validated exact
decision under `famed.lab-observation-decision.v1`. Archive preflight reuses
current checksum/selector/count-bound decisions; stale or missing review
coverage fails before mutation. Legacy summary-only resolutions require their
reviewed bundle once to recover structured identities (ADR-039). V1-v4 remain compatible, and
`--history-decisions` remains an alias for `--retained-decisions` (ADR-039).

## Deduplication Engine

Compares candidates and existing records. Classifies as new, updated, redundant, conflicting, needs_review. Maintains reversible duplicate links and merge confidence. AI semantic scoring only as helper evidence. No destructive merge without trace; no silent discard of conflicts. Deterministic deduplication uses explicit identity and stable normalized fields; semantic equivalence that requires clinical wording interpretation routes through AI-assisted scoring or review rather than growing keyword taxonomies (ADR-018). For an archive-backed lab restatement, one-sided missing unit or reference metadata is compatible only when normalized identity, nonblank LOINC code, value, and calendar date agree; populated conflicts stay distinct (REQ-004, REQ-008). When a candidate fact from a re-import matches a `user_correction` row, or a medication fact recorded through `source_type: user_update`: non-interactive → skip + emit `skipped_user_correction`; interactive correction conflict resolution supports keep / accept / revoke outcomes (ADR-004, REQ-024). Medication authority convergence is order-independent: recording an exact dated `user_update` after a lower-authority import first persists the authoritative row, then records an append-only `medication_redundant_row_superseded` event linking that row to the removed lower-authority row's stable canonical representation before removing the redundant DB copy. Volatile creation metadata is excluded; distinct periods and other user-authored rows remain separate (REQ-004, REQ-024). Cross-source medication suppression is reversible: a row skipped because an equivalent exists under another `source_path` is recorded in `medication_dedup_links` with the owning source and the full skipped payload. After every source import, links for the affected people are re-evaluated, and a fact no longer stated by any other source is restored under a source that does state it, emitting `medication_deduplicated_row_restored`. Restoration is withheld while a `user_correction` or `user_update` row governs that medication, and the link is retained so the fact stays recoverable if that authority is revoked (ADR-006, ADR-004, ADR-006). Lab observations use the same mechanism: a skipped duplicate is linked, and an observation no longer reported by any other source is restored with `lab_deduplicated_row_restored` under the same authority rules. Both categories share one `dedup_links` table discriminated by `subject_type`, while identity matching and restoration eligibility stay category-specific (ADR-006). A current cross-source medication dose/frequency contradiction receives a stable identity derived from its person, medication key/type, disagreeing axis, source paths, and normalized candidate values. Report refresh appends one idempotent `medication_conflict_detected` event in `awaiting_user_decision` only while the latest event for that stable identity is pending; a governing `user_correction` or `user_update` records resolution and keeps ordinary report refresh from re-opening the conflict, while explicit correction revocation may reopen it (ADR-042).

## C-CDA lab result extraction

C-CDA result extraction skips the SNOMED 246514001 companion "units" observation and uncoded operational assay metadata (lot identifiers, expiration dates, internal controls, and performing locations) before result validation. The metadata gate reuses the shared ADR-035 non-result classifier, never excludes a coded observation solely by display name, and leaves the retained source unchanged. Clinical extraction keeps a digit-free result or one bounded comparator scalar in `value_text`, and leaves a comparator's numeric `value` empty so it cannot become an exact trend point. A bounded culture result may additionally carry the structured `CFU/mL` unit plus a digit-free organism label; the unit is captured separately while the complete result remains textual. Any separately supplied unit must agree with embedded `CFU/mL` after normalization or the observation is rejected for review. A complete C-CDA `ST` value may also remain textual when it is one nonnegative ascending numeric interval with a structured unit, one positive ordinal such as `1+`, or a digit-free label followed by one parenthesized ordinal; these forms infer no numeric value, unit, analyte identity, or clinical meaning (ADR-036). Narrative suffixes including threshold/timing culture prose, compound readings, malformed or descending intervals, and every other clinical observation that fails deterministic validation are recorded as `lab_observation_rejected` audit events in `awaiting_user_decision` state with analyte, unit, and reason, so a result the source states cannot disappear without a trace (REQ-005, REQ-008, ADR-018, ADR-035, ADR-036). An exact source-bound v5 Human Resolver decision may remove a matched source-coded `ST` observation from the current awaiting set and accept it verbatim, reject it, or quarantine it. The decision is audited and cannot generalize its prose shape into deterministic parsing (ADR-039). Vendor unit spellings resolve through two reviewed lanes: analyte-independent notation aliases in `CANONICAL_UNIT_ALIASES`, and a closed analyte-scoped `REVIEWED_UNIT_EQUIVALENCES` table for equivalences whose validity depends on the analyte. `mEq/L` maps to `mmol/L` only for sodium, potassium and chloride, because the identity holds only at valence 1; a divalent analyte keeps its source unit and is rejected for review rather than converted, and no valence arithmetic is performed (ADR-035).

## Human Resolver

Resolves ambiguous person matches, unknown people, conflicting values, low-confidence normalization, manual overrides. Provides authoritative value when structural inference fails. Decides accept/reject/edit/quarantine. For unknown person outcomes, supports dismissing a wrong-person document, assigning an existing person, creating/updating matching data, or requesting matching-logic refinement. Interactive person resolution (`--interactive` flag) presents OCR'd or extracted document text alongside the source path so the user can inspect content; lists existing persons; and guides new-person creation through display name, birth year, sex, and relationship assignment to each existing person with reciprocal closure applied automatically. Works on a cold start (vault with zero configured persons). User-correction vs import-value interactive conflict prompts support keep / accept / revoke outcomes for lab and medication imports (ADR-004, REQ-023). During an attended `fm-ingest-inbox` run, each newly pending medication contradiction is presented with its disagreeing axes, candidate values, and retained source paths; the resolver may choose displayed values, supply values, or defer, but the agent does not recommend a medical dose or frequency. Because medication authority is row-scoped, every pending axis for one medication must receive an explicit value before a single correction is applied; deferring any axis leaves that medication pending. A confirmed complete choice routes through `fm-correct-fact`, which appends the linked resolution events and refreshes the report (ADR-042). For a rejected source-coded C-CDA `ST` Lab observation, an attended retained decision binds the action and review metadata to exact source fields; verbatim acceptance is reconstructed from the archive rather than retyped by the resolver (ADR-039). Never prompted to manually confirm deterministic import of unambiguous structured data. Requests concise and evidence-rich.

## Data Steward

Applies authority hierarchy (ADR-003, ADR-004), writes accepted records. Keeps source/vault/DB/audit trail consistent. Maintains note identity, backlinks, hubs, processed source references. Marks downstream artifacts stale after corrections/deletions. Preserves `source_type: user_correction` and `source_type: user_update` rows from import overwrites. When a dated medication update exactly restates a lower-authority imported period, writes or updates the authoritative row first, append-only audits the redundant row's stable canonical representation plus its surviving-row link, and removes only that redundant lower-authority DB copy; retained sources remain unchanged and different periods or user-authored rows are preserved (REQ-004, REQ-024). Dated medication updates preserve an explicitly supplied RxNorm CUI and otherwise use the same local reviewed/downloaded RxNorm lookup as other medication persistence paths; a cache miss remains null and visibly unresolved, with no live lookup. Reference storage resolves from an injected root or the supplied vault root, never the process working directory. Legacy migration cleanup may relabel an update row as `source_import` only when the exact DB `source_path` resolves to a note that already declares retained-source provenance; it preserves unproven user-update notes and rows (REQ-024). Writes `user_fact_corrected` and `user_correction_revoked` audit events for CLI-directed corrections via `fm-correct-fact` → `scripts/correct_fact.py` → `src/famed/correct_fact.py` (REQ-023). Writes `user_update_recorded` audit events for category-specific dated update notes via `fm-record-user-update` → `scripts/record_user_update.py` → `src/famed/user_update_sync.py` (REQ-024). For the closed ADR-035 repair lane, updates only exact reviewed `inferred` DB matches and their matching vault representations, and emits one `lab_inferred_loinc_repaired` event per DB row; source-provided codes remain unchanged. Each category (medications, labs, history) is persisted to its own subfolder under `Records/{person_id}/{Category}/` and appears in the corresponding category index. Emits `skipped_user_correction` audit event when a non-interactive re-import skips a conflicting user-authored row. For history Layer 2: after the Human Resolver accepts structured candidates, `accept_history_candidates` updates the vault note's `accepted_facts` front-matter field, sets `verification_status: accepted`, appends an `## Accepted Structured Facts` section, logs `history_candidates_accepted`, and regenerates `medical-history-summary.md`. The history summary is fact-oriented: diagnoses/findings, procedures, medication changes, and follow-up items are grouped logically with dates on the facts; a top-of-report structured Digest summarizes allowlisted chronic background conditions with first/last mention dates under ADR-029; vital signs render as a compact latest-value snapshot instead of a long dated event list; source visits/checkups are retained as provenance and review gaps, not the primary report structure. The report also derives diagnosis-support review prompts from local vault history, lab, medication, and vital notes; those prompts appear inline and in a review section but do not create, modify, or delete canonical facts. For visit prep, `fm-prepare-doctor-visit` writes a timestamped derived packet under `Records/<person_id>/Reports/doctor-visit-packets/` by composing existing report notes only; it does not create or modify canonical facts.

## Findings ledger

`fm-findings-ledger` → `scripts/findings_ledger.py` → `src/famed/findings_ledger.py`
appends record/supersede/stale JSONL events to `ledger/findings.jsonl` without
mutating prior entries. Genetics report generation records deterministic,
source-cited PGx coverage/readiness findings through the same contract: repeat
generation adds no duplicates, a changed interpretation supersedes the prior
finding, and an identity the latest complete recomputation no longer produces is
marked stale before composites render (ADR-025, ADR-027).
`supersede_stale_variants` handles state changes; `mark_orphaned_findings_stale`
reconciles identities that disappear entirely.

## Reference cache

- **Reference cache:** rebuildable public datasets (RxNorm, LOINC, CPIC, ClinVar, GenCC, targeted gnomAD), untracked. `fm-refresh-reference-cache` is the user-facing refresh/status workflow; `python scripts/cache.py status|refresh|clean` remains its implementation surface. Version manifest at `data/reference/manifest.json`. RxNorm fetched from RxNav REST API (paginated concepts in JSONL). LOINC is user-downloaded and placed under `data/reference/loinc/`; `python scripts/cache.py --dataset loinc refresh` parses a manually placed `Loinc.csv` into `loinc_concepts.jsonl` rather than calling UTS/UMLS APIs. Reviewed name/unit result mappings take precedence during initial LOINC inference so known method-specific, unit-incompatible, and order-only collisions cannot win when the source omits a method. Other normalized component/long-common-name collisions use the deterministic preference reviewed override map > `STATUS=ACTIVE` > serum/plasma `SYSTEM` > quantitative `SCALE` rather than file order (ADR-034). The result's unit then selects the mass or molar concept, and a concept whose property contradicts the unit is never used (ADR-034). Existing persisted codes are not passed through a general bulk re-inference; ADR-035 permits only a closed, exact-match, audited repair of reviewed-wrong `inferred` codes, while source-provided conflicts remain unchanged. CPIC, ClinVar, the ClinGen-origin GenCC inheritance subset, and targeted gnomAD rows build into `data/reference/famed-reference.db`. The GenCC boundary matches exact HGNC+disease CURIEs and refines only consistent Strong/Definitive AD/AR assertions; all other cases remain unresolved (ADR-022). The gnomAD target set comes only from public ClinVar pathogenic/likely-pathogenic GRCh38 alleles, joins by exact normalized chromosome/position/reference/alternate identity, validates its configured official release, fetches bounded GraphQL batches, and reconciles atomically. A failed attempt is status-visible and preserves the last good snapshot. The cache supplies display-only global joint/exome/genome frequency states to Health Advisory without report-time network access or household-derived remote selectors (ADR-023).

## Cold rebuild and retained replay

Cold rebuild orchestration begins with `fm-rebuild-vault` →
`scripts/rebuild_vault.py` → `src/famed/rebuild_vault.py`,
`src/famed/retained_replay.py`, `src/famed/authority_replay.py`,
`src/famed/dna_replay.py`, and `src/famed/rebuild_compare.py`. Its identity
stage requires explicit
non-overlapping source and target vault/DB paths, parses
and validates every source person note before creating target storage, closes
reciprocal relationships, writes all target people before family links, and
requires a clean SQLite foreign-key check. It preserves source person-note user
context and does not mutate source storage. The next stage copies the retained
medical corpus into an explicit isolated processed root, replays the shared
single-source registry with a savepoint and visible outcome per source, preserves
canonical `data/processed/...` provenance, and defers report refresh until batch
persistence completes for each affected person. A failed source restores both
its DB savepoint and its person-scoped vault tree; a report-refresh failure
restores pre-refresh reports while leaving already committed source notes and
rows consistent. Resume mode revalidates the identity graph and foreign keys,
resets target-derived medical state to that boundary, refreshes static target
files from the current tracked vault template, and then replays the same
isolated target from scratch. When authority replay is requested, the
orchestrator validates and materializes category-native update snapshots in
isolated processed storage before ordinary replay. This lets a raw medication
timeline prove exact equality against its authoritative snapshot on a fresh
target; it does not apply authority rows early. The next ordered stage reads
those snapshots and persists dated updates after ordinary sources. When a
current `user_update` row points to an authoritative vault note whose snapshot
is absent, preparation reconstructs that exact category-native snapshot in
isolated storage; resume preflight allows only those derived target-only paths.
A target-only recovered snapshot is refreshed from the current validated vault
note on every resume, while a real snapshot present in the source processed
tree remains source authority. The authority stage preserves authoritative
update-note bytes in the target vault, applies active `user_correction` rows from
the read-only source DB last (ADR-004), copies any correction-owned vault notes, and
refreshes affected reports after the overlay transaction. A non-DNA issue
audit stops at this boundary and uses targeted read-only comparisons for its
named tables and vault paths; it explicitly excludes DNA tables, Genes
artifacts, and DNA-dependent reports and is not replacement-ready. The workflow
continues to the DNA stage only when the active issue or declared audit task
includes DNA (ADR-040). That stage discovers retained sources only under the isolated
`<person_id>/Genes/` trees and calls the same deterministic persistence core as
inbox ingestion. One
savepoint and per-person vault snapshots cover all DNA rows, source notes,
profile summaries, and the complete DNA-dependent report cascade; any parser or
report failure restores both stores. Rebuild report refresh passes no findings
ledger path, so it cannot consume or mutate the working ledger. Any failed
source remains explicit diagnostic evidence rather than being hidden or treated
as a prerequisite to comparison. For a complete audit, the final diagnostic
stage regenerates the
isolated household's deterministic derived outputs without the working findings
ledger, then compares the working
and redriven processed corpora, DB tables, provenance, vault files, and derived
reports. It emits a versioned deterministic discrepancy audit with complete
counts and bounded evidence samples, including retained-source replay failures.
Semantic DB differences use multiset counts so duplicate loss remains visible.
When schemas differ, the schema observation remains explicit while declared
semantic, authority, provenance, operational, and volatile columns shared by
both sides are still compared; one additive migration cannot hide the rest of a
table.
User-update/correction authority assignments and provenance remain separate
projections. Volatile metadata and ledger-driven differences are classified
separately but never suppressed. Vault classification checks Reports before the
broader template-record namespace, preserves an explicit `needs_review` note as
a review item when it exists on only one side, and distinguishes parsed
front-matter formatting differences from volatile-value differences and
substantive content changes. The comparison opens both DBs read-only, and its
output must live outside protected storage; any confirmed bug is fixed and
reprocessed through its owning workflow only after separate review.

Retained-source reprocessing: one shared internal registry maps retained source path categories to handler callbacks for both single-source and batch replay. Labs, medication lists, user updates, history files, and retained health-summary ZIP archives are registered. The health-summary archive handler replays C-CDA member XML directly from the ZIP and removes any legacy preserved extracted XML after successful backfill.
History replay preflights matching vault notes before any mutation (ADR-039).
A standalone retained source containing accepted facts requires source-bound
semantic replay decisions. A HealthSummary archive requires them for matching
facts previously accepted by the Clinical Interpreter, while deterministic
C-CDA facts are regenerated normally. Failed preflight leaves vault, DB,
reports, stale markers, and audit untouched. History acceptance, review-note
creation, and reviewed-no-facts resolution refresh both the medical-history and
pending-review projections. Accepted history also refreshes every composite
that reads it, for the person and for each blood relative whose ADR-028
family-history context names them (ADR-010); auto-accept does this once per
batch and not at all when the caller defers report refresh.
Registry entries may add a bounded deterministic path predicate when category
and suffix alone are insufficient. The current predicate recognizes only
stable radiology, ophthalmology, and cardiology study-name prefixes on PDFs
retained under `Labs/`; it routes those sources through History review while
preserving the original retained path as provenance. Ordinary lab PDFs remain
on the lab handler, and zero or multiple matches fail visibly.
Before ordinary batch routing, a multi-dose medication narrative may be
classified as authority-deferred only when its filename date selects a
category-native update snapshot and strict timeline semantics exactly match
that snapshot. The comparison covers medication name, all doses, frequency,
ordered start dates, derived end dates, and status. A mismatch remains on the
ordinary handler and fails visibly; the deferred narrative writes no DB row.
The ordered authority overlay discovers medication and lab update notes through
their authoritative DB rows. Because History updates have no DB row, it also
discovers category-native dated History update notes directly from the working
vault. That discovery excludes any note declaring `source_document` or
`source_path`: retained-clinical-source interpretation is History review
material, not user-authored authority.
History auto-accept receives the active processed-storage root explicitly. It
resolves canonical `data/processed/...` archive/member provenance beneath that
root, including isolated rebuild storage, and fails the source replay if the
retained C-CDA file/member is missing or invalid. Attended history extraction
may still use its documented narrative fallback; deterministic retained replay
must not silently downgrade from structured XML because of storage layout.
For semantically heterogeneous retained History documents, an attended
`famed.history-extraction-decisions.v1` bundle may enter isolated replay as
proposal input. Each entry binds one
`clinical_interpreter.fact_extraction.v1` payload to canonical source
provenance, the isolated retained-file checksum, and the matching person. The
loader rejects stale/missing evidence, duplicate provenance, wrong person,
unsupported category, and below-threshold proposals before a handler runs.
Replay invokes the Clinical Interpreter contract for otherwise unresolved
notes, then the existing deterministic validator and Data Steward acceptance
path. Semantic immunization candidates additionally pass through the
immunization persistence contract; the model output and bundle never become
source authority.
History notes accepted through that path record
`extraction_mode: clinical_interpreter`; the note schema permits the field so a
differential audit can retain semantic extraction variance as review evidence
without declaring it contract-explained. History candidate facts may carry
optional value/unit fields for vital-sign rendering and CVX/lot fields for
immunization persistence. The immunization steward runs before History
acceptance performs the final dependent FHIR refresh.

## Data category status

Full status text for the Data Categories table rows in `docs/SYSTEM.md`.

### Medications

Text portal exports, HTML/image medication sources, and vendor-style CSV medication exports are supported. The current-medications report groups non-PRN rows by type and schedule, then combines all as-needed rows in one final type-labelled section without changing canonical facts. `medication-conflicts.md` owns detailed contradiction evidence; the compact `pending-review.md` projection links to it without duplicating candidate rows. Cross-source dose/frequency contradictions preserve both facts, render with source paths and `verification_status: needs_review`, and append one stable pending audit event; explicit user authority clears both current projections and records the resolution (ADR-042, ADR-043). A prescription's dispense-authorization window is held in `medications.authorized_through`, separate from `end_date`, which means a therapy stop only: a C-CDA `INT`-mood `effectiveTime/high` is a refill expiry and never terminates a medication, while an `EVN`-mood high does; source `statusCode` stays authoritative for currency, a lapsed authorization renders as a renewal action, and FHIR export carries it as an `authorized-through` extension rather than folding it into `effectivePeriod` (ADR-041). When contradictory active rows must be ranked, a row without a start date stands in the currency of its source document's medication list — judged by the start dates of the drugs on that list, never by the archive or import date. C-CDA medication dates written in UTC are re-expressed in the offsets the document itself declares before truncation, and only when every declared offset agrees on the resulting date, so an end-of-local-day boundary does not read as the next day and a document spanning a daylight-saving change fails closed; no configured or machine-derived timezone is involved.

### DNA/genomics

Supported: 23andMe, AncestryDNA, MyHeritage, Genotek, Nebula Genomics, generic VCF/VCF.GZ, and single-file ZIP-wrapped raw DNA ingestion on the unified pipeline (auto-detect, stream normalize, `genotypes` table, vault notes, and dependent Genetics report refresh; imputation readiness is printed on demand). ZIP imports parse the contained raw DNA file but retain the ZIP as the authoritative source. VCF imports preserve GRCh37/GRCh38 metadata when present; imputed calls keep quality metadata and low-r2 rows are filtered before persistence. Direct genotyped calls take precedence over imputed rows for the same person/rsID. Imputation readiness reports aggregate source-kind/build/quality metadata only; they never upload, export, score, or list genotype rows. PRS model/input validation is on-demand through `fm-prs-validation` with an explicit local pinned model package; it reports only aggregate model/input compatibility and never scores, infers ancestry, estimates disease risk, lists rsIDs/genotypes/weights, exposes source paths, or makes network requests. The rebuildable `famed-reference.db` stores CPIC recommendations, drug-gene pairs, allele definitions, exact diplotype-to-gene-result translations, ClinVar variant pathogenicity plus source identifiers, ClinGen-origin GenCC inheritance assertions, and a targeted exact-GRCh38 gnomAD frequency snapshot derived only from public ClinVar reference rows. CPIC results retain their per-gene vocabulary: metabolizer status for metabolism genes and Function status for supported transporters (ADR-021). Do not adopt a separate genome browser/web-app stack, TTS/voice surface, or chat UI; bulk genotype exploration remains outside caregiver-facing `famed` outputs.

### Insights/reports/exports

Available: the doctor-visit packet; `lab-results.md` and flagged-for-discussion;
local query; medication-safety review alerts; the non-authoritative health
advisory (counts-only genetic evidence disposition, exact-match ClinGen
inheritance refinement, display-only gnomAD frequency, annotated screening
reminders, blood-relative family history); the Genetics report (gene-centric
PGx table and carrier screening); FHIR export (Patient, lab Observation,
MedicationStatement, DiagnosticReport, accepted-history Condition,
FamilyMemberHistory, Immunization) with standalone validation; on-demand
imputation readiness and PRS model/input validation; the append-only findings
ledger; and scrubbed-generic correctness audits for lab, medication, and
DNA/ClinVar annotation facts. PRS score rendering, interaction severity scoring,
and further FHIR resources are not implemented (see `docs/PLAN.md`).
