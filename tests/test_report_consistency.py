from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from famed import report_consistency
from famed.pgx_coverage import GeneResult, InterpretationState
from famed.report_consistency import (
    ReportConsistencyError,
    assert_report_consistency,
    find_genetics_contradictions,
)


def _write_genetics(vault_root: Path, rows: str) -> None:
    reports = vault_root / "Records" / "dummy_alice" / "Reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "genetics.md").write_text(
        "---\nperson_id: dummy_alice\n---\n\n## Medication Genes (wallet card)\n\n"
        "| Gene | Result | Affects current medications |\n| --- | --- | --- |\n" + rows,
        encoding="utf-8",
    )


def _patch_calls(monkeypatch: pytest.MonkeyPatch, state: InterpretationState) -> None:
    monkeypatch.setattr(
        report_consistency,
        "build_gene_results",
        lambda *_a: {
            "CYP2C19": GeneResult("CYP2C19", state, "metabolizer", None, 3, 3),
        },
    )


def test_no_contradiction_without_genetics_report(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    assert find_genetics_contradictions(conn, tmp_path, "dummy_alice", tmp_path / "r.db") == []


def test_assert_report_consistency_raises_on_stale_gene_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_genetics(tmp_path, "| CYP2C19 | normal metabolizer | — |\n")
    _patch_calls(monkeypatch, "poor metabolizer")

    with pytest.raises(ReportConsistencyError, match="CYP2C19"):
        assert_report_consistency(sqlite3.connect(":memory:"), tmp_path, "dummy_alice", tmp_path)


def test_assert_report_consistency_passes_when_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_genetics(tmp_path, "| CYP2C19 | normal metabolizer | — |\n")
    _patch_calls(monkeypatch, "normal metabolizer")

    assert_report_consistency(sqlite3.connect(":memory:"), tmp_path, "dummy_alice", tmp_path)


def test_genetics_contradiction_detected_when_rendered_result_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """genetics.md must agree with freshly computed gene results (ADR-025 #4, ADR-031)."""
    vault_root = tmp_path / "vault"
    reports = vault_root / "Records" / "dummy_alice" / "Reports"
    reports.mkdir(parents=True)
    (reports / "genetics.md").write_text(
        "---\nperson_id: dummy_alice\n---\n\n## Medication Genes (wallet card)\n\n"
        "| Gene | Result | Affects current medications |\n| --- | --- | --- |\n"
        "| CYP2C19 | normal metabolizer | — |\n| SLCO1B1 | normal function | — |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        report_consistency,
        "build_gene_results",
        lambda *_a: {
            "CYP2C19": GeneResult("CYP2C19", "poor metabolizer", "metabolizer", None, 3, 3),
            "SLCO1B1": GeneResult("SLCO1B1", "normal function", "function", None, 1, 1),
        },
    )

    found = find_genetics_contradictions(
        sqlite3.connect(":memory:"), vault_root, "dummy_alice", tmp_path / "ref.db"
    )

    assert [(c.gene, c.rendered_state, c.canonical_state) for c in found] == [
        ("CYP2C19", "normal metabolizer", "poor metabolizer")
    ]
