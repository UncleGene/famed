---
name: fm-genetics
description: Generate or refresh one person's Genetics report (genetics.md) from imported DNA, current medications, and the local CPIC/ClinVar reference cache — DNA source, a gene-centric PGx wallet card, and carrier screening when a carrier is found.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-011
    - REQ-012
    - REQ-013
    - REQ-014
    - REQ-016
  related_decisions:
    - ADR-020
    - ADR-021
    - ADR-025
    - ADR-031
---

# When to use
- Use when the user wants to regenerate or inspect a person's Genetics report: what their DNA says about medication genes, carrier status, and where it came from.
- DNA and medication ingestion refresh this report automatically; direct use is for manual regeneration or after a reference-cache refresh.
- Do not use for dosing recommendations, disease-risk scoring, or interaction severity scoring.

# Inputs
- `person_id` for the report target.
- Local repo with `famed-vault/`, `data/famed.db`, and optionally `data/reference/famed-reference.db`.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`.
2. Invoke the internal helper from the repo root:

   ```bash
   python scripts/generate_genetics.py --person-id <person_id>
   ```

3. Inspect `Records/<person_id>/Reports/genetics.md`. The helper also records the
   deterministic, source-cited PGx findings in `ledger/findings.jsonl`; repeat runs
   do not append duplicate active findings.
4. Report:
   - Report path, or that no DNA is imported
   - Interpreted medication genes and any affecting current medications
   - Genes measured only partially and not interpreted
   - Whether a carrier was found

# Skeptic pass
- Stop if the person note or user DB is missing.
- Do not infer a result from a coverage match; interpretive output requires an exact source-backed CPIC translation.
- Treat results as consumer genotyping, not clinical-grade testing.
- Do not surface bulk genotype rows.

# Output
```md
## Genetics
- Person:
- Report path:
- Interpreted genes:
- Genes not interpreted:
- Carrier found:
```

# Hand-off
- No DNA imported -> `fm-ingest-inbox` with a DNA file.
- Missing CPIC/ClinVar cache -> `fm-refresh-reference-cache`, then re-run this skill.
- User wants aggregate imputation readiness -> `fm-imputation-readiness` (prints on demand).
- User wants a doctor-facing summary -> `fm-prepare-doctor-visit`.
