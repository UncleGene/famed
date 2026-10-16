---
name: fm-refresh-reference-cache
description: Refresh, inspect, or rebuild famed's local medical reference datasets through the supported cache workflow, including the public ClinVar-targeted gnomAD snapshot.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C0
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-014
    - REQ-016
    - REQ-017
    - REQ-021
  related_decisions:
    - ADR-011
    - ADR-015
    - ADR-023
    - ADR-025
---

# When to use
- Use when the user asks to inspect, download, refresh, or rebuild one or more
  local reference-cache datasets.
- Use for an explicit gnomAD refresh. The invocation is the required user action
  authorizing external reference requests.
- Do not use to query references while rendering reports or to choose remote
  targets from household genotypes, current flags, person identifiers, dates,
  or other private vault state.

# Inputs
- Optional supported dataset identifier, such as `clinvar`, `gnomad`, `cpic`,
  `gencc`, `rxnorm`, or `loinc`.
- Local repo with `data/reference/` writable.
- For gnomAD, a populated local ClinVar cache containing public GRCh38 variant
  coordinates.

# Procedure
1. Open the active project guide and `docs/DECISIONS.md` ADR-023 before a gnomAD
   refresh. Verify that the requested operation stays within the public
   ClinVar-derived target contract.
2. Inspect current cache state:

   ```bash
   python scripts/cache.py status
   ```

3. Refresh the requested scope:
   - One dataset:

     ```bash
     python scripts/cache.py --dataset <dataset> refresh
     ```

   - All supported datasets:

     ```bash
     python scripts/cache.py refresh
     ```

   - Before refreshing `gnomad`, refresh `clinvar` first when ClinVar is missing
     or when the user wants the public target corpus brought current. Never
     construct or narrow the gnomAD target list from household data.
4. Treat any `UNAVAILABLE` result as a failed dataset refresh. The implementation
   validates the configured release from an official gnomAD release page,
   requests public targets in bounded batches, records the failed attempt, and
   preserves the last valid snapshot on acquisition, identity, schema, or
   validation failure; do not delete or replace it manually.
5. Confirm final status with `python scripts/cache.py status`. Successful cache
   changes run the configured household derived-report cascade automatically.
6. Report each requested dataset's status, version, row count when available,
   and whether derived reports refreshed.

# Skeptic pass
- Stop if a gnomAD target cannot be reproduced from public ClinVar cache rows
  alone.
- Stop if ClinVar has no structurally valid GRCh38 targets; do not fall back to
  rsID-only matching, liftover, strand guessing, or fuzzy allele repair.
- Do not treat missing data or zero allele number as zero frequency.
- Do not interpret source-flagged exome/genome discordance or invent a local
  discordance threshold.
- Do not run a live reference request from report generation.
- Do not clear the cache as a recovery step; preserve last-good data and report
  the failure.

# Output
```md
## Reference Cache Refresh
- Requested datasets:
- Status:
- Versions:
- Rows:
- Derived reports refreshed:
- Failures:
```

# Hand-off
- Missing manual LOINC files -> follow `docs/procedures/backup-and-restore-vault-db.md`.
- Successful gnomAD refresh -> `fm-generate-health-advisory` when the user wants
  an attended advisory regeneration.
- Reference correctness concern -> `fm-correctness-audit`.
