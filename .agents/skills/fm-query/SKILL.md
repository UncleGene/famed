---
name: fm-query
description: Answer a plain-language question about one person's health data using current vault notes as context and source citations for every claim.
metadata:
  reasoning_class: high
  business_logic: true
  minimum_capability: C3
  capability_reference: docs/references/model-capability-matrix.md
  related_requirements:
    - REQ-015
    - REQ-019
  related_decisions:
    - ADR-013
    - ADR-014
    - ADR-015
---

# When to use
- Use when the user asks a plain-language question about a specific person's health data, such as "What are dummy_alice's current medications?", "Summarize dummy_bob's recent labs", "Does dummy_alice have any lab conflicts?", or "What history notes exist for dummy_carol?"
- Use when the user wants a cited, vault-grounded answer rather than a raw note lookup.
- Do not use when the user wants to ingest or correct data; route to `fm-ingest-inbox` or `fm-correct-fact`.
- Do not use when the user asks about a report that needs regenerating; run the relevant ingestion or sync workflow first.

# Inputs
- `person_id` - required. The vault person identifier, for example `dummy_alice`. If unknown, ask the user or enumerate available IDs with `list_person_ids`.
- `question` - required. The plain-language question to answer.
- Local repo with `famed-vault/` available.

# Procedure
1. Verify the person exists in `famed-vault/Persons/<person_id>.md`. If not found, list available person IDs and ask the user to clarify.
2. Read `docs/references/model-capability-matrix.md` and apply its live-agent-session rule: the agent executing this skill performs the C3 reasoning itself and does not invoke a separately configured model.
3. Assemble current vault context with native local file tools. Read reports first, then the relevant Labs, Medications, and History notes. Use `famed.query.collect_person_vault_notes` only as an optional deterministic enumerator; the live agent reads and reasons over the returned source content itself. Notes with `verification_status: needs_review` are included but labeled `[UNVERIFIED]`. Exclude stale artifacts from evidence; mention them only as a coverage gap that may require regeneration.
4. Check for missing expected reports: `lab-results.md`, `current-medications.md`, and `medical-history-summary.md`. If any are absent, preserve that gap and tell the user before presenting the answer.
5. Answer only from the assembled vault context. Every factual claim must cite a source note filename in brackets, for example `[Reports/lab-results.md]`, and include confidence metadata or verification status appropriate to its supporting notes.

# Skeptic pass
- Do not answer from memory or training data. Use only the vault context assembled in Procedure step 3.
- The agent executing this skill answers from the vault itself. There is no separate model provider to invoke.
- Re-open every cited note and verify that it supports the claim before answering; surface unsupported claims instead of treating them as valid.
- Notes labeled `[UNVERIFIED]` have not been reviewed. If the answer relies on them, include that caveat.
- Missing expected reports mean the answer may be incomplete. Say so explicitly.

# Output
Return the answer text with source citations. Example:

```md
Dummy Alice's current medications include Zetia 10 mg (active) and Vitamin D
2000 IU (supplement, active) [Reports/current-medications.md]. A medication
conflict was flagged for Atorvastatin because two sources report different
doses [Reports/medication-conflicts.md].
```

If the vault notes are insufficient to answer, state that explicitly and list which reports or note categories are missing.

# Hand-off
- Missing or stale reports -> run `fm-ingest-inbox` or the relevant sync workflow, then re-invoke `fm-query`.
- Correction needed on a persisted fact -> `fm-correct-fact`.
- User wants a structured visit packet -> `fm-prepare-doctor-visit`.
