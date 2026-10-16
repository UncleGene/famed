# How to Generate the Genetics Report (PGx Wallet Card)

## Goal

Create or refresh one person's Genetics report, whose gene-centric table is the PGx wallet card to show a prescriber or pharmacist (ADR-031).

## Recommended path

If medication and DNA data are already imported, ask your AI CLI:

```text
Use fm-genetics for dummy_alice.
```

## Output

The card is written to:

```text
Records/<person_id>/Reports/genetics.md
```

Related readiness findings may also be recorded in:

```text
ledger/findings.jsonl
```

## What the card includes

- Active medication count
- CPIC coverage/readiness status
- Conservative star allele calling and metabolizer or Function results when available
- Whether local genotype support exists
- Evidence/source caveats

## What it does not do

- Dosing recommendations
- Conservative medication-safety review alerts; use `fm-generate-medication-safety-alerts`

## Related procedures

- `docs/procedures/how-to-import-dna-data.md`
