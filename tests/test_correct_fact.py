from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from famed.correct_fact import (
    _USER_CORRECTION_SOURCE,
    CorrectFactError,
    apply_history_refutation,
    apply_lab_correction,
    apply_medication_correction,
    revoke_history_refutation,
    revoke_lab_correction,
    revoke_medication_correction,
)
from famed.lab_sync import replace_lab_results_for_source_path, upsert_lab_result
from famed.medication_sync import (
    replace_medications_for_source_path,
    upsert_medication,
    write_medication_conflicts_report,
)
from famed.pending_review import refresh_pending_review_report_if_configured
from famed.person_sync import validate_vault_notes
from famed.setup import SCHEMA_SQL
from famed.storage_contract import (
    AUDIT_USER_CORRECTION_REVOKED,
    AUDIT_USER_FACT_CORRECTED,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_alice', 'Dummy Alice')")
    return conn


def _write_person_note(vault_root: Path) -> None:
    person_note = vault_root / "Persons" / "dummy_alice.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    person_note.write_text(
        (
            "---\nperson_id: dummy_alice\nrecord_type: person"
            "\ndisplay_name: Dummy Alice\n---\n# Dummy Alice\n"
        ),
        encoding="utf-8",
    )


def _fhir_resources(vault_root: Path, resource_type: str) -> list[dict[str, Any]]:
    export_path = vault_root / "Records" / "dummy_alice" / "Reports" / "fhir-export.json"
    bundle = json.loads(export_path.read_text(encoding="utf-8"))
    return [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == resource_type
    ]


def _lab_row(
    *,
    person_id: str = "dummy_alice",
    test_name: str = "Glucose",
    value: float = 95.0,
    unit: str = "mg/dL",
    observed_at: str = "2023-01-15",
    source_type: str = "manual",
    source_path: str = "data/processed/dummy_alice/Labs/lab.csv",
    row_index: int = 0,
) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "loinc_code": "2345-7",
        "test_name": test_name,
        "value": value,
        "unit": unit,
        "reference_range_low": 70.0,
        "reference_range_high": 100.0,
        "observed_at": observed_at,
        "source_type": source_type,
        "source_path": source_path,
        "row_index": row_index,
    }


def _medication_row(
    *,
    person_id: str = "dummy_alice",
    medication_name: str = "Vitamin D",
    medication_type: str = "supplement",
    dose: str = "1000 IU",
    frequency: str = "daily",
    status: str = "active",
    source_type: str = "manual",
    source_path: str = "data/processed/dummy_alice/Medications/meds.txt",
    row_index: int = 0,
) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "rxnorm_cui": None,
        "medication_name": medication_name,
        "medication_type": medication_type,
        "dose": dose,
        "frequency": frequency,
        "start_date": None,
        "end_date": None,
        "status": status,
        "source_type": source_type,
        "source_path": source_path,
        "row_index": row_index,
    }


# ---------------------------------------------------------------------------
# apply_lab_correction
# ---------------------------------------------------------------------------


def test_apply_lab_correction_updates_existing_row(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab_row(value=95.0))

    result = apply_lab_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Glucose",
        "2023-01-15",
        90.0,
        reason="Manual measurement re-verified",
    )

    assert result["old_value"] == 95.0
    assert result["new_value"] == 90.0

    row = conn.execute(
        """
        SELECT value, source_type
        FROM lab_results
        WHERE person_id = 'dummy_alice' AND test_name = 'Glucose'
        """
    ).fetchone()
    assert row[0] == 90.0
    assert row[1] == "user_correction"


def test_apply_lab_correction_inserts_synthetic_row_when_no_prior_record(
    tmp_path: Path,
) -> None:
    conn = _make_db()

    result = apply_lab_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "HbA1c",
        "2023-02-10",
        5.9,
        "%",
        reason="User-authored insert, no prior import",
    )

    assert result["old_value"] is None
    assert result["new_value"] == 5.9

    row = conn.execute(
        "SELECT value, source_type, source_path FROM lab_results WHERE test_name = 'HbA1c'"
    ).fetchone()
    assert row[0] == 5.9
    assert row[1] == "user_correction"
    assert row[2] == _USER_CORRECTION_SOURCE


def test_apply_lab_correction_logs_audit_event(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab_row(value=105.0))

    apply_lab_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Glucose",
        "2023-01-15",
        90.0,
        reason="Corrected typo",
    )

    audit = conn.execute(
        "SELECT event_type, message FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_FACT_CORRECTED,),
    ).fetchone()
    assert audit is not None
    assert "Glucose" in audit[1]
    assert "105.0" in audit[1]
    assert "90.0" in audit[1]


