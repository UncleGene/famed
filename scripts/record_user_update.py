#!/usr/bin/env python3
"""Internal entry point for recording user-authored facts.

Invoked by fm-record-user-update skill after the user has confirmed the
proposed facts. The AI never calls this autonomously (ADR-001).

Supports medications, lab observations, and history facts — written to
category-specific dated authoritative update notes per person-date pair
(REQ-024).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist user-authored facts for a person on a given date."
    )
    parser.add_argument("--person-id", required=True, help="Vault person_id")
    parser.add_argument(
        "--date",
        required=True,
        help="ISO 8601 date for the update note (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--medications-json",
        default="[]",
        help=(
            "JSON array of medication objects, each with at minimum "
            "'medication_name'. Optional: medication_type, dose, frequency, "
            "status, start_date, end_date, rxnorm_cui."
        ),
    )
    parser.add_argument(
        "--labs-json",
        default="[]",
        help=(
            "JSON array of lab observation objects, each with at minimum "
            "'test_name' and 'observed_at'. Optional: value, unit, "
            "reference_range_low, reference_range_high, loinc_code."
        ),
    )
    parser.add_argument(
        "--history-json",
        default="[]",
        help=(
            "JSON array of history fact objects, each with at minimum "
            "'description'. Optional: category, date, details."
        ),
    )
    parser.add_argument(
        "--vault-root",
        default=str(repo_root() / "famed-vault"),
        help="Path to the Obsidian vault root",
    )
    parser.add_argument(
        "--db",
        default=str(repo_root() / "data" / "famed.db"),
        help="Path to the SQLite database",
    )
    parser.add_argument(
        "--processed-root",
        help=(
            "Path to retained processed sources; defaults beside the supplied "
            "vault root under data/processed"
        ),
    )
    parser.add_argument(
        "--reference-root",
        help=(
            "Path to local reference datasets; defaults beside the supplied "
            "vault root under data/reference"
        ),
    )
    args = parser.parse_args()

    medications, labs, history = _parse_inputs(args)
    vault_root = Path(args.vault_root)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    from famed.user_update_sync import (
        record_user_update_history_facts,
        record_user_update_lab_facts,
        record_user_update_medications,
    )

    processed_root = Path(args.processed_root) if args.processed_root else None
    reference_root = Path(args.reference_root) if args.reference_root else None

    conn = sqlite3.connect(db_path)
    try:
        med_count = record_user_update_medications(
            args.person_id,
            args.date,
            medications,
            vault_root,
            conn,
            processed_root=processed_root,
            reference_root=reference_root,
        )
        lab_count = record_user_update_lab_facts(
            args.person_id,
            args.date,
            labs,
            vault_root,
            conn,
            processed_root=processed_root,
        )
        hist_count = record_user_update_history_facts(
            args.person_id,
            args.date,
            history,
            vault_root,
            conn,
            processed_root=processed_root,
        )
        conn.commit()
    finally:
        conn.close()

    total = med_count + lab_count + hist_count
    parts = []
    if med_count:
        parts.append(f"{med_count} medication")
    if lab_count:
        parts.append(f"{lab_count} lab")
    if hist_count:
        parts.append(f"{hist_count} history")
    print(f"Recorded {total} new fact(s) ({', '.join(parts)}) for {args.person_id} on {args.date}")
    if med_count:
        med_note = (
            f"Records/{args.person_id}/Medications/{args.date}-{args.person_id}-medications.md"
        )
        print(f"  Medications note: {med_note}")
    if lab_count:
        lab_note = f"Records/{args.person_id}/Labs/{args.date}-{args.person_id}-labs.md"
        print(f"  Labs note: {lab_note}")
    if hist_count:
        history_note = f"Records/{args.person_id}/History/{args.date}-{args.person_id}-history.md"
        print(f"  History note: {history_note}")


def _parse_inputs(args: argparse.Namespace) -> tuple[list[dict], list[dict], list[dict]]:
    medications: list[dict] = []
    labs: list[dict] = []
    history: list[dict] = []

    for name, field, target in [
        ("--medications-json", args.medications_json, medications),
        ("--labs-json", args.labs_json, labs),
        ("--history-json", args.history_json, history),
    ]:
        try:
            parsed = json.loads(field)
        except json.JSONDecodeError as exc:
            print(f"ERROR: {name} is not valid JSON: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, list):
            print(f"ERROR: {name} must be a JSON array", file=sys.stderr)
            sys.exit(1)
        target.extend(parsed)

    if not medications and not labs and not history:
        print(
            "ERROR: at least one of --medications-json, --labs-json, "
            "--history-json must contain a non-empty array",
            file=sys.stderr,
        )
        sys.exit(1)

    return medications, labs, history


if __name__ == "__main__":
    main()
