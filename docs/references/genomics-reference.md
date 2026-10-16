# Reference — Genomics, PGx, Carrier Screening, and PRS

## DNA Inputs

Supported providers (auto-detected; single-file ZIP wrappers are accepted and
the ZIP stays the retained source):

| Provider | Format | Detection |
| :--- | :--- | :--- |
| 23andMe v2–v5 | TSV | `23andMe` in header. |
| AncestryDNA | TSV | `allele1` / `allele2` columns. |
| MyHeritage | CSV | `RSID,CHROMOSOME,POSITION,RESULT`. |
| Genotek | TSV | `Genotek` in header. |
| Nebula Genomics | VCF | `source=Nebula` in VCF header. |
| Generic VCF / VCF.GZ | VCF | `##fileformat=VCF`. |

## Annotation Database Sources

Annotation sources are built locally into untracked
`data/reference/famed-reference.db`:

| Source | Content | Approximate scale |
| :--- | :--- | :--- |
| ClinVar | Pathogenicity classifications | ~4M variants |
| CPIC | Pharmacogenes, drug guidelines, allele definitions, and exact diplotype-to-gene-result translations | REST API / 34 pharmacogenes |
| GenCC (ClinGen subset) | Versioned gene-disease validity and AD/AR inheritance assertions for exact ClinVar identifier matching | ~3.6K ClinGen assertions |
| gnomAD | Global exome/genome allele frequency for the public ClinVar pathogenic/likely-pathogenic target set | Targeted snapshot |

The standard profile (CPIC + ClinVar + the ClinGen-origin GenCC subset +
targeted gnomAD) is approximately 1.2 GB.

### GenCC inheritance contract

`genetic-inheritance-v1` retains stable SGC id/version, HGNC, mapped and original
disease CURIEs, GenCC validity, HPO inheritance mode, submitter, dates, and source
URLs from the official new-format GenCC TSV. Only ClinGen submissions are cached.
Health-advisory refinement requires exact HGNC plus disease-CURIE overlap and
consistent Strong/Definitive autosomal-dominant or autosomal-recessive evidence.
No condition-name matching is permitted. Weak, conflicting, disputed, refuted,
complex, missing, or unmatched evidence preserves the unresolved ADR-022
fallback. GenCC provenance refines caregiver-facing wording only; it does not
create canonical medical facts or diagnoses (ADR-022).

### gnomAD frequency contract

The targeted `gnomad_frequencies` snapshot is selected only from public ClinVar
reference rows with pathogenic/likely-pathogenic text and valid GRCh38
coordinates and alleles. It never uses household genotypes, person identifiers,
dates, or current flags as remote selectors. Refresh normalizes each target to
one exact minimal GRCh38 chromosome/position/reference/alternate identity,
validates release provenance from an official gnomAD release page, fetches
targets in bounded GraphQL batches, reconciles the table atomically, and
preserves the last good snapshot while durably recording a failed acquisition
or validation attempt.

Health Advisory reads the local table only. It displays global joint, exome,
and genome allele count/number/frequency when available; source-filtered,
missing, zero-denominator, and release-flagged discordant results remain
explicitly unavailable or discordant. Frequency does not change flag
eligibility, disposition, evidence tier, inheritance wording, or prompt
priority. rsID joins, liftover, strand guessing, fuzzy repair, ancestry-specific
interpretation, and live report-time gnomAD requests are excluded (ADR-023).

## PGx Coverage

- CYP panel: CYP2D6, CYP2C19, CYP2C9, CYP3A4/5, CYP2B6, CYP1A2.
- Drug transporters: SLCO1B1, ABCG2, ABCB1, OCT1/2.
- Full panel: 34 CPIC pharmacogenes.
- Secondary: DPWG guidelines.
- Drug-gene alerts: PharmGKB evidence level.

Current implemented report (ADR-031): medication ingestion, DNA ingestion, and
successful reference-cache refreshes update `Records/<person_id>/Reports/genetics.md`
automatically; `fm-genetics` is available for explicit regeneration or inspection.
The coverage states below are computed internally and drive the Genetics report's
medication column and the PGx findings ledger.