def test_apply_lab_correction_updates_unit(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab_row(value=5.0, unit="mmol/L"))

    apply_lab_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Glucose",
        "2023-01-15",
        90.0,
        "mg/dL",
        reason="Convert to mg/dL",
    )

    row = conn.execute(
        "SELECT unit FROM lab_results WHERE person_id = 'dummy_alice' AND test_name = 'Glucose'"
    ).fetchone()
    assert row[0] == "mg/dL"


def test_apply_lab_correction_refreshes_fhir_export(tmp_path: Path) -> None:
    conn = _make_db()
    _write_person_note(tmp_path)
    upsert_lab_result(conn, _lab_row(value=95.0))

    apply_lab_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Glucose",
        "2023-01-15",
        90.0,
        "mg/dL",
        reason="Corrected value",
    )

    observations = _fhir_resources(tmp_path, "Observation")
    assert len(observations) == 1
    assert observations[0]["code"]["text"] == "Glucose"
    assert observations[0]["valueQuantity"] == {"value": 90.0, "unit": "mg/dL"}
    assert observations[0]["extension"] == [
        {
            "url": "https://famed.local/fhir/StructureDefinition/source-path",
            "valueString": "data/processed/dummy_alice/Labs/lab.csv",
        },
        {
            "url": "https://famed.local/fhir/StructureDefinition/source-type",
            "valueString": "user_correction",
        },
    ]


def test_apply_lab_correction_refreshes_flagged_report(tmp_path: Path) -> None:
    conn = _make_db()
    _write_person_note(tmp_path)
    upsert_lab_result(conn, _lab_row(value=95.0))

    apply_lab_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Glucose",
        "2023-01-15",
        110.0,
        "mg/dL",
        reason="Corrected value",
    )

    flagged = (
        tmp_path / "Records" / "dummy_alice" / "Reports" / "flagged-for-discussion.md"
    ).read_text(encoding="utf-8")
    assert "Glucose" in flagged
    assert "2023-01-15" in flagged


# ---------------------------------------------------------------------------
# revoke_lab_correction
# ---------------------------------------------------------------------------


def test_revoke_lab_correction_deletes_synthetic_row(tmp_path: Path) -> None:
    conn = _make_db()
    # Insert a correction with no prior import (synthetic source path)
    apply_lab_correction(
        conn, tmp_path, "dummy_alice", "HbA1c", "2023-02-10", 5.9, reason="User-authored insert"
    )

    revoke_lab_correction(
        conn, tmp_path, "dummy_alice", "HbA1c", "2023-02-10", reason="Revocation test"
    )

    row = conn.execute("SELECT * FROM lab_results WHERE test_name = 'HbA1c'").fetchone()
    assert row is None


def test_revoke_lab_correction_restores_manual_source_type(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab_row(value=95.0))
    apply_lab_correction(
        conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", 90.0, reason="Corrected value"
    )

    revoke_lab_correction(
        conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", reason="Re-verified source"
    )

    row = conn.execute("SELECT source_type FROM lab_results WHERE test_name = 'Glucose'").fetchone()
    assert row[0] == "manual"


def test_revoke_lab_correction_logs_audit_event(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab_row(value=95.0))
    apply_lab_correction(
        conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", 90.0, reason="Corrected"
    )

    revoke_lab_correction(
        conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", reason="Revocation"
    )

    audit = conn.execute(
        "SELECT event_type, message FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_CORRECTION_REVOKED,),
    ).fetchone()
    assert audit is not None
    assert "Glucose" in audit[1]


def test_revoke_lab_correction_raises_when_no_correction_exists(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab_row())

    with pytest.raises(CorrectFactError, match="no user_correction row found"):
        revoke_lab_correction(
            conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", reason="No correction to revoke"
        )


# ---------------------------------------------------------------------------
# Guard integration: corrected lab is preserved across re-import
# ---------------------------------------------------------------------------


def test_corrected_lab_preserved_on_reimport(tmp_path: Path) -> None:
    """After apply_lab_correction, re-importing the originating source skips the row."""
    conn = _make_db()
    source_path = "data/processed/dummy_alice/Labs/lab.csv"
    upsert_lab_result(conn, _lab_row(value=95.0, source_path=source_path))

    apply_lab_correction(
        conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", 90.0, reason="Corrected"
    )

    # Re-import with old (conflicting) value: non-interactive should skip
    _, skipped = replace_lab_results_for_source_path(
        conn,
        source_path,
        [_lab_row(value=95.0, source_path=source_path)],
        non_interactive=True,
    )
    assert skipped == 1

    # DB should still hold the corrected value
    row = conn.execute(
        "SELECT value, source_type FROM lab_results WHERE test_name = 'Glucose'"
    ).fetchone()
    assert row[0] == 90.0
    assert row[1] == "user_correction"


