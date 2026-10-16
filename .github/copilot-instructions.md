# GitHub Copilot Instructions for famed

Follow the root `AGENTS.md` common project rules. This file adds GitHub-specific coding and review guidance only.


When generating or reviewing code:

- Prefer small, testable changes tied to `docs/REQUIREMENTS.md` and `docs/DECISIONS.md`.
- Never let AI interpretation output write directly to canonical storage.
- Preserve interpreter/canonicalizer/dedup/resolver/steward boundaries.
- Preserve idempotency, provenance, audit trail, quarantine, and source/vault/DB authority.
- Domain modules must use the internal AI/orchestration interface; do not import provider-specific or orchestration internals directly.
- Do not create one-off ingestion pipelines where unified inbox orchestration is intended.
- Update docs or flag documentation drift when behavior changes.
- After determining a change plan, ask for user confirmation before starting implementation.
- For Python changes, run or request `pyrefly check` as the type-checking gate.
- For changes touching Obsidian vault Markdown, `.base` files, `.canvas` files, or live-vault workflows, apply the relevant nested Obsidian skill (`obsidian-markdown`, `obsidian-bases`, `json-canvas`, `obsidian-cli`, `defuddle`) from `.agents/skills/obsidian-skills/skills/` as part of development and gating.
