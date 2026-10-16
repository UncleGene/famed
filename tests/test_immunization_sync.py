from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from famed.immunization_sync import (
    ImmunizationSyncError,
    persist_immunization_rows,
    remove_immunization_artifacts_for_source_path,
    write_immunizations_report,
)
from famed.person_sync import validate_note_front_matter
from famed.setup import SCHEMA_SQL


def _rows() -> list[dict[str, str]]:
    return [
        {
            "person_id": "dummy_alice",
            "vaccine_name": "Influenza vaccine",
            "cvx_code": "140",
            "administered_date": "2023-01-15",
            "status": "completed",
            "lot_number": "FLU-2026",
            "source_type": "source_import",
            "evidence_snippet": "Influenza vaccine",
        }
    ]


def test_persist_immunization_rows_writes_note_rows_and_report(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"

    note_path = persist_immunization_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=Path("2023-02-03-mixed-summary.xml"),
        processed_source_path="data/processed/dummy_alice/History/2023-02-03-mixed-summary.xml",
        checksum="abc123",
        source_type="ccda_xml",
        rows=_rows(),
        document_date="2023-02-03",
    )

    assert note_path == (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Immunizations"
        / "2023-02-03-mixed-summary-immunizations.md"
    )
    note_text = note_path.read_text(encoding="utf-8")
    assert "record_type: immunization" in note_text
    assert "Influenza vaccine" in note_text
    assert "FLU-2026" in note_text
    assert validate_note_front_matter(note_path) == []

    db_rows = conn.execute(
        """
        SELECT vaccine_name, cvx_code, administered_date, status, lot_number,
               source_path, source_paths
        FROM immunizations
        """
    ).fetchall()
    assert db_rows == [
        (
            "Influenza vaccine",
            "140",
            "2023-01-15",
            "completed",
            "FLU-2026",
            "data/processed/dummy_alice/History/2023-02-03-mixed-summary.xml",
            '["data/processed/dummy_alice/History/2023-02-03-mixed-summary.xml"]',
        )
    ]

    report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "immunizations.md"
    assert report_path.exists()
    assert "report_type: immunizations" in report_path.read_text(encoding="utf-8")


