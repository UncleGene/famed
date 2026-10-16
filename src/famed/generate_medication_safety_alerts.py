#!/usr/bin/env python3
"""Internal helper for fm-generate-medication-safety-alerts."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from famed.findings_ledger import FINDINGS_LEDGER_PATH
from famed.medication_safety_alerts import (
    MedicationSafetyAlertsError,
    write_medication_safety_alerts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate medication-safety review alerts.")
    parser.add_argument("--person-id", required=True, help="Vault person_id to report on.")
    parser.add_argument(
        "--vault-path",
        default="famed-vault",
        help="Path to the Obsidian vault (default: famed-vault).",
    )
    parser.add_argument(
        "--findings-ledger",
        default=str(FINDINGS_LEDGER_PATH),
        help="Path to findings JSONL ledger (default: ledger/findings.jsonl).",
    )
    parser.add_argument(
        "--db-path",
        default="data/famed.db",
        help="Path to famed SQLite DB (default: data/famed.db).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)
    ledger_path = Path(args.findings_ledger)
    db_path = Path(args.db_path)
    person_note = vault_root / "Persons" / f"{args.person_id}.md"

    if not person_note.is_file():
        print(f"Person note not found: {person_note}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path) if db_path.is_file() else None
    try:
        report_path, finding_count = write_medication_safety_alerts(
            vault_root,
            args.person_id,
            findings_ledger_path=ledger_path,
            conn=conn,
        )
    except MedicationSafetyAlertsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()

    print(f"Wrote {report_path}")
    print(f"Medication/PGx findings included: {finding_count}")
    if not ledger_path.is_file():
        print(f"Findings ledger missing: {ledger_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
