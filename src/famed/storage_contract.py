#!/usr/bin/env python3
"""Shared storage authority and ingestion-state contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

ProcessingState = Literal["processed", "quarantined", "awaiting_user_decision", "stale"]
AuthorityDecision = Literal["new", "source_wins", "vault_wins", "db_wins", "ambiguous"]
Resolution = Literal["source_wins", "vault_wins", "db_wins", "quarantined", "user_resolved"]
ConflictChoice = Literal["keep_correction", "accept_import", "revoke_and_import"]
AuditEventType = Literal[
    "history_import_needs_review",
    "history_candidates_accepted",
    "history_source_reviewed_no_facts",
    "history_source_reprocessed",
    "immunization_import_accepted",
    "immunization_source_reprocessed",
    "lab_import_accepted",
    "lab_import_quarantined",
    "lab_inferred_loinc_repaired",
    "lab_source_provided_loinc_overridden",
    "lab_panel_note_marked_superseded",
    "lab_source_path_migrated",
    "lab_source_reprocessed",
    "medication_import_accepted",
    "medication_conflict_detected",
    "medication_conflict_resolution_recorded",
    "medication_redundant_row_superseded",
    "medication_deduplicated_row_restored",
    "lab_deduplicated_row_restored",
    "lab_observation_rejected",
    "lab_value_implausible",
    "lab_observation_resolution_recorded",
    "medication_source_note_retired",
    "medication_note_marked_superseded",
    "medication_source_reprocessed",
    "user_fact_corrected",
    "skipped_user_correction",
    "user_correction_revoked",
    "user_update_recorded",
    "inbox_route_quarantined",
    "placeholder_import_accepted",
    "sync_resolution_recorded",
    "dependent_output_marked_stale",
    "vital_import_needs_review",
]

STATE_PROCESSED: ProcessingState = "processed"
STATE_QUARANTINED: ProcessingState = "quarantined"
STATE_AWAITING_USER_DECISION: ProcessingState = "awaiting_user_decision"
STATE_STALE: ProcessingState = "stale"
VALID_PROCESSING_STATES: tuple[ProcessingState, ...] = (
    STATE_PROCESSED,
    STATE_QUARANTINED,
    STATE_AWAITING_USER_DECISION,
    STATE_STALE,
)

AUTHORITY_NEW: AuthorityDecision = "new"
AUTHORITY_SOURCE_WINS: AuthorityDecision = "source_wins"
AUTHORITY_VAULT_WINS: AuthorityDecision = "vault_wins"
AUTHORITY_DB_WINS: AuthorityDecision = "db_wins"
AUTHORITY_AMBIGUOUS: AuthorityDecision = "ambiguous"
VALID_AUTHORITY_DECISIONS: tuple[AuthorityDecision, ...] = (
    AUTHORITY_NEW,
    AUTHORITY_SOURCE_WINS,
    AUTHORITY_VAULT_WINS,
    AUTHORITY_DB_WINS,
    AUTHORITY_AMBIGUOUS,
)

RESOLUTION_SOURCE_WINS: Resolution = "source_wins"
RESOLUTION_VAULT_WINS: Resolution = "vault_wins"
RESOLUTION_DB_WINS: Resolution = "db_wins"
RESOLUTION_QUARANTINED: Resolution = "quarantined"
RESOLUTION_USER_RESOLVED: Resolution = "user_resolved"
VALID_RESOLUTIONS: tuple[Resolution, ...] = (
    RESOLUTION_SOURCE_WINS,
    RESOLUTION_VAULT_WINS,
    RESOLUTION_DB_WINS,
    RESOLUTION_QUARANTINED,
    RESOLUTION_USER_RESOLVED,
)

AUDIT_HISTORY_IMPORT_NEEDS_REVIEW: AuditEventType = "history_import_needs_review"
AUDIT_HISTORY_CANDIDATES_ACCEPTED: AuditEventType = "history_candidates_accepted"
AUDIT_HISTORY_SOURCE_REVIEWED_NO_FACTS: AuditEventType = "history_source_reviewed_no_facts"
AUDIT_HISTORY_SOURCE_REPROCESSED: AuditEventType = "history_source_reprocessed"
AUDIT_IMMUNIZATION_IMPORT_ACCEPTED: AuditEventType = "immunization_import_accepted"
AUDIT_IMMUNIZATION_SOURCE_REPROCESSED: AuditEventType = "immunization_source_reprocessed"
AUDIT_LAB_IMPORT_ACCEPTED: AuditEventType = "lab_import_accepted"
AUDIT_LAB_IMPORT_QUARANTINED: AuditEventType = "lab_import_quarantined"
AUDIT_LAB_INFERRED_LOINC_REPAIRED: AuditEventType = "lab_inferred_loinc_repaired"
AUDIT_LAB_SOURCE_PROVIDED_LOINC_OVERRIDDEN: AuditEventType = "lab_source_provided_loinc_overridden"
AUDIT_LAB_PANEL_NOTE_MARKED_SUPERSEDED: AuditEventType = "lab_panel_note_marked_superseded"
AUDIT_LAB_SOURCE_PATH_MIGRATED: AuditEventType = "lab_source_path_migrated"
AUDIT_LAB_SOURCE_REPROCESSED: AuditEventType = "lab_source_reprocessed"
AUDIT_MEDICATION_IMPORT_ACCEPTED: AuditEventType = "medication_import_accepted"
AUDIT_MEDICATION_CONFLICT_DETECTED: AuditEventType = "medication_conflict_detected"
AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED: AuditEventType = (
    "medication_conflict_resolution_recorded"
)
AUDIT_MEDICATION_REDUNDANT_ROW_SUPERSEDED: AuditEventType = "medication_redundant_row_superseded"
AUDIT_MEDICATION_DEDUPLICATED_ROW_RESTORED: AuditEventType = "medication_deduplicated_row_restored"
AUDIT_LAB_DEDUPLICATED_ROW_RESTORED: AuditEventType = "lab_deduplicated_row_restored"
AUDIT_LAB_OBSERVATION_REJECTED: AuditEventType = "lab_observation_rejected"
AUDIT_LAB_VALUE_IMPLAUSIBLE: AuditEventType = "lab_value_implausible"
AUDIT_LAB_OBSERVATION_RESOLUTION_RECORDED: AuditEventType = "lab_observation_resolution_recorded"
AUDIT_MEDICATION_SOURCE_NOTE_RETIRED: AuditEventType = "medication_source_note_retired"
AUDIT_MEDICATION_NOTE_MARKED_SUPERSEDED: AuditEventType = "medication_note_marked_superseded"
AUDIT_MEDICATION_SOURCE_REPROCESSED: AuditEventType = "medication_source_reprocessed"
AUDIT_USER_FACT_CORRECTED: AuditEventType = "user_fact_corrected"
AUDIT_SKIPPED_USER_CORRECTION: AuditEventType = "skipped_user_correction"
AUDIT_USER_CORRECTION_REVOKED: AuditEventType = "user_correction_revoked"
AUDIT_USER_UPDATE_RECORDED: AuditEventType = "user_update_recorded"
AUDIT_INBOX_ROUTE_QUARANTINED: AuditEventType = "inbox_route_quarantined"
AUDIT_PLACEHOLDER_IMPORT_ACCEPTED: AuditEventType = "placeholder_import_accepted"
AUDIT_SYNC_RESOLUTION_RECORDED: AuditEventType = "sync_resolution_recorded"
AUDIT_DEPENDENT_OUTPUT_MARKED_STALE: AuditEventType = "dependent_output_marked_stale"
AUDIT_VITAL_IMPORT_NEEDS_REVIEW: AuditEventType = "vital_import_needs_review"
VALID_AUDIT_EVENT_TYPES: tuple[AuditEventType, ...] = (
    AUDIT_HISTORY_IMPORT_NEEDS_REVIEW,
    AUDIT_HISTORY_CANDIDATES_ACCEPTED,
    AUDIT_HISTORY_SOURCE_REVIEWED_NO_FACTS,
    AUDIT_HISTORY_SOURCE_REPROCESSED,
    AUDIT_IMMUNIZATION_IMPORT_ACCEPTED,
    AUDIT_IMMUNIZATION_SOURCE_REPROCESSED,
    AUDIT_LAB_IMPORT_ACCEPTED,
    AUDIT_LAB_IMPORT_QUARANTINED,
    AUDIT_LAB_INFERRED_LOINC_REPAIRED,
    AUDIT_LAB_SOURCE_PROVIDED_LOINC_OVERRIDDEN,
    AUDIT_LAB_PANEL_NOTE_MARKED_SUPERSEDED,
    AUDIT_LAB_SOURCE_PATH_MIGRATED,
    AUDIT_LAB_SOURCE_REPROCESSED,
    AUDIT_MEDICATION_IMPORT_ACCEPTED,
    AUDIT_MEDICATION_CONFLICT_DETECTED,
    AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED,
    AUDIT_MEDICATION_REDUNDANT_ROW_SUPERSEDED,
    AUDIT_MEDICATION_DEDUPLICATED_ROW_RESTORED,
    AUDIT_LAB_DEDUPLICATED_ROW_RESTORED,
    AUDIT_LAB_OBSERVATION_REJECTED,
    AUDIT_LAB_VALUE_IMPLAUSIBLE,
    AUDIT_LAB_OBSERVATION_RESOLUTION_RECORDED,
    AUDIT_MEDICATION_SOURCE_NOTE_RETIRED,
    AUDIT_MEDICATION_NOTE_MARKED_SUPERSEDED,
    AUDIT_MEDICATION_SOURCE_REPROCESSED,
    AUDIT_USER_FACT_CORRECTED,
    AUDIT_SKIPPED_USER_CORRECTION,
    AUDIT_USER_CORRECTION_REVOKED,
    AUDIT_USER_UPDATE_RECORDED,
    AUDIT_INBOX_ROUTE_QUARANTINED,
    AUDIT_PLACEHOLDER_IMPORT_ACCEPTED,
    AUDIT_SYNC_RESOLUTION_RECORDED,
    AUDIT_DEPENDENT_OUTPUT_MARKED_STALE,
    AUDIT_VITAL_IMPORT_NEEDS_REVIEW,
)

AUDIT_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT,
    source_path TEXT,
    status TEXT,
    resolution TEXT,
    message TEXT,
    occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

DEDUP_SUBJECT_MEDICATION = "medication"
DEDUP_SUBJECT_LAB_RESULT = "lab_result"

DEDUP_LINKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dedup_links (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    person_id TEXT NOT NULL,
    suppressed_source_path TEXT NOT NULL,
    owner_source_path TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

INBOX_ROOT = Path("data/inbox")
PROCESSED_ROOT = Path("data/processed")
QUARANTINE_ROOT = Path("data/quarantine")
REFERENCE_ROOT = Path("data/reference")


@dataclass(frozen=True)
class StorageRoots:
    inbox: Path
    processed: Path
    quarantine: Path
    reference: Path


def configured_storage_roots(repo_root: Path) -> StorageRoots:
    """Return the repo-local storage roots used by ingestion and setup."""
    return StorageRoots(
        inbox=repo_root / INBOX_ROOT,
        processed=repo_root / PROCESSED_ROOT,
        quarantine=repo_root / QUARANTINE_ROOT,
        reference=repo_root / REFERENCE_ROOT,
    )


def validate_processing_state(value: str) -> ProcessingState:
    if value not in VALID_PROCESSING_STATES:
        allowed = ", ".join(VALID_PROCESSING_STATES)
        raise ValueError(f"invalid processing state {value!r}; expected one of: {allowed}")
    return cast(ProcessingState, value)


def validate_authority_decision(value: str) -> AuthorityDecision:
    if value not in VALID_AUTHORITY_DECISIONS:
        allowed = ", ".join(VALID_AUTHORITY_DECISIONS)
        raise ValueError(f"invalid authority decision {value!r}; expected one of: {allowed}")
    return cast(AuthorityDecision, value)


def validate_resolution(value: str) -> Resolution:
    if value not in VALID_RESOLUTIONS:
        allowed = ", ".join(VALID_RESOLUTIONS)
        raise ValueError(f"invalid resolution {value!r}; expected one of: {allowed}")
    return cast(Resolution, value)


def validate_audit_event_type(value: str) -> AuditEventType:
    if value not in VALID_AUDIT_EVENT_TYPES:
        allowed = ", ".join(VALID_AUDIT_EVENT_TYPES)
        raise ValueError(f"invalid audit event type {value!r}; expected one of: {allowed}")
    return cast(AuditEventType, value)


def ensure_audit_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(AUDIT_EVENTS_SCHEMA_SQL)


def ensure_dedup_links_table(conn: sqlite3.Connection) -> None:
    """Create the shared dedup-link store, migrating the medication-only predecessor.

    ADR-006 introduced `medication_dedup_links`; ADR-006 generalised it to every
    deduplicated category. Existing links are carried over so suppressed facts
    recorded under the earlier shape stay recoverable.
    """
    conn.execute(DEDUP_LINKS_SCHEMA_SQL)
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='medication_dedup_links'"
    ).fetchone()
    if legacy is None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO dedup_links (
            id, subject_type, person_id, suppressed_source_path, owner_source_path,
            payload, created_at
        )
        SELECT id, 'medication', person_id, suppressed_source_path, owner_source_path,
               payload, created_at
        FROM medication_dedup_links
        """
    )
    conn.execute("DROP TABLE medication_dedup_links")


