from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from famed.lab_sync import (
    LabSyncError,
    replace_lab_results_for_source_path,
    upsert_lab_result,
)
from famed.medication_sync import (
    MedicationSyncError,
    replace_medications_for_source_path,
    upsert_medication,
)
from famed.setup import SCHEMA_SQL
from famed.storage_contract import (
    AUDIT_SKIPPED_USER_CORRECTION,
    AUDIT_USER_CORRECTION_REVOKED,
    ConflictChoice,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_alice', 'Dummy Alice')")
    return conn


def _lab(
    *,
    person_id: str = "dummy_alice",
    test_name: str = "Glucose",
    value: float = 95.0,
    observed_at: str = "2023-01-15",
    source_type: str = "manual",
    source_path: str = "data/processed/dummy_alice/Labs/lab.csv",
    row_index: int = 0,
) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "loinc_code": "",
        "test_name": test_name,
        "value": value,
        "unit": "mg/dL",
        "reference_range_low": 70.0,
        "reference_range_high": 100.0,
        "observed_at": observed_at,
        "source_type": source_type,
        "source_path": source_path,
        "row_index": row_index,
    }


def _med(
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


def _stub_resolver(choice: ConflictChoice):  # type: ignore[no-untyped-def]
    """Return a resolver stub that always returns *choice*."""

    def _resolve(*_args: object, **_kwargs: object) -> ConflictChoice:
        return choice

    return _resolve


SOURCE = "data/processed/dummy_alice/Labs/lab.csv"
MED_SOURCE = "data/processed/dummy_alice/Medications/meds.txt"


# ---------------------------------------------------------------------------
# Labs — no resolver provided + non_interactive=False raises
# ---------------------------------------------------------------------------


def test_lab_interactive_no_resolver_raises() -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab(value=95.0, source_type="user_correction"))

    with pytest.raises(LabSyncError, match="no conflict_resolver was provided"):
        replace_lab_results_for_source_path(conn, SOURCE, [_lab(value=80.0)], non_interactive=False)


# ---------------------------------------------------------------------------
# Labs — keep_correction
# ---------------------------------------------------------------------------


def test_lab_interactive_keep_correction_skips_and_audits() -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab(value=90.0, source_type="user_correction"))

    _, skipped = replace_lab_results_for_source_path(
        conn,
        SOURCE,
        [_lab(value=80.0)],
        non_interactive=False,
        conflict_resolver=_stub_resolver("keep_correction"),
    )
    assert skipped == 1

    row = conn.execute(
        "SELECT value, source_type FROM lab_results WHERE test_name = 'Glucose'"
    ).fetchone()
    assert row[0] == 90.0
    assert row[1] == "user_correction"

    audit = conn.execute(
        "SELECT event_type FROM audit_events WHERE event_type = ?",
        (AUDIT_SKIPPED_USER_CORRECTION,),
    ).fetchone()
    assert audit is not None


# ---------------------------------------------------------------------------
# Labs — accept_import (no revoke event)
# ---------------------------------------------------------------------------


def test_lab_interactive_accept_import_inserts_and_no_revoke_event() -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab(value=90.0, source_type="user_correction"))

    inserted, skipped = replace_lab_results_for_source_path(
        conn,
        SOURCE,
        [_lab(value=80.0)],
        non_interactive=False,
        conflict_resolver=_stub_resolver("accept_import"),
    )
    assert inserted == 1
    assert skipped == 0

    row = conn.execute(
        "SELECT value, source_type FROM lab_results WHERE test_name = 'Glucose'"
    ).fetchone()
    assert row[0] == 80.0
    assert row[1] == "manual"

    revoke = conn.execute(
        "SELECT id FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_CORRECTION_REVOKED,),
    ).fetchone()
    assert revoke is None


# ---------------------------------------------------------------------------
# Labs — revoke_and_import (revoke event emitted)
# ---------------------------------------------------------------------------


