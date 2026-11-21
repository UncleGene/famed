# Initial Vault Setup

## Goal

Create a working local vault and database for famed.

## Prerequisites

- Python 3.11+
- Obsidian installed
- Terminal access on macOS

## Steps

1. Open a terminal in the repository root.
2. Run:

```bash
python -m pip install -e .
python scripts/setup.py
```

3. Confirm these paths now exist:
   - `tmp/`
   - `famed-vault/`
   - `data/famed.db`
   - `data/inbox/`
   - `data/inbox/persons/`
4. Add or sync at least one family member, then confirm the person-specific inbox root exists:
   - `data/inbox/<person_id>/`
5. Open Obsidian and choose **Open folder as vault**.
6. Select `famed-vault/`.
7. Verify the top-level folders: `Persons/` and `Records/`.

## Troubleshooting

- To check dependencies without changing the vault or database:

```bash
python scripts/setup.py --check            # on Linux add --skip-keychain
```

- If keychain setup fails, rerun with:

```bash
python scripts/setup.py --skip-keychain
```

- If the vault exists already, setup will only add missing template files and directories.