- Medication coverage state: `covered by CPIC reference`, `no CPIC match`, or `CPIC reference missing`.
- Genotype readiness state: `genotype data present`, `no local gene-linked genotype data`, or `not assessed`.
- Interpretation state: conservative CPIC star allele calling and source-backed
  per-gene metabolizer or Function results when allele definitions, local
  genotype data, and an exact CPIC diplotype translation are present;
  `phenotype not interpreted` or `not interpretable` otherwise.
- When available, the deterministic called diplotype appears only in the optional
  technical appendix for inspection; the main PGx coverage table stays
  phenotype-first.
- The report does not render CPIC dosing recommendation text or recommend dosing changes.

### CPIC Gene-Result Translation

The local cache stores CPIC's official
`https://api.cpicpgx.org/v1/diplotype` view (ADR-021). It supplies exact
diplotypes and `generesult` values rather than requiring local combination
rules.

- Metabolism genes use CPIC metabolizer-result labels.
- SLCO1B1 uses Function labels including Normal, Decreased, Poor, Increased,
  Possible Decreased, and Indeterminate.
- ABCG2 currently has Normal, Decreased, and Poor Function translations.
- ABCB1 currently has no CPIC diplotype translation and remains coverage-only.
- Multi-gene medication presentation must retain each gene's result; no shared
  severity ordering exists across metabolizer and Function vocabularies.

Current implemented wallet card: the gene-centric table in
`Records/<person_id>/Reports/genetics.md` lists every CPIC gene the chip can call,
with the current medications each affects, so it serves future prescriptions too.

- It reuses the same medication, CPIC, and genotype-readiness states as PGx coverage.
- It shows conservative metabolizer or transporter-Function results when available.
- It is a compact wallet/prescriber-readiness summary, not a clinical-grade PGx result.
- It does not surface raw diplotypes in the main wallet summary.
- It does not score interaction severity, render CPIC recommendation text, or recommend dosing changes.

## Carrier Screening

ACMG-recommended 5-gene panel:

- CFTR — cystic fibrosis.
- SMN1 — spinal muscular atrophy.
- HEXA — Tay-Sachs.
- HBB — sickle cell / beta-thalassemia.
- GJB2 — hereditary hearing loss.

Output states:

- `carrier`
- `clear`
- `not callable from this chip`

`not callable` must explain what additional test would be required and must never be treated as negative.

## Imputation

- User-initiated via Michigan/TOPMed Imputation Server.
- Provider deletes data after seven days.
- Needed for PRS because polygenic scores often require millions of variants while consumer chips provide a smaller subset.
- `fm-imputation-readiness` prints the local/offline readiness report on demand (ADR-031); it is not a vault note. It reports only aggregate source-kind, genome-build, and imputation-quality metadata and never uploads, exports, scores, or lists genotype rows.

## Polygenic Risk Scores

Minimum targets:

- Coronary artery disease.
- Type 2 diabetes.
- Hypertension.

Must report:

- Population-adjusted percentile.
- 95% confidence interval.
- Ancestry cohort limitations.
- Variance explained.
- Confidence of individual prediction.

Source/model contract:

- Native scoring is out of scope until a model-specific decision (ADR-024);
  only source/model readiness and input validation exist today.
- Use only pinned, versioned public scoring models with complete variant weights
  and metadata; GWAS trait-association rows alone are not score models.
- First implementation validates model/input compatibility before rendering any
  person-facing score.
- Required validation: genome build, allele identity, effect-allele orientation,
  missing-variant handling, and aggregate reportable model/input coverage.
  Missing-variant and coverage findings are descriptive unless the pinned source
  model supplies thresholds.
- `Records/<person_id>/Reports/prs-validation.md` is the validation-only surface
  produced by `fm-prs-validation` for an explicit local pinned model package. It
  reports aggregate metadata and compatibility counts only, never rsIDs,
  genotype rows, weights, source paths, ancestry inference, disease-risk
  estimates, or scoring output.
- No local ancestry inference, transferability correction, recalibration, or
  disease-probability estimate in the first implementation.

## Per-Variant Confidence

Two independent axes:

1. Call quality — how reliably the chip measured the variant.
2. Genotype → phenotype predictability — how reliably genotype predicts phenotype.

A variant can have strong evidence but weak phenotype predictability.

## Panel Coverage Reporting

Distinguish:

- `measured`
- `not on chip`
- `no-call`

`not on chip` is never a negative result.
