---
name: fm-generate-health-advisory
description: Generate a non-authoritative health advisory report for one person from existing vault reports, normalized genotype data, and local ClinVar/GenCC reference evidence.
metadata:
  reasoning_class: low
  business_logic: true
  minimum_capability: C4
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-013
    - REQ-014
    - REQ-016
    - REQ-017
    - REQ-021
  related_decisions:
    - ADR-011
    - ADR-014
    - ADR-015
    - ADR-018
    - ADR-020
    - ADR-022
    - ADR-023
    - ADR-026
---

# When to use
- Use when the user wants a non-authoritative health advisory for one person that combines ClinVar genetic risk flags, age-based preventive screening reminders, lab-trend observations, and doctor discussion prompts.
- Use after ingestion and report refresh have produced current DNA, lab, history, medication, carrier-screening, and PGx reports where available.
- Do not use to ingest source files, create canonical facts, diagnose conditions, score disease probability, recommend treatment, recommend dosing, or replace clinician review.

# Inputs
- `person_id` for the report target.
- Local repo with `famed-vault/` available and writable.
- `data/famed.db` with normalized genotype rows for the person.
- Local ClinVar and GenCC reference tables at `data/reference/famed-reference.db`;
  a missing ClinVar cache renders an explicit not-evaluated state, while a
  missing or non-matching GenCC table preserves unresolved inheritance wording.
  The optional local gnomAD table adds exact-match global population-frequency
  context; missing or filtered frequency remains visibly unavailable, while
  source-flagged discordance remains visibly discordant. Neither state changes
  flag policy.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`.
2. Invoke the internal helper from the repo root:

   ```bash
   python scripts/generate_health_advisory.py --person-id <person_id>
   ```

3. When this skill is being run in a live interactive session (not unattended
   automation), read the deterministic `health-advisory.md` just written plus
   its listed Source Reports, and look for genuine cross-domain patterns
   connecting at least two of: genetic risk flags, lab observations, family
   history, preventive screening, medications. Author one candidate insight
   object per pattern worth surfacing: `{statement, linked_evidence,
   domains_linked, evidence_tier, confidence, weakest_cited_tier}` (the
   contract in `synthesis_contract.py`, decided in ADR-026). An empty list is
   a valid, expected outcome when no genuine pattern exists this generation —
   do not invent one to have something to say.
   - `linked_evidence`: vault-relative paths to reports actually consulted.
   - `domains_linked`: name at least 2 of the Health Domains in
     `evidence-model.md` — a single-domain statement is not cross-domain
     synthesis. Use `Oncology & Genitourinary` only when the cited evidence
     substantively concerns oncology or genitourinary context; a generic
     screening reminder or isolated history mention is insufficient. Pairing
     it with `Systemic Interactions` requires a genuine relationship across
     evidence types, such as a longitudinal lab pattern plus distinct history
     or family-history context.
   - `evidence_tier` / `weakest_cited_tier`: if every citation is a raw fact
     (lab value, medication, family-history note, screening reminder) with no
     epistemic tier, set `weakest_cited_tier` to `null` and cap `evidence_tier`
     at `E3`. If a citation is a tier-bearing genetic risk flag, read its tier
     directly from the rendered Genetic Risk Flags table's Evidence column and
     use that as `weakest_cited_tier` — never claim a stronger `evidence_tier`
     than the weakest tier actually cited.
   - Write conservative, citation-grounded, conversation-starter language. Do
     not author dosing, diagnosis, or treatment-plan language — the
     validation gate in the next step rejects it, but it should not be
     written in the first place.
4. Persist the candidates (skip this step entirely during unattended/
   automated regeneration — synthesis is live-agent-only, ADR-026). Step 2
   always carries any prior "## Synthesized Insights" section over with
   `synthesis_status: stale` rather than dropping it; persisting here replaces
   it with a current section. A person with no prior live-agent run simply has
   no synthesis section:

   ```bash
   python scripts/persist_synthesized_insights.py --person-id <person_id> --insights-json '<JSON array of candidates>'
   ```

   Report any rejections printed by this step — a rejection means the
   statement, citation, or tier needs revising, not that the step failed.
5. Inspect `Records/<person_id>/Reports/health-advisory.md`.
6. Report:
   - Report path
   - Genetic risk flag count
   - Genetic evidence disposition counts or the not-evaluated reason
   - Inheritance-refined flag count and GenCC SGC identifiers
   - gnomAD population-frequency context status
   - Missing source reports
   - Whether age-based screening reminders were rendered
   - Whether out-of-range lab observations were rendered
   - Synthesized insights accepted / rejected (omit if step 4 was skipped)
7. Keep boundaries sharp. This skill writes a derived advisory report only. It uses existing vault reports, normalized genotype rows, and the local reference cache; it does not write canonical medical truth.

# Skeptic pass
- Stop if the person note does not exist.
- Stop if the helper exits non-zero.
- Do not treat generated advisory content as medical advice. It is caregiver-prepared context for clinician discussion and must include the clinical safety warning.
- Genetic risk flags are population-level observations from ClinVar pathogenic/likely-pathogenic rows present in the imported genotype, not diagnoses or treatment guidance.
- Disposition counts explain deterministic filtering only. Excluded candidates
  are not findings or diagnoses: do not expose their identifiers, cite them as
  insights, or create doctor prompts from them. Missing genotype/reference data
  must remain not evaluated rather than being described as zero or negative.
- Treat inheritance as resolved only when the deterministic report shows an exact
  ClinVar HGNC+disease identifier match to consistent Strong/Definitive ClinGen
  autosomal-dominant or autosomal-recessive GenCC assertions (ADR-022). Never
  infer inheritance from condition-name similarity. Weak, conflicting, disputed,
  refuted, complex, missing, or unmatched evidence must remain unresolved.
- `carrier-compatible` and `risk-associated` are conversation-starter labels,
  not carrier confirmation, personal-risk quantification, or diagnosis.
- Treat gnomAD frequency as display-only global context. It must come from the
  versioned local cache through exact normalized GRCh38 allele identity.
  Missing/zero-AN/source-filtered data is unavailable; source-flagged
  exome/genome discordance is discordant. Neither state changes eligibility,
  disposition, evidence tier, inheritance wording, risk language, or prompt
  priority (ADR-023).
- Missing reference cache, missing genotype data, missing source reports, or absent birth year must remain visible as absence/limited-context output instead of being silently hidden.
- Do not expose bulk genotype data.
- Do not run step 4 during unattended/automated regeneration — synthesis only happens in a live agent session (ADR-026); no local model or API call ever authors an insight.
- An insight's `evidence_tier` must never exceed its `weakest_cited_tier` (or `E3` when citing only raw facts) — read tiers from the rendered report, do not guess or round up.
- Do not use a Health Domain label to paper over conflicting source evidence;
  omit the candidate and route the conflict through the existing review path.

# Output
```md
## Health Advisory
- Person:
- Report path:
- Genetic risk flags:
- Genetic evidence disposition:
- Inheritance-refined flags:
- Population-frequency context:
- Missing source reports:
- Screening reminders:
- Lab observations:
- Synthesized insights:
```

# Hand-off
- Missing or stale source reports -> regenerate the relevant upstream report or rerun the source workflow before sharing the advisory.
- User wants medication-safety review prompts -> `fm-generate-medication-safety-alerts`.
- User wants a doctor-facing visit summary -> `fm-prepare-doctor-visit`.
- User wants FHIR export -> `fm-export-fhir`.
- User wants diagnosis, treatment, dosing, or clinical-grade genetic interpretation -> clinician review outside `famed`.
