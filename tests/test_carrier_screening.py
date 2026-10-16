"""Tests for conservative carrier-screening report generation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from famed.annotation_cache import open_annotation_db
from famed.carrier_screening import (
    build_carrier_screening_results,
)
from famed.setup import SCHEMA_SQL


def _user_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_genotype(
    conn: sqlite3.Connection,
    person_id: str,
    rsid: str,
    allele1: str,
    allele2: str,
    *,
    source_path: str = "data/processed/dummy_alice/Genes/genome.txt",
) -> None:
    conn.execute(
        """
        INSERT INTO genotypes
            (id, person_id, rsid, chromosome, position, allele1, allele2, provider, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{person_id}-{rsid}-{source_path}",
            person_id,
            rsid,
            "1",
            100,
            allele1,
            allele2,
            "23andme",
            source_path,
        ),
    )


def _insert_clinvar(
    ref_db_path: Path,
    *,
    rsid: str,
    gene: str,
    significance: str,
    alternate_allele: str,
    condition: str = "Example condition",
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO clinvar_variants
            (rsid, variation_id, significance, condition, review_status,
             last_evaluated, gene, chromosome, position, reference_allele, alternate_allele)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rsid,
            "var1",
            significance,
            condition,
            "reviewed by expert panel",
            "2021-01-01",
            gene,
            "1",
            100,
            "A",
            alternate_allele,
        ),
    )
    conn.commit()
    conn.close()


def _state_for(results: list[Any], gene: str) -> str:
    return next(result.state for result in results if result.gene == gene)


def test_carrier_when_alternate_allele_observed(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db, rsid="rs123", gene="CFTR", significance="Pathogenic", alternate_allele="G"
    )
    _insert_genotype(user, "dummy_alice", "rs123", "A", "G")

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    assert _state_for(results, "CFTR") == "carrier"
    cftr = next(result for result in results if result.gene == "CFTR")
    assert cftr.variants[0].state == "alternate allele observed"


def test_carrier_source_path_points_to_alternate_allele_source(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db, rsid="rs123", gene="CFTR", significance="Pathogenic", alternate_allele="G"
    )
    _insert_genotype(
        user,
        "dummy_alice",
        "rs123",
        "A",
        "A",
        source_path="data/processed/dummy_alice/Genes/first.txt",
    )
    _insert_genotype(
        user,
        "dummy_alice",
        "rs123",
        "A",
        "G",
        source_path="data/processed/dummy_alice/Genes/second.txt",
    )

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    cftr = next(result for result in results if result.gene == "CFTR")
    assert cftr.variants[0].source_path == "data/processed/dummy_alice/Genes/second.txt"


def test_clear_only_when_all_pathogenic_variants_measured_without_alternate(
    tmp_path: Path,
) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db, rsid="rs123", gene="HBB", significance="Likely pathogenic", alternate_allele="T"
    )
    _insert_clinvar(
        ref_db, rsid="rs456", gene="HBB", significance="Pathogenic", alternate_allele="G"
    )
    _insert_genotype(user, "dummy_alice", "rs123", "A", "A")
    _insert_genotype(user, "dummy_alice", "rs456", "C", "C")

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    assert _state_for(results, "HBB") == "clear"


def test_not_callable_when_pathogenic_variant_missing(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db, rsid="rs123", gene="GJB2", significance="Pathogenic", alternate_allele="T"
    )
    _insert_clinvar(
        ref_db, rsid="rs456", gene="GJB2", significance="Pathogenic", alternate_allele="G"
    )
    _insert_genotype(user, "dummy_alice", "rs123", "A", "A")

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    assert _state_for(results, "GJB2") == "not callable from this chip"


def test_not_callable_when_alternate_allele_uninterpretable(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db, rsid="rs123", gene="CFTR", significance="Pathogenic", alternate_allele="-"
    )
    _insert_genotype(user, "dummy_alice", "rs123", "A", "A")

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    assert _state_for(results, "CFTR") == "not callable from this chip"
    cftr = next(result for result in results if result.gene == "CFTR")
    assert cftr.variants[0].state == "uninterpretable reference allele"


def test_not_callable_for_multibase_alternate_allele(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db, rsid="rs123", gene="CFTR", significance="Pathogenic", alternate_allele="CTT"
    )
    _insert_genotype(user, "dummy_alice", "rs123", "C", "C")

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    assert _state_for(results, "CFTR") == "not callable from this chip"
    cftr = next(result for result in results if result.gene == "CFTR")
    assert cftr.variants[0].state == "uninterpretable reference allele"


def test_benign_clinvar_rows_do_not_create_clear_call(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="HEXA", significance="Benign", alternate_allele="G")
    _insert_genotype(user, "dummy_alice", "rs123", "A", "A")

    results = build_carrier_screening_results(user, ref_db, "dummy_alice")

    assert _state_for(results, "HEXA") == "not callable from this chip"
