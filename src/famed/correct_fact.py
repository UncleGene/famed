"""Deterministic apply/revoke path for user-directed fact corrections.

Implements ADR-004 / REQ-023: a user_correction row is the highest-authority
representation of a fact and is preserved across all re-import and reprocess runs
until explicitly revoked.

The public surface is four functions:
  apply_lab_correction        – set source_type='user_correction' on a lab row
  revoke_lab_correction       – remove user_correction flag from a lab row
  apply_medication_correction – set source_type='user_correction' on a medication row
  revoke_medication_correction– remove user_correction flag from a medication row
  apply_history_refutation    – refute a history fact by label and date (ADR-004)
  revoke_history_refutation   – withdraw a history refutation

Each function updates the DB row in-place, regenerates affected vault notes and
reports, and logs the appropriate audit event.  The AI never calls these functions
autonomously; they are invoked only after the user has confirmed the correction
through the fm-correct-fact skill.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from famed.dependent_reports import (
    refresh_fhir_dependent_export,
    refresh_history_dependent_reports,
    refresh_lab_dependent_reports,
    refresh_medication_dependent_reports,
)
from famed.history_sync import (
    _refresh_history_pending_review,
    apply_history_refutations_to_notes,
    history_fact_exists,
    history_refutation_key,
    load_history_refutations,
    write_history_refutations,
    write_medical_history_summary_report,
)
from famed.medication_sync import (
    _normalize_frequency,
    build_medication_conflicts_from_db,
    medication_conflict_axis_value_is_stated,
    medication_conflict_identity,
    medication_name_key,
    pending_medication_conflict_axes,
    record_medication_conflict_detection_events,
    record_medication_conflict_resolution_events,
    write_current_medications_report,
    write_medication_conflicts_report,
)
from famed.storage_contract import (
    AUDIT_USER_CORRECTION_REVOKED,
    AUDIT_USER_FACT_CORRECTED,
    STATE_PROCESSED,
    log_audit_event,
)

# Synthetic source_path used for correction rows that have no originating source import.
_USER_CORRECTION_SOURCE = "__user_correction__"


class CorrectFactError(Exception):
    """Raised when a fact correction cannot be applied or revoked."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _synthetic_lab_row_id(person_id: str, test_name: str, observed_at: str) -> str:
    """Stable synthetic row ID for a user-correction lab row with no originating source."""
    raw = f"user_correction|{person_id}|{test_name}|{observed_at}".encode()
    return hashlib.sha1(raw).hexdigest()


def _synthetic_medication_row_id(person_id: str, name_key: str, medication_type: str) -> str:
    """Stable synthetic row ID for a user-correction medication row with no originating source."""
    raw = f"user_correction|{person_id}|{name_key}|{medication_type}".encode()
    return hashlib.sha1(raw).hexdigest()


