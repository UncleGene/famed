# famed — Decisions

Accepted architecture and product decisions. Each ADR states the decision as it
currently stands. When a decision changes, edit the owning ADR (or replace it)
so this file never carries an overridden decision.

## Index

| ID | Decision area | Load full body when touching |
|---|---|---|
| ADR-001 | AI proposes, code persists | AI extraction, validation boundary, persistence |
| ADR-002 | Agentic roles | Interpreter, Canonicalizer, Dedup Engine, Human Resolver, Data Steward |
| ADR-003 | Authority hierarchy | Source/vault/DB/user precedence and divergence |
| ADR-004 | User corrections and refutations | `fm-correct-fact`, `user_correction`, history refutation |
| ADR-005 | Dated user update notes | `fm-record-user-update`, same-day semantics |
| ADR-006 | Reversible deduplication | Duplicate classification, `dedup_links`, restoration |
| ADR-007 | Runtime and local environment | Python/SQLite/Obsidian, readiness check, performance budget |
| ADR-008 | Coding standards | LOINC, RxNorm, ICD-10, rsID, reviewed RxNorm lane |
| ADR-009 | Note contract | Front matter, note body, idempotent notes |
| ADR-010 | Household vault | Vault layout, person partitioning, cross-person reads |
| ADR-011 | External reference data | Reference cache, `famed-reference.db` |
| ADR-012 | Documentation protocol | Core docs, inline traceability, skill source |
| ADR-013 | Skills-first AI | Business skills, capability matrix, no provider layer |
| ADR-014 | Inbox workflow and vault-as-context | Inbox drop, vault as AI context, DB-read boundary |
| ADR-015 | Skills as user interface | User-facing surface, one intent per invocation |
| ADR-016 | AI fallback for labs | Unrecognized lab formats |
| ADR-017 | Health-summary archives | Archive retention, member provenance, provenance supersession |
| ADR-018 | Deterministic processing boundary | Parser scope, phrase lists, render-time classification |
| ADR-019 | Preventive screening currency | Screening annotations in the health advisory |
| ADR-020 | Conservative DNA interpretation | PGx, carrier, risk boundaries |
| ADR-021 | CPIC gene-result vocabularies | Metabolizer vs Function results |
| ADR-022 | Genetic risk flags | Zygosity, ClinVar↔GenCC inheritance refinement |
| ADR-023 | Targeted gnomAD frequency | Population-frequency cache and display |
| ADR-024 | PRS validation before scoring | PRS model/input validation |
| ADR-025 | Derived-report dependency graph | Report refresh, cache invalidation, consistency gate |
| ADR-026 | Live-agent synthesis | Synthesized insights, validation gate, staleness |
| ADR-027 | Findings ledger | Append-only findings |
| ADR-028 | Family-history context | Blood-relative scope, sanitization |
| ADR-029 | History digest allowlist | Chronic/event rollups |
| ADR-030 | Flagged-for-discussion report | Out-of-range, trending, recency rules |
| ADR-031 | Caregiver report set | Doctor packet, Genetics report, inputs-only reports |
| ADR-032 | Privacy of tracked files | No real names, `dummy_` fixtures |
| ADR-033 | Scrubbed web search | Correctness audit queries |
| ADR-034 | LOINC inference and provenance | Code origin, canonical selection, unit-aware inference |
| ADR-035 | Reviewed lab correction lanes | Unit fill, unit equivalence, inferred-code repair, row overrides |
| ADR-036 | Textual lab results | `value_text`, comparators, ordinal/interval text |
| ADR-037 | Dual-reference-range series | One series, per-reading thresholds |
| ADR-038 | Source notes as evidence projections | Lab/medication source notes, retirement |
| ADR-039 | Attended replay decisions | Retained decision bundles, replay safety |
| ADR-040 | Isolated rebuild | `fm-rebuild-vault`, scoped audits |
| ADR-041 | Medication authorization window | `authorized_through`, C-CDA dates |
| ADR-042 | Medication contradictions | Per-axis detection, import-time resolution |
| ADR-043 | Pending-review reports | `pending-review.md`, `fm-review-pending` |

---

## ADR-001 AI Proposes, Deterministic Code Persists
Status: accepted | Related REQs: REQ-001, REQ-002, REQ-003, REQ-005, REQ-019, REQ-023

### Context
Messy medical documents cannot be normalized reliably by deterministic parsing
alone, but AI-only normalization is unsafe.

### Decision
- Ingestion is split into an AI-assisted **Clinical Interpreter**, which proposes
  candidate facts with confidence and raw evidence, and a deterministic
  **Canonicalizer**, which enforces schema, terminology, units, and
  accept/quarantine rules.
- AI is proposal-only. It may interpret, rank, classify, summarize, and explain.
  It cannot write canonical truth, override a schema, or silently merge or
  discard candidates.
- Every untrusted proposal is schema-validated against its source
  (`validate_fact_extraction`) before any category handler or persistence path
  runs.

### Consequences
- More components and tests, less risk of silent AI corruption.
- Interpretation can improve without changing persistence semantics.

---

## ADR-002 Agentic Role Split
Status: accepted | Related REQs: REQ-001–REQ-005

### Decision

| Role | Authority | Responsibility |
| :--- | :--- | :--- |
| Clinical Interpreter | Proposal only | Understand heterogeneous inputs; produce candidate facts. |
| Canonicalizer | Deterministic | Validate schemas, map codes, normalize units, reject or quarantine. |
| Deduplication Engine | Hybrid | Classify new/duplicate/conflicting facts; keep reversible links. |
| Human Resolver | Human | Resolve ambiguity, conflicts, low-confidence matches, overrides. |
| Data Steward | Persistence | Apply authority rules, write notes and DB rows, audit changes. |

In practice the Clinical Interpreter is the live agent executing an `fm-*`
skill (ADR-013).

---

## ADR-003 Authority Hierarchy
Status: accepted | Related REQs: REQ-001, REQ-004, REQ-005, REQ-006, REQ-023, REQ-024

### Decision

```text
user_correction / user_update > retained source document > Obsidian vault note > famed.db
```

- The vault is the canonical human-readable working record; `famed.db` is a
  derived index.
- A `user_correction` row, or a row owned by a dated update note
  (`user_update`), is the highest-authority representation of its fact until
  the user revokes it (ADR-004, ADR-005).

Divergence rules:

| Condition | Rule | Action |
| :--- | :--- | :--- |
| Vault modified; DB unchanged since last sync | Vault wins | Update DB, log resolution |
| DB modified by pipeline; vault unchanged | DB wins | Update vault, log resolution |
| Vault and DB modified independently | Ambiguous | Halt record, ask the user |
| Import matches a user-authored row; values agree | User row wins | Skip import row |
| Import matches a user-authored row; values differ; interactive | User row wins unless revoked | Offer keep / accept import / revoke |
| Import matches a user-authored row; values differ; non-interactive | User row wins | Skip; emit `skipped_user_correction` |

