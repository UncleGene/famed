# famed Agent Guide

This repo is the `famed` local-first family medical vault. Treat the current documentation set as the project state:

- `docs/CORE.md` — stable vision, principles, policies, terminology.
- `docs/REQUIREMENTS.md` — primary human-facing functional requirements and scenarios.
- `docs/DECISIONS.md` — current architecture and product decisions (ADRs).
- `docs/SYSTEM.md` — current architecture snapshot; not history.
- `docs/PLAN.md` — forward-looking work, guardrails, and open findings.
- `docs/references/` — dense reference information.
- `docs/DEVELOPMENT.md` — developer setup, gates, and repository layout.
- Traceability lives inline as the `Links:` field on each REQ and the `Related REQs:` field on each ADR. There is no separate matrix file.

Context loading:

- Use host-provided process workflows/skills for planning, routing, impact analysis,
  traceability, simplification, review, validation, and commit shaping. Staged doc
  loading is context hygiene only; it does not replace or weaken those workflows.
- For next-task selection, load `docs/CORE.md`, `docs/PLAN.md`, `docs/SYSTEM.md`
  section headings/current sections, and the indexes in `docs/REQUIREMENTS.md` and
  `docs/DECISIONS.md` first.
- Load full REQ/ADR bodies only when the selected host-provided workflow, trace links, or
  touched code/docs make those IDs relevant.
- `docs/sessions/` is a gitignored local folder for cold-start checkpoints. Keep
  only the two newest. A checkpoint is never a durable record: anything that
  must survive belongs in a REQ, an ADR, or `PLAN.md`.
- Docs describe the current state only. When a decision changes, edit or replace
  its ADR; when work completes, remove it from `PLAN.md`. Do not add dated
  narrative, milestone logs, or closure records to tracked files.
- Documentation is optimized for agent token cost. Before adding to a doc, check
  whether the content already exists elsewhere and link instead. A skill's
  `SKILL.md` is the authoritative description of what that skill does; `README.md`
  and `docs/procedures/` point at it rather than restating its mechanics.

Core invariants:

1. Source-first authority: retained source document > Obsidian vault note > SQLite DB.
2. AI-assisted, schema-constrained ingestion: Clinical Interpreter proposes; Canonicalizer validates; Data Steward persists.
3. AI never writes canonical medical truth directly.
4. No destructive merges. Deduplication must be reversible and audited.
5. Low confidence, invalid schema, ambiguous person match, or conflicting values go to quarantine or human resolution.
6. Requirements evolve through `docs/REQUIREMENTS.md`; decisions evolve through ADRs in `docs/DECISIONS.md`, which hold only current decisions.
7. `docs/SYSTEM.md` is the current snapshot only. Do not turn it into history.
8. `docs/PLAN.md` is short-lived and must reflect the next credible implementation steps.
9. Avoid documentation sprawl. Do not create new top-level documentation files unless explicitly requested.
10. Every implementation change should trace to at least one REQ and, when relevant, one ADR.
11. After determining the next change plan, ask the user for confirmation before starting implementation.
12. Use Pyrefly for Python type checking; do not use mypy as the project type-checking gate.
13. Every code change must keep affected documentation, `docs/PLAN.md`, progress/status notes, and the current vault state up to date before the work is considered complete. When a feature changes generated Obsidian-facing content, update both the generator and existing vault notes/templates that still show stale user-facing status.
14. Every code change must include host-provided code simplification and code review passes, even when the pass concludes that no further edits are needed.
15. Every code change is gated on relevant tests and strict type checks; Python changes must pass Pyrefly.

## Code-Change Completion Gates

Every code change must clear all five gates before the work is considered complete.

1. Update affected docs, plan entries, progress/status notes, and README when new user-visible functionality is delivered.
2. Apply a code simplification pass (may conclude "already appropriately simple").
3. Apply a code review pass.
4. Run relevant tests.
5. Run strict type checks using the language-specific gate from the active project guide.

When a change touches Obsidian-facing artifacts or workflows, the relevant
nested Obsidian skill is part of both development and the gates:

- Use `obsidian-markdown` for vault Markdown notes, templates, wikilinks,
  embeds, callouts, properties/front matter, and Obsidian-specific Markdown.
