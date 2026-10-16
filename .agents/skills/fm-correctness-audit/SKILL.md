---
name: fm-correctness-audit
description: Proactively cross-check already-persisted coding/normalization choices (LOINC, RxNorm, units, reference ranges, drug-name variants, DNA/ClinVar annotations) against authoritative public sources via scrubbed-generic live web search, surfacing discrepancies as reviewable findings. Never persists a correction itself.
metadata:
  reasoning_class: high
  business_logic: true
  minimum_capability: C3
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-003
    - REQ-014
    - REQ-021
  related_decisions:
    - ADR-018
    - ADR-033
  status: active
---

# When to use
- Use when the user asks for a correctness/quality pass over already-persisted
  coding or normalization choices — e.g. "double-check our LOINC codes",
  "audit the RxNorm mappings", "are any of these reference ranges wrong",
  or "verify the ClinVar annotations behind DNA risk flags".
- Use as a periodic proactive check instead of waiting for someone to notice a
  wrong value in a report.
- Do not use to answer a one-off factual question outside an audit context;
  route a plain question to `fm-query` instead.
- Do not use to generate new medical/clinical claims or advice; this skill
  only checks whether an existing code/unit/range *label* matches what an
  authoritative public source says that label should be. For DNA, it checks
  local ClinVar annotation fields behind existing candidate flags, not whether
  a person's genotype changes their health risk. It never reasons about a
  person's health.
- Implementation:
  `src/famed/correctness_audit.py` +
  `scripts/correctness_audit.py` provide the findings ledger (record/resolve/
  dismiss/list-open) and `labs`/`medications` fact enumeration against
  `data/famed.db`. The `dna` enumeration path lists grouped genotype rsIDs
  joined to pathogenic-text local ClinVar annotation from
  `data/reference/famed-reference.db`
  without person IDs, genotype values, source paths, dates, or household-derived
  counts. The `history` enumeration path named in Procedure step 1 is not yet
  built.

# Inputs
- Scope: which category to audit (`labs`, `medications`, `dna`, `history`, or
  `all`). Defaults to `all` if unspecified.
- Optional `person_id` to scope the audit to one person; omitted audits every
  person's current records.
- Local repo with `famed-vault/` and the SQLite DB available.
- Live web search/fetch tool access (host-provided; e.g. `WebSearch`/
  `WebFetch` in Claude Code).

# Procedure
1. Enumerate the persisted coding/normalization facts in scope. For `labs`
   and `medications`, run `scripts/correctness_audit.py list-facts --category
   labs --db-path data/famed.db` (or `medications`) to get every distinct
   `(display name, code, reading/row count)` tuple currently persisted. For
   `dna`, run `scripts/correctness_audit.py list-facts --category dna --db-path
   data/famed.db --ref-db-path data/reference/famed-reference.db` to get every
   distinct local genotype rsID that has pathogenic-text local ClinVar
   annotation, with its `variation_id`, gene, significance, condition, review
   status. Local selection is allowed to start from local records, but the
   issued live query must remain generic. Do not
   expand a DNA audit query with person IDs, allele calls, raw genotype values,
   source paths, dates, or whether a household member carries the alternate
   allele. For `history` (ICD-10-coded diagnosis labels), no enumeration helper
   exists yet — read directly from the vault notes and/or DB. Never read from
   retained source files (those are
   immutable originals, not the audit target).
2. For each fact, construct a **scrubbed-generic** query: a generic
   clinical/coding lookup containing only the standard's own terms (analyte
   name, drug generic name, code) and never a person-identifying value —
   no name, no `<person_id>`, no raw observed value, no date. Example: `LOINC
   code Free PSA` is permitted; `dummy_alice Free PSA 3.9` is not. DNA examples:
   `ClinVar VCV000000123 rs123 GENE1 pathogenicity` is permitted;
   `dummy_alice rs123 A/T` is not.
3. **Stop condition (ADR-033):** if a fact cannot be reduced to a
   generic query without an identifying value, skip it and record it as
   `unauditable` rather than searching. This should be rare — coding/unit/range
   facts are inherently generic — but composite or ambiguous labels may need
   this fallback.
4. Issue the query via the host's live web search/fetch tool. Compare the
   retrieved authoritative value (code, unit convention, or standard reference
   range) against the persisted local value.
5. Where they disagree, record a discrepancy finding (see Output) citing both
   the local record (vault path/DB row + field) and the retrieved external
   source (URL/publisher/retrieval date). Where they agree, no finding is
   recorded — this is not a coverage report, only a discrepancy report.
   Findings persist across sessions in the append-only ledger
   `data/correctness-audit-findings.jsonl` (`famed.correctness_audit`;
   invoke via `scripts/correctness_audit.py record ...`), mirroring the
   `data/inbox-run-feedback.jsonl` pattern, so a re-run does not re-surface
   an already-open, already-resolved, or already-dismissed finding. Use
   `scripts/correctness_audit.py list-open` to see current open findings and
   `resolve`/`dismiss` to close one with a reason.
6. Never modify the local record, DB row, or retained source. Findings are
   proposal-only; a human (or a later `fm-correct-fact`/reviewed-repair-lane
   invocation, per ADR-018/ADR-035 precedent) resolves them.
7. Summarize: facts audited, facts skipped as unauditable, discrepancies
   found, and where each finding was recorded.

# Skeptic pass
- Refuse to issue any query that still contains a name, `<person_id>`, raw
  observed value, or date after attempting to genericize it. Record it as
  `unauditable` instead (Procedure step 3) rather than searching anyway.
- Do not treat a retrieved web result as automatically correct. Prefer
  primary/authoritative sources (e.g. loinc.org for LOINC, RxNav/NLM for
  RxNorm) over secondary aggregators; if the result is ambiguous or the
  source is not authoritative, record the finding as low-confidence rather
  than asserting a discrepancy.
- Do not auto-apply any correction, regenerate any report, or write to the
  DB/vault. That boundary belongs to deterministic validation and human
  resolution (ADR-018), never to this skill.
- Do not expand scope into clinical interpretation (e.g. "is this reference
  range clinically appropriate for this person") — that is a different,
  out-of-scope question from "does this coding label match the standard."
- Stop and flag rather than guess if the local record's own provenance is
  ambiguous (e.g. unclear whether a code is source-provided or inferred) —
  the reviewed-repair precedent (ADR-035) only allows correcting inferred
  codes, never source-provided ones.

# Output
```md
## Correctness Audit
- Scope:
- Facts audited:
- Facts skipped (unauditable):
- Discrepancies found:

### Discrepancy findings
- Local record: <vault path or DB row + field>
- Local value: <code/unit/range>
- Retrieved value: <code/unit/range>
- Source: <URL/publisher, retrieval date>
- Confidence: <high|medium|low>
- Status: open (unresolved; awaiting human review)
```

# Hand-off
- Discrepancy confirmed and the local value is `inferred` provenance ->
  reviewed-repair lane precedent (ADR-035) via the relevant canonicalization
  path.
- Discrepancy confirmed and the local value is `source-provided` -> preserve
  for human review per source-first authority; do not override (REQ-003,
  ADR-018).
- Need to record/list/mark-stale a derived medical finding (not a coding
  discrepancy) -> `fm-findings-ledger`.
- Need a one-off answer instead of an audit -> `fm-query`.
