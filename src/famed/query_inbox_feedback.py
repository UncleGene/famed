#!/usr/bin/env python3
"""Summarize inbox run feedback from data/inbox-run-feedback.jsonl."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from famed.freeform_verification import classify_source_family
from famed.inbox_feedback import InboxFileRecord, feedback_path, read_inbox_run_feedback

_TIERS = ("high", "medium", "low", "unknown")
_DECISIONS = ("accepted", "edited", "rejected", "dismissed", "deferred", "none")
_FIXTURE_GAP_MIN_UNRESOLVED = 2


def _aggregate(records: list[InboxFileRecord]) -> dict:
    tier_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    category_tiers: dict[str, Counter[str]] = {}
    uncertain_unresolved: list[dict] = []
    fixture_gap_groups: dict[tuple[str, str], list[InboxFileRecord]] = {}
    run_ids: set[str] = set()

    for r in records:
        run_ids.add(r.run_id)
        tier_counts[r.confidence_tier] += 1
        key = r.user_decision if r.user_decision is not None else "none"
        decision_counts[key] += 1

        cat = r.category or "(unknown)"
        if cat not in category_tiers:
            category_tiers[cat] = Counter()
        category_tiers[cat][r.confidence_tier] += 1

        if r.confidence_tier in ("low", "unknown") and r.user_decision is None:
            uncertain_unresolved.append(
                {
                    "source_path": r.source_path,
                    "confidence_tier": r.confidence_tier,
                    "proposal_outcome": r.proposal_outcome,
                    "category": r.category,
                    "run_id": r.run_id,
                }
            )
            family = classify_source_family(Path(r.source_path))
            fixture_gap_groups.setdefault((cat, family), []).append(r)

    return {
        "record_count": len(records),
        "run_count": len(run_ids),
        "tier_counts": dict(tier_counts),
        "decision_counts": dict(decision_counts),
        "category_breakdown": {cat: dict(ct) for cat, ct in sorted(category_tiers.items())},
        "uncertain_unresolved": uncertain_unresolved,
        "fixture_gap_candidates": _fixture_gap_candidates(fixture_gap_groups),
    }


def _fixture_gap_candidates(groups: dict[tuple[str, str], list[InboxFileRecord]]) -> list[dict]:
    candidates: list[dict] = []
    for (category, family), group_records in sorted(groups.items()):
        if len(group_records) < _FIXTURE_GAP_MIN_UNRESOLVED:
            continue
        run_count = len({r.run_id for r in group_records})
        reason_counts = Counter(r.uncertainty_reason for r in group_records if r.uncertainty_reason)
        outcome_counts = Counter(r.proposal_outcome for r in group_records if r.proposal_outcome)
        examples = [r.source_path for r in group_records[:3]]
        candidates.append(
            {
                "category": None if category == "(unknown)" else category,
                "source_family": family,
                "unresolved_count": len(group_records),
                "run_count": run_count,
                "top_uncertainty_reasons": dict(reason_counts.most_common(3)),
                "proposal_outcomes": dict(outcome_counts.most_common()),
                "example_source_paths": examples,
                "suggested_action": (
                    "Add or refine manual verification fixture coverage for this "
                    "category/source-family pattern."
                ),
            }
        )
    candidates.sort(
        key=lambda item: (item["unresolved_count"], item["run_count"], item["source_family"]),
        reverse=True,
    )
    return candidates


def _reprocess_source_paths(records: list[InboxFileRecord]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for r in records:
        if r.confidence_tier not in ("low", "unknown") or r.user_decision is not None:
            continue
        if r.source_path in seen:
            continue
        paths.append(r.source_path)
        seen.add(r.source_path)
    return paths


def _format_text(agg: dict, data_file: Path) -> str:
    n = agg["record_count"]
    runs = agg["run_count"]
    lines = [
        "Inbox Run Feedback Summary",
        f"Data: {data_file}",
        f"Records: {n} across {runs} run{'s' if runs != 1 else ''}",
        "",
    ]

    if n == 0:
        lines.append("No records found.")
        return "\n".join(lines)

    lines.append("Confidence tier")
    tiers = agg["tier_counts"]
    for t in _TIERS:
        count = tiers.get(t, 0)
        pct = count * 100 // n if n else 0
        lines.append(f"  {t:<10} {count:>4}  ({pct:>3}%)")

    lines += ["", "User decision"]
    decisions = agg["decision_counts"]
    shown = set(decisions.keys()) | {"accepted", "none"}
    for d in _DECISIONS:
        if d not in shown:
            continue
        count = decisions.get(d, 0)
        pct = count * 100 // n if n else 0
        lines.append(f"  {d:<12} {count:>4}  ({pct:>3}%)")

    cat_bd = agg["category_breakdown"]
    if cat_bd:
        lines += ["", "By category"]
        for cat, ct in cat_bd.items():
            total = sum(ct.values())
            tier_parts = "  ".join(f"{t}: {ct.get(t, 0)}" for t in _TIERS)
            lines.append(f"  {cat:<20} {total:>4} records  —  {tier_parts}")

    uncertain = agg["uncertain_unresolved"]
    if uncertain:
        top5 = uncertain[:5]
        n_total = len(uncertain)
        n_top = len(top5)
        header = f"Uncertain and unresolved (low/unknown, no decision) — top {n_top} of {n_total}"
        lines += ["", header]
        for u in top5:
            lines.append(
                f"  [{u['confidence_tier']}]  {u['source_path']}"
                + (f"  —  {u['proposal_outcome']}" if u["proposal_outcome"] else "")
            )

    gaps = agg["fixture_gap_candidates"]
    if gaps:
        lines += ["", "Fixture gap candidates"]
        for gap in gaps:
            category = gap["category"] or "(unknown)"
            lines.append(
                f"  {category:<20} {gap['source_family']:<28} "
                f"{gap['unresolved_count']:>4} unresolved across {gap['run_count']} run"
                f"{'s' if gap['run_count'] != 1 else ''}"
            )
            reasons = gap["top_uncertainty_reasons"]
            if reasons:
                reason_text = "; ".join(f"{reason} ({count})" for reason, count in reasons.items())
                lines.append(f"    reasons: {reason_text}")
            examples = gap["example_source_paths"]
            if examples:
                lines.append(f"    examples: {', '.join(examples)}")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize inbox run feedback from data/inbox-run-feedback.jsonl."
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 datetime; include only records with recorded_at >= this value.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Filter to a single run by run_id.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format: human-readable text (default) or machine-readable JSON.",
    )
    parser.add_argument(
        "--reprocess-list",
        action="store_true",
        help=(
            "Emit source paths for unresolved low/unknown-confidence records in "
            "the selected --run-id instead of the summary."
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
    if args.reprocess_list and args.run_id is None:
        print(
            "--reprocess-list requires --run-id so the batch is scoped to one run", file=sys.stderr
        )
        return 1
    if args.since is not None:
        try:
            datetime.fromisoformat(args.since)
        except ValueError:
            print(f"--since must be an ISO-8601 datetime, got {args.since!r}", file=sys.stderr)
            return 1
    root = Path(args.data_root).resolve() if args.data_root else _default_root()
    records = read_inbox_run_feedback(root, run_id=args.run_id, since=args.since)
    if args.reprocess_list:
        paths = _reprocess_source_paths(records)
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "count": len(paths),
                        "reprocess_source_paths": paths,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif paths:
            print("\n".join(paths))
        else:
            print(f"No unresolved low/unknown-confidence files found for run_id {args.run_id}.")
        return 0

    agg = _aggregate(records)

    if args.format == "json":
        print(json.dumps(agg, ensure_ascii=False, indent=2))
    else:
        print(_format_text(agg, feedback_path(root)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