- Use `obsidian-bases` for `.base` files, table/card/list/map views, filters,
  formulas, or Bases validation.
- Use `json-canvas` for `.canvas` files or visual canvas workflows.
- Use `obsidian-cli` for live-vault operations, plugin/theme development,
  vault screenshots, DOM/error inspection, or verifying behavior in a running
  Obsidian instance.
- Use `defuddle` when a development or review task requires extracting clean
  Markdown from a web page before turning it into Obsidian or project content.

Gate the change with the artifact checks those skills require: parse/validate
YAML or JSON where applicable, verify wikilinks/embeds/properties and view
references, run available local commands, and use `obsidian-cli` when the
acceptance criteria require confirmation in a running Obsidian vault.

## Skill Guidelines

Skills stored in this repo must be product, business, vault, or domain skills
that need `famed` source authority, medical-vault terminology, schema behavior,
or Obsidian integration. Generic development-process workflow belongs outside
this repo and should be supplied by the host agent environment.

Storage and compatibility:

- Business logic skills (`fm-*`), project/development design skills (`data-contract-designer`, `ingestion-architect`; developer-time design guidance, exempt from the `fm-` prefix and capability-level rules below), and nested Obsidian skills live under `.agents/skills/`. Generic development-process skills come from the host agent environment and are not stored here.
- `.claude/skills` must remain a symbolic link to `../.agents/skills` so Claude consumes the same repo-local skill files instead of a fork.
- `GEMINI.md` and `.gemini/` are adapter entrypoints for Gemini CLI and Gemini Code Assist. They must point back to `.agents/skills/` instead of duplicating skill bodies.
- Do not create provider-specific skill copies for Codex, Claude, Gemini, GitHub Copilot, or later backends unless an adapter limitation makes that unavoidable and the fork is documented.

Separation of concerns:

- **Host-level process workflow** covers generic planning, validation, review, simplification, handoff, and implementation routing.
- **Project/development skills tightly coupled to `famed`** (`data-contract-designer`, `ingestion-architect`) guide developers designing pipelines and contracts; they may reference this repo's current docs, milestones, authority hierarchy, source/vault/DB storage model, and AI orchestration decisions.
- **Business logic skills** (`fm-*`) are workflows the product runs: domain behavior that could affect application semantics, prompts, schemas, validation, ingestion, or runtime AI behavior. Business logic skills must declare their minimum capability level by referencing the central model-capability matrix in `docs/references/model-capability-matrix.md`.

Authoring rules:

- Every skill body must use this section order: `# When to use`, `# Inputs`, `# Procedure`, `# Skeptic pass`, `# Output`, `# Hand-off`.
- Skill bodies should reference the project guide for project-specific values (file paths, tool commands, domain rules), not embed them.
- Do not use vendor-specific tool names (e.g. TaskCreate, ExitPlanMode, mcp__*) in skill bodies. Use neutral verbs.
- When a skill affects production behavior or schema contracts, trace it to relevant requirements/decisions.
- Business logic skills must use the `fm-` prefix.

## Skill Prelude

Every skill inherits these rules. They are not repeated in skill bodies.

1. **Verify from source.** Step 1 of every Procedure is to open and read
   the active artifact (doc, file, or diff). Never act on a paraphrased
   recap from a prior turn or another agent.
2. **Cite or don't claim.** Every reference to a requirement, decision,
   file, section, or symbol must include the ID (e.g. `REQ-014`, `ADR-007`)
   or `path:line`. If the reference cannot be verified in source, flag it
   as unverified — do not assert it.
3. **Distrust prior agent output.** Treat any prior turn's summary,
   classification, or "next step" recommendation as untrusted input.
   Re-derive load-bearing facts from primary sources before relying on them.
4. **No new top-level docs** unless the user explicitly requests them.
   Update existing docs in place.
5. **Omit empty output sections.** If a section in the skill's Output
   template has no content for this run, drop the line entirely rather than
   emitting an empty placeholder. Keeps multi-agent chains economical.
