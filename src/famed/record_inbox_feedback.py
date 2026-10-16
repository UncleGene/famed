#!/usr/bin/env python3
"""Persist live-agent inbox run feedback to data/inbox-run-feedback.jsonl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from famed.inbox_feedback import InboxFileRecord, append_inbox_run_feedback, make_run_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record per-file inbox run feedback from a live fm-ingest-inbox session."
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "ISO-8601 run identifier shared across all files in this run. "
            "Defaults to the current UTC time if omitted."
        ),
    )
    parser.add_argument(
        "--records-json",
        required=True,
        help=(
            "JSON array of per-file outcome objects. Each object must include "
            "'source_path', 'confidence_tier' (high/medium/low/unknown), and "
            "'proposal_outcome'. Optional fields: 'person_id', 'category', "
            "'user_decision', 'vault_note_path', 'uncertainty_reason', 'reviewer_note'."
        ),
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help=(
            "Repo root containing the data/ directory. "
            "Defaults to the repo root inferred from this module's location."
        ),
    )
    return parser.parse_args()


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    args = parse_args()
    root = Path(args.data_root).resolve() if args.data_root else _default_root()
    recorded_at = make_run_id()
    run_id = args.run_id or recorded_at

    try:
        raw = json.loads(args.records_json)
    except json.JSONDecodeError as exc:
        print(f"--records-json is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(raw, list):
        print("--records-json must be a JSON array", file=sys.stderr)
        return 1

    records: list[InboxFileRecord] = []
    for i, obj in enumerate(raw):
        if not isinstance(obj, dict):
            print(f"Record {i}: expected a JSON object, got {type(obj).__name__}", file=sys.stderr)
            return 1
        source_path = obj.get("source_path", "")
        if not source_path:
            print(f"Record {i}: 'source_path' is required and must be non-empty", file=sys.stderr)
            return 1
        confidence_tier = obj.get("confidence_tier", "unknown")
        if confidence_tier not in ("high", "medium", "low", "unknown"):
            print(
                f"Record {i}: 'confidence_tier' must be high/medium/low/unknown, "
                f"got {confidence_tier!r}",
                file=sys.stderr,
            )
            return 1
        user_decision = obj.get("user_decision")
        if user_decision is not None and user_decision not in (
            "accepted",
            "edited",
            "rejected",
            "dismissed",
            "deferred",
        ):
            print(
                f"Record {i}: 'user_decision' must be one of "
                "accepted/edited/rejected/dismissed/deferred or null, "
                f"got {user_decision!r}",
                file=sys.stderr,
            )
            return 1
        records.append(
            InboxFileRecord(
                run_id=run_id,
                source_path=source_path,
                person_id=obj.get("person_id"),
                category=obj.get("category"),
                confidence_tier=confidence_tier,
                proposal_outcome=obj.get("proposal_outcome", ""),
                user_decision=user_decision,
                vault_note_path=obj.get("vault_note_path"),
                uncertainty_reason=obj.get("uncertainty_reason"),
                reviewer_note=obj.get("reviewer_note"),
                recorded_at=recorded_at,
            )
        )

    if not records:
        print("No records to write.", file=sys.stderr)
        return 0

    path = append_inbox_run_feedback(root, records)
    print(f"Appended {len(records)} feedback record(s) to {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