Non-interactive ambiguity is quarantined.

---

## ADR-004 User Corrections and Refutations
Status: accepted | Related REQs: REQ-005, REQ-006, REQ-010, REQ-016, REQ-023

### Context
A user who fixes a persisted fact must not have the fix undone by the next
re-import. History facts have no DB row, and a clinic export can repeat an
unsupported diagnosis across several documents with the same visit date.

### Decision
- `fm-correct-fact` is the single user surface. It proposes the change, confirms
  with the user, and delegates persistence to the Data Steward. AI never applies
  a correction on its own.
- **Lab and medication facts:** the corrected row carries
  `source_type: user_correction` and follows ADR-003. All import paths check for
  it before applying an incoming value. Audit events: `user_fact_corrected`,
  `skipped_user_correction`, `user_correction_revoked`.
- **History facts:** the user *refutes* a fact. The identity is the
  whitespace/case-normalized label plus its recorded date (the note's event date
  when the fact has none). It matches that fact from any source; the same label
  on another date is unaffected. Refutations live in
  `Records/<person_id>/History/history-corrections.md`
  (`record_type: history_correction`). The check sits at the single write
  boundary for history facts: matching facts move to `refuted_facts` and render
  under `## Refuted by User`. Apply, revoke, and rebuild replay re-partition
  existing notes, so no reader needs its own filter.
- Revocation is always available and restores source authority. Retained
  sources are never modified.
- Conflict reports and dependent reports regenerate after every correction or
  revocation.

### Consequences
- Re-imports cannot silently undo a user fix.
- Each label variant needs its own refutation; there is no synonym inference.
- FHIR Condition IDs inside an affected note can shift; exports regenerate
  deterministically.

### Alternatives considered
- Editing the note by hand: overwritten on reprocess, no audit trail.
- Filtering refuted facts in each reader: one missed reader leaks the fact.
- Matching refutations by source document or by label on any date: misses
  repeats in other documents, or hides a genuine later diagnosis.

---

## ADR-005 Dated User Update Notes
Status: accepted | Related REQs: REQ-006, REQ-009, REQ-024

### Decision
- User-authored updates (new prescriptions, stops, dose changes, home-test lab
  results, diagnoses) are persisted through `fm-record-user-update` into one
  dated note per person, date, and category:
  - `Records/<person_id>/Medications/<date>-<person_id>-medications.md`
  - `Records/<person_id>/Labs/<date>-<person_id>-labs.md`
  - `Records/<person_id>/History/<date>-<person_id>-history.md`
- Same-date updates merge into that category's note. Medications use snapshot
  semantics keyed by normalized name (a later same-day entry overwrites).
  Labs deduplicate by `(test_name, observed_at)`; history by normalized
  description.
- Medication and lab rows carry `source_type: user_update` with the note as
  `source_path`; the history note itself is the authoritative artifact.
- Re-imports treat these rows exactly like `user_correction` rows (ADR-003).
- Reviewed RxNorm lookup applies to dated medication updates; a cache miss stays
  visibly unresolved.

### Alternatives considered
- Event-log semantics for same-day updates: more complex than the caregiver's
  "this is what I'm taking today" model.
- One mixed-category note: breaks directory-as-category navigation.

---

## ADR-006 Reversible Deduplication
Status: accepted | Related REQs: REQ-001, REQ-003, REQ-004, REQ-006, REQ-008, REQ-009, REQ-023, REQ-024

### Decision
- Deduplication is hybrid: deterministic pre-filter by person, category, date,
  code, unit, source, and value; AI-assisted scoring only as helper evidence for
  hard cases; final classes `new`, `updated`, `redundant`, `conflicting`,
  `needs_review`.
- No destructive merge. A row skipped as cross-source redundant is recorded in a
  shared `dedup_links` table (`subject_type` = `medication` or `lab_result`)
  with the suppressed and owning `source_path` and the full skipped payload.
- A source's links are re-derived on every re-import. After each import, links
  for the affected people are re-evaluated: when no other source still states
  the fact, the skipped row is restored under the source that does
  (`medication_deduplicated_row_restored`, `lab_deduplicated_row_restored`).
- Restoration never overrides user authority; the link is retained so the fact
  stays recoverable if the correction is revoked.
- Link storage helpers are shared (`storage_contract`); identity matching and
  restoration eligibility stay in each category module.

### Consequences
- A fact stated by any retained source cannot vanish from canonical storage
  because a different source stopped stating it.
- AI similarity never overrides deterministic conflict rules.

---

## ADR-007 Runtime and Local Environment
Status: accepted | Related REQs: REQ-017, REQ-019, REQ-020

### Decision
- **Stack:** Python, SQLite (standard-library `sqlite3`), Obsidian with Dataview,
  macOS Keychain for secrets. Orchestration frameworks, retrieval libraries,
  medical NLP toolkits, and a web frontend are not adopted; each needs its own
  decision when a concrete need appears.
- **Readiness check:** `python scripts/setup.py --check` reports `OK`,
  `MISSING`, or `OUTDATED` per dependency with remediation. Required items
  (Python packages, Dataview, Keychain) block; optional tools (Tesseract OCR,
  PLINK) warn.
- **Performance budget:** on a recent Apple Silicon laptop, an interactive query
  stays within p95 ≤ 10 s for up to 10 people, 100k lab rows, and one DNA profile
  per person. Background imports must not crash or be killed by memory pressure.

---

## ADR-008 Coding Standards
Status: accepted | Related REQs: REQ-003, REQ-006, REQ-008, REQ-009, REQ-011

### Decision

| Domain | Standard | Notes |
| :--- | :--- | :--- |
| Lab tests | LOINC | Required for structured results where available. |
| Medications | RxNorm CUI | Required for PGx matching where available. |
| Diagnoses | ICD-10 | Optional; manually correctable. |
| Genetic variants | rsID | Reports use rsIDs only; no bulk genotype dumps. |

- **Reviewed RxNorm lane:** `data/reference/rxnorm/rxnorm_review_overrides.jsonl`
  holds human-approved mappings (`name`, `rxcui`, `reviewed_by`, `reviewed_at`,
  optional `cpic_drug` alias). A valid reviewed entry wins over the downloaded
  cache. Agents may propose entries; a human approves them.
- A medication without a deterministic or reviewed mapping keeps
  `rxnorm_cui` null and renders as `unresolved`.

---

## ADR-009 Note Contract
Status: accepted | Related REQs: REQ-006

### Decision
- YAML front matter holds compact metadata only (`person_id`, dates, tags,
  categories, status, source references). No embedded JSON blobs.
