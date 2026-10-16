#!/usr/bin/env python3
"""Vital-sign candidate canonicalization and vault persistence helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from famed.clinical_interpreter import CandidateVitalFact, InterpreterFactExtraction
from famed.import_labs import (
    display_source_file_name,
    retain_processed_source,
    slugify_filename,
    source_stem_without_redundant_date_prefix,
)
from famed.lab_sync import log_ingestion_event, source_file_checksum, write_category_index_note
from famed.person_sync import ensure_person_record_scaffold, load_person_display_name
from famed.storage_contract import (
    AUDIT_VITAL_IMPORT_NEEDS_REVIEW,
    STATE_PROCESSED,
    log_audit_event,
)


class VitalSyncError(ValueError):
    """Raised when vital candidates cannot be safely persisted."""


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _validate_optional_iso_date(
    value: str | None, field_name: str, source_label: str
) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VitalSyncError(
            f"{field_name} must be an ISO date or datetime in {source_label}, got: {value!r}"
        ) from exc
    return normalized


def canonicalize_candidate_vital_facts(
    extraction: InterpreterFactExtraction,
    source_label: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact in extraction.facts:
        if not isinstance(fact, CandidateVitalFact):
            continue
        rows.append(
            {
                "person_id": extraction.person_id,
                "vital_name": fact.vital_name,
                "value": fact.value,
                "unit": _normalize_optional_text(fact.unit),
                "reference_range_low": fact.reference_range_low,
                "reference_range_high": fact.reference_range_high,
                "observed_at": _validate_optional_iso_date(
                    fact.observed_at, "observed_at", source_label
                ),
                "confidence": fact.confidence,
                "evidence_snippet": _normalize_optional_text(fact.evidence_snippet),
            }
        )

    if not rows:
        raise VitalSyncError(f"no vital facts found in {source_label}")
    return rows


def vital_document_date(
    rows: list[dict[str, Any]],
    document_date: str | None,
    source_label: str,
) -> str | None:
    normalized_document_date = _validate_optional_iso_date(
        document_date, "document_date", source_label
    )
    if normalized_document_date:
        return normalized_document_date.split("T", 1)[0]
    dates = [str(row["observed_at"]).split("T", 1)[0] for row in rows if row.get("observed_at")]
    return min(dates) if dates else None


def processed_vital_source_path(
    processed_root: Path,
    person_id: str,
    document_date: str | None,
    source_file: Path,
) -> Path:
    date_prefix = document_date or "undated"
    source_stem = source_stem_without_redundant_date_prefix(source_file.stem, document_date)
    return (
        processed_root
        / person_id
        / "Vitals"
        / f"{date_prefix}-{source_stem}{source_file.suffix.lower()}"
    )


def retain_vital_source_for_import(
    root: Path,
    processed_root: Path,
    person_id: str,
    document_date: str | None,
    source_file: Path,
) -> tuple[str, str]:
    checksum = source_file_checksum(source_file)
    processed_source = processed_vital_source_path(
        processed_root, person_id, document_date, source_file
    )
    processed_source = retain_processed_source(source_file, processed_source, checksum)
    return checksum, str(processed_source.relative_to(root))


def render_vital_note(
    rows: list[dict[str, Any]],
    *,
    source_file_name: str,
    source_path: str,
    document_date: str | None,
) -> str:
    if not rows:
        raise VitalSyncError("at least one vital item is required to render a note")
    person_ids = {row["person_id"] for row in rows}
    if len(person_ids) != 1:
        raise VitalSyncError("all vital items in one note must belong to one person_id")

    front_matter: dict[str, Any] = {
        "person_id": rows[0]["person_id"],
        "record_type": "vitals",
        "source_file_name": source_file_name,
        "source_path": source_path,
        "event_date": document_date,
        "verification_status": "needs_review",
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    table_rows = [
        "| vital_name | value | unit | ref_low | ref_high | observed_at | confidence | evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        table_rows.append(
            "| "
            + " | ".join(
                [
                    _markdown_table_cell(row.get("vital_name")),
                    _markdown_table_cell(row.get("value")),
                    _markdown_table_cell(row.get("unit")),
                    _markdown_table_cell(row.get("reference_range_low")),
                    _markdown_table_cell(row.get("reference_range_high")),
                    _markdown_table_cell(row.get("observed_at")),
                    _markdown_table_cell(row.get("confidence")),
                    _markdown_table_cell(row.get("evidence_snippet")),
                ]
            )
            + " |"
        )

    return (
        f"---\n{yaml_block}\n---\n\n"
        "# Vital Signs Review\n\n"
        "[[_index|← Vitals]]\n\n"
        "## Candidate Vital Signs\n\n"
        f"{chr(10).join(table_rows)}\n\n"
        "## Review\n\n"
        "- Verification status: needs_review\n"
        f"- Source file: {source_file_name}\n"
        "- Confirm, edit, or reject candidate vitals before treating them as authoritative.\n"
    )


def _markdown_table_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def vital_note_path(
    person_vitals_dir: Path,
    document_date: str | None,
    source_file: Path,
) -> Path:
    date_prefix = document_date or "undated"
    slug = slugify_filename(
        source_stem_without_redundant_date_prefix(source_file.stem, document_date)
    )
    suffix = "" if slug.endswith("-vitals") else "-vitals"
    return person_vitals_dir / f"{date_prefix}-{slug}{suffix}-review.md"


def persist_vital_rows(
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
    source_file_name: str | None = None,
) -> Path:
    ensure_person_record_scaffold(vault_root, person_id)
    person_vitals_dir = vault_root / "Records" / person_id / "Vitals"
    person_vitals_dir.mkdir(parents=True, exist_ok=True)

    note_path = vital_note_path(person_vitals_dir, document_date, source_file)
    note_path.write_text(
        render_vital_note(
            rows,
            source_file_name=source_file_name
            or display_source_file_name(source_file, document_date),
            source_path=processed_source_path,
            document_date=document_date,
        ),
        encoding="utf-8",
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_vitals_dir,
        person_id,
        "Vitals",
        display_name=display_name,
    )

    note_source_path = str(note_path.relative_to(vault_root))
    log_ingestion_event(
        conn,
        source_type=source_type,
        source_path=processed_source_path,
        source_checksum=checksum,
        status=STATE_PROCESSED,
        message=f"vital review note: {note_source_path}",
    )
    log_audit_event(
        conn,
        event_type=AUDIT_VITAL_IMPORT_NEEDS_REVIEW,
        subject_type="vitals",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_PROCESSED,
        message=f"vital review note: {note_source_path}",
    )
    return note_path
