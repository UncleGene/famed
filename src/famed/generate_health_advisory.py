#!/usr/bin/env python3
"""Internal helper for fm-generate-health-advisory."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from famed.annotation_cache import REFERENCE_DB_FILENAME
from famed.health_advisory import write_health_advisory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a health advisory report for one person."
    )
    parser.add_argument("--person-id", required=True, help="Vault person_id to report on.")
    parser.add_argument(
        "--vault-path",
        default="famed-vault",
        help="Path to the Obsidian vault (default: famed-vault).",
    )
    parser.add_argument(
        "--db-path",
        default="data/famed.db",
        help="Path to famed SQLite DB (default: data/famed.db).",
    )
    parser.add_argument(
        "--reference-db",
        default=str(Path("data") / "reference" / REFERENCE_DB_FILENAME),
        help="Path to famed-reference.db (default: data/reference/famed-reference.db).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)
    db_path = Path(args.db_path)
    ref_db_path = Path(args.reference_db)
    person_note = vault_root / "Persons" / f"{args.person_id}.md"

    if not person_note.is_file():
        print(f"Person note not found: {person_note}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    with sqlite3.connect(db_path) as conn:
        report_path, flag_count = write_health_advisory(
            vault_root=vault_root,
            person_id=args.person_id,
            conn=conn,
            ref_db_path=ref_db_path,
        )

    print(f"Wrote {report_path}")
    print(f"Genetic risk flags: {flag_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