- Substantive content lives in the Markdown body.
- Metadata stays flat or shallow for Dataview and deterministic validation.
- Every persisted note is idempotent. Generated sections may be regenerated in
  place; documented user-authored sections survive regeneration.

---

## ADR-010 One Household Vault With Person-Partitioned Records
Status: accepted | Related REQs: REQ-006, REQ-007, REQ-016, REQ-021

### Decision
- One vault, one SQLite index, one reference cache, and one inbox root per
  household.

```text
famed-vault/
  Persons/<person_id>.md
  Records/<person_id>/
    Labs/  Medications/  History/  Genes/  Insights/  Reports/
```

- Person data stays under `Records/<person_id>/` and `Persons/<person_id>.md`.
  There is no shared household folder.
- Every note is reachable from a hub or index.
- A cross-person read is allowed only through a declared feature that names its
  relationship scope and its refresh dependency on the relatives it reads
  (family-history context, ADR-028).
- If sharing one member's records becomes a requirement, it is served by a
  derived per-person export, not by restructuring storage.

### Alternatives considered
- Per-person vaults: isolation, but a multi-vault router for inbox, refresh,
  review, and rebuild, and no family-history context.

---

## ADR-011 External Reference Data
Status: accepted | Related REQs: REQ-003, REQ-011, REQ-017, REQ-021

### Decision
- Public reference data is a rebuildable, untracked cache under
  `data/reference/`, never committed. Only lightweight manifests are tracked.
- Annotation data (CPIC, ClinVar, GenCC, targeted gnomAD) builds into a separate
  `data/reference/famed-reference.db`, so it can be deleted and rebuilt without
  touching user records.
- LOINC is downloaded manually by the user (no UTS/UMLS API). RxNorm comes from
  the public RxNav API.
- `fm-refresh-reference-cache` is the user surface for refresh and status.
- User-owned medical data and reference data stay in separate locations.

---

## ADR-012 Documentation Structure and Agent Protocol
Status: accepted | Related REQs: REQ-018, REQ-019

### Decision
- Core docs: `CORE.md`, `REQUIREMENTS.md`, `DECISIONS.md`, `SYSTEM.md`,
  `PLAN.md`. Dense references live under `docs/references/`, caregiver
  procedures under `docs/procedures/`.
- Traceability is inline: `Links:` on requirements, `Related REQs:` on decisions.
- `DECISIONS.md` records current decisions only; a changed decision edits or
  replaces its ADR. `SYSTEM.md` is a current snapshot, not history. `PLAN.md`
  lists only forward-looking work.
- Session checkpoints (`docs/sessions/`) are local working notes and are not
  tracked.
- Repo-local skills under `.agents/skills/` are the canonical source for every
  agent provider; provider adapters point to them instead of copying them.

---

## ADR-013 Skills-First AI Without a Provider Layer
Status: accepted | Related REQs: REQ-001, REQ-002, REQ-010, REQ-015, REQ-019

### Decision
- All AI-dependent product behavior is specified as business logic skills,
  schema contracts, fixtures, and manual verification flows.
- The live agent executing a skill *is* the Clinical Interpreter. The runtime
  ships no model-provider layer, routing config, or unattended model calls.
  Reintroducing an unattended provider path requires a new decision.
- The provider-neutral boundary that remains is the `FactInterpreter` protocol
  plus `validate_fact_extraction` (ADR-001).
- Business logic skills are distinct from development-process skills and declare
  a minimum capability level from
  `docs/references/model-capability-matrix.md`. The matrix calibrates what the
  executing agent must be capable of; it is not a provider allowlist.

### Alternatives considered
- Provider-first rollout: optimizes model plumbing before verified behavior.
- Model requirements inside every skill: duplication and drift.

---

## ADR-014 Inbox Workflow and Vault-as-Context
Status: accepted | Related REQs: REQ-001, REQ-014, REQ-015, REQ-016, REQ-018

### Decision
1. **Canonical workflow.** New data enters by placing files in `data/inbox/` and
   invoking one skill (`fm-ingest-inbox`). Manual Obsidian editing is a fallback
   for review, correction, and human-authored notes.
2. **Vault-as-context.** The vault and its linked retained sources are the
   authoritative context for reports, insights, and queries. Those paths do not
   read `data/inbox/`, `data/processed/`, or `famed.db` as a substitute for
   evidence the vault already exposes.
3. **DB-read boundary.** A report may read `famed.db` directly as a join,
   aggregate, or structural index (a genotype↔ClinVar join, a latest-date
   lookup, a relationship lookup that selects which vault notes to read). When a
   DB read supplies caregiver-facing evidence not already cited in the report,
   the report records the vault notes it stands in for in front matter (for
   example `family_history_sources`).
4. **Reports refresh on ingestion** (ADR-025).

---

## ADR-015 Skills Are the User-Facing Interface
Status: accepted | Related REQs: REQ-001, REQ-015, REQ-018, REQ-019

### Decision
1. Repo-local skills under `.agents/skills/` are the documented user surface.
   `scripts/` and `src/famed/` are implementation detail that skills invoke.
   New user-visible workflows are authored as skills.
2. One intent, one invocation. Skills may compose other skills and call internal
   Python entry points, but the user never chains commands for one intent.
3. Single-focused skills plus orchestrating skills. The orchestrating skill is
   the documented entry; its parts may be invoked directly for a narrower step.
4. Skill output uses role names, not implementation flags.
5. ADR-001 still binds: the skill orchestrates and proposes; deterministic code
   writes canonical truth.

---

## ADR-016 AI Fallback for Unrecognized Lab Formats
Status: accepted | Related REQs: REQ-001, REQ-002, REQ-008

### Decision
- Deterministic lab parsers (Quest, MyChart, POCT, generic numeric) run first
  for known formats.
- When they yield zero rows or fail, the source is left for attended AI
  extraction by the agent running the ingest skill instead of being quarantined
  outright. AI-extracted lab facts pass the same Canonicalizer, LOINC mapping,
  schema validation, and Data Steward path.
- If AI extraction also fails or is low confidence, the source is quarantined.

### Alternatives considered
- A new deterministic parser per format: unbounded and brittle.
- AI as the only lab path: deterministic parsers are faster and auditable for
  regular formats.

---

## ADR-017 Retained Health-Summary Archives
Status: accepted | Related REQs: REQ-001, REQ-004, REQ-006, REQ-010

### Decision
- A health-summary ZIP (IHE XDM / C-CDA bundle) is the authoritative retained
  source. Extracted member XML files are transient parse inputs and are never
  preserved under `data/processed/`.
- Provenance for a fact supported by one member uses
  `data/processed/.../archive.zip#member.xml`. When several members of one
  archive restate a fact, presentation compacts provenance to the archive path.
