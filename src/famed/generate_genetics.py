#!/usr/bin/env python3
"""Internal helper for fm-genetics."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from famed.annotation_cache import REFERENCE_DB_FILENAME
from famed.findings_ledger import FINDINGS_LEDGER_PATH
from famed.genetics_report import write_genetics_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Genetics report for one person.")
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
        help="Path to famed-reference.db.",
    )
    parser.add_argument(
        "--findings-ledger",
        default=str(FINDINGS_LEDGER_PATH),
        help="Path to findings JSONL ledger (default: ledger/findings.jsonl).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)
    db_path = Path(args.db_path)
    person_note = vault_root / "Persons" / f"{args.person_id}.md"
    if not person_note.is_file():
        print(f"Person note not found: {person_note}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    with sqlite3.connect(db_path) as conn:
        report_path = write_genetics_report(
            conn,
            vault_root,
            args.person_id,
            Path(args.reference_db),
            findings_ledger_path=Path(args.findings_ledger),
        )
        conn.commit()

    if report_path is None:
        print(f"Skipped Genetics report for {args.person_id}: no DNA imported.")
        return 0
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
