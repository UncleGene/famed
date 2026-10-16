#!/usr/bin/env python3
"""Internal helper for fm-imputation-readiness.

Prints the readiness report on demand; it is not a per-person vault note (ADR-031).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from famed.imputation_readiness import (
    ImputationReadinessError,
    build_imputation_readiness,
    render_imputation_readiness_report,
    validate_imputation_person_id,
)
from famed.person_sync import load_person_display_name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a DNA imputation-readiness report.")
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)
    db_path = Path(args.db_path)
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
            readiness = build_imputation_readiness(conn, args.person_id)
        except ImputationReadinessError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(
        render_imputation_readiness_report(
            args.person_id,
            readiness,
            display_name=load_person_display_name(vault_root, args.person_id),
        )
    )
    print(f"State: {readiness.state}")
    print(
        f"Variants: {readiness.total_variants}; "
        f"direct: {readiness.direct_variants}; imputed: {readiness.imputed_variants}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