- Reprocessing operates directly on the retained ZIP.
- **Same-provider supersession:** a later archive from the same person and the
  same normalized provider family may replace older archive provenance on the
  same canonical fact or event. Different-provider overlap and partial overlap
  are preserved. Retained ZIPs and audit events are never deleted. When provider
  identity is unclear, provenance is preserved.

---

## ADR-018 Deterministic Processing Boundary
Status: accepted | Related REQs: REQ-001, REQ-002, REQ-003, REQ-004, REQ-010

### Decision
Deterministic code is limited to:
1. Parsing machine-readable structure and well-bounded formats (CSV, C-CDA
   sections, known lab export rows, explicit date/value fields).
2. Validation, unit/code normalization, authority rules, deduplication by
   explicit identity, persistence, reports, and audit.
3. Stable, reversible presentation transforms.

Deterministic code must not grow source-specific semantic phrase lists or broad
keyword inference to **extract or interpret facts from heterogeneous clinical
prose**. That belongs to the Clinical Interpreter or an `fm-*` skill, followed by
the same deterministic validation.

**Render-time classification is allowed.** Choosing which report heading an
already-extracted, already-validated fact renders under (for example
`_history_medical_area`) is not fact extraction. Such rules must:
- be checked against real data for cross-specialty and negation collisions,
  using multi-word phrases or word boundaries where a bare term could collide;
- only ever affect navigation, never canonical content, dates, codes, or
  downstream clinical claims.

### Consequences
- Some deterministic auto-accept coverage is intentionally smaller.
- Review passes challenge any new parser branch that adds clinical phrase lists.

---

## ADR-019 Preventive Screening Currency
Status: accepted | Related REQs: REQ-016

### Decision
- Health-advisory screening reminders are age-based and annotated, not removed,
  when a screening is already current: `— last done <date>; not due again until
  ~<year>` (or "one-time screening" for hepatitis C). An elapsed interval leaves
  the reminder unannotated.
- Lipid, diabetes, and hepatitis C currency come from `lab_results` and fixed
  re-screening intervals.
- Colorectal currency comes from the latest colonoscopy-related history fact.
  Exactly two follow-up phrasings are parsed: "… in `<N>` years" and
  "… due `<Month> <Year>`". Otherwise the default is 10 years, or 5 when a
  sibling fact in the same note mentions a polyp or adenoma.
- This is a closed, bounded exception to ADR-018. A third phrasing must be
  handled by structured `next_due` extraction through the history skill, not by
  another regex.
- No reminder is shown for a condition already in the person's history Digest.

---

## ADR-020 Conservative DNA Interpretation Boundary
Status: accepted | Related REQs: REQ-011, REQ-012, REQ-013, REQ-016

### Decision
1. DNA provider is auto-detected from headers and content.
2. Star-allele calling is deterministic and only where CPIC provides allele
   definitions; other genes stay `not interpretable`. Consumer-chip limits
   (missing rare alleles, no copy number) are stated.
3. Phenotypes follow CPIC translation tables only (ADR-021); no novel inference.
4. ClinVar-based risk flags are population-level observations, not diagnoses
   (ADR-022).
5. Every interpretation output carries the clinical-safety callout. No dosing
   recommendations, diagnoses, or treatment plans. Consumer DNA evidence is
   capped at tier E2.
6. Caregiver-facing PGx output shows a plain-language result per gene; full CPIC
   evidence detail lives in an optional technical section.
7. Every interpretation surface ships as an `fm-*` skill.

### Out of scope
Dosing recommendations, clinical-grade PGx reports, interaction severity
scoring, and disease-probability scoring.

---

## ADR-021 CPIC Gene-Result Vocabularies
Status: accepted | Related REQs: REQ-012, REQ-014, REQ-016

### Decision
1. CPIC's `/diplotype` API is the authoritative genotype-to-gene-result source.
   The cache stores gene, normalized and display diplotype, allele functions,
   activity values, and `generesult`, with source URL, refresh time, and row
   count.
2. Each result carries an explicit vocabulary (`metabolizer` or `function`).
   Transporter results (SLCO1B1, ABCG2) are "Function" results, never called
   metabolizer phenotypes.
3. Results come only from an exact cached diplotype match. Missing rows,
   incomplete coverage, or unsupported genes stay uninterpreted.
4. Multi-gene medications keep one `gene: result` per gene. Function and
   metabolizer results are never ranked against each other.
5. No dosing, no de novo interaction severity, no inference from CPIC prose.

---

## ADR-022 Genetic Risk Flags
Status: accepted | Related REQs: REQ-014, REQ-016, REQ-017, REQ-021

### Decision
1. **Zygosity.** Each flag shows heterozygous (1 copy) or homozygous (2 copies)
   and its ClinVar Variation ID.
2. **Default framing.** A one-copy pathogenic finding may mean carrier status
   (recessive) or risk (dominant); without resolved inheritance the flag says so
   and routes the question to a clinician.
3. **Inheritance refinement.** A ClinGen-origin GenCC subset (submitter
   `GENCC:000102`) is cached locally. Inheritance resolves only when ClinVar and
   GenCC match on exact HGNC CURIE plus at least one exact disease CURIE (case and
   delimiter normalization only; no name similarity). Only Strong or Definitive
   autosomal dominant (`HP:0000006`) or autosomal recessive (`HP:0000007`)
   assertions qualify. Conflicting, disputed, refuted, weak, or complex-mode
   evidence leaves the default framing.
4. Resolved wording: heterozygous recessive → carrier-compatible observation;
   dominant → risk-associated observation; homozygous recessive → two-copy
   risk-associated observation. Each refined flag cites its GenCC SGC record.
   None is a diagnosis; the E2 cap holds.
5. **Disposition transparency.** Every pathogenic-text allele match is counted in
   exactly one disposition (accepted, conflicting classification, insufficient
   review, policy exclusion, duplicate condition, beyond display limit). Excluded
   variants appear only as counts. Missing genotype or reference data renders as
   not evaluated, never as zero.

### Out of scope
X-linked, mitochondrial, and semidominant reasoning; phasing; compound
heterozygosity.

---

## ADR-023 Targeted gnomAD Frequency Cache
Status: accepted | Related REQs: REQ-014, REQ-016, REQ-021

### Decision
1. A versioned local gnomAD snapshot covers only public ClinVar
   pathogenic/likely-pathogenic variants with valid GRCh38 coordinates. The
   target set is never derived from household genotypes or other vault state.
2. It is acquired only during an explicit reference refresh and stored in
   `famed-reference.db` with release provenance. Reports never make live gnomAD
   requests.
3. Joins use exact GRCh38 chromosome/position/reference/alternate after one
   documented minimal-representation step. No rsID joins, liftover, or strand
   guessing.
4. Overall, exome, and genome AC/AN/AF are kept with filter state and gnomAD's own
   exome/genome discordance flag. Missing data or AN = 0 means unavailable, not
   zero.
