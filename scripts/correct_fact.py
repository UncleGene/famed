#!/usr/bin/env python3
"""Internal entry point for user-directed fact corrections. Invoked by fm-correct-fact skill."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply or revoke a user-directed fact correction.")
    parser.add_argument(
        "--category",
        required=True,
        choices=("labs", "medications", "history"),
        help="Record category to correct",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=("apply", "revoke"),
        help="Whether to apply or revoke the correction",
    )
    parser.add_argument("--person-id", required=True, help="Person identifier")
    parser.add_argument("--reason", required=True, help="Human-readable reason for this correction")
    parser.add_argument("--actor", default="user", help="Who initiated the correction")

    # Labs identity
    parser.add_argument("--test-name", help="Lab test name (required for labs category)")
    parser.add_argument("--observed-at", help="Observation timestamp ISO 8601 (required for labs)")
    parser.add_argument("--value", type=float, help="Corrected numeric value (apply, labs only)")
    parser.add_argument("--unit", help="Corrected unit (apply, labs only)")

    # Medications identity
    parser.add_argument(
        "--medication-name", help="Medication name (required for medications category)"
    )
    parser.add_argument(
        "--medication-type",
        help=(
            "Medication type: prescription|otc|supplement|herbal|vaccine (required for medications)"
        ),
    )
    parser.add_argument("--dose", help="Corrected dose string (apply, medications only)")
    parser.add_argument("--frequency", help="Corrected frequency string (apply, medications only)")

    # History identity (refute/revoke only)
    parser.add_argument("--description", help="History fact label to refute (history only)")
    parser.add_argument("--recorded-date", help="History fact date YYYY-MM-DD (history only)")

    parser.add_argument(
        "--db",
        default="data/famed.db",
        help="Path to SQLite database (default: data/famed.db)",
    )
    parser.add_argument(
        "--vault",
        default="famed-vault",
        help="Path to vault root (default: famed-vault)",
    )
    args = parser.parse_args()

    root = repo_root()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (root / db_path).resolve()

    vault_root = Path(args.vault)
    if not vault_root.is_absolute():
        vault_root = (root / vault_root).resolve()

    from famed.correct_fact import (
        CorrectFactError,
        apply_history_refutation,
        apply_lab_correction,
        apply_medication_correction,
        revoke_history_refutation,
        revoke_lab_correction,
        revoke_medication_correction,
    )

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

            if args.category == "labs":
                if not args.test_name or not args.observed_at:
                    print(
                        "Error: --test-name and --observed-at are required for labs category",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if args.action == "apply":
                    if args.value is None:
                        print(
                            "Error: --value is required when applying a lab correction",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    result = apply_lab_correction(
                        conn,
                        vault_root,
                        args.person_id,
                        args.test_name,
                        args.observed_at,
                        args.value,
                        args.unit,
                        reason=args.reason,
                        actor=args.actor,
                    )
                    print(
                        f"Applied correction: {result['test_name']!r} at {result['observed_at']}: "
                        f"{result['old_value']!r} → {result['new_value']!r}"
                        + (f" [{result['unit']}]" if result["unit"] else "")
                    )
                else:
                    result = revoke_lab_correction(
                        conn,
                        vault_root,
                        args.person_id,
                        args.test_name,
                        args.observed_at,
                        reason=args.reason,
                        actor=args.actor,
                    )
                    print(
                        f"Revoked correction on {result['test_name']!r} at "
                        f"{result['observed_at']}; corrected value was {result['old_value']!r}"
                    )

            elif args.category == "history":
                if not args.description or not args.recorded_date:
                    print(
                        "Error: --description and --recorded-date are required "
                        "for history category",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                history_action = (
                    apply_history_refutation
                    if args.action == "apply"
                    else revoke_history_refutation
                )
                result = history_action(
                    conn,
                    vault_root,
                    args.person_id,
                    args.description,
                    args.recorded_date,
                    reason=args.reason,
                    actor=args.actor,
                )
                verb = "Refuted" if args.action == "apply" else "Revoked refutation of"
                print(
                    f"{verb} history fact {result['description']!r} dated "
                    f"{result['recorded_date']}; {result['changed_notes']} note(s) updated"
                )

            else:  # medications
                if not args.medication_name or not args.medication_type:
                    print(
                        "Error: --medication-name and --medication-type are required "
                        "for medications category",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if args.action == "apply":
                    if args.dose is None and args.frequency is None:
                        print(
                            "Error: at least one of --dose or --frequency is required "
                            "when applying a medication correction",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    result = apply_medication_correction(
                        conn,
                        vault_root,
                        args.person_id,
                        args.medication_name,
                        args.medication_type,
                        args.dose,
                        args.frequency,
                        reason=args.reason,
                        actor=args.actor,
                    )
                    print(
                        f"Applied correction: {result['medication_name']!r} "
                        f"({result['medication_type']}): "
                        f"dose {result['old_dose']!r} → {result['new_dose']!r}, "
                        f"frequency {result['old_frequency']!r} → {result['new_frequency']!r}"
                    )
                else:
                    result = revoke_medication_correction(
                        conn,
                        vault_root,
                        args.person_id,
                        args.medication_name,
                        args.medication_type,
                        reason=args.reason,
                        actor=args.actor,
                    )
                    print(
                        f"Revoked correction on {result['medication_name']!r} "
                        f"({result['medication_type']}); "
                        f"corrected dose was {result['old_dose']!r}, "
                        f"frequency was {result['old_frequency']!r}"
                    )

    except CorrectFactError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
