# Reference — Model Capability Matrix

This reference calibrates minimum AI capability levels for business logic skills. It is guidance for routing and evaluation, not a hard-coded provider allowlist. Provider examples are illustrative and must not be embedded in skill bodies.

## Capability Levels

| Level | Minimum capability | Appropriate use | Example provider/model class |
|---|---|---|---|
| C0 | No model required | Deterministic parsing, schema validation, relocation, provenance, canonicalization, audit logging | Python code, SQLite, deterministic fixtures |
| C1 | Small structured extraction model | Simple field extraction from clean text, fixture generation | Lightweight hosted models, fast coding-agent extraction modes |
| C2 | Strong structured medical extraction model | Free-form text/PDF interpretation with evidence snippets, confidence, person/category proposals, candidate facts | Claude/Gemini/Codex-class agents with schema enforcement |
| C3 | Advanced medical reasoning model | Cross-document conflict explanation, ambiguous clinical narrative decomposition, multi-category extraction requiring careful uncertainty handling | Higher-capability Claude/Gemini/OpenAI-class agents |
| C4 | Expert review or multi-agent reasoning | Evidence-backed insights, E1/E2 verification support, advisory synthesis, pharmacogenomic interpretation support | Strong reasoning agents plus deterministic evidence checks |

## Skill Requirements

Business logic skills must state the minimum capability level they require and link to this file. They should not maintain their own provider matrix.

Project development skills do not require model capability levels unless they directly affect production ingestion semantics, schemas, validation, canonicalization, prompts, or runtime medical AI behavior.

## Current Minimums

| Business behavior | Minimum level before live invocation | Notes |
|---|---|---|
| Differential cold rebuild audit | C0 | Deterministic isolated identity, medical, and authority replay with issue-scoped read-only comparison for non-DNA audits; DNA replay, target-only household regeneration, and complete multiplicity-aware semantic/user-authority/provenance comparison only when the active issue or declared audit task includes DNA; paired rollback, vault/foreign-key validation, explicit scope exclusions, and working-store immutability proof. |
| Imputation-readiness report generation | C0 | Deterministic aggregate reporting over local genotype source-kind, genome-build, and imputation-quality metadata; no upload, export, PRS scoring, or genotype-row disclosure. |
| PRS model/input validation report generation | C0 | Deterministic validation of an explicit local pinned model package against imported DNA rows; aggregate model/input coverage only, with no upload, score, ancestry inference, disease-risk estimate, genotype-row disclosure, rsID list, weight list, source path, or network request. |
| Free-form text/PDF candidate fact extraction | C2 | First priority for manual verification and skill tuning. No direct persistence. |
| Whole-inbox free-form batch processing | C2 | C3 when cross-document ambiguity, unknown-person resolution, or conflicting evidence requires careful decomposition. Proposal-only; no direct persistence. |
| Multi-category mixed-document decomposition | C2 | C3 if categories conflict or evidence is ambiguous. |
| Conflict explanation for human resolver | C3 | Explanation only; deterministic rules and human decision remain authority. |
| Correctness-audit web-search verification | C3 | LOINC/RxNorm/unit/range and DNA/ClinVar annotation checks use scrubbed-generic queries only, with no person-identifying value (ADR-033). Proposal-only; findings never auto-applied. |
| Evidence-backed insight generation | C4 | Must pass evidence-tier validation before surfacing. |

## Evaluation Rule

A business logic skill still needs fixtures, schema validation, quarantine behavior, and a manual verification path appropriate to the requirement it implements. Satisfying the capability level is necessary, not sufficient.

## Live AI Agent Sessions

Every skill run is a live AI agent session. When a skill is executed by an agent (Claude, Gemini, Codex, etc.), the agent itself is the Clinical Interpreter and fact extractor:

- The capability gate (C2/C3) is satisfied by the agent's own reasoning.
- The agent reads source files directly using its native tools.
- There is no model-provider layer to invoke (ADR-013).

The levels above therefore describe what the *executing agent* must be capable of. They are not a provider allowlist, and no configuration switches a skill between them.
