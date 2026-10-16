---
name: fm-imputation-readiness
description: Generate an offline imputation-readiness report from already imported local DNA rows before PRS work.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-011
  related_decisions:
    - ADR-008
    - ADR-011
    - ADR-025
---

# When to use
- Use when the user wants to inspect whether one person's imported DNA data is ready for later PRS input validation.
- Use after importing a direct-to-consumer DNA file or an imputed VCF when deciding whether external imputation is still needed.
- Do not use to upload DNA, export genotype data, run imputation, calculate PRS, or make diagnoses.

# Inputs
- `person_id` for the report target.
- Local repo with `famed-vault/` and `data/famed.db`.
- Imported genotype rows for the person, if any.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`.
2. Invoke the internal helper from the repo root:

   ```bash
   python scripts/generate_imputation_readiness.py --person-id <person_id>
   ```

3. Read the report the helper prints. It is on-demand output and is not written to the vault (ADR-031).
4. Report:
   - Report path
   - Readiness state
   - Direct/genotyped variant count
   - Imputed variant count
   - Any missing genome-build or imputation-quality metadata counts

# Skeptic pass
- Stop if the person note or user DB is missing.
- Stop if the helper writes any vault file; readiness output is printed only (ADR-031).
- Do not surface rsIDs, genotype values, source file paths, or bulk DNA data.
- Treat direct/genotyped-only consumer data as insufficient for PRS readiness; recommend user-initiated external imputation before PRS work.
- Treat imported imputed data with missing build or quality metadata as needing review, not as PRS-ready.
- Do not infer risk, disease probability, ancestry, or clinical action from readiness metadata.

# Output
```md
## Imputation Readiness
- Person:
- Report path:
- Readiness state:
- Direct/genotyped variants:
- Imputed variants:
- Metadata gaps:
```

# Hand-off
- Missing DNA profile or genotype rows -> `fm-ingest-inbox` with a DNA file.
- Direct/genotyped-only DNA -> user-initiated external imputation, then `fm-ingest-inbox` with the imputed VCF.
- Imputed data ready -> later PRS input-validation implementation.
