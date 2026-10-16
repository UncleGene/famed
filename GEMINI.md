# famed Gemini Adapter

Gemini should follow the repository-wide project guide in `AGENTS.md`.
This file is a Gemini context entrypoint only; it does not replace the
canonical agent instructions or skill source.

@./AGENTS.md

## Canonical Sources

- Project guide: `AGENTS.md`
- Portable skills: `.agents/skills/**/SKILL.md`
- Current project state: `docs/CORE.md`, `docs/REQUIREMENTS.md`,
  `docs/DECISIONS.md`, `docs/SYSTEM.md`, `docs/PLAN.md`

## Operating Rules

- Treat `.agents/skills/` as the single source of truth for reusable skills.
- Discover skills recursively under `.agents/skills/**/SKILL.md`. This includes
  ignored local bundles such as `.agents/skills/obsidian-skills/` when they are
  present in the working tree.
- Do not copy skill bodies into Gemini-specific files. Gemini commands should
  import the canonical skill files when possible.
- Follow `AGENTS.md` confirmation, documentation, traceability, simplification,
  review, test, and Pyrefly requirements.
- For changes touching Obsidian-facing artifacts or workflows, include the
  relevant nested Obsidian skill from `.agents/skills/obsidian-skills/skills/`
  as development and validation context when it is present locally.
- Preserve medical safety invariants: source-first authority, schema-constrained
  ingestion, quarantine on ambiguity or low confidence, no destructive merges,
  and no direct AI writes to canonical medical truth.
- When Gemini cannot execute a skill natively, read the referenced skill file
  and perform the workflow manually.

## Gemini Surfaces

- Code Assist Enterprise indexing: `.aiexclude` excludes local medical data,
  generated vault content, databases, temporary files, and binary documents from
  Gemini Code Assist code customization.
- Gemini CLI context scanning: `.geminiignore` keeps broad context scans focused
  on tracked project guidance and source files.
- Gemini CLI: use `.gemini/commands/*.toml` for reusable slash commands. These
  commands inject `AGENTS.md` and the relevant `.agents/skills/**/SKILL.md`
  files so the canonical workflow is reused; include nested paths explicitly
  when a nested skill is relevant.
- Gemini Code Assist on GitHub: use `.gemini/styleguide.md` for review guidance
  and `.gemini/config.yaml` for repository review configuration. This supports
  review behavior only; it is not a full skill execution runtime.
- Gemini Code Assist in IDEs: use this `GEMINI.md` and manually include relevant
  skill files as context when the IDE surface does not load project commands.
