# Developing famed

This guide is for contributors. Users should start with the
[README](../README.md).

## Architecture in One Page

- **Data layers** (authority order): user corrections/updates > retained source
  files (`data/processed/`) > Obsidian vault (`famed-vault/`) > SQLite index
  (`data/famed.db`). See ADR-003.
- **Roles:** the Clinical Interpreter (the live agent running a skill) proposes
  facts; deterministic Canonicalizers validate and code them; the Deduplication
  Engine keeps reversible links; the Human Resolver (the user) settles
  ambiguity; the Data Steward persists and audits. See ADR-001, ADR-002.
- **Interface:** every user workflow is a skill under `.agents/skills/`.
  `scripts/` and `src/famed/` are internal (ADR-015).
- **Reports** refresh through the dependency graph in
  `src/famed/dependent_reports.py` (ADR-025).

Read in this order: [CORE](CORE.md) → [SYSTEM](SYSTEM.md) →
[REQUIREMENTS](REQUIREMENTS.md) and [DECISIONS](DECISIONS.md) indexes →
[PLAN](PLAN.md). Component detail is in
[references/component-detail.md](references/component-detail.md).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"           # add ",ocr" for OCR tests/workflows
python scripts/setup.py --skip-keychain     # local vault/DB scaffold
```

Optional: the nested Obsidian authoring skills referenced in `AGENTS.md`
(`obsidian-markdown`, `obsidian-bases`, `json-canvas`, `obsidian-cli`,
`defuddle`) come from the
[kepano/obsidian-skills](https://github.com/kepano/obsidian-skills) bundle.
Clone it into `.agents/skills/obsidian-skills/` (git-ignored) when working on
vault-facing artifacts.

## Quality Gates

Every change must pass:

```bash
pytest                                   # full suite
ruff check . && ruff format --check .    # lint and formatting (Ruff is pinned)
pyrefly check                            # type checking (do not use mypy)
python scripts/check_no_real_names.py    # tracked-file privacy (ADR-032)
```

`pyrefly check --min-severity warn` is a useful periodic audit but not a gate.

Code changes also need a simplification pass and a review pass, and must keep
affected docs, skills, and procedures current. The full checklist and the
famed-specific review gates are in [AGENTS.md](../AGENTS.md).

## Repository Layout

| Path | Contents |
|---|---|
| `src/famed/` | Library code: ingestion, canonicalization, sync, reports, exports |
| `scripts/` | Thin CLI entry points that skills call |
| `tests/` | Pytest suite with synthetic fixtures |
| `.agents/skills/` | Canonical skills (`fm-*` business skills, design skills) |
| `.claude/skills` | Symlink to `.agents/skills` for Claude Code |
| `AGENTS.md` | Agent rules, invariants, skill routing, review gates |
| `CLAUDE.md`, `GEMINI.md` | Provider entry points that defer to `AGENTS.md` |
| `.gemini/`, `.github/copilot-instructions.md`, `.github/prompts/` | Gemini and Copilot adapters |
| `.aiexclude`, `.geminiignore` | Keep agent indexing away from local medical data |
| `vault-template/`, `templates/` | Files copied into a new vault; person YAML template |
| `docs/` | Core docs, `references/`, caregiver `procedures/` |
| `famed-vault/`, `data/`, `ledger/`, `tmp/` | Local data (git-ignored) |

## Runtime Stack

| Area | Choice |
|---|---|
| Language | Python 3.11+ (`PyYAML`, `pypdf`; optional `pytesseract`/`Pillow` for OCR) |
| Storage | SQLite via the standard-library `sqlite3` module |
| Vault | Obsidian + Dataview |
| Secrets | macOS Keychain (setup creates a placeholder slot; `--skip-keychain` elsewhere) |
| AI | The executing agent; no provider SDKs in the runtime (ADR-013) |
| Optional tools | Tesseract OCR; PLINK for advanced genetics workflows |

Domain modules must not import orchestration frameworks or provider SDKs.

## Writing Tests and Fixtures

- Use synthetic people: `dummy_alice`, `Dummy Alice` (ADR-032). Never copy real
  names, birth dates, record values, dates, or file names from a local vault
  into tests or docs.
- Use synthetic values and dates unless the test exercises an exact analyte
  name's coding behavior.
- Keep tests isolated from live storage: pass explicit vault/DB/processed roots
  and run from an unrelated working directory when testing path handling.

## Documentation Conventions

- Docs describe the current state; git holds history (ADR-012). Edit or replace
  an ADR when a decision changes; remove finished work from `PLAN.md`.
- New behavior updates the owning REQ (`Links:`), the relevant ADR
  (`Related REQs:`), `SYSTEM.md` when architecture changes, the skill's
  `SKILL.md`, and the caregiver procedure.
- A skill's `SKILL.md` is authoritative for its mechanics; README and procedures
  link to it rather than repeating it.
- Do not add new top-level docs without agreement.

## Skills

Skill authoring rules (section order, capability levels, `fm-` prefix for
business logic, no vendor-specific tool names) are in `AGENTS.md`. Minimum
capability levels live in
[references/model-capability-matrix.md](references/model-capability-matrix.md).
Generic development-process workflows (planning, review, validation) are
expected from your agent environment and are not stored in this repo.

## Internal Entry Points

Skills call these; they are useful for debugging:

| Script | Purpose |
|---|---|
| `scripts/inbox_orchestrator.py` | Route and execute inbox files (dry run by default) |
| `scripts/sync_vault.py` | Rebuild the DB index from the vault; `--validate-only` checks vault contracts |
| `scripts/reprocess_source.py` | Replay one retained source |
| `scripts/rebuild_vault.py` | Isolated rebuild and differential audit |
| `scripts/cache.py` | Reference data `status`, `download`, `refresh`, `clean` |
| `scripts/correct_fact.py`, `scripts/record_user_update.py` | Corrections and dated updates |
| `scripts/export_fhir.py` | FHIR export and validation |
