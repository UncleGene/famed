---
name: fm-prs-validation
description: Generate a validation-only PRS model/input compatibility report from an explicit local pinned model package and imported DNA rows.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-011
    - REQ-016
    - REQ-021
  related_decisions:
    - ADR-011
    - ADR-015
    - ADR-020
    - ADR-024
    - ADR-025
---

# When to use
- Use when the user wants to validate whether one person's imported DNA rows are structurally compatible with an explicit pinned PRS scoring model before any scoring decision.
- Use after imputation-readiness indicates PRS input validation can proceed and a local public model package has been explicitly acquired by the user.
- Do not use to acquire a model, upload DNA, export genotype data, calculate a PRS score, infer ancestry, estimate disease probability, or make clinical recommendations.

# Inputs
- `person_id` for the report target.
- Path to a local pinned PRS model JSON package.
- Local repo with `famed-vault/` and `data/famed.db`.
- Imported genotype rows for the person, if any.

# Procedure
1. Verify the active person exists in `famed-vault/Persons/<person_id>.md` and the local PRS model package exists.
2. Confirm the model package was acquired by explicit user action and is public/non-household-derived.
3. Invoke the internal helper from the repo root:

   ```bash
   python scripts/generate_prs_validation.py --person-id <person_id> --model-path <model.json>
   ```

4. Inspect `Records/<person_id>/Reports/prs-validation.md`.
5. Report:
   - Report path
   - Validation state
   - Model score ID, source, release, trait, and genome build
   - Aggregate model/input coverage
   - Aggregate coordinate or allele-identity conflicts

# Skeptic pass
- Stop if the person note, user DB, or local model file is missing.
- Stop if the model package lacks pinned score ID, source, release, genome build, trait, development/validation population, published metrics, license, variant weights, or effect/other alleles.
- Stop if the report would be written outside `Records/<person_id>/Reports/`.
- Do not surface rsIDs, genotype values, variant weights, source file paths, dates, person names, or bulk DNA data.
- Treat missing variants and coverage as descriptive unless the pinned source model supplies its own thresholds.
- Do not infer risk, disease probability, ancestry, clinical action, percentile, confidence interval, calibration, or treatment relevance from validation metadata.

# Output
```md
## PRS Validation
- Person:
- Report path:
- Validation state:
- Model:
- Coverage:
- Metadata or compatibility gaps:
```

# Hand-off
- Missing DNA profile or genotype rows -> `fm-ingest-inbox` with a DNA file.
- Direct/genotyped-only DNA -> `fm-imputation-readiness` before PRS validation.
- Missing or invalid model package -> acquire a pinned public PRS model package by explicit user action before retrying.
- Validation complete -> a later source-specific ADR is still required before any PRS score rendering.
