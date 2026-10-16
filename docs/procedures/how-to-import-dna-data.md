# How to Import DNA Data

## Goal

Import one person's consumer DNA file and refresh the dependent DNA-derived reports.

## Supported inputs

- 23andMe
- AncestryDNA
- MyHeritage
- Genotek
- Nebula Genomics
- Generic `VCF` / `VCF.GZ`

## Recommended path

1. Put the file in:

   ```text
   data/inbox/<person_id>/
   ```

   Legacy `data/inbox/<person_id>/dna/` still works, but the single per-person drop folder is preferred.

2. Ask your AI CLI:

   ```text
   Use fm-ingest-inbox to process my inbox.
   ```

3. Review any questions about person match or unsupported content before persistence.

## What refreshes automatically

- `Records/<person_id>/Reports/genetics.md` (DNA source, gene-centric PGx wallet card, carrier status when a carrier is found; ADR-031)
- Imputation readiness is printed on demand by `fm-imputation-readiness`; it is not a vault note

## Notes

- VCF imports preserve build and source-kind provenance when available.
- Low-quality imputed rows are filtered before persistence.
- Use `fm-imputation-readiness` to inspect whether imported DNA is direct-only,
  imputed-but-missing metadata, or ready for a later PRS input-validation slice.
  The readiness report is aggregate-only and does not upload or export DNA.
- Use `fm-prs-validation` only after explicitly acquiring a local pinned public
  PRS model package. It writes aggregate model/input compatibility only and does
  not score DNA, list variants, infer ancestry, or estimate disease risk.
- Bulk genotype browsing is not a caregiver-facing output in `famed`.

## Related procedures

- `docs/procedures/how-to-generate-pgx-wallet-card.md`
- `docs/procedures/how-to-export-fhir-r4-bundle.md`