def dedup_link_id(
    subject_type: str, person_id: str, suppressed_source_path: str, identity: str
) -> str:
    return hashlib.sha1(
        f"{subject_type}|{person_id}|{suppressed_source_path}|{identity}".encode()
    ).hexdigest()


def record_dedup_link(
    conn: sqlite3.Connection,
    *,
    subject_type: str,
    person_id: str,
    suppressed_source_path: str,
    owner_source_path: str,
    identity: str,
    payload: dict[str, object],
) -> None:
    """Record a reversible link for a row skipped as cross-source redundant.

    ADR-006 requires deduplication to use reversible linkage carrying provenance.
    The link keeps the skipped row recoverable so the fact cannot vanish from the
    canonical store if the owning source later stops stating it (ADR-006).
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO dedup_links (
            id, subject_type, person_id, suppressed_source_path, owner_source_path, payload
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_link_id(subject_type, person_id, suppressed_source_path, identity),
            subject_type,
            person_id,
            suppressed_source_path,
            owner_source_path,
            json.dumps(payload, sort_keys=True, default=str),
        ),
    )


def clear_dedup_links_for_source(
    conn: sqlite3.Connection, *, subject_type: str, suppressed_source_path: str
) -> None:
    conn.execute(
        "DELETE FROM dedup_links WHERE subject_type = ? AND suppressed_source_path = ?",
        (subject_type, suppressed_source_path),
    )


def load_dedup_links(
    conn: sqlite3.Connection, *, subject_type: str, person_ids: set[str]
) -> list[tuple[str, str, str, dict[str, object]]]:
    """Return (link_id, person_id, suppressed_source_path, payload) for the given people."""
    if not person_ids:
        return []
    placeholders = ",".join("?" * len(person_ids))
    rows = conn.execute(
        f"""
        SELECT id, person_id, suppressed_source_path, payload
        FROM dedup_links
        WHERE subject_type = ? AND person_id IN ({placeholders})
        """,
        (subject_type, *sorted(person_ids)),
    ).fetchall()
    return [(row[0], row[1], row[2], json.loads(row[3])) for row in rows]


def delete_dedup_link(conn: sqlite3.Connection, link_id: str) -> None:
    conn.execute("DELETE FROM dedup_links WHERE id = ?", (link_id,))


def log_audit_event(
    conn: sqlite3.Connection,
    *,
    event_type: AuditEventType | str,
    subject_type: str,
    subject_id: str | None = None,
    source_path: str | None = None,
    status: ProcessingState | str | None = None,
    resolution: Resolution | str | None = None,
    message: str | None = None,
) -> None:
    event_type = validate_audit_event_type(event_type)
    if status is not None:
        status = validate_processing_state(status)
    if resolution is not None:
        resolution = validate_resolution(resolution)

    ensure_audit_events_table(conn)
    conn.execute(
        """
        INSERT INTO audit_events (
            id, event_type, subject_type, subject_id,
            source_path, status, resolution, message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            event_type,
            subject_type,
            subject_id,
            source_path,
            status,
            resolution,
            message,
        ),
    )


def mark_dependent_output_stale(
    conn: sqlite3.Connection,
    *,
    output_path: str,
    reason: str,
    source_path: str | None = None,
    person_id: str | None = None,
) -> None:
    """Audit that a derived output must be regenerated before use."""
    if not output_path.strip():
        raise ValueError("output_path is required when marking a dependent output stale")
    if not reason.strip():
        raise ValueError("reason is required when marking a dependent output stale")
    log_audit_event(
        conn,
        event_type=AUDIT_DEPENDENT_OUTPUT_MARKED_STALE,
        subject_type="derived_output",
        subject_id=person_id,
        source_path=source_path,
        status=STATE_STALE,
        message=f"{output_path}: {reason}",
    )
