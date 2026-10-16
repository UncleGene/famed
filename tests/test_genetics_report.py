from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

from famed.carrier_screening import CarrierGeneResult
from famed.genetics_report import DnaImport, render_genetics_report, write_genetics_report
from famed.pgx_coverage import GeneResult
from famed.setup import SCHEMA_SQL


def _gene(gene: str, state: str, vocabulary: str = "metabolizer") -> GeneResult:
    return GeneResult(gene, cast(Any, state), cast(Any, vocabulary), "*1/*2", 5, 6)


def _render(carriers: list[CarrierGeneResult]) -> str:
    return render_genetics_report(
        person_id="dummy_alice",
        display_name="Dummy Alice",
        imports=[DnaImport("23andMe", 631455, "v5", "2023-06-11")],
        gene_results={
            "CYP2C19": _gene("CYP2C19", "normal metabolizer"),
            "SLCO1B1": _gene("SLCO1B1", "decreased function", "function"),
            "CYP2D6": _gene("CYP2D6", "not interpretable", "uninterpreted"),
        },
        medications_by_gene={"CYP2C19": ["examplezole"]},
        carriers=carriers,
    )


def test_genetics_report_is_gene_centric_wallet_card() -> None:
    """Every callable gene is listed, not only those for today's medications (ADR-031)."""
    report = _render([])

    assert "| 23andMe | 631,455 | v5 | 2023-06-11 |" in report
    assert "| CYP2C19 | normal metabolizer | examplezole |" in report
    assert "| SLCO1B1 | decreased function | — |" in report
    assert "| CYP2D6 |" not in report
    assert "not interpreted from this chip: CYP2D6" in report
    assert "[[Records/dummy_alice/Reports/health-advisory#Genetic Risk Flags" in report


def test_carrier_section_only_when_a_carrier_is_found() -> None:
    clear = CarrierGeneResult("CFTR", "cystic fibrosis", "clear", "reason", ())
    carrier = CarrierGeneResult("HBB", "sickle cell", "carrier", "reason", ())

    without = _render([clear])
    with_carrier = _render([clear, carrier])

    assert "## Carrier Screening" in without
    assert "| CFTR |" not in without
    assert "No carrier variant observed" in without
    assert "| HBB | sickle cell | carrier |" in with_carrier
    assert "| CFTR |" not in with_carrier


def test_write_genetics_report_skips_people_without_dna(tmp_path: Path) -> None:
    vault_root = tmp_path / "famed-vault"
    reports = vault_root / "Records" / "dummy_alice" / "Reports"
    reports.mkdir(parents=True)
    (reports / "genetics.md").write_text("stale\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    assert write_genetics_report(conn, vault_root, "dummy_alice", tmp_path / "ref.db") is None
    assert not (reports / "genetics.md").exists()