5. Frequency is display context only. It never changes flag eligibility,
   disposition, tier, wording, or prompt priority.
6. Refresh reconciles exactly to the current target set, is idempotent, keeps the
   last good snapshot on failure, and triggers the report cascade.

---

## ADR-024 PRS Validation Before Scoring
Status: accepted | Related REQs: REQ-011, REQ-016, REQ-021

### Decision
1. Native PRS scoring is out of scope until a model-specific decision pins at
   least one score model per condition and accepts scoring, percentile,
   confidence-interval, coverage, and reporting rules.
2. Any future scoring uses a versioned public scoring-model source with complete
   metadata and variant weights. GWAS trait associations are not a scoring model.
3. Current work is validation only: `fm-prs-validation` checks an explicit local
   pinned model package against imported DNA for metadata, build compatibility,
   allele identity, effect-allele orientation, missing variants, and aggregate
   coverage. Findings are descriptive; no invented pass/fail thresholds.
4. Output is aggregate only: no genotype rows, rsID lists, weights, source paths,
   ancestry inference, or network access.
5. Model acquisition is an explicit user action limited to pinned public score
   identifiers and never transmits private data.

---

## ADR-025 Derived-Report Dependency Graph
Status: accepted | Related REQs: REQ-006, REQ-011, REQ-012, REQ-014, REQ-016, REQ-023

### Decision
1. Producer/consumer relationships among derived outputs are declared in one
   place (`dependent_reports.py`). One refresh orchestrator regenerates in
   dependency order, per person or per household. A report that reads another's
   output must register that edge.
2. Every successful ingestion event, correction, and reference-cache
   `download`/`refresh` regenerates the affected reports before the command
   returns. A failure to refresh fails the calling workflow.
3. Reports that read a relative's records refresh when that relative's inputs
   change (ADR-010, ADR-028).
4. Findings are superseded on recomputed state: finding identity includes the
   computed interpretation, so a changed result supersedes the old finding.
5. A deterministic consistency gate fails when two current reports assert
   contradictory phenotype or finding states for the same person, medication,
   and gene.
6. Household refresh is idempotent: byte-stable output except timestamps, no
   duplicate findings.

The one exception to eager refresh is agent-authored synthesis (ADR-026).

---

## ADR-026 Live-Agent Synthesis in Advice Reports
Status: accepted | Related REQs: REQ-014, REQ-016

### Decision
1. **Three stages.** Deterministic code assembles a source-cited evidence bundle
   (genetic flags, labs, family history, screenings, medications) → the live
   agent running the skill synthesizes cross-domain insights → a deterministic
   validation gate checks each insight before persistence.
2. Synthesis runs only in a live agent session (capability level C4). No
   unattended model call produces it.
3. **Insight contract:** `{statement, linked_evidence, domains_linked,
   evidence_tier, confidence}`. Every `linked_evidence` entry must resolve to an
   existing vault note or section. At least two reviewed `domains_linked` values
   are required (vocabulary in `docs/references/evidence-model.md`).
4. **Gate.** An insight is rejected if a citation does not resolve or the
   statement contains dosing, diagnosis, or treatment-plan language. Rejected
   insights are dropped, not weakened.
5. **Tier inheritance.** An insight's tier must not exceed the weakest tier among
   its citations. Recombining already-tiered vault evidence does not require new
   literature verification.
6. **Staleness.** Every deterministic refresh, attended or not, carries the
   synthesis section over marked `synthesis_status: stale` with its
   `generated_at`; only a new live run replaces it. Synthesis older than
   30 days, or contradicted by newer canonical facts, renders under a prominent
   dated warning.
7. Synthesis lives in the health advisory only. The doctor-visit packet stays a
   facts-only document (ADR-031) and links the advisory.

### Consequences
- Advice reports gain an additive synthesis section; deterministic sections are
  unchanged.
- Between live runs a reader sees current deterministic content and a visibly
  stale synthesis section.

---

## ADR-027 Findings Ledger
Status: accepted | Related REQs: REQ-014

### Decision
- Derived findings live in an append-only, gitignored ledger
  (`ledger/findings.jsonl`). Findings are recorded, superseded, or marked stale
  by appending events; prior entries are never mutated.
- Producers that recompute a domain reconcile their set: a changed state
  supersedes, an identity no longer produced is marked stale.
- `fm-findings-ledger` is the maintenance surface. Detailed behavior lives in
  REQ-014 and `docs/references/evidence-model.md`.

---

## ADR-028 Family-History Context
Status: accepted | Related REQs: REQ-016

### Decision
- Risk-oriented family-history context reads only ascending and lateral blood
  relatives: parents, grandparents, siblings, half-siblings. Spouses and
  children are excluded. `_HERITABLE_RELATIONSHIP_TYPES` in `health_advisory.py`
  is the single declaration of this scope.
- Relative facts are sanitized before rendering:
  - placeholders and negative history ("No active problems documented") are not
    conditions;
  - acute, procedure, injury, symptom, screening, and encounter facts are
    excluded;
  - non-cancer sex-anatomic issues are excluded; cancer terms are kept;
  - remaining facts must match an explicit chronic/heritable allowlist;
  - equivalent labels for one relative collapse to one canonical label;
  - facts flagged in the relative's diagnosis-support review are omitted.
- The report records the relative notes it read (`family_history_sources`).
- FHIR `FamilyMemberHistory` is not restricted: it represents the structural
  family graph for provider handoff.

---

## ADR-029 History Digest Uses a Conservative Allowlist
Status: accepted | Related REQs: REQ-010, REQ-016

### Decision
- The Digest at the top of `medical-history-summary.md` lists chronic
  conditions only when a diagnosis/finding fact matches an explicit chronic
  allowlist in code. Procedures, follow-ups, screenings, injuries, and acute
  diagnoses stay date-stamped in the report body.
- The Digest also lists cancers and cerebrovascular events (stroke, TIA,
  cerebral infarction) as background a reader must see first. Matching skips
  "family history of" facts and clauses that negate, rule out, or screen for the
  condition; the label names the cancer site when the source does.
- Chronic-repeat suppression in the body uses only the chronic allowlist.
- When a condition is not clearly chronic, it stays an episodic fact.
- This is a rendering rule; accepted source facts are unchanged. A schema-level
  `course` field remains a possible later enhancement.

---

## ADR-030 Flagged-for-Discussion Report
Status: accepted | Related REQs: REQ-008, REQ-016, REQ-025

### Decision
`Records/<person_id>/Reports/flagged-for-discussion.md` has these sections:

- **Out of Range:** latest reading outside the reference bounds that are present
  (single-sided ranges are classified by their one bound). Excluded when the
  latest reading is more than 180 days before the person's most recent lab draw;
  that anchor ignores point-of-care (`POCT…`) tests. Splits into **Newly Out of
  Range** (previous reading in range) and **Other Out of Range** when any newly
  out-of-range result exists.
