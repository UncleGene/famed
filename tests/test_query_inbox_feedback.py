"""Tests for inbox feedback analytics."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from famed.inbox_feedback import InboxFileRecord, append_inbox_run_feedback
from famed.query_inbox_feedback import (
    _aggregate,
    _format_text,
    _reprocess_source_paths,
    feedback_path,
)


def _r(
    *,
    run_id: str = "run-A",
    source_path: str = "data/inbox/dummy_alice/lab.pdf",
    confidence_tier: str = "high",
    proposal_outcome: str = "high_confidence_ready_for_canonicalizer",
    user_decision: str | None = "accepted",
    category: str | None = "labs",
    recorded_at: str = "2023-06-29T10:00:00+00:00",
    person_id: str | None = "dummy_alice",
    uncertainty_reason: str | None = None,
) -> InboxFileRecord:
    return InboxFileRecord(
        run_id=run_id,
        source_path=source_path,
        person_id=person_id,
        category=category,
        confidence_tier=confidence_tier,  # type: ignore[arg-type]
        proposal_outcome=proposal_outcome,
        user_decision=user_decision,  # type: ignore[arg-type]
        vault_note_path=None,
        uncertainty_reason=uncertainty_reason,
        reviewer_note=None,
        recorded_at=recorded_at,
    )


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------


def test_aggregate_empty() -> None:
    agg = _aggregate([])
    assert agg["record_count"] == 0
    assert agg["run_count"] == 0
    assert agg["tier_counts"] == {}
    assert agg["decision_counts"] == {}
    assert agg["uncertain_unresolved"] == []


def test_aggregate_tier_counts() -> None:
    records = [
        _r(confidence_tier="high"),
        _r(confidence_tier="high"),
        _r(confidence_tier="medium"),
        _r(confidence_tier="low", user_decision=None),
    ]
    agg = _aggregate(records)
    assert agg["tier_counts"] == {"high": 2, "medium": 1, "low": 1}
    assert agg["record_count"] == 4


def test_aggregate_decision_counts_none_decision() -> None:
    records = [
        _r(user_decision="accepted"),
        _r(user_decision="edited"),
        _r(user_decision=None),
    ]
    agg = _aggregate(records)
    assert agg["decision_counts"]["accepted"] == 1
    assert agg["decision_counts"]["edited"] == 1
    assert agg["decision_counts"]["none"] == 1


def test_aggregate_run_count() -> None:
    records = [
        _r(run_id="run-1"),
        _r(run_id="run-1"),
        _r(run_id="run-2"),
    ]
    agg = _aggregate(records)
    assert agg["run_count"] == 2
    assert agg["record_count"] == 3


def test_aggregate_category_breakdown() -> None:
    records = [
        _r(category="labs", confidence_tier="high"),
        _r(category="labs", confidence_tier="medium"),
        _r(category="history", confidence_tier="low", user_decision=None),
        _r(category=None, confidence_tier="unknown", user_decision=None),
    ]
    agg = _aggregate(records)
    assert agg["category_breakdown"]["labs"] == {"high": 1, "medium": 1}
    assert agg["category_breakdown"]["history"] == {"low": 1}
    assert agg["category_breakdown"]["(unknown)"] == {"unknown": 1}


def test_aggregate_uncertain_unresolved() -> None:
    records = [
        _r(confidence_tier="high", user_decision="accepted"),
        _r(confidence_tier="low", user_decision=None, source_path="uncertain.pdf"),
        _r(confidence_tier="unknown", user_decision=None, source_path="unknown.pdf"),
        _r(confidence_tier="low", user_decision="rejected", source_path="resolved.pdf"),
    ]
    agg = _aggregate(records)
    paths = {u["source_path"] for u in agg["uncertain_unresolved"]}
    assert "uncertain.pdf" in paths
    assert "unknown.pdf" in paths
    assert "resolved.pdf" not in paths  # has a decision
    assert len(agg["uncertain_unresolved"]) == 2


def test_aggregate_fixture_gap_candidates_group_repeated_uncertainty() -> None:
    records = [
        _r(
            run_id="run-1",
            source_path="data/inbox/dummy_alice/Comprehensive Metabolic Panel.pdf",
            confidence_tier="low",
            proposal_outcome="needs_user_verification",
            user_decision=None,
            uncertainty_reason="missing reference range",
        ),
        _r(
            run_id="run-2",
            source_path="data/inbox/dummy_bob/POC Group Chem Panel.pdf",
            confidence_tier="unknown",
            proposal_outcome="needs_user_verification",
            user_decision=None,
            uncertainty_reason="missing reference range",
        ),
        _r(
            source_path="data/inbox/dummy_alice/Visit Summary.pdf",
            confidence_tier="low",
            user_decision=None,
            category="history",
            uncertainty_reason="unclear category split",
        ),
    ]

    agg = _aggregate(records)

    assert agg["fixture_gap_candidates"] == [
        {
            "category": "labs",
            "source_family": "lab_cmp",
            "unresolved_count": 2,
            "run_count": 2,
            "top_uncertainty_reasons": {"missing reference range": 2},
            "proposal_outcomes": {"needs_user_verification": 2},
            "example_source_paths": [
                "data/inbox/dummy_alice/Comprehensive Metabolic Panel.pdf",
                "data/inbox/dummy_bob/POC Group Chem Panel.pdf",
            ],
            "suggested_action": (
                "Add or refine manual verification fixture coverage for this "
                "category/source-family pattern."
            ),
        }
    ]


def test_aggregate_fixture_gap_candidates_omit_single_uncertain_file() -> None:
    records = [
        _r(
            source_path="data/inbox/dummy_alice/Visit Summary.pdf",
            confidence_tier="low",
            user_decision=None,
            category="history",
        )
    ]

    agg = _aggregate(records)

    assert len(agg["uncertain_unresolved"]) == 1
    assert agg["fixture_gap_candidates"] == []


# ---------------------------------------------------------------------------
# _reprocess_source_paths
# ---------------------------------------------------------------------------


def test_reprocess_source_paths_selects_unresolved_low_and_unknown_once() -> None:
    records = [
        _r(source_path="ready.pdf", confidence_tier="high", user_decision="accepted"),
        _r(source_path="low.pdf", confidence_tier="low", user_decision=None),
        _r(source_path="unknown.pdf", confidence_tier="unknown", user_decision=None),
        _r(source_path="resolved.pdf", confidence_tier="low", user_decision="deferred"),
        _r(source_path="low.pdf", confidence_tier="low", user_decision=None),
    ]

    assert _reprocess_source_paths(records) == ["low.pdf", "unknown.pdf"]


# ---------------------------------------------------------------------------
# _format_text
# ---------------------------------------------------------------------------


def test_format_text_empty(tmp_path: Path) -> None:
    agg = _aggregate([])
    text = _format_text(agg, feedback_path(tmp_path))
    assert "No records found" in text


def test_format_text_shows_tier_distribution(tmp_path: Path) -> None:
    records = [_r(confidence_tier="high")] * 3 + [_r(confidence_tier="low", user_decision=None)]
    agg = _aggregate(records)
    text = _format_text(agg, feedback_path(tmp_path))
    assert "high" in text
    assert "low" in text
    assert "75%" in text or "3" in text


def test_format_text_shows_uncertain_unresolved(tmp_path: Path) -> None:
    records = [_r(confidence_tier="low", user_decision=None, source_path="mystery.pdf")]
    agg = _aggregate(records)
    text = _format_text(agg, feedback_path(tmp_path))
    assert "mystery.pdf" in text


def test_format_text_shows_fixture_gap_candidates(tmp_path: Path) -> None:
    records = [
        _r(
            source_path="data/inbox/dummy_alice/Comprehensive Metabolic Panel.pdf",
            confidence_tier="low",
            user_decision=None,
            uncertainty_reason="missing reference range",
        ),
        _r(
            source_path="data/inbox/dummy_bob/POC Group Chem Panel.pdf",
            confidence_tier="unknown",
            user_decision=None,
            uncertainty_reason="missing reference range",
        ),
    ]
    agg = _aggregate(records)
    text = _format_text(agg, feedback_path(tmp_path))
    assert "Fixture gap candidates" in text
    assert "lab_cmp" in text
    assert "missing reference range (2)" in text


# ---------------------------------------------------------------------------
# main() via read path
# ---------------------------------------------------------------------------


def test_main_empty_returns_0(tmp_path: Path, capsys) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/query_inbox_feedback.py", "--data-root", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "No records found" in result.stdout


def test_main_json_format(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [_r(confidence_tier="high"), _r(confidence_tier="low", user_decision=None)],
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["record_count"] == 2
    assert "tier_counts" in data
    assert "decision_counts" in data
    assert "category_breakdown" in data
    assert "uncertain_unresolved" in data
    assert "fixture_gap_candidates" in data


def test_main_run_id_filter(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [_r(run_id="run-X"), _r(run_id="run-Y")],
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--run-id",
            "run-X",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["record_count"] == 1
    assert data["run_count"] == 1


def test_main_since_filter(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [
            _r(recorded_at="2023-01-01T00:00:00+00:00"),
            _r(recorded_at="2023-06-29T10:00:00+00:00"),
        ],
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--since",
            "2023-06-01T00:00:00+00:00",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["record_count"] == 1


def test_main_rejects_invalid_since(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--since",
            "yesterday",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode != 0
    assert "ISO-8601" in result.stderr


def test_main_text_output_has_expected_sections(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [_r(confidence_tier="high"), _r(confidence_tier="medium", user_decision="edited")],
    )
    result = subprocess.run(
        [sys.executable, "scripts/query_inbox_feedback.py", "--data-root", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "Confidence tier" in result.stdout
    assert "User decision" in result.stdout
    assert "By category" in result.stdout


def test_main_reprocess_list_requires_run_id(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--reprocess-list",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode != 0
    assert "requires --run-id" in result.stderr


def test_main_reprocess_list_outputs_paths_for_selected_run(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [
            _r(run_id="run-X", source_path="x-low.pdf", confidence_tier="low", user_decision=None),
            _r(
                run_id="run-X",
                source_path="x-unknown.pdf",
                confidence_tier="unknown",
                user_decision=None,
            ),
            _r(
                run_id="run-X",
                source_path="x-done.pdf",
                confidence_tier="low",
                user_decision="edited",
            ),
            _r(run_id="run-Y", source_path="y-low.pdf", confidence_tier="low", user_decision=None),
        ],
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--run-id",
            "run-X",
            "--reprocess-list",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["x-low.pdf", "x-unknown.pdf"]


def test_main_reprocess_list_handles_empty_selection(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [
            _r(
                run_id="run-X",
                source_path="x-done.pdf",
                confidence_tier="low",
                user_decision="edited",
            )
        ],
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--run-id",
            "run-X",
            "--reprocess-list",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "No unresolved low/unknown-confidence files found for run_id run-X." in result.stdout


def test_main_reprocess_list_json_output(tmp_path: Path) -> None:
    append_inbox_run_feedback(
        tmp_path,
        [_r(run_id="run-X", source_path="x-low.pdf", confidence_tier="low", user_decision=None)],
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/query_inbox_feedback.py",
            "--data-root",
            str(tmp_path),
            "--run-id",
            "run-X",
            "--reprocess-list",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == {
        "run_id": "run-X",
        "count": 1,
        "reprocess_source_paths": ["x-low.pdf"],
    }
