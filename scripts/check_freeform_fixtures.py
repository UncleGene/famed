#!/usr/bin/env python3
"""Validate manual free-form inbox verification fixtures."""

from __future__ import annotations

import argparse
from pathlib import Path

from famed.freeform_verification import (
    FreeformFixtureError,
    audit_current_inbox,
    validate_fixture_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate recorded manual free-form inbox fixture expectations."
    )
    parser.add_argument("fixture", help="YAML fixture file to validate")
    parser.add_argument(
        "--data-root",
        default="data",
        help="Data root used to resolve source identifiers (default: data)",
    )
    parser.add_argument(
        "--audit-current-inbox",
        action="store_true",
        help="Also report current data/inbox and data/quarantine outcome and fixture gaps.",
    )
    args = parser.parse_args()
    fixture_path = Path(args.fixture)
    data_root = Path(args.data_root)

    try:
        matches = validate_fixture_file(fixture_path, data_root=data_root)
    except FreeformFixtureError as exc:
        print(f"Fixture validation failed: {exc}")
        return 1

    for match in matches:
        print(f"ok\t{match.matched_by}\t{match.requested}\t{match.path}")
    print(f"Validated {len(matches)} fixture(s).")
    if args.audit_current_inbox:
        try:
            report = audit_current_inbox(data_root, fixture_path=fixture_path)
        except FreeformFixtureError as exc:
            print(f"Inbox audit failed: {exc}")
            return 1
        print()
        print("Inbox/quarantine audit")
        for outcome, count in sorted(report.outcome_counts.items()):
            print(f"count\t{outcome}\t{count}")
        print(f"files\t{len(report.items)}")
        print()
        for item in report.items:
            covered = "fixture" if item.fixture_covered else "gap"
            display_path = item.path
            try:
                display_path = item.path.relative_to(data_root.resolve())
            except ValueError:
                pass
            print(f"file\t{item.outcome}\t{item.family}\t{covered}\t{display_path}\t{item.note}")
        print()
        print("Source family inventory")
        for summary in report.family_summaries:
            outcomes = ",".join(summary.outcomes)
            print(
                f"family\t{summary.status}\t{summary.family}\t{summary.count}\t"
                f"{outcomes}\t{summary.required_action}"
            )
        if report.unresolved_families:
            print()
            print("Unresolved source families")
            for summary in report.unresolved_families:
                print(
                    f"unresolved\t{summary.status}\t{summary.family}\t"
                    f"{summary.count}\t{summary.required_action}"
                )
        if report.missing_required_families:
            print()
            print("Missing required fixture families:")
            for family in report.missing_required_families:
                print(f"missing\t{family}")
        if report.stale_quarantine_paths:
            print()
            print("Legacy quarantine re-evaluation targets:")
            for path in report.stale_quarantine_paths:
                try:
                    path = path.relative_to(data_root.resolve())
                except ValueError:
                    pass
                print(f"quarantine\t{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