- **Not Re-checked** (part of Out of Range): results whose latest reading is out
  of range and falls 180 days to 2 years before the most recent draw. They are
  listed after current results with their date, because an abnormal result that
  was never re-measured must not disappear. Older results are excluded.
- **Re-checked under another name:** a test counts as re-measured when a
  test with the same LOINC component (ignoring only fasting-state suffixes
  such as `^post CFst`), property, and system was drawn more than one day
  after it, e.g. "Glucose" re-checked as "Glucose, Fasting". Challenge and
  serial-specimen suffixes (a 2-hour tolerance test) stay distinct: a
  baseline result does not re-check them. A re-checked result leaves Out of
  Range and Not Re-checked; the later test speaks for itself. A one-day gap is
  the same draw restated, not a re-check. Tests without a LOINC code never
  match.
- **Trending:** latest value still in range, moving toward a nearby bound,
  within the 2 most recent distinct panel dates. "Nearby" is 10% of the range
  width, or 10% of a single bound's magnitude. Tests without a bound are
  excluded. No separate percentage-change gate. A test never appears in both Out
  of Range and Trending.
- **Recently Normalized:** latest in range, previous out of range, within the
  180-day window, not already in Trending.
- **Coronary Calcium:** accepted history facts with objective coronary calcium /
  Agatston findings, as source-cited bullets.

Each flagged row shows the previous value and a compact SVG of the previous and
latest readings, dated by exactly those two readings, with each reading's own
reference range.

The health advisory and this report share `build_lab_trends_from_db` and one
flagged-row filter; neither parses the other's Markdown.

Thresholds (180 days, 2 years, 10%, two panel dates) are implementation
constants, not user settings. The advisory marks Not Re-checked results as
"not re-checked since" and turns them into re-check discussion prompts.

---

## ADR-031 Caregiver Report Set
Status: accepted | Related REQs: REQ-011, REQ-012, REQ-013, REQ-014, REQ-016, REQ-025

### Decision
1. **Composite reports.** Each report has a stated audience. Composite reports
   link sub-reports instead of inlining their bodies; there are no link-only hub
   reports.
2. **Doctor-visit packet** (`fm-prepare-doctor-visit`) is the one doctor-facing
   artifact: a curated standalone summary of about two printed pages with patient
   identity and age, allergies, problem list, current medications with renewals
   due, abnormal or changing labs ranked by distance from range, coronary calcium
   findings, latest vitals, PGx gene results, immunizations, blood-relative family
   history, and — only when needed — a factual **Record Status** section linking
   pending review items and disagreeing sources. It carries no auto-generated
   questions; those are the caregiver's to write. Each section links its detail
   report; whole sub-report bodies are never inlined.
3. **One Genetics report** (`genetics.md`, `fm-genetics`) holds the DNA source, a
   gene-centric PGx table that doubles as the printable wallet card (every CPIC
   gene the chip can call, not only current medications), carrier screening only
   when a carrier is found, and genetic-flag disposition. Imputation readiness
   and PRS validation are on-demand skill output.
4. **Inputs-only generation.** A report exists only when its inputs exist; the
   Reports index groups reports by audience and names absent ones with the
   reason. Review surfaces that establish absence (pending review, conflict
   reports) always exist.
5. **One shared ranking.** The advisory, flagged report, and packet order lab
   results by one distance-from-range measure.

### Consequences
- The packet deliberately duplicates selected facts for print; this is the only
  exception to the no-inlining rule.
- Packets are timestamped snapshots; older packets keep their contents.

---

## ADR-032 Privacy of Tracked Files
Status: accepted | Related REQs: REQ-021

### Decision
- Tracked files never name real household members or carry their identifying or
  medical details. Use generic phrasing ("a household member") or the
  `<person_id>` placeholder.
- Example and fixture people use a `dummy_` prefix for identifiers
  (`dummy_alice`) and a `Dummy ` prefix for display names (`Dummy Alice`), so the
  privacy check never depends on what names happen to be in a local vault.
- Fixtures use synthetic values and dates unless a test needs an exact analyte
  name for coding behavior.
- `python scripts/check_no_real_names.py` (also `tests/test_doc_privacy.py`)
  scans tracked files against the local vault's person IDs and display names. A
  hit blocks the commit.

---

## ADR-033 Scrubbed Web Search for Correctness Audits
Status: accepted | Related REQs: REQ-003, REQ-014, REQ-021

