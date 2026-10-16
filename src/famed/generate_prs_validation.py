#!/usr/bin/env python3
"""Internal helper for fm-prs-validation."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from famed.imputation_readiness import ImputationReadinessError, validate_imputation_person_id
from famed.prs_validation import PRSValidationError, write_prs_validation_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a PRS model/input validation report.")
    parser.add_argument("--person-id", required=True, help="Vault person_id to report on.")
    parser.add_argument(
        "--model-path",
        required=True,
        help="Path to a local pinned PRS model JSON package.",
    )
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)
    db_path = Path(args.db_path)
    model_path = Path(args.model_path)
    try:
        validate_imputation_person_id(args.person_id)
    except ImputationReadinessError as exc:
        print(f"Invalid person_id: {exc}", file=sys.stderr)
        return 1

    person_note = vault_root / "Persons" / f"{args.person_id}.md"
    if not person_note.is_file():
        print(f"Person note not found: {person_note}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    with sqlite3.connect(db_path) as conn:
        try:
            report_path, validation = write_prs_validation_report(
                conn,
                vault_root,
                args.person_id,
                model_path,
            )
        except PRSValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(f"Wrote {report_path}")
    print(f"State: {validation.state}")
    print(
        f"Model variants: {validation.model_variants}; "
        f"matched: {validation.matched_variants}; missing: {validation.missing_variants}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
