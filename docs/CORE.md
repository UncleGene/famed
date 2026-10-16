# famed — Core Context

## Vision

famed is a local-first medical intelligence system that transforms fragmented personal and family health data into structured longitudinal health records, evidence-backed insights, and actionable confidence-scored guidance.

**Primary user workflow.** The user drops one or more files into the local inbox and invokes a single CLI skill. That one invocation drives the workflow end-to-end: automatic processing wherever person, category, and content are unambiguous, and explicit prompts only when ambiguity, conflict, or low confidence cannot be resolved deterministically. The user never chains commands to satisfy a single intent (ADR-015). Skills are the documented user-facing interface; Python scripts are implementation detail. Manual Obsidian entry remains a fallback for review, correction, and human-authored notes.

**Obsidian's dual role.** The vault is both the canonical human presentation layer and the navigable context that AI agents read when generating reports, insights, and recommendations. Every successful ingestion event or reference-cache change refreshes all configured reports affected by the change so the vault stays current as the AI's authoritative working surface.

The product is designed for technically proficient individuals and caregivers managing multi-person family health data. It must remain usable as an Obsidian-backed local vault before any browser UI exists.

## Target Users

- Technically proficient individuals managing personal or family health.
- Caregivers managing multi-generational medical data.
- Quantified-self, longevity, and medication-safety focused users.

## Non-Goals

- Not a diagnostic tool.
- Not a physician replacement.
- Not a fully autonomous medical decision-maker.
- Not a cloud-first health record system.
- Not a general-purpose orchestration framework.

The system may provide best-effort guidance, but must use conservative clinical framing, confidence scores, evidence tiers, and source citations.

## Core Principles

1. **Local-first by default.** Personal medical data, DNA data, vault notes, and database files remain local unless the user explicitly initiates an external action.
2. **Source-first authority.** Retained source documents outrank vault notes, and vault notes outrank the derived SQLite index.
3. **Human authority for ambiguity.** When the system cannot confidently resolve person identity, conflicting values, ambiguous source authority, or low-confidence normalization, it must ask the user or quarantine the record.
4. **AI-assisted, not AI-authoritative.** AI may interpret and propose. Deterministic validation and explicit authority rules decide persistence.
5. **Frictionless ingestion.** Where person, category, and content are unambiguous, ingestion runs end-to-end without per-file prompts. Human input is reserved for ambiguity, conflict, or low-confidence proposals.
6. **Vault-as-context.** Report generation, insight derivation, and query answering read from the Obsidian vault and its retained sources. The vault is not just the user-facing presentation — it is the AI's authoritative context. Outputs cite vault notes and source files.
7. **Reports refresh on dependency changes.** Every successful ingestion event or reference-cache change triggers regeneration of all configured per-person reports affected by the change, including reports that read a relative's records (ADR-025). Derived outputs are not allowed to drift behind canonical state between ingestion runs or successful reference-cache updates.
8. **Schema-constrained normalization.** Medical facts must pass structured validation before becoming canonical records.
9. **Reversible deduplication.** Duplicate detection must preserve linkage and provenance; it must not perform opaque destructive merges.
10. **Auditability.** Every accepted, quarantined, corrected, deleted, superseded, or user-resolved fact must be traceable.
11. **Idempotency.** Re-running ingestion on the same or equivalent input must update existing records, not create duplicates.
12. **Minimal redundancy.** Fields are not duplicated across source/vault/DB unless there is an explicit UX or performance reason.
13. **Provider-agnostic AI.** Model choice is configuration. Domain code must not fork by provider.
14. **Documentation remains lean.** Humans edit requirements and sometimes planning priorities. AI updates decisions, system snapshots, plans, inline traceability links, and skill instructions under strict guardrails: confirm implementation plans before code changes, keep affected docs and progress current, and gate code changes with review, simplification, tests, and strict type checks.

## Core Vocabulary

| Term | Meaning |
| :--- | :--- |
| Source document | Original imported file or retained processed file used as evidence. |
| Raw evidence | Specific snippet, table row, page, or file region supporting a fact. |
| Interpreted fact | AI-assisted structured candidate extracted from raw evidence, including field-level confidence. |
| Canonical record | Deterministically validated, normalized record accepted into the vault/DB model. |
| Clinical fact | Atomic medical statement such as a lab observation, medication statement, diagnosis, genetic variant, or wearable observation. |
| Quarantine | State for invalid, low-confidence, unresolved, or non-interactive ambiguous data. |
| Human Resolver | Single human-in-the-loop who resolves ambiguity and approves overrides. |
| Findings ledger | Append-only record of derived insights, revisions, and supersessions. |

## Authority Policy

Imported data follows this hierarchy:

```text
Retained source document > Obsidian vault note > famed.db
```

Rules:

- Source files remain locally available for reprocessing until the user deletes them.
- The Obsidian vault is the canonical human-readable working record.
- `famed.db` is a derived operational index.
- Any disagreement between layers is resolved using the higher-authority layer unless both layers changed independently.
- Ambiguous divergence halts the affected record and requires user resolution or quarantine.
- `--non-interactive` must quarantine unresolved ambiguity rather than auto-picking a winner.
- Data may be duplicated across source, Markdown, and DB only for explicit UX,
  navigation, provenance, or query-performance reasons. Rebuildable DB indexes,
  reports, category hubs, and stale-state markers must be derived again from the
  higher-authority layer rather than treated as independent truth.

## Confidence Policy

Confidence requirements live with the relevant behavior: candidate extraction in REQ-002, human resolution in REQ-005, duplicate handling in REQ-004, and derived insights in REQ-014. The detailed confidence model is in `docs/references/evidence-model.md`.

## Evidence Policy

Evidence-backed insight requirements live in REQ-014. Evidence tiers, verification rules, and findings-ledger behavior are maintained in `docs/references/evidence-model.md`.

## Privacy and Security Policy

Privacy and security requirements live in REQ-021. Local-first handling of personal data remains a core principle here; detailed report/export constraints live in the relevant output requirements and references.

## Documentation Policy

Core docs: `CORE.md`, `REQUIREMENTS.md`, `DECISIONS.md`, `SYSTEM.md`, `PLAN.md`. Dense references live in `docs/references/`, caregiver procedures in `docs/procedures/`, and developer guidance in `docs/DEVELOPMENT.md`. Traceability lives inline as `Links:` fields on requirements and `Related REQs:` fields on decisions. Docs describe the current state; history lives in git. Agent guardrails are defined in `AGENTS.md` and ADR-012.