### Decision
1. A correctness-verification skill run by a live agent (`fm-correctness-audit`)
   may issue web search/fetch queries only when the query contains no
   person-identifying value: no name, `<person_id>`, raw value, date, or other
   record detail. Queries are generic coding or clinical fact lookups ("LOINC code
   free PSA").
2. The skill is proposal-only. Discrepancies are recorded as reviewable findings
   in `data/correctness-audit-findings.jsonl`; nothing is auto-applied.
3. Each finding cites the challenged local record and the external source.
4. The skill refuses a query it cannot reduce to a generic form.
5. Candidate facts may come from local lab, medication, and DNA/ClinVar
   annotation rows; emitted audit facts and queries never contain person IDs,
   genotypes, source paths, dates, or household-derived counts.

### Alternatives considered
- Richer verbatim queries with an audit log: larger privacy surface on a
  local-first system.

---

## ADR-034 LOINC Inference and Provenance
Status: accepted | Related REQs: REQ-003, REQ-008

### Decision
1. **Origin.** `lab_results.loinc_code_origin` is `source_provided` when the code
   came with the data or from user authority, `inferred` when famed filled it,
   and empty when there is no code. It is classified in the universal lab-row
   funnel, so every import, reprocess, and note reload agrees.
2. **Canonical selection.** When several concepts share a normalized name, the
   index chooses by: reviewed override map → `STATUS=ACTIVE` → serum/plasma
   `SYSTEM` → quantitative `SCALE` → insertion order. Ranking by method is not
   used.
3. **Unit-aware inference.** The parsed cache keeps `PROPERTY`. Mass (`MCnc`) and
   molar (`SCnc`) concepts are also indexed under property-qualified keys. The
   result's unit maps to mass or molar through a closed unit set, and inference
   prefers the matching concept. It never falls back to a concept whose property
   contradicts the unit; such a row stays uncoded. Other unit kinds are
   unaffected.
4. Reviewed name/unit overrides outrank the index, including the unit-qualified
   keys, for audit-confirmed method ties and name-key misses.
5. **No bulk re-inference.** The general lookup is never re-run over persisted
   rows. Existing codes change only through source reprocessing or the reviewed
   lanes in ADR-035.

### Consequences
- Inferred codes are queryable for review.
- Point-of-care results with an unstated specimen receive the serum mass code; it
  is a property fix, not a specimen claim.

### Alternatives considered
- Re-running the general index over coded rows: not canonical-stable and crosses
  source authority.

---

## ADR-035 Reviewed Lab Correction Lanes
Status: accepted | Related REQs: REQ-003, REQ-005, REQ-006, REQ-008, REQ-016, REQ-025

### Context
Some source data is structurally incomplete or uses a different spelling, and
some famed-inferred codes are wrong. Every fix must be narrow, reviewed, and
must never weaken source authority.

### Decision
1. **Unit fill.** A closed `CANONICAL_UNIT_BY_LOINC` map supplies a unit only
   when the source omits it and the LOINC code fixes the unit. It never overrides
   a source unit. Codes with known count-vs-percent ambiguity are excluded.
2. **Non-result rows.** Uncoded rows that describe lot numbers, expiration dates,
   internal controls, or performing locations are source metadata and are
   dropped before validation. A coded observation is never dropped by name.
3. **Unit equivalence.** Analyte-independent notation aliases (`10*3/uL` = `K/uL`)
   live in `CANONICAL_UNIT_ALIASES`. Equivalences that depend on the analyte live
   in a closed `REVIEWED_UNIT_EQUIVALENCES` table keyed by test name and source
   unit. `mEq/L` = `mmol/L` only for monovalent ions (sodium, potassium,
   chloride); divalent analytes keep their unit and are rejected for review. No
   numeric unit conversion is ever performed.
4. **Inferred-code repair.** A closed table keyed by exact normalized test name,
   canonical unit, and current code may correct rows whose
   `loinc_code_origin` is `inferred`. Each DB change is mirrored to the matching
   vault cell, audited as `lab_inferred_loinc_repaired`, idempotent, and followed
   by the lab-dependent report cascade.
5. **Source-provided override.** A source-provided code is changed only by an
   individually reviewed, row-ID-scoped entry `(row_id, expected_old_code,
   new_code)` after unambiguous external evidence. It re-verifies the row first,
   is idempotent, and is audited as `lab_source_provided_loinc_overridden`. It
   never becomes a pattern match.

Adding an entry to any lane is a reviewed act with a stated reason and a
regression test.

---

## ADR-036 Textual Lab Results
Status: accepted | Related REQs: REQ-001, REQ-003, REQ-006, REQ-008, REQ-011, REQ-016

### Decision
1. `lab_results.value_text` preserves the source-facing result text; `value`
   stays the numeric projection; `unit` is measurement metadata only and never
   holds result text.
2. All persistence, authority overlays, corrections, deduplication, conflicts,
   reports, rebuild comparison, and FHIR carry `value_text`. FHIR uses
   `valueQuantity` for numbers, `valueString` for text, and `dataAbsentReason`
   only when both are absent.
3. Accepted text shapes in C-CDA results:
   - digit-free results ("Negative", "No growth");
   - one bounded comparator scalar (`<0.1`), with no numeric `value`;
   - a bounded culture form with a `CFU/mL` unit;
   - one ascending nonnegative interval with a structured unit;
   - one ordinal (`2+`, or a label followed by one parenthesized ordinal).
4. These forms never become exact trend points and never infer a unit, code,
   analyte, or clinical meaning. Matching is anchored to the whole value.
5. Anything else that fails validation — compound readings, descending
   intervals, narrative suffixes, culture prose with thresholds or timing — is
   rejected as an audited `lab_observation_rejected` review item, never silently
   dropped. An exact Human Resolver decision may accept such an observation
   verbatim (ADR-039).

---

## ADR-037 Dual-Reference-Range Series
Status: accepted | Related REQs: REQ-008, REQ-016, REQ-025

### Decision
1. Reviewed aliases of one analyte that differ only by reference-range policy
   (for example `TSH` and `TSH Narrow Ref Range`) converge into one series.
   Persisted `test_name`, `ref_low`, and `ref_high` are not rewritten.
2. Each reading is evaluated against its own bounds, and every surface that shows
   a reading shows that reading's range.
3. The trend function does not compare two consecutive readings whose reference
   ranges differ; a policy switch is a discontinuity, not a trend.
4. Distinct measurements stay separate (free PSA vs percent free PSA; percent vs
   absolute-count CBC forms; RDW-CV vs RDW-SD).

---

## ADR-038 Source Notes Are Evidence Projections
Status: accepted | Related REQs: REQ-001, REQ-003, REQ-004, REQ-006, REQ-008, REQ-009, REQ-016, REQ-024

### Decision
1. A generated Lab or medication source note is a deterministic projection owned
   by exactly one retained `source_path`. When one source holds several events,
   note identity may include the source-local date/time, so one note never spans
   several observation dates. Medication notes carry
   `projection_role: source_evidence`.
2. Ingestion and replay never merge rows from different sources into one note.
   Reprocessing a source regenerates only its own note and keeps user-authored
   sections.
3. Cross-source deduplication and authority stay in canonical persistence. The
   Lab index, `lab-results.md`, and `current-medications.md` are the canonical
   current views. They link the evidence index rather than repeating its note
   list.
4. A source note may repeat an observation shown in another source note; it is
   labeled as source evidence, not as current truth.
5. A note whose source no longer backs any canonical row stays navigable as
   historical evidence. A Lab note may be labeled `superseded_by` only when every
   fact it holds is covered by exactly one other current note; partial coverage
   stays unlabeled for review. Labels are reversible and audited; notes are never
   deleted for this reason.
6. **Empty medication replay.** Negative assertions such as "No known
   medications" are not medication facts. When exact-source replay yields no
   medication facts, generated notes with that exact `source_path` are retired
   (`medication_source_note_retired`) and navigation is refreshed. Nonblank
   user-authored `## Notes` content blocks retirement and fails the replay for
   review before any mutation.
7. Dated `user_update` and `user_correction` notes are authoritative records,
   not evidence projections.

### Alternatives considered
- Cross-source "event" notes: synthetic identity that changes when a bridging
  source arrives.
- Rewriting source notes to mirror the DB: loses what the document actually said.

---

## ADR-039 Attended Replay Decisions
Status: accepted | Related REQs: REQ-001, REQ-002, REQ-005, REQ-006, REQ-008, REQ-009, REQ-010

### Context
Some retained sources need Clinical Interpreter or Human Resolver judgment.
Reprocessing must reproduce that judgment without re-asking, and must never
silently drop facts that were accepted earlier.

### Decision
1. **Decision bundles.** `--retained-decisions` accepts a versioned, source-bound
   bundle (`famed.retained-semantic-decisions`; `--history-decisions` remains an
   alias). An entry binds to the canonical retained path, the file checksum, and
   the person, and holds at most one extraction per `(source_path, category)`:
   - `history` for retained History documents;
   - `labs` for a bounded domain-study PDF retained under `Labs/`;
   - `medications` for a retained Medications list (replacing, not merging with,
     the bounded supplement parser) or for a dated `visitSummary.pdf` under
     `History/`;
   - a Human Resolver `resolution` (`accept_verbatim`, `reject`, `quarantine`)
     for one exact rejected, source-coded C-CDA `ST` Lab observation, bound to
     the archive member, checksum, source code, test name, full `value_text`,
     unit, timestamp, bounds, and expected occurrence count.
2. Each extraction is validated (checksum, person, evidence, confidence,
   category schema) and then runs through its normal Canonicalizer and Data
   Steward inside the per-source savepoint. Bundles are proposal input, never
   authority. Verbatim acceptance rebuilds the fact from the retained XML; the
   bundle never supplies medical fields.
3. A document or "current as of" date is not a medication start date unless the
   source states the start.
4. **Durable Lab decisions.** Each `lab_observation_resolution_recorded` audit
   event carries the structured decision (`famed.lab-observation-decision.v1`).
   Replay reuses valid recorded decisions; an explicit decision overrides the
   same selector; stale, malformed, overlapping, or incomplete decisions fail
   before mutation.
5. **History replay safety.** Before mutating retained History, replay checks the
   matching vault notes. A source with facts accepted by the Clinical Interpreter
   requires a matching decision bundle; deterministic C-CDA facts reproduce
   without one. A failed preflight changes nothing.

### Consequences
- Reprocessing can fail closed instead of downgrading accepted history.
- Workflows that reprocess semantically accepted sources must keep their bundles.

---

## ADR-040 Isolated Rebuild as Diagnostic Oracle
Status: accepted | Related REQs: REQ-006, REQ-008, REQ-009, REQ-011

### Decision
1. `fm-rebuild-vault` replays the retained corpus into explicit, non-overlapping
   isolated storage: identity graph first, then ordinary sources, then authority
   overlays (dated updates, then active corrections), then DNA when in scope.
2. The rebuild is a differential diagnostic, not a replacement workflow. It
   compares working and rebuilt stores read-only and classifies every difference
   as a candidate bug, a contract-explained difference, or a review item. It
   never mutates working storage.
3. Validation follows the active issue's scope. A non-DNA audit stops after the
   authority overlays, compares only its named tables and vault paths, lists DNA
   tables and DNA-dependent reports as excluded, and does not claim a complete or
   replacement-ready result.
4. Rebuilds never copy working state into the target to manufacture parity.

---

## ADR-041 Medication Authorization Window and C-CDA Dates
Status: accepted | Related REQs: REQ-003, REQ-006, REQ-009

### Decision
1. `medications.authorized_through` records when a prescription stops being
   dispensable. It never ends a medication.
2. `end_date` comes only from a genuine therapy stop: an `EVN`-mood
   administration period, an explicit discontinuation, or a user statement. A
   C-CDA `INT`-mood `effectiveTime/high` populates `authorized_through`.
3. User-authored end dates (`user_update`, `manual`, `user_correction`) are
   always real therapy ends and outrank sources that still list the drug.
4. Source `statusCode` decides currency. A lapsed authorization renders as a
   renewal action, not a discontinuation. FHIR exports it as an
   `authorized-through` extension.
5. When contradictory active rows must be ranked, a row without a start date uses
   the currency of its source document's list (judged by the start dates of the
   drugs on it), never the archive or import date.
