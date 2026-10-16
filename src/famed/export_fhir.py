#!/usr/bin/env python3
"""Internal helper for fm-export-fhir."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from famed.fhir_export import (
    FHIR_EXPORT_FILENAME,
    FhirExportError,
    validate_fhir_export_file,
    write_fhir_export,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a deterministic FHIR R4 bundle.")
    parser.add_argument("--person-id", required=True, help="Vault person_id to export.")
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
        "--validate-only",
        action="store_true",
        help="Validate the existing FHIR export without regenerating it.",
    )
    return parser.parse_args()


def _print_validation_result(export_path: Path, issues: list[str]) -> int:
    if not issues:
        print(f"Validation passed: {export_path}")
        return 0
    print(f"Validation failed: {export_path}", file=sys.stderr)
    for issue in issues:
        print(f"- {issue}", file=sys.stderr)
    return 1


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)
    db_path = Path(args.db_path)
    person_note = vault_root / "Persons" / f"{args.person_id}.md"
    export_path = vault_root / "Records" / args.person_id / "Reports" / FHIR_EXPORT_FILENAME

    if args.validate_only:
        return _print_validation_result(export_path, validate_fhir_export_file(export_path))

    if not person_note.is_file():
        print(f"Person note not found: {person_note}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    try:
        with sqlite3.connect(db_path) as conn:
            (
                export_path,
                observation_count,
                medication_statement_count,
                diagnostic_report_count,
                condition_count,
                family_member_history_count,
                immunization_count,
            ) = write_fhir_export(conn, vault_root, args.person_id)
    except FhirExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    validation_issues = validate_fhir_export_file(export_path)
    if validation_issues:
        return _print_validation_result(export_path, validation_issues)

    print(f"Wrote {export_path}")
    print(f"Validation passed: {export_path}")
    print(f"Observation resources included: {observation_count}")
    print(f"MedicationStatement resources included: {medication_statement_count}")
    print(f"DiagnosticReport resources included: {diagnostic_report_count}")
    print(f"Condition resources included: {condition_count}")
    print(f"FamilyMemberHistory resources included: {family_member_history_count}")
    print(f"Immunization resources included: {immunization_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