def test_corrected_lab_no_longer_blocks_reimport_after_revoke(tmp_path: Path) -> None:
    """After revoke_lab_correction, re-importing the source applies the import value."""
    conn = _make_db()
    source_path = "data/processed/dummy_alice/Labs/lab.csv"
    upsert_lab_result(conn, _lab_row(value=95.0, source_path=source_path))

    apply_lab_correction(
        conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", 90.0, reason="Corrected"
    )
    revoke_lab_correction(conn, tmp_path, "dummy_alice", "Glucose", "2023-01-15", reason="Revoked")

    inserted, skipped = replace_lab_results_for_source_path(
        conn,
        source_path,
        [_lab_row(value=95.0, source_path=source_path)],
        non_interactive=True,
    )
    assert inserted == 1
    assert skipped == 0

    row = conn.execute(
        "SELECT value, source_type FROM lab_results WHERE test_name = 'Glucose'"
    ).fetchone()
    assert row[0] == 95.0
    assert row[1] == "manual"


# ---------------------------------------------------------------------------
# apply_medication_correction
# ---------------------------------------------------------------------------


def test_apply_medication_correction_updates_existing_row(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_medication(conn, _medication_row(dose="1000 IU"))

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        reason="User confirmed new dose",
    )

    assert result["old_dose"] == "1000 IU"
    assert result["new_dose"] == "2000 IU"

    row = conn.execute(
        "SELECT dose, source_type FROM medications WHERE medication_name = 'Vitamin D'"
    ).fetchone()
    assert row[0] == "2000 IU"
    assert row[1] == "user_correction"
    reports_dir = tmp_path / "Records" / "dummy_alice" / "Reports"
    assert not (reports_dir / "pgx-coverage.md").exists()
    assert not (reports_dir / "pgx-wallet-card.md").exists()


def test_apply_medication_correction_inserts_synthetic_row_when_no_prior_record(
    tmp_path: Path,
) -> None:
    conn = _make_db()

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Omega-3",
        "supplement",
        new_dose="1 g",
        reason="User-authored entry",
    )

    assert result["old_dose"] is None
    assert result["new_dose"] == "1 g"

    row = conn.execute(
        "SELECT dose, source_type, source_path FROM medications WHERE medication_name = 'Omega-3'"
    ).fetchone()
    assert row[0] == "1 g"
    assert row[1] == "user_correction"
    assert row[2] == _USER_CORRECTION_SOURCE


def test_apply_medication_correction_logs_audit_event(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_medication(conn, _medication_row(dose="1000 IU"))

    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        reason="Dose correction",
    )

    audit = conn.execute(
        "SELECT event_type, message FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_FACT_CORRECTED,),
    ).fetchone()
    assert audit is not None
    assert "Vitamin D" in audit[1]
    assert "1000 IU" in audit[1]
    assert "2000 IU" in audit[1]


