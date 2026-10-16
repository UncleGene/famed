"""Tests for validation-only PRS model/input compatibility reporting."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from famed.generate_prs_validation import main as generate_prs_validation_main
from famed.person_sync import validate_note_front_matter
from famed.prs_validation import (
    PRSValidationError,
    build_prs_validation,
    load_prs_model,
    render_prs_validation_report,
    write_prs_validation_report,
)
from famed.setup import SCHEMA_SQL


def _user_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _model_payload() -> dict[str, object]:
    return {
        "metadata": {
            "score_id": "PGS_DUMMY_CAD",
            "source": "Dummy PGS Catalog fixture",
            "release": "2023-01-01",
            "genome_build": "GRCh37",
            "trait": "coronary artery disease",
            "development_population": "Dummy European ancestry cohort",
            "validation_population": "Dummy held-out validation cohort",
            "performance_metrics": "AUC 0.60 in synthetic fixture metadata",
            "license": "Synthetic test fixture",
        },
        "variants": [
            {
                "rsid": "rs100",
                "chromosome": "1",
                "position": 100,
                "effect_allele": "A",
                "other_allele": "G",
                "weight": 0.01,
            },
            {
                "rsid": "rs200",
                "chromosome": "2",
                "position": 200,
                "effect_allele": "C",
                "other_allele": "T",
                "weight": -0.02,
            },
        ],
    }


def _write_model(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    model_path = tmp_path / "dummy-prs-model.json"
    model_path.write_text(json.dumps(payload or _model_payload()), encoding="utf-8")
    return model_path


def _insert_genotype(
    conn: sqlite3.Connection,
    person_id: str,
    rsid: str,
    *,
    chromosome: str = "1",
    position: int | None = 100,
    allele1: str = "A",
    allele2: str = "G",
    source_kind: str = "imputed",
    quality: float | None = 0.91,
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
            chromosome,
            position,
            allele1,
            allele2,
            "vcf",
            "data/processed/dummy_alice/Genes/genome.vcf",
            source_kind,
            quality,
            genome_build,
        ),
    )


def test_load_model_requires_pinned_metadata_and_variants(tmp_path: Path) -> None:
    model_path = _write_model(tmp_path)

    model = load_prs_model(model_path)

    assert model.metadata.score_id == "PGS_DUMMY_CAD"
    assert model.metadata.genome_build == "GRCh37"
    assert len(model.variants) == 2


def test_load_model_rejects_missing_metadata(tmp_path: Path) -> None:
    payload = _model_payload()
    metadata = payload["metadata"]
    assert isinstance(metadata, dict)
    metadata["release"] = ""

    with pytest.raises(PRSValidationError, match="release"):
        load_prs_model(_write_model(tmp_path, payload))


def test_load_model_rejects_duplicate_rsids(tmp_path: Path) -> None:
    payload = _model_payload()
    variants = payload["variants"]
    assert isinstance(variants, list)
    variants[1]["rsid"] = "rs100"

    with pytest.raises(PRSValidationError, match="duplicate"):
        load_prs_model(_write_model(tmp_path, payload))


def test_load_model_rejects_identical_effect_and_other_alleles(tmp_path: Path) -> None:
    payload = _model_payload()
    variants = payload["variants"]
    assert isinstance(variants, list)
    variants[0]["other_allele"] = "A"

    with pytest.raises(PRSValidationError, match="effect and other"):
        load_prs_model(_write_model(tmp_path, payload))


def test_validation_counts_aggregate_coverage_without_score(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs100")
    model = load_prs_model(_write_model(tmp_path))

    validation = build_prs_validation(conn, "dummy_alice", model)

    assert validation.state == "model/input validation complete"
    assert validation.model_variants == 2
    assert validation.matched_variants == 1
    assert validation.missing_variants == 1
    assert validation.allele_identity_matches == 1
    assert validation.coverage_percent == 50.0


def test_validation_reports_no_build_compatible_dna(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs100", genome_build="GRCh38")
    model = load_prs_model(_write_model(tmp_path))

    validation = build_prs_validation(conn, "dummy_alice", model)

    assert validation.state == "no build-compatible DNA"
    assert validation.local_variants == 1
    assert validation.build_compatible_local_variants == 0


def test_validation_flags_coordinate_and_allele_conflicts(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs100",
        position=101,
        allele1="C",
        allele2="T",
    )
    model = load_prs_model(_write_model(tmp_path))

    validation = build_prs_validation(conn, "dummy_alice", model)

    assert validation.state == "model/input needs review"
    assert validation.coordinate_mismatches == 1
    assert validation.allele_identity_conflicts == 1


def test_validation_counts_missing_local_position_as_coordinate_mismatch(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs100", position=None)
    model = load_prs_model(_write_model(tmp_path))

    validation = build_prs_validation(conn, "dummy_alice", model)

    assert validation.state == "model/input needs review"
    assert validation.coordinate_mismatches == 1


def test_validation_prefers_direct_call_for_duplicate_rsid(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs100",
        allele1="C",
        allele2="T",
        source_kind="imputed",
    )
    _insert_genotype(
        conn,
        "dummy_alice",
        "rs100",
        allele1="A",
        allele2="G",
        source_kind="genotyped",
        quality=None,
    )
    model = load_prs_model(_write_model(tmp_path))

    validation = build_prs_validation(conn, "dummy_alice", model)

    assert validation.state == "model/input validation complete"
    assert validation.allele_identity_matches == 1
    assert validation.allele_identity_conflicts == 0


def test_render_report_omits_variant_rows_weights_and_source_paths(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs100")
    model = load_prs_model(_write_model(tmp_path))
    validation = build_prs_validation(conn, "dummy_alice", model)

    content = render_prs_validation_report(
        "dummy_alice",
        validation,
        display_name="Dummy Alice Example",
    )

    front_matter = yaml.safe_load(content.split("---", 2)[1])
    assert front_matter["report_type"] == "prs_validation"
    assert front_matter["model_score_id"] == "PGS_DUMMY_CAD"
    assert "model/input validation complete" in content
    assert "50.0%" in content
    assert "rs100" not in content
    assert "0.01" not in content
    assert "data/processed" not in content


def test_write_report_registers_reports_index(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs100")
    vault_root = tmp_path / "vault"
    person_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    person_dir.mkdir(parents=True)
    (vault_root / "Persons").mkdir(parents=True)
    (vault_root / "Persons" / "dummy_alice.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: person\n"
        "display_name: Dummy Alice Example\n"
        "---\n"
        "# Dummy Alice Example\n",
        encoding="utf-8",
    )

    report_path, validation = write_prs_validation_report(
        conn,
        vault_root,
        "dummy_alice",
        _write_model(tmp_path),
    )

    assert validation.state == "model/input validation complete"
    assert report_path == person_dir / "prs-validation.md"
    validate_note_front_matter(report_path)
    assert "[[prs-validation]]" in (person_dir / "_index.md").read_text(encoding="utf-8")


def test_write_report_rejects_traversal_person_id(tmp_path: Path) -> None:
    conn = _user_conn()

    with pytest.raises(PRSValidationError):
        write_prs_validation_report(conn, tmp_path / "vault", "../escape", _write_model(tmp_path))


def test_cli_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "famed.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    _insert_genotype(conn, "dummy_alice", "rs100")
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
            "generate_prs_validation.py",
            "--person-id",
            "dummy_alice",
            "--model-path",
            str(_write_model(tmp_path)),
            "--vault-path",
            str(vault_root),
            "--db-path",
            str(db_path),
        ],
    )

    assert generate_prs_validation_main() == 0
    assert (vault_root / "Records" / "dummy_alice" / "Reports" / "prs-validation.md").is_file()


def test_cli_rejects_missing_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            "generate_prs_validation.py",
            "--person-id",
            "dummy_alice",
            "--model-path",
            str(tmp_path / "missing.json"),
            "--vault-path",
            str(vault_root),
            "--db-path",
            str(db_path),
        ],
    )

    assert generate_prs_validation_main() == 1
