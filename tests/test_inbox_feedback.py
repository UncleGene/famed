"""Tests for inbox run feedback persistence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from famed.inbox_feedback import (
    ConfidenceTier,
    InboxFileRecord,
    UserDecision,
    append_inbox_run_feedback,
    feedback_path,
    make_run_id,
    read_inbox_run_feedback,
)


def _record(
    *,
    run_id: str = "2023-06-29T10:00:00+00:00",
    source_path: str = "data/inbox/dummy_alice/lab.pdf",
    person_id: str | None = "dummy_alice",
    category: str | None = "labs",
    confidence_tier: ConfidenceTier = "high",
    proposal_outcome: str = "high_confidence_ready_for_canonicalizer",
    user_decision: UserDecision = "accepted",
    vault_note_path: str | None = "Records/dummy_alice/Labs/2023-06-29-panel.md",
    uncertainty_reason: str | None = None,
    reviewer_note: str | None = None,
    recorded_at: str = "2023-06-29T10:00:05+00:00",
) -> InboxFileRecord:
    return InboxFileRecord(
        run_id=run_id,
        source_path=source_path,
        person_id=person_id,
        category=category,
        confidence_tier=confidence_tier,
        proposal_outcome=proposal_outcome,
        user_decision=user_decision,
        vault_note_path=vault_note_path,
        uncertainty_reason=uncertainty_reason,
        reviewer_note=reviewer_note,
        recorded_at=recorded_at,
    )


# ---------------------------------------------------------------------------
# feedback_path
# ---------------------------------------------------------------------------


def test_feedback_path(tmp_path: Path) -> None:
    assert feedback_path(tmp_path) == tmp_path / "data" / "inbox-run-feedback.jsonl"


# ---------------------------------------------------------------------------
# append_inbox_run_feedback
# ---------------------------------------------------------------------------


def test_append_creates_file(tmp_path: Path) -> None:
    r = _record()
    path = append_inbox_run_feedback(tmp_path, [r])
    assert path.exists()


def test_append_writes_valid_jsonl(tmp_path: Path) -> None:
    r = _record()
    append_inbox_run_feedback(tmp_path, [r])
    lines = feedback_path(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["source_path"] == r.source_path
    assert obj["confidence_tier"] == "high"


def test_append_multiple_records(tmp_path: Path) -> None:
    r1 = _record(source_path="data/inbox/dummy_alice/lab1.pdf")
    r2 = _record(source_path="data/inbox/dummy_alice/lab2.pdf")
    append_inbox_run_feedback(tmp_path, [r1, r2])
    lines = feedback_path(tmp_path).read_text().splitlines()
    assert len(lines) == 2


def test_append_does_not_overwrite(tmp_path: Path) -> None:
    """Second append adds new lines; prior lines are untouched."""
    r1 = _record(run_id="run-1", source_path="a.pdf")
    r2 = _record(run_id="run-2", source_path="b.pdf")
    append_inbox_run_feedback(tmp_path, [r1])
    append_inbox_run_feedback(tmp_path, [r2])
    lines = feedback_path(tmp_path).read_text().splitlines()
    assert len(lines) == 2
    run_ids = [json.loads(line)["run_id"] for line in lines]
    assert run_ids == ["run-1", "run-2"]


def test_append_creates_parent_dir(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "root"
    append_inbox_run_feedback(root, [_record()])
    assert feedback_path(root).exists()


# ---------------------------------------------------------------------------
# read_inbox_run_feedback
# ---------------------------------------------------------------------------


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_inbox_run_feedback(tmp_path) == []


def test_read_roundtrip(tmp_path: Path) -> None:
    r = _record(
        uncertainty_reason="no MRN in document",
        reviewer_note="confirmed dummy_alice by DOB",
        vault_note_path=None,
        user_decision="edited",
    )
    append_inbox_run_feedback(tmp_path, [r])
    results = read_inbox_run_feedback(tmp_path)
    assert len(results) == 1
    assert results[0] == r


def test_read_filter_by_run_id(tmp_path: Path) -> None:
    r1 = _record(run_id="run-A", source_path="a.pdf")
    r2 = _record(run_id="run-B", source_path="b.pdf")
    append_inbox_run_feedback(tmp_path, [r1, r2])
    results = read_inbox_run_feedback(tmp_path, run_id="run-A")
    assert len(results) == 1
    assert results[0].source_path == "a.pdf"


def test_read_filter_by_since(tmp_path: Path) -> None:
    r_old = _record(run_id="old", recorded_at="2023-01-01T00:00:00+00:00")
    r_new = _record(run_id="new", recorded_at="2023-06-29T10:00:00+00:00")
    append_inbox_run_feedback(tmp_path, [r_old, r_new])
    results = read_inbox_run_feedback(tmp_path, since="2023-06-01T00:00:00+00:00")
    assert len(results) == 1
    assert results[0].run_id == "new"


def test_read_since_with_z_suffix_timezone(tmp_path: Path) -> None:
    """since filter must correctly handle Z vs +00:00 format for the same instant."""
    r = _record(run_id="r", recorded_at="2023-06-29T10:00:00+00:00")
    append_inbox_run_feedback(tmp_path, [r])
    # Z and +00:00 are the same UTC offset — record should be included
    results = read_inbox_run_feedback(tmp_path, since="2023-06-29T09:00:00Z")
    assert len(results) == 1


def test_read_since_null_recorded_at_does_not_crash(tmp_path: Path) -> None:
    """A JSON null for recorded_at must not raise TypeError."""
    path = feedback_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"run_id":"r","source_path":"x","confidence_tier":"high","proposal_outcome":"","recorded_at":null}\n'
    )
    results = read_inbox_run_feedback(tmp_path, since="2023-01-01T00:00:00+00:00")
    assert len(results) == 1  # null recorded_at → age unknown → included


def test_read_since_missing_recorded_at_includes_record(tmp_path: Path) -> None:
    """A record missing recorded_at must not be silently dropped by the since filter."""
    path = feedback_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"run_id":"r","source_path":"x","confidence_tier":"high","proposal_outcome":""}\n'
    )
    results = read_inbox_run_feedback(tmp_path, since="2023-01-01T00:00:00+00:00")
    assert len(results) == 1  # missing recorded_at → age unknown → included


def test_read_skips_malformed_lines(tmp_path: Path) -> None:
    path = feedback_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'not-json\n{"run_id":"r","source_path":"x","confidence_tier":"high","proposal_outcome":"","recorded_at":"2023-06-29T00:00:00+00:00"}\n'
    )
    results = read_inbox_run_feedback(tmp_path)
    assert len(results) == 1


def test_read_skips_blank_lines(tmp_path: Path) -> None:
    r = _record()
    append_inbox_run_feedback(tmp_path, [r])
    path = feedback_path(tmp_path)
    path.write_text("\n" + path.read_text() + "\n")
    results = read_inbox_run_feedback(tmp_path)
    assert len(results) == 1


def test_null_optional_fields_roundtrip(tmp_path: Path) -> None:
    r = _record(
        person_id=None,
        category=None,
        user_decision=None,
        vault_note_path=None,
        uncertainty_reason=None,
        reviewer_note=None,
    )
    append_inbox_run_feedback(tmp_path, [r])
    results = read_inbox_run_feedback(tmp_path)
    assert results[0].person_id is None
    assert results[0].user_decision is None


# ---------------------------------------------------------------------------
# make_run_id
# ---------------------------------------------------------------------------


def test_make_run_id_is_iso_format() -> None:
    rid = make_run_id()
    from datetime import datetime

    # Should parse without error and be timezone-aware
    dt = datetime.fromisoformat(rid)
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# CLI — record_inbox_feedback.py
# ---------------------------------------------------------------------------


def test_cli_appends_records(tmp_path: Path) -> None:
    records_json = json.dumps(
        [
            {
                "source_path": "data/inbox/dummy_alice/lab.pdf",
                "confidence_tier": "high",
                "proposal_outcome": "high_confidence_ready_for_canonicalizer",
                "person_id": "dummy_alice",
                "category": "labs",
                "user_decision": "accepted",
                "vault_note_path": "Records/dummy_alice/Labs/2023-06-29-panel.md",
            }
        ]
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/record_inbox_feedback.py",
            "--run-id",
            "test-run-001",
            "--records-json",
            records_json,
            "--data-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr
    written = read_inbox_run_feedback(tmp_path)
    assert len(written) == 1
    assert written[0].run_id == "test-run-001"
    assert written[0].person_id == "dummy_alice"


def test_cli_rejects_invalid_confidence_tier(tmp_path: Path) -> None:
    records_json = json.dumps(
        [{"source_path": "x.pdf", "confidence_tier": "SUPER_HIGH", "proposal_outcome": ""}]
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/record_inbox_feedback.py",
            "--records-json",
            records_json,
            "--data-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode != 0


def test_cli_rejects_missing_source_path(tmp_path: Path) -> None:
    records_json = json.dumps([{"confidence_tier": "high", "proposal_outcome": ""}])
    result = subprocess.run(
        [
            sys.executable,
            "scripts/record_inbox_feedback.py",
            "--records-json",
            records_json,
            "--data-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode != 0


def test_cli_rejects_invalid_json(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/record_inbox_feedback.py",
            "--records-json",
            "not-json",
            "--data-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode != 0