def test_lab_interactive_revoke_and_import_emits_revoke_event() -> None:
    conn = _make_db()
    upsert_lab_result(conn, _lab(value=90.0, source_type="user_correction"))

    inserted, _ = replace_lab_results_for_source_path(
        conn,
        SOURCE,
        [_lab(value=80.0)],
        non_interactive=False,
        conflict_resolver=_stub_resolver("revoke_and_import"),
    )
    assert inserted == 1

    row = conn.execute(
        "SELECT value, source_type FROM lab_results WHERE test_name = 'Glucose'"
    ).fetchone()
    assert row[0] == 80.0

    revoke = conn.execute(
        "SELECT event_type FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_CORRECTION_REVOKED,),
    ).fetchone()
    assert revoke is not None


# ---------------------------------------------------------------------------
# Medications — no resolver provided + non_interactive=False raises
# ---------------------------------------------------------------------------


def test_medication_interactive_no_resolver_raises() -> None:
    conn = _make_db()
    upsert_medication(conn, _med(dose="2000 IU", source_type="user_correction"))

    with pytest.raises(MedicationSyncError, match="no conflict_resolver was provided"):
        replace_medications_for_source_path(
            conn, MED_SOURCE, [_med(dose="1000 IU")], non_interactive=False
        )


# ---------------------------------------------------------------------------
# Medications — keep_correction
# ---------------------------------------------------------------------------


def test_medication_interactive_keep_correction_skips_and_audits() -> None:
    conn = _make_db()
    upsert_medication(conn, _med(dose="2000 IU", source_type="user_correction"))

    _, skipped = replace_medications_for_source_path(
        conn,
        MED_SOURCE,
        [_med(dose="1000 IU")],
        non_interactive=False,
        conflict_resolver=_stub_resolver("keep_correction"),
    )
    assert skipped == 1

    row = conn.execute(
        "SELECT dose, source_type FROM medications WHERE medication_name = 'Vitamin D'"
    ).fetchone()
    assert row[0] == "2000 IU"
    assert row[1] == "user_correction"

    audit = conn.execute(
        "SELECT event_type FROM audit_events WHERE event_type = ?",
        (AUDIT_SKIPPED_USER_CORRECTION,),
    ).fetchone()
    assert audit is not None


# ---------------------------------------------------------------------------
# Medications — accept_import
# ---------------------------------------------------------------------------


def test_medication_interactive_accept_import_inserts_no_revoke_event() -> None:
    conn = _make_db()
    upsert_medication(conn, _med(dose="2000 IU", source_type="user_correction"))

    inserted, _ = replace_medications_for_source_path(
        conn,
        MED_SOURCE,
        [_med(dose="1000 IU")],
        non_interactive=False,
        conflict_resolver=_stub_resolver("accept_import"),
    )
    assert inserted == 1

    row = conn.execute(
        "SELECT dose, source_type FROM medications WHERE medication_name = 'Vitamin D'"
    ).fetchone()
    assert row[0] == "1000 IU"
    assert row[1] == "manual"

    revoke = conn.execute(
        "SELECT id FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_CORRECTION_REVOKED,),
    ).fetchone()
    assert revoke is None


# ---------------------------------------------------------------------------
# Medications — revoke_and_import
# ---------------------------------------------------------------------------


def test_medication_interactive_revoke_and_import_emits_revoke_event() -> None:
    conn = _make_db()
    upsert_medication(conn, _med(dose="2000 IU", source_type="user_correction"))

    inserted, _ = replace_medications_for_source_path(
        conn,
        MED_SOURCE,
        [_med(dose="1000 IU")],
        non_interactive=False,
        conflict_resolver=_stub_resolver("revoke_and_import"),
    )
    assert inserted == 1

    revoke = conn.execute(
        "SELECT event_type FROM audit_events WHERE event_type = ?",
        (AUDIT_USER_CORRECTION_REVOKED,),
    ).fetchone()
    assert revoke is not None