def test_apply_medication_correction_resolves_pending_import_conflict(tmp_path: Path) -> None:
    conn = _make_db()
    _write_person_note(tmp_path)
    first_source = "data/processed/dummy_alice/Medications/source-a.txt"
    second_source = "data/processed/dummy_alice/Medications/source-b.txt"
    replace_medications_for_source_path(
        conn,
        first_source,
        [
            _medication_row(
                medication_name="Examplemed",
                medication_type="prescription",
                dose="10 mg",
                source_path=first_source,
            )
        ],
    )
    replace_medications_for_source_path(
        conn,
        second_source,
        [
            _medication_row(
                medication_name="Examplemed",
                medication_type="prescription",
                dose="20 mg",
                source_path=second_source,
            )
        ],
    )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")
    refresh_pending_review_report_if_configured(
        conn,
        root=tmp_path.parent,
        vault_root=tmp_path,
        person_id="dummy_alice",
    )
    pending_path = tmp_path / "Records" / "dummy_alice" / "Reports" / "pending-review.md"
    assert "pending_review_count: 1" in pending_path.read_text(encoding="utf-8")

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        new_dose="10 mg",
        reason="User selected the source-supported dose",
        actor="caregiver",
    )

    assert result["resolved_conflicts"] == 1
    events = conn.execute(
        """
        SELECT event_type, subject_id, status, resolution, message
        FROM audit_events
        WHERE subject_type = 'medication_conflict'
        ORDER BY rowid
        """
    ).fetchall()
    assert [event[0] for event in events] == [
        "medication_conflict_detected",
        "medication_conflict_resolution_recorded",
    ]
    assert events[0][1] == events[1][1]
    assert events[1][2:4] == ("processed", "user_resolved")
    resolution = json.loads(events[1][4])["resolution"]
    assert resolution == {
        "actor": "caregiver",
        "dose": "10 mg",
        "frequency": "daily",
        "reason": "User selected the source-supported dose",
    }
    report = (
        tmp_path / "Records" / "dummy_alice" / "Reports" / "medication-conflicts.md"
    ).read_text(encoding="utf-8")
    assert "verification_status: accepted" in report
    assert "pending_review_count: 0" in report
    assert "_No conflicting medication entries found._" in report
    pending_report = pending_path.read_text(encoding="utf-8")
    assert "pending_review_count: 0" in pending_report
    assert "Examplemed" not in pending_report

    replace_medications_for_source_path(
        conn,
        second_source,
        [
            _medication_row(
                medication_name="Examplemed",
                medication_type="prescription",
                dose="20 mg",
                source_path=second_source,
            )
        ],
    )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")
    detected_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'medication_conflict_detected'
        """
    ).fetchone()[0]
    assert detected_count == 1


def test_apply_medication_correction_audits_unrendered_conflict(tmp_path: Path) -> None:
    conn = _make_db()
    for source_path, dose in (
        ("data/processed/dummy_alice/Medications/source-a.txt", "10 mg"),
        ("data/processed/dummy_alice/Medications/source-b.txt", "20 mg"),
    ):
        replace_medications_for_source_path(
            conn,
            source_path,
            [
                _medication_row(
                    medication_name="Examplemed",
                    medication_type="prescription",
                    dose=dose,
                    source_path=source_path,
                )
            ],
        )

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        new_dose="10 mg",
        reason="User selected a source-supported value",
    )

    assert result["resolved_conflicts"] == 1
    events = conn.execute(
        """
        SELECT event_type, subject_id
        FROM audit_events
        WHERE subject_type = 'medication_conflict'
        ORDER BY rowid
        """
    ).fetchall()
    assert [event[0] for event in events] == [
        "medication_conflict_detected",
        "medication_conflict_resolution_recorded",
    ]
    assert events[0][1] == events[1][1]


def test_apply_medication_correction_uses_conflict_identity_for_source_row(
    tmp_path: Path,
) -> None:
    conn = _make_db()
    first_source = "data/processed/dummy_alice/Medications/source-a.txt"
    second_source = "data/processed/dummy_alice/Medications/source-b.txt"
    replace_medications_for_source_path(
        conn,
        first_source,
        [
            _medication_row(
                medication_name="dabigatran etexilate (Pradaxa) 150 MG capsule",
                medication_type="prescription",
                dose="150 mg",
                frequency="Take 150 mg by mouth once daily.",
                source_path=first_source,
                source_type="zip_xml",
            )
            | {"rxnorm_cui": "1037045"}
        ],
    )
    replace_medications_for_source_path(
        conn,
        second_source,
        [
            _medication_row(
                medication_name="dabigatran (PRADAXA) 150 mg capsule",
                medication_type="prescription",
                dose="150 mg",
                frequency="Take 1 capsule (150 mg) by mouth 2 times daily",
                source_path=second_source,
                source_type="zip_xml",
            )
            | {"rxnorm_cui": "1037045"}
        ],
    )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "dabigatran (Pradaxa)",
        "prescription",
        new_dose="150 mg",
        new_frequency="twice daily",
        reason="User selected the source-supported twice-daily value.",
    )

    assert result["resolved_conflicts"] == 1
    rows = conn.execute(
        """
        SELECT medication_name, dose, frequency, source_type, source_path, rxnorm_cui
        FROM medications
        WHERE person_id = 'dummy_alice'
          AND lower(medication_name) LIKE '%pradaxa%'
        ORDER BY source_type = 'user_correction' DESC, medication_name
        """
    ).fetchall()
    corrected = rows[0]
    assert corrected[0] == "dabigatran (PRADAXA) 150 mg capsule"
    assert corrected[1] == "150 mg"
    assert corrected[2] == "twice daily"
    assert corrected[3] == "user_correction"
    assert corrected[4] == second_source
    assert corrected[5] == "1037045"
    assert all(row[4] != _USER_CORRECTION_SOURCE for row in rows)


def test_apply_medication_correction_supersedes_prior_conflict_resolution(
    tmp_path: Path,
) -> None:
    conn = _make_db()
    first_source = "data/processed/dummy_alice/Medications/source-a.txt"
    second_source = "data/processed/dummy_alice/Medications/source-b.txt"
    replace_medications_for_source_path(
        conn,
        first_source,
        [
            _medication_row(
                medication_name="dabigatran etexilate (Pradaxa) 150 MG capsule",
                medication_type="prescription",
                dose="150 mg",
                frequency="Take 150 mg by mouth once daily.",
                source_path=first_source,
                source_type="zip_xml",
            )
            | {"rxnorm_cui": "1037045"}
        ],
    )
    replace_medications_for_source_path(
        conn,
        second_source,
        [
            _medication_row(
                medication_name="dabigatran (PRADAXA) 150 mg capsule",
                medication_type="prescription",
                dose="150 mg",
                frequency="Take 1 capsule (150 mg) by mouth 2 times daily",
                source_path=second_source,
                source_type="zip_xml",
            )
            | {"rxnorm_cui": "1037045"}
        ],
    )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")
    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "dabigatran (Pradaxa)",
        "prescription",
        new_dose="150 mg",
        new_frequency="twice daily",
        reason="User selected the twice-daily value.",
    )

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "dabigatran (PRADAXA) 150 mg capsule",
        "prescription",
        new_dose="150 mg",
        new_frequency="once daily",
        reason="User clarified the current dose is once daily.",
    )

    assert result["resolved_conflicts"] == 1
    latest_resolution = conn.execute(
        """
        SELECT message
        FROM audit_events
        WHERE event_type = 'medication_conflict_resolution_recorded'
        ORDER BY rowid DESC
        LIMIT 1
        """
    ).fetchone()[0]
    assert json.loads(latest_resolution)["resolution"]["frequency"] == "once daily"
    report = (
        tmp_path / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
    ).read_text(encoding="utf-8")
    assert "| dabigatran (Pradaxa) | 150 mg | once daily | active |" in report
    assert "twice daily" not in report


def test_revoking_medication_correction_reopens_source_conflict(tmp_path: Path) -> None:
    conn = _make_db()
    first_source = "data/processed/dummy_alice/Medications/source-a.txt"
    second_source = "data/processed/dummy_alice/Medications/source-b.txt"
    for source_path, dose in ((first_source, "10 mg"), (second_source, "20 mg")):
        replace_medications_for_source_path(
            conn,
            source_path,
            [
                _medication_row(
                    medication_name="Examplemed",
                    medication_type="prescription",
                    dose=dose,
                    source_path=source_path,
                )
            ],
        )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")
    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        new_dose="10 mg",
        reason="User selected the source-supported dose",
    )

    revoke_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        reason="User withdrew the correction",
    )

    detected_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'medication_conflict_detected'
        """
    ).fetchone()[0]
    assert detected_count == 2
    report = (
        tmp_path / "Records" / "dummy_alice" / "Reports" / "medication-conflicts.md"
    ).read_text(encoding="utf-8")
    assert "verification_status: needs_review" in report
    assert "pending_review_count: 1" in report
    assert "Examplemed" in report


