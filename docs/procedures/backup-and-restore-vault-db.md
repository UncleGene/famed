# Backup and Restore Vault + Database

## Goal

Create a local backup copy of the vault and SQLite database, and restore safely when needed.

## Backup

Run from repository root:

```bash
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
cp -R famed-vault "backups/famed-vault-$stamp"
cp data/famed.db "backups/famed-db-$stamp.sqlite"
```

Optional integrity check:

```bash
sqlite3 "backups/famed-db-$stamp.sqlite" "PRAGMA integrity_check;"
```

Expected output is `ok`.

## Restore

1. Close Obsidian if open.
2. Copy backup vault and DB back into place:

```bash
cp -R "backups/famed-vault-YYYYMMDD-HHMMSS" famed-vault
cp "backups/famed-db-YYYYMMDD-HHMMSS.sqlite" data/famed.db
```

3. Re-open Obsidian and verify notes are visible.
4. Optional DB check:

```bash
sqlite3 data/famed.db "PRAGMA integrity_check;"
```

## Notes

- Keep backups on encrypted storage when possible.
- If vault and DB disagree, treat the vault as source of truth; rerun `python scripts/sync_vault.py` to rebuild the DB index from it.

## Reference Data Cache — Not Backup Material

The `data/reference/` directory holds rebuildable public datasets such as RxNorm and manually downloaded LOINC files. It is not user-owned medical data.

- **Do not** include `data/reference/` in backups. It is excluded by `.gitignore` for the same reason.
- After a fresh checkout or restore of the vault and DB, rebuild non-credentialed reference caches and restore manually downloaded files as needed:
  ```bash
  python scripts/cache.py download
  ```
- For LOINC, download the current zip from <https://loinc.org/downloads/>, extract it under `data/reference/loinc/`, then run `python scripts/cache.py --dataset loinc refresh` to parse `Loinc.csv`.
- Use `python scripts/cache.py status` to check cache health, and `python scripts/cache.py refresh` to update stale datasets.
