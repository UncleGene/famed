#!/usr/bin/env python3
"""Immunization canonicalization and vault persistence helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from famed.dependent_reports import refresh_fhir_dependent_export
from famed.import_labs import (
    display_source_file_name,
    slugify_filename,
    source_stem_without_redundant_date_prefix,
)
from famed.lab_sync import log_ingestion_event, write_category_index_note
from famed.person_sync import ensure_person_record_scaffold, load_person_display_name
from famed.source_paths import prune_superseded_healthsummary_source_paths
from famed.storage_contract import (
    AUDIT_IMMUNIZATION_IMPORT_ACCEPTED,
    AUDIT_IMMUNIZATION_SOURCE_REPROCESSED,
    STATE_PROCESSED,
    log_audit_event,
    mark_dependent_output_stale,
)


class ImmunizationSyncError(ValueError):
    """Raised when immunization rows cannot be safely persisted."""


IMMUNIZATIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS immunizations (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    vaccine_name TEXT NOT NULL,
    cvx_code TEXT,
    administered_date TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    lot_number TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_path TEXT,
    source_paths TEXT,
    evidence_snippet TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(person_id) REFERENCES persons(id)
);
"""


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


_DATE_LIST_SUFFIX_RE = re.compile(
    r"""
    (
        \s*\(\s*Given\s+\d{1,2}/\d{1,2}/\d{4}(?:\s*,\s*\d{1,2}/\d{1,2}/\d{4})*\s*\)\s*$
      |
        \s+\d{1,2}/\d{1,2}/\d{4}(?:\s*,\s*\d{1,2}/\d{1,2}/\d{4})*\s*$
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_vaccine_name(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    cleaned = _DATE_LIST_SUFFIX_RE.sub("", normalized).strip(" ,;-")
    return _normalize_optional_text(cleaned)


def _validate_optional_iso_date(
    value: str | None, field_name: str, source_label: str
) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImmunizationSyncError(
            f"{field_name} must be an ISO date or datetime in {source_label}, got: {value!r}"
        ) from exc
    return normalized


def _canonical_immunization_event_key(
    *,
    person_id: str,
    vaccine_name: str,
    cvx_code: str | None,
    administered_date: str | None,
    lot_number: str | None,
    source_path: str,
    row_index: int,
) -> str:
    normalized_name = _normalize_optional_text(vaccine_name) or ""
    normalized_cvx = _normalize_optional_text(cvx_code) or ""
    normalized_date = _normalize_optional_text(administered_date) or ""
    normalized_lot = _normalize_optional_text(lot_number) or ""
    fallback_name = normalized_name.lower() if not normalized_cvx and not normalized_lot else ""
    conservative_source_fallback = (
        f"{source_path}|{row_index}"
        if not normalized_date and not normalized_cvx and not normalized_lot
        else ""
    )
    raw = "|".join(
        [
            person_id.strip(),
            normalized_date,
            normalized_cvx,
            normalized_lot,
            fallback_name,
            conservative_source_fallback,
        ]
    )
    return raw


def immunization_row_id(event_key: str) -> str:
    raw = event_key.encode()
    return hashlib.sha1(raw).hexdigest()


def immunization_document_date(
    rows: list[dict[str, Any]],
    document_date: str | None,
    source_label: str,
) -> str | None:
    normalized_document_date = _validate_optional_iso_date(
        document_date, "document_date", source_label
    )
    if normalized_document_date:
        return normalized_document_date.split("T", 1)[0]
    dates = [
        str(row["administered_date"]).split("T", 1)[0]
        for row in rows
        if row.get("administered_date")
    ]
    return min(dates) if dates else None


def ensure_immunizations_schema(conn: sqlite3.Connection) -> None:
    conn.execute(IMMUNIZATIONS_SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(immunizations)")}
    if "source_paths" not in columns:
        conn.execute("ALTER TABLE immunizations ADD COLUMN source_paths TEXT")


def _deserialize_source_paths(
    serialized_paths: str | None,
    primary_source_path: str | None,
) -> list[str]:
    paths: list[str] = []
    if serialized_paths:
        try:
            loaded = json.loads(serialized_paths)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, list):
            paths.extend(str(item).strip() for item in loaded if str(item).strip())
    if primary_source_path:
        primary = primary_source_path.strip()
        if primary:
            paths.append(primary)
    return sorted(dict.fromkeys(paths))


def _serialize_source_paths(paths: list[str]) -> str:
    pruned_paths = prune_superseded_healthsummary_source_paths([path for path in paths if path])
    return json.dumps(pruned_paths, sort_keys=True)


def _preferred_text(*values: str | None) -> str | None:
    candidates = [value.strip() for value in values if value is not None and value.strip()]
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: (len(candidate), candidate.casefold()))


def validate_immunization_rows_for_persistence(
    conn: sqlite3.Connection,
    source_path: str,
    immunizations: list[dict[str, Any]],
) -> None:
    """Reject clinically conflicting same-event rows before any writes occur."""
    ensure_immunizations_schema(conn)
    incoming_status_by_id: dict[str, str] = {}
    for row_index, row in enumerate(immunizations, start=1):
        person_id = str(row.get("person_id") or "").strip()
        vaccine_name = normalize_vaccine_name(str(row.get("vaccine_name") or ""))
        if not person_id or not vaccine_name:
            raise ImmunizationSyncError(
                f"person_id and vaccine_name are required for immunization row {row_index}"
            )
        cvx_code = _normalize_optional_text(row.get("cvx_code"))
        administered_date = _validate_optional_iso_date(
            row.get("administered_date"), "administered_date", source_path
        )
        lot_number = _normalize_optional_text(row.get("lot_number"))
        status = (_normalize_optional_text(row.get("status")) or "completed").casefold()
        row_id = immunization_row_id(
            _canonical_immunization_event_key(
                person_id=person_id,
                vaccine_name=vaccine_name,
                cvx_code=cvx_code,
                administered_date=administered_date,
                lot_number=lot_number,
                source_path=source_path,
                row_index=row_index,
            )
        )

        prior_status = incoming_status_by_id.get(row_id)
        if prior_status is not None and prior_status != status:
            raise ImmunizationSyncError(
                "same immunization event has conflicting status values within one source: "
                f"{prior_status!r} vs {status!r}"
            )
        incoming_status_by_id[row_id] = status

        existing = conn.execute(
            """
            SELECT status, source_path, source_paths
            FROM immunizations
            WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
        if existing is None:
            continue
        other_source_paths = [
            path
            for path in _deserialize_source_paths(existing[2], existing[1])
            if path != source_path
        ]
        if not other_source_paths:
            continue
        existing_status = str(existing[0] or "completed").strip().casefold()
        if existing_status != status:
            raise ImmunizationSyncError(
                "same immunization event has conflicting status values across sources: "
                f"{existing_status!r} vs {status!r}"
            )


def remove_immunizations_for_source_path(
    conn: sqlite3.Connection,
    source_path: str,
) -> int:
    ensure_immunizations_schema(conn)
    rows = conn.execute(
        """
        SELECT id, source_path, source_paths
        FROM immunizations
        """,
    ).fetchall()
    affected = 0
    for row_id, primary_source_path, serialized_paths in rows:
        existing_paths = _deserialize_source_paths(serialized_paths, primary_source_path)
        if source_path not in existing_paths:
            continue
        remaining_paths = [path for path in existing_paths if path != source_path]
        if remaining_paths:
            conn.execute(
                "UPDATE immunizations SET source_path = ?, source_paths = ? WHERE id = ?",
                (remaining_paths[0], _serialize_source_paths(remaining_paths), row_id),
            )
        else:
            conn.execute("DELETE FROM immunizations WHERE id = ?", (row_id,))
        affected += 1
    return affected


def replace_immunizations_for_source_path(
    conn: sqlite3.Connection,
    source_path: str,
    immunizations: list[dict[str, Any]],
) -> int:
    ensure_immunizations_schema(conn)
    validate_immunization_rows_for_persistence(conn, source_path, immunizations)
    return _replace_validated_immunizations_for_source_path(conn, source_path, immunizations)


def _replace_validated_immunizations_for_source_path(
    conn: sqlite3.Connection,
    source_path: str,
    immunizations: list[dict[str, Any]],
) -> int:
    remove_immunizations_for_source_path(conn, source_path)
    inserted = 0
    for row_index, row in enumerate(immunizations, start=1):
        person_id = str(row.get("person_id") or "").strip()
        vaccine_name = normalize_vaccine_name(str(row.get("vaccine_name") or ""))
        if not person_id or not vaccine_name:
            raise ImmunizationSyncError(
                f"person_id and vaccine_name are required for immunization row {row_index}"
            )
        cvx_code = _normalize_optional_text(row.get("cvx_code"))
        administered_date = _validate_optional_iso_date(
            row.get("administered_date"), "administered_date", source_path
        )
        lot_number = _normalize_optional_text(row.get("lot_number"))
        source_type = _normalize_optional_text(row.get("source_type")) or "source_import"
        evidence_snippet = _normalize_optional_text(row.get("evidence_snippet"))
        status = (_normalize_optional_text(row.get("status")) or "completed").casefold()
        event_key = _canonical_immunization_event_key(
            person_id=person_id,
            vaccine_name=vaccine_name,
            cvx_code=cvx_code,
            administered_date=administered_date,
            lot_number=lot_number,
            source_path=source_path,
            row_index=row_index,
        )
        row_id = immunization_row_id(event_key)
        existing = conn.execute(
            """
            SELECT vaccine_name, cvx_code, administered_date, status, lot_number,
                   source_type, source_path, source_paths, evidence_snippet
            FROM immunizations
            WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
        source_paths = [source_path]
        if existing is not None:
            source_paths = _deserialize_source_paths(existing[7], existing[6]) + [source_path]
            vaccine_name = _preferred_text(vaccine_name, existing[0]) or vaccine_name
            cvx_code = _preferred_text(cvx_code, existing[1])
            administered_date = _preferred_text(administered_date, existing[2])
            status = _preferred_text(status, existing[3]) or status
            lot_number = _preferred_text(lot_number, existing[4])
            source_type = _preferred_text(source_type, existing[5]) or source_type
            evidence_snippet = _preferred_text(evidence_snippet, existing[8])
        source_paths = prune_superseded_healthsummary_source_paths(source_paths)
        conn.execute(
            """
            INSERT INTO immunizations (
                id, person_id, vaccine_name, cvx_code, administered_date,
                status, lot_number, source_type, source_path, source_paths, evidence_snippet
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                vaccine_name = excluded.vaccine_name,
                cvx_code = excluded.cvx_code,
                administered_date = excluded.administered_date,
                status = excluded.status,
                lot_number = excluded.lot_number,
                source_type = excluded.source_type,
                source_path = excluded.source_path,
                source_paths = excluded.source_paths,
                evidence_snippet = excluded.evidence_snippet
            """,
            (
                row_id,
                person_id,
                vaccine_name,
                cvx_code,
                administered_date,
                status,
                lot_number,
                source_type,
                source_paths[0],
                _serialize_source_paths(source_paths),
                evidence_snippet,
            ),
        )
        inserted += 1
    return inserted


def render_immunization_note(
    rows: list[dict[str, Any]],
    *,
    source_file_name: str,
    source_path: str,
    document_date: str | None,
) -> str:
    if not rows:
        raise ImmunizationSyncError("at least one immunization row is required to render a note")
    person_ids = {row["person_id"] for row in rows}
    if len(person_ids) != 1:
        raise ImmunizationSyncError(
            "all immunization rows in one note must belong to one person_id"
        )
    front_matter: dict[str, Any] = {
        "person_id": rows[0]["person_id"],
        "record_type": "immunization",
        "source_file_name": source_file_name,
        "source_path": source_path,
        "event_date": document_date,
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    table_rows = [
        "| vaccine_name | cvx_code | administered_date | status | lot_number | evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        table_rows.append(
            "| "
            + " | ".join(
                [
                    _markdown_table_cell(row.get("vaccine_name")),
                    _markdown_table_cell(row.get("cvx_code")),
                    _markdown_table_cell(row.get("administered_date")),
                    _markdown_table_cell(row.get("status")),
                    _markdown_table_cell(row.get("lot_number")),
                    _markdown_table_cell(row.get("evidence_snippet")),
                ]
            )
            + " |"
        )
    return (
        f"---\n{yaml_block}\n---\n\n"
        "# Immunizations\n\n"
        "[[_index|← Immunizations]]\n\n"
        "## Immunization Records\n\n"
        f"{chr(10).join(table_rows)}\n"
    )


def _markdown_table_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def immunization_note_path(
    person_immunizations_dir: Path,
    document_date: str | None,
    source_file: Path,
) -> Path:
    date_prefix = document_date or "undated"
    slug = slugify_filename(
        source_stem_without_redundant_date_prefix(source_file.stem, document_date)
    )
    suffix = "" if slug.endswith("-immunizations") else "-immunizations"
    return person_immunizations_dir / f"{date_prefix}-{slug}{suffix}.md"


def _query_immunization_rows(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    ensure_immunizations_schema(conn)
    cursor = conn.execute(
        """
        SELECT vaccine_name, cvx_code, administered_date, status, lot_number,
               source_path, source_paths
        FROM immunizations
        WHERE person_id = ?
        ORDER BY administered_date DESC, vaccine_name COLLATE NOCASE, source_path
        """,
        (person_id,),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]


def write_immunizations_report(
    vault_root: Path,
    person_id: str,
    conn: sqlite3.Connection,
) -> Path | None:
    """Write immunizations.md, or remove it when the person has none (ADR-031)."""
    person_reports_dir = vault_root / "Records" / person_id / "Reports"
    report_path = person_reports_dir / "immunizations.md"
    rows = _query_immunization_rows(conn, person_id)
    if not rows:
        report_path.unlink(missing_ok=True)
        if person_reports_dir.is_dir():
            write_category_index_note(
                person_reports_dir,
                person_id,
                "Reports",
                note_label="report",
                display_name=load_person_display_name(vault_root, person_id),
            )
        return None
    person_reports_dir.mkdir(parents=True, exist_ok=True)
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "immunizations",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    table_rows = [
        "| vaccine_name | administered_date | status | cvx_code | lot_number |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        table_rows.append(
            "| "
            + " | ".join(
                [
                    _markdown_table_cell(row.get("vaccine_name")),
                    _markdown_table_cell(row.get("administered_date")),
                    _markdown_table_cell(row.get("status")),
                    _markdown_table_cell(row.get("cvx_code")),
                    _markdown_table_cell(row.get("lot_number")),
                ]
            )
            + " |"
        )
    table = "\n".join(table_rows) + "\n"
    report_path.write_text(
        f"---\n{yaml_block}\n---\n\n# Immunizations\n\n{table}",
        encoding="utf-8",
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return report_path


def persist_immunization_rows(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    source_file: Path,
    processed_source_path: str,
    checksum: str,
    source_type: str,
    rows: list[dict[str, Any]],
    document_date: str | None,
    retained_source: bool = False,
    source_file_name: str | None = None,
    refresh_export: bool = True,
) -> Path:
    ensure_person_record_scaffold(vault_root, person_id)
    person_immunizations_dir = vault_root / "Records" / person_id / "Immunizations"
    person_immunizations_dir.mkdir(parents=True, exist_ok=True)
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized_row = dict(row)
        normalized_row["vaccine_name"] = normalize_vaccine_name(
            str(normalized_row.get("vaccine_name") or "")
        )
        normalized_rows.append(normalized_row)
    validate_immunization_rows_for_persistence(conn, processed_source_path, normalized_rows)
    note_path = immunization_note_path(person_immunizations_dir, document_date, source_file)
    note_path.write_text(
        render_immunization_note(
            normalized_rows,
            source_file_name=source_file_name
            or display_source_file_name(source_file, document_date),
            source_path=processed_source_path,
            document_date=document_date,
        ),
        encoding="utf-8",
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_immunizations_dir,
        person_id,
        "Immunizations",
        display_name=display_name,
    )
    _replace_validated_immunizations_for_source_path(conn, processed_source_path, normalized_rows)
    if retained_source:
        mark_dependent_output_stale(
            conn,
            output_path=f"Records/{person_id}/Reports/immunizations.md",
            reason="retained immunization source reprocessed",
            source_path=processed_source_path,
            person_id=person_id,
        )
    note_source_path = str(note_path.relative_to(vault_root))
    message_prefix = "reprocessed note" if retained_source else "immunization note"
    audit_event = (
        AUDIT_IMMUNIZATION_SOURCE_REPROCESSED
        if retained_source
        else AUDIT_IMMUNIZATION_IMPORT_ACCEPTED
    )
    log_ingestion_event(
        conn,
        source_type=source_type,
        source_path=processed_source_path,
        source_checksum=checksum,
        status=STATE_PROCESSED,
        message=f"{message_prefix}: {note_source_path}",
    )
    log_audit_event(
        conn,
        event_type=audit_event,
        subject_type="immunization",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_PROCESSED,
        message=f"{message_prefix}: {note_source_path}",
    )
    write_immunizations_report(vault_root, person_id, conn)
    if refresh_export:
        refresh_fhir_dependent_export(conn, vault_root, person_id)
    return note_path


def remove_immunization_artifacts_for_source_path(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    processed_source_path: str,
    refresh_export: bool = True,
) -> None:
    person_immunizations_dir = vault_root / "Records" / person_id / "Immunizations"
    if person_immunizations_dir.is_dir():
        for candidate in person_immunizations_dir.glob("*.md"):
            if candidate.name == "_index.md":
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            if (
                "record_type: immunization" in text
                and f"source_path: {processed_source_path}" in text
            ):
                candidate.unlink(missing_ok=True)
        write_category_index_note(
            person_immunizations_dir,
            person_id,
            "Immunizations",
            display_name=load_person_display_name(vault_root, person_id),
        )
    remove_immunizations_for_source_path(conn, processed_source_path)
    write_immunizations_report(vault_root, person_id, conn)
    if refresh_export:
        refresh_fhir_dependent_export(conn, vault_root, person_id)
