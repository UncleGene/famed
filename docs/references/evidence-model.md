# Reference — Evidence, Confidence, and Findings

## Evidence Tiers

| Tier | Criteria |
| :--- | :--- |
| E1 — Clinical-grade | CPIC/DPWG guideline, ClinVar pathogenic/likely-pathogenic, multiple large studies. |
| E2 — Well-replicated | Multiple GWAS, meta-analysis, replicated strong association. |
| E3 — Supported | 2–5 studies, plausible mechanism, or uncorroborated wearable/device data cap. |
| E4 — Suggestive | Single well-powered study. |
| E5 — Speculative | Preliminary, animal, N=1, weak, or exploratory. |

## Verification Rules

- E1–E2 claims must be verified via evidence-check style pipeline using PubMed and Semantic Scholar before surfacing.
- E3–E5 claims are flagged as unverified/lower confidence.
- Every AI-generated insight must show evidence tier.
- Every insight must cite sources.

## Genetic Risk Flags

ClinVar pathogenic/likely-pathogenic variants present in a person's genotype are
classified at E1 (clinical-grade reference) but **capped at E2 in this
implementation** because the source is consumer genotyping, not clinical-grade
sequencing. Surfaced as **population-level risk observations**, not individual
diagnoses. Rows with conflicting ClinVar classifications are excluded from
flags and counted only in the disposition summary. Rows that pass the report
policy with lower review status are at E3/E4 with prominent `[UNVERIFIED]`
labels. Every genetic risk flag carries: ClinVar
Variation ID (accession-equivalent), review status, associated condition, and
**zygosity** (heterozygous/homozygous) from the local genotype (ADR-022). When
ClinVar's HGNC+disease identifiers exactly match consistent Strong/Definitive
ClinGen assertions in the local GenCC cache, the flag may additionally render a
source-cited autosomal-recessive carrier-compatible or autosomal-dominant
risk-associated observation (ADR-022). Missing identifiers, weak evidence,
conflicting/refuted/disputed assertions, and complex inheritance retain the
carrier-or-risk-pending fallback. These are never diagnoses and inheritance does
not raise the consumer-DNA E2 cap.

An accepted flag may also display **global population frequency** from the
versioned local gnomAD snapshot (ADR-023). The match is exact on normalized
GRCh38 chromosome, position, reference allele, and alternate allele. The report
shows source-release provenance and treats missing data, zero allele number,
failed source filters, structural mismatch, or a gnomAD release-provided
exome/genome discordance flag as unavailable or discordant rather than zero.
The application does not invent a discordance threshold. Frequency is context
only: it does not change eligibility, disposition, evidence tier, inheritance
wording, risk language, or prompt priority. Ancestry-specific and popmax
interpretation remain outside the accepted contract.

The health advisory separately reports a deterministic **genetic evidence
disposition**. A candidate is a ClinVar pathogenic-text row whose alternate
allele is present in the imported genotype. Each candidate is counted once as
accepted, conflicting, insufficiently reviewed, excluded by established report
policy, duplicate-condition suppressed, or eligible beyond the display limit.
Excluded candidates are counts only: they are not findings, diagnoses, doctor
prompts, or evidence-tiered insights, and their identifiers are not rendered.
Missing genotype or ClinVar data is `not evaluated`, never a negative result.

## Confidence Model

Composite insight confidence derives from:

- Data quality.
- Evidence tier.
- Source provenance completeness.
- Ingestion verification status.

For raw/canonical facts, confidence includes:

- extraction confidence,
- person match confidence,
- normalization confidence,
- duplicate/merge confidence,
- review status.

## Findings Ledger

- Append-only `ledger/findings.jsonl`.
- Gitignored.
- Current implemented maintenance surface: `fm-findings-ledger` invokes `scripts/findings_ledger.py`.
- Current automatic producer: the Genetics report writer (`fm-genetics`, also run
  by medication/DNA ingestion) records deterministic PGx coverage/readiness
  findings for CPIC-covered rows.
- Events are JSONL records: `finding_recorded`, `finding_superseded`, and `finding_marked_stale`.
- Findings can be revised or superseded without losing history.
- Re-running ledger recording appends new findings rather than mutating prior ones.
  Repeat PGx generation does not append a duplicate while the same deterministic
  finding is already active.
- Corrections/deletions mark impacted findings stale until recalculated.
- `E1`/`E2` findings require a verified evidence check before recording.
- Current finding views exclude stale and superseded findings.
- Not implemented: AI-generated evidence validation of new literature claims,
  dosing recommendations, interaction severity scoring, and disease probability
  scoring.

## Health Domains

- Heart & Circulation.
- Energy & Vitamins.
- Digestion & Diet.
- Sleep & Brain.
- Medication Safety.
- Oncology & Genitourinary. Use for source-cited oncology or genitourinary
  evidence only; a generic screening reminder or isolated history mention does
  not establish a cross-domain pattern by itself.
- Systemic Interactions.
