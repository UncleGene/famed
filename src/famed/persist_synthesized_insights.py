#!/usr/bin/env python3
"""Persist live-agent-authored synthesized insights into an advice report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from famed.report_synthesis import persist_report_synthesized_insights

_REPORT_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "health_advisory": ("health-advisory.md", ("## Source Reports",)),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and persist live-agent-authored synthesized insights."
    )
    parser.add_argument("--person-id", required=True, help="Vault person_id to report on.")
    parser.add_argument(
        "--report-type",
        default="health_advisory",
        choices=sorted(_REPORT_TARGETS),
        help="Report to update (default: health_advisory).",
    )
    parser.add_argument(
        "--insights-json",
        required=True,
        help=(
            "JSON array of candidate insight objects, each with 'statement', "
            "'linked_evidence' (list of vault-relative paths), 'domains_linked' "
            "(list, >=2 of the Health Domains in evidence-model.md), "
            "'evidence_tier' (E1-E5), 'confidence' (0-1), and optional "
            "'weakest_cited_tier' (E1-E5, or omitted/null when every citation "
            "is an untiered raw fact)."
        ),
    )
    parser.add_argument(
        "--vault-path",
        default="famed-vault",
        help="Path to the Obsidian vault (default: famed-vault).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_path)

    try:
        candidates = json.loads(args.insights_json)
    except json.JSONDecodeError as exc:
        print(f"--insights-json is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(candidates, list):
        print("--insights-json must be a JSON array", file=sys.stderr)
        return 1

    try:
        report_filename, insert_before = _REPORT_TARGETS[args.report_type]
        section, rejections = persist_report_synthesized_insights(
            vault_root,
            args.person_id,
            report_filename=report_filename,
            candidates=candidates,
            insert_before_headings=insert_before,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Accepted {len(section.insights)} synthesized insight(s) for "
        f"{args.person_id} {args.report_type}."
    )
    for reason in rejections:
        print(f"Rejected: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
