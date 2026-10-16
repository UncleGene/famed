"""Persist and read per-file inbox run feedback records.

Each `fm-ingest-inbox` run appends one JSON line per processed file to
``data/inbox-run-feedback.jsonl``.  Records are grouped by ``run_id`` (an
ISO-8601 timestamp chosen by the agent at run start) and capture what the
AI proposed, what the user decided, and why any file was uncertain.

This closes the gap between ``fm-inbox-batch-processor`` step 13 / ``fm-ingest-inbox``
step 9 (both instruct the agent to record feedback) and durable storage.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

ConfidenceTier = Literal["high", "medium", "low", "unknown"]
UserDecision = Literal["accepted", "edited", "rejected", "dismissed", "deferred"] | None


@dataclass(frozen=True)
class InboxFileRecord:
    """Outcome record for one file in one inbox run."""

    run_id: str
    source_path: str
    person_id: str | None
    category: str | None
    confidence_tier: ConfidenceTier
    proposal_outcome: str
    user_decision: UserDecision
    vault_note_path: str | None
    uncertainty_reason: str | None
    reviewer_note: str | None
    recorded_at: str


def feedback_path(root: Path) -> Path:
    return root / "data" / "inbox-run-feedback.jsonl"


def append_inbox_run_feedback(root: Path, records: list[InboxFileRecord]) -> Path:
    """Append *records* to the JSONL feedback file; create it if absent."""
    path = feedback_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    return path


def read_inbox_run_feedback(
    root: Path,
    *,
    run_id: str | None = None,
    since: str | None = None,
) -> list[InboxFileRecord]:
    """Return records from the feedback file, optionally filtered.

    :param run_id: Return only records matching this exact run_id.
    :param since: Return only records whose ``recorded_at`` >= this ISO string.
    """
    path = feedback_path(root)
    if not path.exists():
        return []

    records: list[InboxFileRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id is not None and obj.get("run_id") != run_id:
                continue
            if since is not None:
                raw_at = obj.get("recorded_at")
                if isinstance(raw_at, str):
                    try:
                        if datetime.fromisoformat(raw_at) < datetime.fromisoformat(since):
                            continue
                    except ValueError:
                        pass  # unparseable timestamp → include the record
                # null or missing recorded_at → include (age unknown)
            records.append(
                InboxFileRecord(
                    run_id=obj.get("run_id", ""),
                    source_path=obj.get("source_path", ""),
                    person_id=obj.get("person_id"),
                    category=obj.get("category"),
                    confidence_tier=obj.get("confidence_tier", "unknown"),
                    proposal_outcome=obj.get("proposal_outcome", ""),
                    user_decision=obj.get("user_decision"),
                    vault_note_path=obj.get("vault_note_path"),
                    uncertainty_reason=obj.get("uncertainty_reason"),
                    reviewer_note=obj.get("reviewer_note"),
                    recorded_at=obj.get("recorded_at", ""),
                )
            )
    return records


def make_run_id() -> str:
    """Return an ISO-8601 run_id stamped in UTC."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()
