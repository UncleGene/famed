---
name: data-contract-designer
description: Define structured contracts between agents/components, especially interpreter-to-canonicalizer boundaries.
metadata:
  reasoning_class: medium
---

# When to use
- Use when an interface, schema, event, JSON shape, validation contract, or role boundary must be defined or changed.
- Use for `famed` Clinical Interpreter ↔ Canonicalizer contracts and any cross-component data contract that must enforce provenance, confidence, idempotency, or quarantine outcomes.
- Do not use for ad-hoc helper signatures or in-process types with no cross-role consumer.

# Inputs
- Producer and consumer (named components or roles)
- Existing contract version, if any
- Active requirements, decisions, and architecture snapshot governing the boundary

# Procedure
1. State producer, consumer, and the requirement/decision that justifies the contract.
2. Specify input and output schema by *meaning and guarantees*, not only field types — required, optional, confidence, provenance.
3. Enumerate validation rules and error/quarantine outcomes for each invalid case.
4. Define a versioning strategy that does not break existing accepted records.
5. Provide examples covering: valid, low-confidence, invalid, conflict.
6. List rejected caller assumptions and non-goals.
7. List open questions that block implementation.
8. Apply the host-provided phase-quality review to new or changed contracts before any implementation.

# Skeptic pass
- Do not invent fields the producer cannot supply or the consumer cannot validate.
- Treat any field labelled "future" or "for X" as out of scope until a current requirement names it.
- Stop if the contract weakens an authority-hierarchy or AI-never-writes-canonical invariant.
- If existing accepted records would silently fail the new contract, the versioning strategy is incomplete — flag and stop.

# Output
```md
## Contract: <producer> → <consumer>
- Version:
- Required fields:
- Optional fields:
- Confidence fields:
- Provenance fields:
- Validation rules:
- Error / quarantine outcomes:
- Versioning strategy:
- Examples (valid / low-confidence / invalid / conflict):
- Non-goals & rejected assumptions:
- Open questions (blocking):
- Linked REQ / ADR:
```

# Hand-off
- Tests and gates → host-provided validation planning
- Ingestion contracts → `ingestion-architect` to verify role-split integrity
- New or changed schema → host-provided phase-quality review before implementation
