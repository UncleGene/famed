# famed Gemini Code Assist Review Guide

Gemini Code Assist reviews must follow the root `AGENTS.md` project rules and
the canonical skill source under `.agents/skills/`.

## Review Priorities

- Lead with correctness, behavioral regressions, medical data safety, security,
  and missing validation.
- Check every implementation change against related requirements, decisions,
  system boundaries, and the current plan.
- Preserve source-first authority: retained source document > Obsidian vault note
  > SQLite DB.
- Verify that AI interpretation remains schema-constrained and cannot write
  canonical medical truth directly.
- Flag invalid schema handling, low confidence, ambiguous person matching,
  conflicting values, irreversible deduplication, or unaudited merges.
- Confirm domain modules do not import provider SDKs, orchestration internals, or
  provider-specific prompts directly.
- For Python changes, require relevant tests and `pyrefly check`; do not accept
  mypy as the project type-checking gate.
- Flag documentation drift when `docs/REQUIREMENTS.md`, `docs/DECISIONS.md`,
  `docs/SYSTEM.md`, or `docs/PLAN.md` should change with the implementation.
- For Obsidian-facing changes, require the relevant nested Obsidian skill to be
  used in development and gating: `obsidian-markdown` for vault Markdown,
  `obsidian-bases` for `.base`, `json-canvas` for `.canvas`, `obsidian-cli`
  for live-vault verification, and `defuddle` for web-to-Markdown intake.

## Skill Reuse

When reviewing a pull request, use the famed-specific review gates in
`AGENTS.md` as the review standard, plus:

- Any domain skill relevant to the changed behavior, especially
  `.agents/skills/ingestion-architect/SKILL.md` or
  `.agents/skills/data-contract-designer/SKILL.md`
- Any nested Obsidian skill relevant to changed Obsidian-facing artifacts or
  workflows when `.agents/skills/obsidian-skills/` is present locally

Gemini Code Assist on GitHub treats this file as natural language review
guidance. It does not execute `.agents/skills/**/SKILL.md` as native tools, so
review comments should explicitly cite the relevant requirement, decision,
system boundary, test gap, or safety invariant.