def test_revocation_reopens_source_backed_conflict_without_duplicate_replay_event(
    tmp_path: Path,
) -> None:
    conn = _make_db()
    first_source = "data/processed/dummy_alice/Medications/source-a.txt"
    second_source = "data/processed/dummy_alice/Medications/source-b.txt"
    for source_path, dose in ((first_source, "10 mg"), (second_source, "20 mg")):
        replace_medications_for_source_path(
            conn,
            source_path,
            [
                _medication_row(
                    medication_name="Examplemed",
                    medication_type="prescription",
                    dose=dose,
                    source_path=source_path,
                )
            ],
        )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")
    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        new_dose="20 mg",
        reason="User selected the other source value",
    )

    revoke_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        reason="User withdrew the correction",
    )
    report_path = tmp_path / "Records" / "dummy_alice" / "Reports" / "medication-conflicts.md"
    report = report_path.read_text(encoding="utf-8")
    assert "verification_status: needs_review" in report

    replace_medications_for_source_path(
        conn,
        first_source,
        [
            _medication_row(
                medication_name="Examplemed",
                medication_type="prescription",
                dose="10 mg",
                source_path=first_source,
            )
        ],
    )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")

    report = report_path.read_text(encoding="utf-8")
    assert "verification_status: needs_review" in report
    detected_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'medication_conflict_detected'
        """
    ).fetchone()[0]
    assert detected_count == 2


def test_medication_correction_requires_values_for_every_pending_axis(
    tmp_path: Path,
) -> None:
    conn = _make_db()
    first_source = "data/processed/dummy_alice/Medications/source-a.txt"
    second_source = "data/processed/dummy_alice/Medications/source-b.txt"
    for source_path, dose, frequency in (
        (first_source, "10 mg", "daily"),
        (second_source, "20 mg", "twice daily"),
    ):
        replace_medications_for_source_path(
            conn,
            source_path,
            [
                _medication_row(
                    medication_name="Examplemed",
                    medication_type="prescription",
                    dose=dose,
                    frequency=frequency,
                    source_path=source_path,
                )
            ],
        )
    write_medication_conflicts_report(conn, tmp_path, "dummy_alice")

    with pytest.raises(
        CorrectFactError,
        match="all pending medication conflict axes.*missing: frequency",
    ):
        apply_medication_correction(
            conn,
            tmp_path,
            "dummy_alice",
            "Examplemed",
            "prescription",
            new_dose="10 mg",
            reason="User selected only the dose",
        )

    source_types = {
        row[0] for row in conn.execute("SELECT source_type FROM medications").fetchall()
    }
    assert source_types == {"manual"}
    resolution_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'medication_conflict_resolution_recorded'
        """
    ).fetchone()[0]
    assert resolution_count == 0

    result = apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Examplemed",
        "prescription",
        new_dose="10 mg",
        new_frequency="daily",
        reason="User selected both source-supported values",
    )
    assert result["resolved_conflicts"] == 2