def _fetch_lab_rows(
    conn: sqlite3.Connection,
    person_id: str,
    test_name: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    """Return all DB rows for (person_id, test_name, observed_at)."""
    rows = conn.execute(
        """
        SELECT id, value, value_text, unit, loinc_code,
               reference_range_low, reference_range_high,
               source_type, source_path
        FROM lab_results
        WHERE person_id = ? AND test_name = ? AND observed_at = ?
        ORDER BY source_type = 'user_correction' DESC, created_at ASC
        """,
        (person_id, test_name, observed_at),
    ).fetchall()
    return [
        {
            "id": row[0],
            "value": row[1],
            "value_text": row[2],
            "unit": row[3],
            "loinc_code": row[4],
            "reference_range_low": row[5],
            "reference_range_high": row[6],
            "source_type": row[7],
            "source_path": row[8],
        }
        for row in rows
    ]


def _fetch_medication_rows(
    conn: sqlite3.Connection,
    person_id: str,
    name_key: str,
    medication_type: str,
) -> list[dict[str, Any]]:
    """Return all DB rows for (person_id, normalised name, medication_type)."""
    rows = conn.execute(
        """
        SELECT id, medication_name, dose, frequency, status, rxnorm_cui,
               start_date, end_date, source_type, source_path
        FROM medications
        WHERE person_id = ? AND medication_type = ?
        ORDER BY source_type = 'user_correction' DESC, created_at ASC
        """,
        (person_id, medication_type),
    ).fetchall()
    matching = []
    for row in rows:
        if medication_name_key(row[1]) == name_key:
            matching.append(
                {
                    "id": row[0],
                    "medication_name": row[1],
                    "dose": row[2],
                    "frequency": row[3],
                    "status": row[4],
                    "rxnorm_cui": row[5],
                    "start_date": row[6],
                    "end_date": row[7],
                    "source_type": row[8],
                    "source_path": row[9],
                }
            )
    return matching


def _fetch_medication_conflict_identity_rows(
    conn: sqlite3.Connection,
    person_id: str,
    conflict_key: str,
    medication_type: str,
) -> list[dict[str, Any]]:
    """Return rows matching the medication-conflict identity for a pending contradiction."""
    rows = conn.execute(
        """
        SELECT id, medication_name, dose, frequency, status, rxnorm_cui,
               start_date, end_date, source_type, source_path
        FROM medications
        WHERE person_id = ? AND medication_type = ?
        ORDER BY source_type = 'user_correction' DESC, created_at ASC
        """,
        (person_id, medication_type),
    ).fetchall()
    return [
        {
            "id": row[0],
            "medication_name": row[1],
            "dose": row[2],
            "frequency": row[3],
            "status": row[4],
            "rxnorm_cui": row[5],
            "start_date": row[6],
            "end_date": row[7],
            "source_type": row[8],
            "source_path": row[9],
        }
        for row in rows
        if medication_conflict_identity(row[1], medication_type)[0] == conflict_key
    ]


def _prefer_matching_medication_row(
    rows: list[dict[str, Any]],
    *,
    dose: str | None,
    frequency: str | None,
) -> list[dict[str, Any]]:
    """Prefer an existing row that already carries the selected correction values."""
    if not rows:
        return rows

    def selected_value_score(row: dict[str, Any]) -> tuple[int, int]:
        dose_score = int(
            dose is not None
            and str(row.get("dose") or "").strip().casefold() == dose.strip().casefold()
        )
        frequency_score = int(
            frequency is not None
            and _normalize_frequency(str(row.get("frequency") or ""))
            == _normalize_frequency(frequency)
        )
        return frequency_score, dose_score

    return sorted(rows, key=selected_value_score, reverse=True)


def _regenerate_lab_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> None:
    refresh_lab_dependent_reports(conn, vault_root, person_id)


def _regenerate_medication_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> None:
    write_current_medications_report(vault_root, person_id, conn)
    write_medication_conflicts_report(conn, vault_root, person_id)
    refresh_medication_dependent_reports(conn, vault_root, person_id)
    refresh_fhir_dependent_export(conn, vault_root, person_id)


# ---------------------------------------------------------------------------
# Lab corrections
# ---------------------------------------------------------------------------


def apply_lab_correction(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    test_name: str,
    observed_at: str,
    new_value: float,
    new_unit: str | None = None,
    *,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Apply a user correction to a lab result.

    Finds the existing row (preferring any existing user_correction row),
    records the old value, updates source_type to 'user_correction', and
    regenerates affected vault notes and reports.

    Returns a summary dict with old_value, new_value, row_id.
    """
    existing_rows = _fetch_lab_rows(conn, person_id, test_name, observed_at)

    if existing_rows:
        target = existing_rows[0]  # user_correction rows are sorted first
        old_value = target["value"]
        old_unit = target["unit"]
        row_id = target["id"]
        conn.execute(
            (
                "UPDATE lab_results SET value = ?, value_text = ?, unit = ?, source_type = "
                "'user_correction' WHERE id = ?"
            ),
            (
                new_value,
                str(new_value),
                new_unit if new_unit is not None else old_unit,
                row_id,
            ),
        )
    else:
        # No existing row — insert a new correction row with a synthetic source path.
        row_id = _synthetic_lab_row_id(person_id, test_name, observed_at)
        old_value = None
        conn.execute(
            """
            INSERT INTO lab_results (id, person_id, test_name, value, value_text, unit, observed_at,
                                     source_type, source_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'user_correction', ?)
            ON CONFLICT(id) DO UPDATE SET
                value = excluded.value,
                value_text = excluded.value_text,
                unit = excluded.unit,
                source_type = 'user_correction'
            """,
            (
                row_id,
                person_id,
                test_name,
                new_value,
                str(new_value),
                new_unit or "",
                observed_at,
                _USER_CORRECTION_SOURCE,
            ),
        )

    log_audit_event(
        conn,
        event_type=AUDIT_USER_FACT_CORRECTED,
        subject_type="lab_result",
        subject_id=person_id,
        source_path=_USER_CORRECTION_SOURCE,
        status=STATE_PROCESSED,
        message=(
            f"user corrected lab {test_name!r} at {observed_at}: "
            f"{old_value!r} → {new_value!r}"
            + (f" [{new_unit}]" if new_unit else "")
            + f"; reason: {reason}; actor: {actor}"
        ),
    )

    _regenerate_lab_reports(conn, vault_root, person_id)

    return {
        "row_id": row_id,
        "person_id": person_id,
        "test_name": test_name,
        "observed_at": observed_at,
        "old_value": old_value,
        "new_value": new_value,
        "unit": new_unit,
    }


def revoke_lab_correction(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    test_name: str,
    observed_at: str,
    *,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Revoke a user correction on a lab result.

    If the correction row has a synthetic source path, it is deleted entirely.
    If it was an in-place update of an import row, source_type is restored to
    'manual' so the next re-import of the originating source can apply its value.

    Returns a summary dict with old_value and revoked_row_id.
    """
    existing_rows = _fetch_lab_rows(conn, person_id, test_name, observed_at)
    correction_row = next((r for r in existing_rows if r["source_type"] == "user_correction"), None)

    if correction_row is None:
        raise CorrectFactError(
            f"no user_correction row found for lab {test_name!r} at {observed_at} "
            f"for person {person_id!r}"
        )

    old_value = correction_row["value"]
    row_id = correction_row["id"]

    if correction_row["source_path"] == _USER_CORRECTION_SOURCE:
        conn.execute("DELETE FROM lab_results WHERE id = ?", (row_id,))
    else:
        conn.execute("UPDATE lab_results SET source_type = 'manual' WHERE id = ?", (row_id,))

    log_audit_event(
        conn,
        event_type=AUDIT_USER_CORRECTION_REVOKED,
        subject_type="lab_result",
        subject_id=person_id,
        source_path=_USER_CORRECTION_SOURCE,
        status=STATE_PROCESSED,
        message=(
            f"user revoked correction on lab {test_name!r} at {observed_at}; "
            f"corrected value was {old_value!r}; reason: {reason}; actor: {actor}"
        ),
    )

    _regenerate_lab_reports(conn, vault_root, person_id)

    return {
        "revoked_row_id": row_id,
        "person_id": person_id,
        "test_name": test_name,
        "observed_at": observed_at,
        "old_value": old_value,
    }


# ---------------------------------------------------------------------------
# Medication corrections
# ---------------------------------------------------------------------------


def apply_medication_correction(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    medication_name: str,
    medication_type: str,
    new_dose: str | None = None,
    new_frequency: str | None = None,
    *,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Apply a user correction to a medication row.

    Finds the existing row (preferring any existing user_correction row),
    records the old dose/frequency, updates source_type to 'user_correction',
    and regenerates affected vault reports.

    Returns a summary dict with old_dose, new_dose, old_frequency, new_frequency.
    """
    name_key = medication_name_key(medication_name)
    existing_rows = _fetch_medication_rows(conn, person_id, name_key, medication_type)

    pending_axes = pending_medication_conflict_axes(
        conn,
        person_id=person_id,
        medication_name=medication_name,
        medication_type=medication_type,
    )
    supplied_axes = frozenset(
        axis
        for axis, value in (("dose", new_dose), ("frequency", new_frequency))
        if medication_conflict_axis_value_is_stated(axis, value)
    )
    missing_axes = pending_axes - supplied_axes
    if missing_axes:
        missing_list = ", ".join(sorted(missing_axes))
        raise CorrectFactError(
            "all pending medication conflict axes require explicit user values "
            f"before applying row-level authority; missing: {missing_list}"
        )
    if not existing_rows and pending_axes:
        conflict_key, canonical_type = medication_conflict_identity(
            medication_name, medication_type
        )
        existing_rows = _fetch_medication_conflict_identity_rows(
            conn, person_id, conflict_key, canonical_type
        )
    existing_rows = _prefer_matching_medication_row(
        existing_rows,
        dose=new_dose,
        frequency=new_frequency,
    )

    if existing_rows:
        target = existing_rows[0]  # user_correction rows sorted first unless a candidate matches.
        old_dose = target["dose"]
        old_frequency = target["frequency"]
        row_id = target["id"]
    else:
        row_id = _synthetic_medication_row_id(person_id, name_key, medication_type)
        old_dose = None
        old_frequency = None

    updated_dose = new_dose if new_dose is not None else old_dose
    updated_frequency = new_frequency if new_frequency is not None else old_frequency
    resolved_conflicts = record_medication_conflict_resolution_events(
        conn,
        person_id=person_id,
        medication_name=medication_name,
        medication_type=medication_type,
        resolved_dose=updated_dose,
        resolved_frequency=updated_frequency,
        actor=actor,
        reason=reason,
    )

    if existing_rows:
        conn.execute(
            (
                "UPDATE medications SET dose = ?, frequency = ?, source_type = "
                "'user_correction' WHERE id = ?"
            ),
            (updated_dose, updated_frequency, row_id),
        )
    else:
        # No existing row — insert a new correction row.
        conn.execute(
            """
            INSERT INTO medications (id, person_id, medication_name, medication_type,
                                     dose, frequency, status, source_type, source_path)
            VALUES (?, ?, ?, ?, ?, ?, 'active', 'user_correction', ?)
            ON CONFLICT(id) DO UPDATE SET
                dose = excluded.dose,
                frequency = excluded.frequency,
                source_type = 'user_correction'
            """,
            (
                row_id,
                person_id,
                medication_name,
                medication_type,
                updated_dose,
                updated_frequency,
                _USER_CORRECTION_SOURCE,
            ),
        )

    log_audit_event(
        conn,
        event_type=AUDIT_USER_FACT_CORRECTED,
        subject_type="medication",
        subject_id=person_id,
        source_path=_USER_CORRECTION_SOURCE,
        status=STATE_PROCESSED,
        message=(
            f"user corrected medication {medication_name!r} ({medication_type}): "
            f"dose {old_dose!r} → {updated_dose!r}, "
            f"frequency {old_frequency!r} → {updated_frequency!r}; "
            f"reason: {reason}; actor: {actor}"
        ),
    )

    _regenerate_medication_reports(conn, vault_root, person_id)

    return {
        "row_id": row_id,
        "person_id": person_id,
        "medication_name": medication_name,
        "medication_type": medication_type,
        "old_dose": old_dose,
        "new_dose": updated_dose,
        "old_frequency": old_frequency,
        "new_frequency": updated_frequency,
        "resolved_conflicts": resolved_conflicts,
    }


def revoke_medication_correction(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    medication_name: str,
    medication_type: str,
    *,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Revoke a user correction on a medication row.

    If the correction row has a synthetic source path, it is deleted.
    Otherwise source_type is restored to 'manual'.

    Returns a summary dict with revoked_row_id and old values.
    """
    name_key = medication_name_key(medication_name)
    existing_rows = _fetch_medication_rows(conn, person_id, name_key, medication_type)
    correction_row = next((r for r in existing_rows if r["source_type"] == "user_correction"), None)

    if correction_row is None:
        raise CorrectFactError(
            f"no user_correction row found for medication {medication_name!r} "
            f"({medication_type}) for person {person_id!r}"
        )

    old_dose = correction_row["dose"]
    old_frequency = correction_row["frequency"]
    row_id = correction_row["id"]

    if correction_row["source_path"] == _USER_CORRECTION_SOURCE:
        conn.execute("DELETE FROM medications WHERE id = ?", (row_id,))
    else:
        conn.execute("UPDATE medications SET source_type = 'manual' WHERE id = ?", (row_id,))

    log_audit_event(
        conn,
        event_type=AUDIT_USER_CORRECTION_REVOKED,
        subject_type="medication",
        subject_id=person_id,
        source_path=_USER_CORRECTION_SOURCE,
        status=STATE_PROCESSED,
        message=(
            f"user revoked correction on medication {medication_name!r} ({medication_type}); "
            f"corrected dose was {old_dose!r}, frequency was {old_frequency!r}; "
            f"reason: {reason}; actor: {actor}"
        ),
    )
    record_medication_conflict_detection_events(
        conn,
        person_id,
        build_medication_conflicts_from_db(conn, person_id),
        reopen_resolved=True,
    )

    _regenerate_medication_reports(conn, vault_root, person_id)

    return {
        "revoked_row_id": row_id,
        "person_id": person_id,
        "medication_name": medication_name,
        "medication_type": medication_type,
        "old_dose": old_dose,
        "old_frequency": old_frequency,
    }


def _regenerate_history_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> int:
    changed_notes = apply_history_refutations_to_notes(vault_root, person_id)
    write_medical_history_summary_report(vault_root, person_id)
    refresh_history_dependent_reports(conn, vault_root, person_id)
    refresh_fhir_dependent_export(conn, vault_root, person_id)
    _refresh_history_pending_review(conn, vault_root, person_id)
    return changed_notes


def apply_history_refutation(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    description: str,
    recorded_date: str,
    *,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Refute a history fact for a person by exact label and recorded date.

    Every accepted history fact from any source with the same normalized label and
    date moves out of ``accepted_facts`` and so out of every report, now and on
    later reprocess or rebuild, until revoked. Retained sources are untouched.
    """
    key = history_refutation_key(description, recorded_date)
    if not history_fact_exists(vault_root, person_id, key):
        raise CorrectFactError(
            f"no history fact {description!r} dated {recorded_date} for person {person_id!r}"
        )
    refutations = load_history_refutations(vault_root, person_id)
    if any(
        history_refutation_key(r["description"], r["recorded_date"]) == key for r in refutations
    ):
        return {
            "person_id": person_id,
            "description": description,
            "recorded_date": recorded_date,
            "changed_notes": 0,
        }
    refutations.append(
        {
            "description": " ".join(description.split()),
            "recorded_date": recorded_date,
            "reason": reason,
            "refuted_at": date.today().isoformat(),
        }
    )
    write_history_refutations(vault_root, person_id, refutations)
    log_audit_event(
        conn,
        event_type=AUDIT_USER_FACT_CORRECTED,
        subject_type="history",
        subject_id=person_id,
        source_path=_USER_CORRECTION_SOURCE,
        status=STATE_PROCESSED,
        message=(
            f"user refuted history fact {description!r} dated {recorded_date}; "
            f"reason: {reason}; actor: {actor}"
        ),
    )
    changed_notes = _regenerate_history_reports(conn, vault_root, person_id)
    return {
        "person_id": person_id,
        "description": description,
        "recorded_date": recorded_date,
        "changed_notes": changed_notes,
    }


def revoke_history_refutation(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    description: str,
    recorded_date: str,
    *,
    reason: str,
    actor: str = "user",
) -> dict[str, Any]:
    """Withdraw a history refutation and restore matching facts to accepted facts."""
    key = history_refutation_key(description, recorded_date)
    refutations = load_history_refutations(vault_root, person_id)
    remaining = [
        r
        for r in refutations
        if history_refutation_key(r["description"], r["recorded_date"]) != key
    ]
    if len(remaining) == len(refutations):
        raise CorrectFactError(
            f"no refutation of {description!r} dated {recorded_date} for person {person_id!r}"
        )
    write_history_refutations(vault_root, person_id, remaining)
    log_audit_event(
        conn,
        event_type=AUDIT_USER_CORRECTION_REVOKED,
        subject_type="history",
        subject_id=person_id,
        source_path=_USER_CORRECTION_SOURCE,
        status=STATE_PROCESSED,
        message=(
            f"user revoked refutation of history fact {description!r} dated {recorded_date}; "
            f"reason: {reason}; actor: {actor}"
        ),
    )
    changed_notes = _regenerate_history_reports(conn, vault_root, person_id)
    return {
        "person_id": person_id,
        "description": description,
        "recorded_date": recorded_date,
        "changed_notes": changed_notes,
    }
