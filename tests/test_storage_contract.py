from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from famed.storage_contract import (
    AUDIT_DEPENDENT_OUTPUT_MARKED_STALE,
    AUDIT_HISTORY_IMPORT_NEEDS_REVIEW,
    AUDIT_IMMUNIZATION_IMPORT_ACCEPTED,
    AUDIT_INBOX_ROUTE_QUARANTINED,
    AUDIT_LAB_IMPORT_ACCEPTED,
    AUDIT_LAB_OBSERVATION_RESOLUTION_RECORDED,
    AUDIT_LAB_SOURCE_PATH_MIGRATED,
    AUDIT_MEDICATION_CONFLICT_DETECTED,
    AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED,
    AUDIT_MEDICATION_IMPORT_ACCEPTED,
    AUDIT_PLACEHOLDER_IMPORT_ACCEPTED,
    AUDIT_SKIPPED_USER_CORRECTION,
    AUDIT_USER_CORRECTION_REVOKED,
    AUDIT_USER_FACT_CORRECTED,
    AUTHORITY_DB_WINS,
    AUTHORITY_VAULT_WINS,
    INBOX_ROOT,
    PROCESSED_ROOT,
    QUARANTINE_ROOT,
    REFERENCE_ROOT,
    RESOLUTION_QUARANTINED,
    STATE_AWAITING_USER_DECISION,
    STATE_PROCESSED,
    STATE_QUARANTINED,
    STATE_STALE,
    configured_storage_roots,
    log_audit_event,
    mark_dependent_output_stale,
    validate_audit_event_type,
    validate_authority_decision,
    validate_processing_state,
    validate_resolution,
)


def test_configured_storage_roots_are_repo_local() -> None:
    root = Path("/repo")

    storage_roots = configured_storage_roots(root)

    assert storage_roots.inbox == root / INBOX_ROOT
    assert storage_roots.processed == root / PROCESSED_ROOT
    assert storage_roots.quarantine == root / QUARANTINE_ROOT
    assert storage_roots.reference == root / REFERENCE_ROOT


def test_storage_contract_validates_processing_states() -> None:
    assert validate_processing_state(STATE_PROCESSED) == "processed"
    assert validate_processing_state(STATE_QUARANTINED) == "quarantined"
    assert validate_processing_state(STATE_AWAITING_USER_DECISION) == "awaiting_user_decision"
    assert validate_processing_state(STATE_STALE) == "stale"

    with pytest.raises(ValueError, match="invalid processing state"):
        validate_processing_state("imported")


def test_storage_contract_validates_authority_decisions() -> None:
    assert validate_authority_decision(AUTHORITY_VAULT_WINS) == "vault_wins"
    assert validate_authority_decision(AUTHORITY_DB_WINS) == "db_wins"

    with pytest.raises(ValueError, match="invalid authority decision"):
        validate_authority_decision("conflict")


def test_storage_contract_validates_resolution_labels() -> None:
    assert validate_resolution(RESOLUTION_QUARANTINED) == "quarantined"

    with pytest.raises(ValueError, match="invalid resolution"):
        validate_resolution("accepted")


def test_storage_contract_validates_audit_event_types() -> None:
    assert (
        validate_audit_event_type(AUDIT_HISTORY_IMPORT_NEEDS_REVIEW)
        == "history_import_needs_review"
    )
    assert (
        validate_audit_event_type(AUDIT_IMMUNIZATION_IMPORT_ACCEPTED)
        == "immunization_import_accepted"
    )
    assert validate_audit_event_type(AUDIT_LAB_IMPORT_ACCEPTED) == "lab_import_accepted"
    assert (
        validate_audit_event_type(AUDIT_LAB_OBSERVATION_RESOLUTION_RECORDED)
        == "lab_observation_resolution_recorded"
    )
    assert validate_audit_event_type(AUDIT_LAB_SOURCE_PATH_MIGRATED) == "lab_source_path_migrated"
    assert (
        validate_audit_event_type(AUDIT_PLACEHOLDER_IMPORT_ACCEPTED)
        == "placeholder_import_accepted"
    )
    assert (
        validate_audit_event_type(AUDIT_MEDICATION_IMPORT_ACCEPTED) == "medication_import_accepted"
    )
    assert (
        validate_audit_event_type(AUDIT_MEDICATION_CONFLICT_DETECTED)
        == "medication_conflict_detected"
    )
    assert (
        validate_audit_event_type(AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED)
        == "medication_conflict_resolution_recorded"
    )
    assert validate_audit_event_type(AUDIT_INBOX_ROUTE_QUARANTINED) == "inbox_route_quarantined"
    assert (
        validate_audit_event_type(AUDIT_DEPENDENT_OUTPUT_MARKED_STALE)
        == "dependent_output_marked_stale"
    )
    assert validate_audit_event_type(AUDIT_USER_FACT_CORRECTED) == "user_fact_corrected"
    assert validate_audit_event_type(AUDIT_SKIPPED_USER_CORRECTION) == "skipped_user_correction"
    assert validate_audit_event_type(AUDIT_USER_CORRECTION_REVOKED) == "user_correction_revoked"

    with pytest.raises(ValueError, match="invalid audit event type"):
        validate_audit_event_type("imported")


def test_log_audit_event_creates_append_only_row() -> None:
    conn = sqlite3.connect(":memory:")

    log_audit_event(
        conn,
        event_type=AUDIT_LAB_IMPORT_ACCEPTED,
        subject_type="lab_panel",
        subject_id="dummy_alice",
        source_path="data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
        status=STATE_PROCESSED,
        message="note: Records/dummy_alice/Labs/2023-04-20-panel.md",
    )

    row = conn.execute(
        """
        SELECT event_type, subject_type, subject_id, source_path, status, message
        FROM audit_events
        """
    ).fetchone()

    assert row == (
        "lab_import_accepted",
        "lab_panel",
        "dummy_alice",
        "data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
        "processed",
        "note: Records/dummy_alice/Labs/2023-04-20-panel.md",
    )


def test_mark_dependent_output_stale_records_audit_event() -> None:
    conn = sqlite3.connect(":memory:")

    mark_dependent_output_stale(
        conn,
        output_path="Records/dummy_alice/Reports/latest-lab-results.md",
        reason="lab value corrected",
        source_path="data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
        person_id="dummy_alice",
    )

    row = conn.execute(
        """
        SELECT event_type, subject_type, subject_id, source_path, status, message
        FROM audit_events
        """
    ).fetchone()

    assert row == (
        "dependent_output_marked_stale",
        "derived_output",
        "dummy_alice",
        "data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
        "stale",
        "Records/dummy_alice/Reports/latest-lab-results.md: lab value corrected",
    )


def test_mark_dependent_output_stale_requires_reason() -> None:
    conn = sqlite3.connect(":memory:")

    with pytest.raises(ValueError, match="reason is required"):
        mark_dependent_output_stale(
            conn,
            output_path="Records/dummy_alice/Reports/latest-lab-results.md",
            reason=" ",
        )