6. **UTC timestamps.** A `+0000` HL7 timestamp is re-expressed in the offsets the
   document itself declares before truncating to a date, and only when every
   declared offset yields the same date; otherwise the UTC date is kept. No
   configured or machine timezone is used. Medication extraction applies this
   rule; other extractors adopt it when a concrete defect warrants.

---

## ADR-042 Medication Contradictions
Status: accepted | Related REQs: REQ-004, REQ-005, REQ-006, REQ-009, REQ-023, REQ-024

### Decision
**Detection** (`medication-conflicts.md`):
1. A contradiction is surfaced even when ranking already picks the rendered
   value; the report is about disagreement between sources.
2. Dose and frequency are compared per axis. A row silent on an axis (NULL,
   empty, `unspecified`) cannot disagree on it.
3. A lapsed `end_date` removes a row from detection; a lapsed
   `authorized_through` does not.
4. Missing `start_date` is never a proxy for recency. Temporal suppression uses
   only ordered, dated rows, per axis, so real titrations stay suppressed.
5. PRN qualifiers survive frequency normalization.
6. Hand-entered (user-authored) text with non-standard frequency is fixed at its
   source; provider documents are never rewritten.

**Same-note statements:** a user-authored stop suppresses a matching active row
from another note regardless of dose. Within one note, suppression is skipped
only when the doses differ (a titration); same drug, same dose, opposing status
in one note is a contradiction and the user statement wins.

**Resolution:**
7. Both source rows are persisted with provenance. During an attended
   `fm-ingest-inbox` run each new contradiction is presented with axes, values,
   and sources; the user picks or supplies values, or defers. A confirmed choice
   becomes a `user_correction` through `fm-correct-fact`. All pending axes of one
   medication must be confirmed before the correction applies.
8. Unattended runs leave the contradiction recorded (`medication_conflict_detected`,
   `awaiting_user_decision`) and report-visible. Resolved contradictions leave the
   pending set; re-imports do not re-ask.

### Rationale
A false all-clear is worse than no report, and a withheld medication is
invisible. Persist-and-flag keeps both values visible with their sources.

### Alternatives considered
- Withholding the conflicting fact: requires fact-scoped quarantine, which does
  not exist; document quarantine would hide every other fact in the source.
- Asking at report time: no user or source document in view.

---

## ADR-043 Pending-Review Reports
Status: accepted | Related REQs: REQ-005, REQ-006, REQ-010, REQ-016, REQ-018

### Decision
1. Each person gets `Records/<person_id>/Reports/pending-review.md`
   (`famed.pending-review-item.v1`), linked from Reports navigation, with the
   current count and actionable summaries.
2. `fm-review-pending` is the single user workflow. It refreshes every person's
   report and lists unassigned inbox/quarantine files in its output only; no
   cross-person note is persisted.
3. Current state comes from category-owned evidence, never from audit status
   alone:
   - medication contradictions from canonical rows;
   - inbox and quarantine files while they exist on disk;
   - History notes at `needs_review`, distinguishing notes with no accepted facts
     from notes whose accepted facts are still under review;
   - Lab rejections derived from retained archive members, exact recorded
     decisions, and canonical coverage. Equivalent repeated statements form one
     item; an accepted observation missing from canonical storage stays
     actionable; unreadable evidence becomes a coverage item.
4. Each item names the person (or "unresolved"), source, subject, reason, and
   resolving skill. Dedicated reports are linked, not copied.
5. Paths that create or resolve review items refresh the affected report.
   An empty report states when absence was established.
6. Unsupported source families are disclosed as coverage gaps, not guessed.
