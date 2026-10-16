# CLAUDE.md

Claude Code-specific setup. Project rules, invariants, and skill routing live in `AGENTS.md` — read it first.

## Commands

```bash
python -m pip install -e ".[dev]"          # install runtime + dev deps
python scripts/setup.py                    # one-time vault/DB/Keychain scaffold
python scripts/setup.py --skip-keychain    # re-run after schema/filesystem changes
pytest                                     # tests
ruff check . && ruff format .              # lint / format
pyrefly check                              # type check (required gate; never mypy)
```

## Skills

`.claude/skills/` is a symlink to `.agents/skills/`. Do not create a separate copy.
Repo-local skills are product, business, vault, or domain skills. Generic
development-process workflow is supplied by the host agent environment.

## Subagent model selection

Match the model to the task's reasoning requirement, not its importance.

| Task type | Model | Rationale |
|---|---|---|
| `Explore` — file search, grep, quick codebase lookup | `haiku` | Read-only pattern-matching; speed beats depth |
| `claude-code-guide` — API/CLI factual questions | `haiku` | Retrieval-heavy; fast is better |
| `general-purpose` — broad research, cross-file analysis | `sonnet` | Balanced reasoning across multiple files |
| `Plan` — architecture or implementation planning | `sonnet` | Structured multi-step reasoning |
| Domain skill chains with ingestion, contracts, vault semantics, or medical-source authority | `sonnet` | Requires doc cross-referencing and domain judgment |
| Large-scope autopilot runs | `sonnet` | Default; only escalate to `opus` when the slice involves a genuinely novel architecture decision not yet settled in the docs |
