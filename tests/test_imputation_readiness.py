"""Tests for offline DNA imputation-readiness reporting."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from famed.dna_sync import DEFAULT_IMPUTED_QUALITY_FLOOR
from famed.generate_imputation_readiness import main as generate_imputation_readiness_main
from famed.imputation_readiness import (
    build_imputation_readiness,
    render_imputation_readiness_report,
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
    *,
    source_kind: str = "genotyped",
    quality: float | None = None,
    genome_build: str | None = "GRCh37",
) -> None:
    conn.execute(
        """
        INSERT INTO genotypes
            (id, person_id, rsid, chromosome, position, allele1, allele2, provider,
             source_path, source_kind, quality, genome_build)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{person_id}-{rsid}-{source_kind}",
            person_id,
            rsid,
            "1",
            100,
            "A",
            "G",
            "vcf" if source_kind == "imputed" else "23andme",
            "data/processed/dummy_alice/Genes/genome.txt",
            source_kind,
            quality,
            genome_build,
        ),
    )


def _write_person_note(vault_root: Path, person_id: str = "dummy_alice") -> None:
    (vault_root / "Persons").mkdir(parents=True)
    (vault_root / "Persons" / f"{person_id}.md").write_text(
        "---\n"
        f"person_id: {person_id}\n"
        "record_type: person\n"
        "display_name: Dummy Alice Example\n"
        "---\n"
        "# Dummy Alice Example\n",
        encoding="utf-8",
    )


def test_no_dna_state() -> None:
    conn = _user_conn()

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "no DNA data"
    assert readiness.total_variants == 0


def test_direct_only_recommends_imputation_before_prs() -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs1")

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "imputation recommended before PRS"
    assert readiness.direct_variants == 1
    assert readiness.imputed_variants == 0


def test_direct_only_missing_build_needs_metadata_review_before_imputation() -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs1", genome_build=None)

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "DNA metadata needs review"
    assert readiness.missing_genome_build_variants == 1


def test_imputed_rows_need_review_when_metadata_missing() -> None:
    conn = _user_conn()
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs1",
        source_kind="imputed",
        quality=None,
        genome_build=None,
    )

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "imputed data needs metadata review"
    assert readiness.imputed_missing_quality == 1
    assert readiness.missing_genome_build_variants == 1


def test_blank_imputed_build_needs_metadata_review() -> None:
    conn = _user_conn()
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs1",
        source_kind="imputed",
        quality=0.91,
        genome_build="",
    )

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "imputed data needs metadata review"
    assert readiness.missing_genome_build_variants == 1


def test_blank_direct_build_needs_metadata_review_before_imputation() -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs1", genome_build=" ")

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "DNA metadata needs review"
    assert readiness.missing_genome_build_variants == 1


def test_direct_missing_build_does_not_block_valid_imputed_readiness() -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs1", source_kind="genotyped", genome_build=None)
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs2",
        source_kind="imputed",
        quality=0.91,
        genome_build="GRCh37",
    )

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "ready for PRS input validation"
    assert readiness.missing_genome_build_variants == 1


def test_imputed_rows_with_quality_and_build_are_ready_for_prs_input_validation() -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs1", source_kind="genotyped")
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs2",
        source_kind="imputed",
        quality=0.91,
        genome_build="GRCh37",
    )

    readiness = build_imputation_readiness(conn, "dummy_alice")

    assert readiness.state == "ready for PRS input validation"
    assert readiness.direct_variants == 1
    assert readiness.imputed_variants == 1
    assert readiness.imputed_with_quality == 1


def test_render_report_uses_aggregate_counts_not_genotype_values() -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs123", source_kind="imputed", quality=0.91)
    readiness = build_imputation_readiness(conn, "dummy_alice")

    content = render_imputation_readiness_report(
        "dummy_alice",
        readiness,
        display_name="Dummy Alice Example",
    )

    front_matter = yaml.safe_load(content.split("---", 2)[1])
    assert front_matter["report_type"] == "imputation_readiness"
    assert "source_reports" not in front_matter
    assert "ready for PRS input validation" in content
    assert "rs123" not in content
    assert "AG" not in content


def test_report_uses_importer_quality_floor_constant() -> None:
    readiness = build_imputation_readiness(_user_conn(), "dummy_alice")

    content = render_imputation_readiness_report("dummy_alice", readiness)

    assert f"({DEFAULT_IMPUTED_QUALITY_FLOOR:g})" in content


def test_cli_rejects_traversal_person_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_imputation_readiness.py",
            "--person-id",
            "../escape",
            "--vault-path",
            str(tmp_path / "vault"),
            "--db-path",
            str(tmp_path / "famed.db"),
        ],
    )

    assert generate_imputation_readiness_main() == 1


def test_cli_prints_report_without_writing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Imputation readiness is on-demand output, not a per-person vault note (ADR-031)."""
    db_path = tmp_path / "famed.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    vault_root = tmp_path / "vault"
    (vault_root / "Persons").mkdir(parents=True)
    (vault_root / "Persons" / "dummy_alice.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: person\n---\n# Dummy Alice Example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_imputation_readiness.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--db-path",
            str(db_path),
        ],
    )

    assert generate_imputation_readiness_main() == 0
    assert "Imputation Readiness" in capsys.readouterr().out
    assert not (vault_root / "Records" / "dummy_alice" / "Reports").exists()
