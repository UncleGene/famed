---
name: ingestion-architect
description: Design and maintain the medical ingestion role split and pipeline behavior.
metadata:
  reasoning_class: medium
---

# When to use
- Use for ingestion, interpretation, canonicalization, deduplication, quarantine, resolver, provenance, source-file lifecycle, or confidence propagation.
- Use when adding or changing a category handler in the unified inbox orchestrator.
- Do not use for storage-layer changes that don't alter pipeline behavior; route to the host-provided system-snapshot workflow.

# Inputs
- Source type, expected category, and the concrete user/operator value
- Active requirements, decisions, architecture snapshot
- Existing role split: Clinical Interpreter, Canonicalizer, Deduplication Engine, Human Resolver, Data Steward

# Procedure
1. Name the concrete ingestion pain, source type, and safety risk before touching abstractions.
2. Confirm the role split:
   - Clinical Interpreter — AI-assisted, proposes only, never writes canonical storage.
   - Canonicalizer — deterministic schema/rules/terminology validation.
   - Deduplication Engine — deterministic pre-filter; reversible links only.
   - Human Resolver — handles ambiguous person match, conflicts, low confidence.
   - Data Steward — applies authority hierarchy, idempotent writes, audit trail.
3. Cover happy path and every failure mode: invalid schema, low confidence, ambiguous identity, conflict, unsupported source, retry/idempotency.
4. Reject generic pipeline layers or multi-provider abstractions not justified by an active requirement or decision.
5. If schemas, role boundaries, or persistence flow change, invoke `data-contract-designer` and the host-provided phase-quality review before implementation.
6. Trace the proposed change to specific REQ and ADR IDs.

# Skeptic pass
- Reject any path where AI output writes canonical storage directly — even via "trusted" wrappers.
- Reject collapsing role responsibilities the architecture keeps distinct (Interpreter ≠ Canonicalizer ≠ Steward).
- Stop if the design lacks a concrete plan for low confidence or ambiguous identity.
- Stop if the change is justified only by future flexibility, not a current requirement.

# Output
```md
## Ingestion Design
- Source type / category:
- User/operator value:
- Role responsibilities (Interpreter / Canonicalizer / Dedup / Resolver / Steward):
- Failure modes (invalid / low-conf / ambiguous / conflict / unsupported / retry):
- Quarantine & escalation rules:
- Acceptance tests:
- Linked REQ / ADR:
- Contracts to define:
```

# Hand-off
- Schema or role-boundary change → `data-contract-designer` → host-provided phase-quality review
- Implementation slice → host-provided implementation planning and validation planning