@pytest.mark.parametrize("unstated_dose", ["", "unknown"])
def test_medication_correction_rejects_unstated_pending_value(
    tmp_path: Path, unstated_dose: str
) -> None:
    conn = _make_db()
    for source_path, dose in (
        ("data/processed/dummy_alice/Medications/source-a.txt", "10 mg"),
        ("data/processed/dummy_alice/Medications/source-b.txt", "20 mg"),
    ):
        replace_medications_for_source_path(
            conn,
            source_path,
            [
                _medication_row(
                    medication_name="Examplemed",
                    medication_type="prescription",
                    dose=dose,
                    source_path=source_path,
                )
            ],
        )

    with pytest.raises(CorrectFactError, match="missing: dose"):
        apply_medication_correction(
            conn,
            tmp_path,
            "dummy_alice",
            "Examplemed",
            "prescription",
            new_dose=unstated_dose,
            reason="User did not supply a stated dose",
        )

    assert {row[0] for row in conn.execute("SELECT source_type FROM medications").fetchall()} == {
        "manual"
    }
    resolution_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'medication_conflict_resolution_recorded'
        """
    ).fetchone()[0]
    assert resolution_count == 0


def test_apply_medication_correction_refreshes_fhir_export(tmp_path: Path) -> None:
    conn = _make_db()
    _write_person_note(tmp_path)
    upsert_medication(conn, _medication_row(dose="1000 IU"))

    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        new_frequency="twice daily",
        reason="Dose correction",
    )

    statements = _fhir_resources(tmp_path, "MedicationStatement")
    assert len(statements) == 1
    assert statements[0]["medicationCodeableConcept"] == {"text": "Vitamin D"}
    assert statements[0]["dosage"] == [{"text": "2000 IU twice daily"}]
    assert statements[0]["extension"] == [
        {
            "url": "https://famed.local/fhir/StructureDefinition/source-path",
            "valueString": "data/processed/dummy_alice/Medications/meds.txt",
        },
        {
            "url": "https://famed.local/fhir/StructureDefinition/source-type",
            "valueString": "user_correction",
        },
    ]


# ---------------------------------------------------------------------------
# revoke_medication_correction
# ---------------------------------------------------------------------------


def test_revoke_medication_correction_deletes_synthetic_row(tmp_path: Path) -> None:
    conn = _make_db()
    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Omega-3",
        "supplement",
        new_dose="1 g",
        reason="User-authored entry",
    )

    revoke_medication_correction(
        conn, tmp_path, "dummy_alice", "Omega-3", "supplement", reason="Revocation test"
    )

    row = conn.execute("SELECT * FROM medications WHERE medication_name = 'Omega-3'").fetchone()
    assert row is None


def test_revoke_medication_correction_restores_manual_source_type(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_medication(conn, _medication_row(dose="1000 IU"))
    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        reason="Correction",
    )

    revoke_medication_correction(
        conn, tmp_path, "dummy_alice", "Vitamin D", "supplement", reason="Revocation"
    )

    row = conn.execute(
        "SELECT source_type FROM medications WHERE medication_name = 'Vitamin D'"
    ).fetchone()
    assert row[0] == "manual"


def test_revoke_medication_correction_logs_audit_event(tmp_path: Path) -> None:
    conn = _make_db()
    upsert_medication(conn, _medication_row())
    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        reason="Corrected",
    )

    revoke_medication_correction(
        conn, tmp_path, "dummy_alice", "Vitamin D", "supplement", reason="Revocation"
    )

    audit = conn.execute(
        "SELECT event_type, message FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_CORRECTION_REVOKED,),
    ).fetchone()
    assert audit is not None
    assert "Vitamin D" in audit[1]


def test_revoke_medication_correction_raises_when_no_correction_exists(
    tmp_path: Path,
) -> None:
    conn = _make_db()
    upsert_medication(conn, _medication_row())

    with pytest.raises(CorrectFactError, match="no user_correction row found"):
        revoke_medication_correction(
            conn, tmp_path, "dummy_alice", "Vitamin D", "supplement", reason="No correction"
        )


# ---------------------------------------------------------------------------
# Guard integration: corrected medication is preserved across re-import
# ---------------------------------------------------------------------------


def test_corrected_medication_preserved_on_reimport(tmp_path: Path) -> None:
    """After apply_medication_correction, re-importing the source skips the row."""
    conn = _make_db()
    source_path = "data/processed/dummy_alice/Medications/meds.txt"
    upsert_medication(conn, _medication_row(dose="1000 IU", source_path=source_path))

    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        reason="Corrected",
    )

    # Re-import with old (conflicting) dose: should skip
    _, skipped = replace_medications_for_source_path(
        conn,
        source_path,
        [_medication_row(dose="1000 IU", source_path=source_path)],
        non_interactive=True,
    )
    assert skipped == 1

    row = conn.execute(
        "SELECT dose, source_type FROM medications WHERE medication_name = 'Vitamin D'"
    ).fetchone()
    assert row[0] == "2000 IU"
    assert row[1] == "user_correction"


def test_corrected_medication_no_longer_blocks_reimport_after_revoke(tmp_path: Path) -> None:
    """After revoke, re-importing the source applies the import value."""
    conn = _make_db()
    source_path = "data/processed/dummy_alice/Medications/meds.txt"
    upsert_medication(conn, _medication_row(dose="1000 IU", source_path=source_path))

    apply_medication_correction(
        conn,
        tmp_path,
        "dummy_alice",
        "Vitamin D",
        "supplement",
        new_dose="2000 IU",
        reason="Corrected",
    )
    revoke_medication_correction(
        conn, tmp_path, "dummy_alice", "Vitamin D", "supplement", reason="Revoked"
    )

    inserted, skipped = replace_medications_for_source_path(
        conn,
        source_path,
        [_medication_row(dose="1000 IU", source_path=source_path)],
        non_interactive=True,
    )
    assert inserted == 1
    assert skipped == 0

    row = conn.execute(
        "SELECT dose, source_type FROM medications WHERE medication_name = 'Vitamin D'"
    ).fetchone()
    assert row[0] == "1000 IU"
    assert row[1] == "manual"


# ---------------------------------------------------------------------------
# History refutations (ADR-004)
# ---------------------------------------------------------------------------


def _write_history_review(vault_root: Path, name: str, source_path: str, facts: str) -> Path:
    note = vault_root / "Records" / "dummy_alice" / "History" / f"{name}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\nperson_id: dummy_alice\nrecord_type: history\n"
        f"source_path: {source_path}\nevent_date: '2099-03-01'\n"
        f"verification_status: accepted\naccepted_facts:\n{facts}---\n\n# History Review\n",
        encoding="utf-8",
    )
    return note


def _fact(description: str, recorded_date: str) -> str:
    return (
        f"- description: {description}\n  category: diagnosis\n  recorded_date: '{recorded_date}'\n"
    )


def _accepted_descriptions(note: Path) -> list[str]:
    import yaml

    text = note.read_text(encoding="utf-8")
    fm = yaml.safe_load(text[3 : text.find("\n---", 3)])
    return [fact["description"] for fact in fm["accepted_facts"]]


def test_history_refutation_hides_label_and_date_across_sources_until_revoked(
    tmp_path: Path,
) -> None:
    conn = _make_db()
    _write_person_note(tmp_path)
    visit = _write_history_review(
        tmp_path,
        "visit",
        "data/processed/a.xml",
        _fact("Synthetic condition, unspecified", "2099-02-01")
        + _fact("Other synthetic condition", "2099-02-01"),
    )
    summary = _write_history_review(
        tmp_path,
        "summary",
        "data/processed/b.xml",
        _fact("synthetic   condition, UNSPECIFIED", "2099-02-01")
        + _fact("Synthetic condition, unspecified", "2099-09-09"),
    )

    result = apply_history_refutation(
        conn,
        tmp_path,
        "dummy_alice",
        "Synthetic condition, unspecified",
        "2099-02-01",
        reason="no supporting evidence",
    )

    assert result["changed_notes"] == 2
    assert _accepted_descriptions(visit) == ["Other synthetic condition"]
    assert _accepted_descriptions(summary) == ["Synthetic condition, unspecified"]
    assert "## Refuted by User" in visit.read_text(encoding="utf-8")
    touched = {"visit.md", "summary.md", "history-corrections.md"}
    assert [
        issue
        for issue in validate_vault_notes(tmp_path)
        if issue.path.name in touched or "history-corrections" in issue.message
    ] == []
    report = tmp_path / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
    report_text = report.read_text(encoding="utf-8")
    assert "#### Synthetic condition, unspecified\nReported on: 2099-09-09\n" in report_text
    conditions = [
        (c["code"]["text"], c.get("recordedDate")) for c in _fhir_resources(tmp_path, "Condition")
    ]
    assert ("Synthetic condition, unspecified", "2099-02-01") not in conditions
    assert ("Synthetic condition, unspecified", "2099-09-09") in conditions
    events = conn.execute(
        "SELECT event_type FROM audit_events WHERE subject_type = 'history'"
    ).fetchall()
    assert events == [(AUDIT_USER_FACT_CORRECTED,)]

    repeat = apply_history_refutation(
        conn,
        tmp_path,
        "dummy_alice",
        "Synthetic condition, unspecified",
        "2099-02-01",
        reason="again",
    )
    assert repeat["changed_notes"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE subject_type = 'history'"
    ).fetchone() == (1,)

    revoke_history_refutation(
        conn,
        tmp_path,
        "dummy_alice",
        "Synthetic condition, unspecified",
        "2099-02-01",
        reason="new evidence",
    )

    assert sorted(_accepted_descriptions(visit)) == [
        "Other synthetic condition",
        "Synthetic condition, unspecified",
    ]
    assert "## Refuted by User" not in visit.read_text(encoding="utf-8")
    assert not (
        tmp_path / "Records" / "dummy_alice" / "History" / "history-corrections.md"
    ).exists()
    last_event = conn.execute(
        "SELECT event_type FROM audit_events WHERE subject_type = 'history' ORDER BY rowid DESC"
    ).fetchone()
    assert last_event == (AUDIT_USER_CORRECTION_REVOKED,)


def test_history_refutation_rejects_unknown_fact(tmp_path: Path) -> None:
    conn = _make_db()
    _write_history_review(
        tmp_path, "visit", "data/processed/a.xml", _fact("Synthetic condition", "2099-02-01")
    )

    with pytest.raises(CorrectFactError, match="no history fact"):
        apply_history_refutation(
            conn, tmp_path, "dummy_alice", "Synthetic condition", "2099-02-02", reason="typo"
        )
    with pytest.raises(CorrectFactError, match="no refutation"):
        revoke_history_refutation(
            conn, tmp_path, "dummy_alice", "Synthetic condition", "2099-02-01", reason="none"
        )


def test_reimported_history_respects_refutation(tmp_path: Path) -> None:
    from famed.history_sync import accept_history_candidates

    conn = _make_db()
    _write_person_note(tmp_path)
    note = _write_history_review(
        tmp_path, "visit", "data/processed/a.xml", _fact("Synthetic condition", "2099-02-01")
    )
    apply_history_refutation(
        conn, tmp_path, "dummy_alice", "Synthetic condition", "2099-02-01", reason="unsupported"
    )

    accept_history_candidates(
        conn=conn,
        vault_root=tmp_path,
        note_path=note,
        accepted_rows=[
            {"description": "Synthetic condition", "recorded_date": "2099-02-01"},
            {"description": "Synthetic finding", "category": "finding"},
        ],
        person_id="dummy_alice",
        source_path="data/processed/a.xml",
    )

    assert _accepted_descriptions(note) == ["Synthetic finding"]
    assert "Synthetic condition (2099-02-01)" in note.read_text(encoding="utf-8")