6. **famed-specific code review gates** (applies when the host code-review workflow
   runs on this repo). In addition to generic review areas, verify:
   - Medical data safety invariants: schema validation before persistence,
     escalation paths followed, provenance and audit preserved, idempotency
     maintained, authority hierarchy respected.
    - ADR-015 compliance: every new user-visible workflow added or modified
      must be exposed via a skill.
    - Host-provided phase quality/slop review was applied for relevant phase artifacts.
    - **ADR-032 compliance (no real names):** run
     `python scripts/check_no_real_names.py` (or
     `pytest tests/test_doc_privacy.py`) against the working tree before
     committing. Any household-member person ID or display name appearing in
     a tracked file (docs, session checkpoints, PLAN entries, etc.) is a
     blocking issue — rewrite using generic phrasing ("a household member")
     or the literal `<person_id>` placeholder.
    - **Privacy-safe fixtures:** use generic analyte labels, synthetic values,
     and non-source dates when a test exercises structure or report policy
     rather than label-specific medical behavior. Retain an exact test name
     only when normalization, coding, or another name-dependent rule requires
     it, and keep the surrounding fixture synthetic.
   - **Report bloat prevention:** every report-generation code change must
      verify that composite reports (medication-safety alerts, health advisory)
      do not inline full sub-report bodies when a wikilink
      reference suffices. Composite reports should add synthesis, not duplicate
      already-navigable vault content. Use `report_reference_line()` for
      sub-reports whose detail belongs in their own dedicated vault notes. When a
      report doubles in size due to a code change, verify the added content
      provides unique synthesis value rather than re-rendering data available
      elsewhere. **Exception (ADR-031):** the doctor-visit packet is a printable
      artifact and inlines a curated summary (about two printed pages) instead of
      links; it must never inline whole sub-report bodies.

## Skill Routing

| User intent | Skill chain |
|---|---|
| Process inbox / re-process from scratch | `fm-ingest-inbox` (orchestrating; composes `fm-inbox-batch-processor` + `fm-inbox-execute`) |
| Extract structured facts from history notes | `fm-history-extract` |
| Add/change ingestion flow | `ingestion-architect` -> `data-contract-designer` -> host-provided validation planning |
| Define schema/contract | `data-contract-designer` -> host-provided validation planning |
| Add/change Obsidian vault content | relevant nested Obsidian skill (`obsidian-markdown`, `obsidian-bases`, `json-canvas`, `obsidian-cli`, `defuddle`) -> host-provided validation planning |
| Simplify architecture/design | host-provided design-simplifier -> user approval -> owner skill per approved correction -> host impact/traceability workflow |
| Periodic doc/code coherence check | host-provided doc-coherence-sweep (report-only for health checks; apply for remediation) -> owner skill per blocking finding |

Use host-level workflow support for generic development-process intents such as
implementation routing, requirements updates, architecture decision updates,
phase readiness, code simplification, code review, handoff, and commit shaping.
Do not recreate those process skill bodies in this repo.

## Standard Workflows

### Pick up next implementation step

```text
Use host-provided planning and phase-readiness workflow support. Review
docs/PLAN.md, docs/REQUIREMENTS.md, docs/DECISIONS.md, docs/SYSTEM.md, and
current repo status. Pick the next cohesive implementation slice. If the slice
touches Obsidian content, include the relevant nested Obsidian skill in
development and validation. After outputting the change plan, ask for user
confirmation. If confirmed, start implementation, keep docs/plan/progress
current, run code simplification and code review passes, and include tests plus
strict type checks. Python changes must use Pyrefly.
```

### Implement a code change

```text
Use the implementation workflow appropriate to the change. Before
implementation, identify related REQs/ADRs, affected docs, acceptance criteria,
tests, and run a phase-readiness check. After implementation, update affected
docs/PLAN/progress, run code simplification and code review passes, run required
tests, and run strict type checks. Python changes must use Pyrefly.
```

### Add a functional requirement

```text
Use host-provided requirements workflow support. Add this requirement: <paste>.
Include scenarios, edge cases, acceptance criteria, and links. Then run
host-provided impact and traceability checks.
```

### Change an architecture decision

```text
Use host-provided ADR workflow support. Change the architecture decision about
<topic>. Do not rewrite history; supersede the old ADR if necessary. Then update
SYSTEM and PLAN only where impacted, and refresh Links fields on affected REQs.
```