def test_write_immunizations_report_removes_report_without_immunizations(
    tmp_path: Path,
) -> None:
    """Inputs-only generation (ADR-031): no immunizations, no report."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    reports_dir.mkdir(parents=True)
    stale = reports_dir / "immunizations.md"
    stale.write_text("---\nreport_type: immunizations\n---\n", encoding="utf-8")

    assert write_immunizations_report(vault_root, "dummy_alice", conn) is None
    assert not stale.exists()


def test_persist_immunization_rows_deduplicates_same_event_across_sources(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    rows = [
        {
            "person_id": "dummy_alice",
            "vaccine_name": "Moderna SARS-CoV-2 Vaccination (Given 1/6/2019, 4/13/2018, 3/17/2018)",
            "cvx_code": "207",
            "administered_date": "2019-01-06",
            "status": "completed",
            "lot_number": "059H21A",
            "source_type": "source_import",
            "evidence_snippet": "Moderna SARS-CoV-2 Vaccination",
        }
    ]

    persist_immunization_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=Path("2023-05-30-doc0001.xml"),
        processed_source_path="data/processed/dummy_alice/History/2023-05-30-doc0001.xml",
        checksum="abc123",
        source_type="ccda_xml",
        rows=rows,
        document_date="2023-05-30",
    )
    persist_immunization_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=Path("2023-05-30-doc0002.xml"),
        processed_source_path="data/processed/dummy_alice/History/2023-05-30-doc0002.xml",
        checksum="def456",
        source_type="ccda_xml",
        rows=[
            {
                **rows[0],
                "vaccine_name": (
                    "Moderna SARS-CoV-2 Vaccination 01/06/2019 , 04/13/2018 , 03/17/2018"
                ),
            }
        ],
        document_date="2023-05-30",
    )

    db_rows = conn.execute(
        """
        SELECT vaccine_name, administered_date, cvx_code, lot_number, source_path, source_paths
        FROM immunizations
        """
    ).fetchall()
    assert db_rows == [
        (
            "Moderna SARS-CoV-2 Vaccination",
            "2019-01-06",
            "207",
            "059H21A",
            "data/processed/dummy_alice/History/2023-05-30-doc0001.xml",
            (
                '["data/processed/dummy_alice/History/2023-05-30-doc0001.xml", '
                '"data/processed/dummy_alice/History/2023-05-30-doc0002.xml"]'
            ),
        )
    ]
    report_text = (
        vault_root / "Records" / "dummy_alice" / "Reports" / "immunizations.md"
    ).read_text(encoding="utf-8")
    assert report_text.count("| Moderna SARS-CoV-2 Vaccination") == 1
    assert "| vaccine_name | administered_date | status | cvx_code | lot_number |" in report_text
    assert "data/processed/dummy_alice/History/2023-05-30-doc0001.xml" not in report_text


def test_persist_immunization_rows_prunes_superseded_healthsummary_archive_source(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    old_archive = "data/processed/dummy_alice/HealthSummary/HealthSummary_May_30_2026.zip"
    new_archive = "data/processed/dummy_alice/HealthSummary/HealthSummary_Jun_03_2026.zip"

    for source_path in (
        f"{old_archive}#IHE_XDM/Dummy Alice/DOC0001.XML",
        f"{new_archive}#IHE_XDM/Dummy Alice/DOC0001.XML",
    ):
        persist_immunization_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("DOC0001.XML"),
            processed_source_path=source_path,
            checksum="abc123",
            source_type="zip_xml",
            rows=_rows(),
            document_date="2023-02-03",
            source_file_name="HealthSummary.zip#DOC0001.XML",
        )

    source_path, source_paths = conn.execute(
        "SELECT source_path, source_paths FROM immunizations"
    ).fetchone()

    assert source_path == f"{new_archive}#IHE_XDM/Dummy Alice/DOC0001.XML"
    assert source_paths == json.dumps([source_path], sort_keys=True)


def test_persist_immunization_rows_keeps_different_healthsummary_provider_sources(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    first_archive = "data/processed/dummy_alice/HealthSummary/ClinicA HealthSummary_May_30_2026.zip"
    second_archive = (
        "data/processed/dummy_alice/HealthSummary/ClinicB HealthSummary_Jun_03_2026.zip"
    )

    for source_path in (
        f"{first_archive}#IHE_XDM/Dummy Alice/DOC0001.XML",
        f"{second_archive}#IHE_XDM/Dummy Alice/DOC0001.XML",
    ):
        persist_immunization_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("DOC0001.XML"),
            processed_source_path=source_path,
            checksum="abc123",
            source_type="zip_xml",
            rows=_rows(),
            document_date="2023-02-03",
            source_file_name="HealthSummary.zip#DOC0001.XML",
        )

    assert json.loads(conn.execute("SELECT source_paths FROM immunizations").fetchone()[0]) == [
        f"{first_archive}#IHE_XDM/Dummy Alice/DOC0001.XML",
        f"{second_archive}#IHE_XDM/Dummy Alice/DOC0001.XML",
    ]


def test_persist_immunization_rows_rejects_same_event_status_conflict_before_writes(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    first_source = "data/processed/dummy_alice/History/2023-05-30-doc0001.xml"
    second_source = "data/processed/dummy_alice/History/2023-05-31-doc0002.xml"

    persist_immunization_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=Path("2023-05-30-doc0001.xml"),
        processed_source_path=first_source,
        checksum="abc123",
        source_type="ccda_xml",
        rows=_rows(),
        document_date="2023-05-30",
    )

    with pytest.raises(
        ImmunizationSyncError,
        match="same immunization event has conflicting status values",
    ):
        persist_immunization_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("2023-05-31-doc0002.xml"),
            processed_source_path=second_source,
            checksum="def456",
            source_type="ccda_xml",
            rows=[{**_rows()[0], "status": "not-done"}],
            document_date="2023-05-31",
        )

    assert conn.execute(
        "SELECT status, source_path, source_paths FROM immunizations"
    ).fetchall() == [("completed", first_source, f'["{first_source}"]')]
    assert not (
        vault_root
        / "Records"
        / "dummy_alice"
        / "Immunizations"
        / "2023-05-31-doc0002-immunizations.md"
    ).exists()


def test_write_immunizations_report_omits_source_references(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    archive_path = "data/processed/dummy_alice/HealthSummary/HealthSummary_May_30_2026.zip"

    for member_name in ("IHE_XDM/Dummy Alice/DOC0001.XML", "IHE_XDM/Dummy Alice/DOC0002.XML"):
        persist_immunization_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("DOC0001.XML"),
            processed_source_path=f"{archive_path}#{member_name}",
            checksum="abc123",
            source_type="zip_xml",
            rows=_rows(),
            document_date="2023-02-03",
            source_file_name=f"HealthSummary_May_30_2026.zip#{Path(member_name).name}",
        )

    report_text = (
        vault_root / "Records" / "dummy_alice" / "Reports" / "immunizations.md"
    ).read_text(encoding="utf-8")
    assert "| vaccine_name | administered_date | status | cvx_code | lot_number |" in report_text
    assert "sources" not in report_text
    assert archive_path not in report_text


def test_immunization_mutations_refresh_configured_fhir_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    source_path = "data/processed/dummy_alice/History/2023-02-03-summary.xml"
    refreshes: list[str] = []

    def fake_refresh(
        passed_conn: sqlite3.Connection,
        passed_vault: Path,
        person_id: str,
    ) -> None:
        assert passed_conn is conn
        assert passed_vault == vault_root
        refreshes.append(person_id)

    monkeypatch.setattr("famed.immunization_sync.refresh_fhir_dependent_export", fake_refresh)

    persist_immunization_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=Path("summary.xml"),
        processed_source_path=source_path,
        checksum="abc123",
        source_type="ccda_xml",
        rows=_rows(),
        document_date="2023-02-03",
    )
    remove_immunization_artifacts_for_source_path(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        processed_source_path=source_path,
    )

    assert refreshes == ["dummy_alice", "dummy_alice"]
