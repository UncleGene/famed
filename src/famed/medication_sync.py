#!/usr/bin/env python3
"""Medication canonicalization and vault persistence helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from famed.allergies import render_allergy_section
from famed.clinical_interpreter import (
    FACT_EXTRACTION_SCHEMA_VERSION,
    VALID_MEDICATION_TYPES,
    CandidateMedicationFact,
    InterpreterEvidence,
    InterpreterFactExtraction,
    MedicationType,
)
from famed.dependent_reports import (
    refresh_fhir_dependent_export,
    refresh_medication_dependent_reports,
)
from famed.import_labs import (
    display_source_file_name,
    retain_processed_source,
    retain_source_file,
    slugify_filename,
    source_stem_without_redundant_date_prefix,
)
from famed.lab_sync import log_ingestion_event, source_file_checksum, write_category_index_note
from famed.person_sync import (
    ensure_person_record_scaffold,
    load_person_display_name,
    parse_front_matter,
)
from famed.reference_cache import load_rxnorm_index, lookup_rxnorm_cui
from famed.reprocess_sources import ReprocessError, ReprocessResult, retained_source_reference
from famed.source_paths import split_archive_source_path
from famed.storage_contract import (
    AUDIT_INBOX_ROUTE_QUARANTINED,
    AUDIT_MEDICATION_CONFLICT_DETECTED,
    AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED,
    AUDIT_MEDICATION_DEDUPLICATED_ROW_RESTORED,
    AUDIT_MEDICATION_IMPORT_ACCEPTED,
    AUDIT_MEDICATION_REDUNDANT_ROW_SUPERSEDED,
    AUDIT_MEDICATION_SOURCE_NOTE_RETIRED,
    AUDIT_MEDICATION_SOURCE_REPROCESSED,
    AUDIT_SKIPPED_USER_CORRECTION,
    AUDIT_USER_CORRECTION_REVOKED,
    DEDUP_SUBJECT_MEDICATION,
    RESOLUTION_USER_RESOLVED,
    STATE_AWAITING_USER_DECISION,
    STATE_PROCESSED,
    STATE_QUARANTINED,
    ConflictChoice,
    clear_dedup_links_for_source,
    configured_storage_roots,
    delete_dedup_link,
    ensure_audit_events_table,
    ensure_dedup_links_table,
    load_dedup_links,
    log_audit_event,
    mark_dependent_output_stale,
    record_dedup_link,
)


class MedicationSyncError(ValueError):
    """Raised when medication candidates cannot be safely persisted."""


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _normalize_status(value: str | None) -> str:
    status = (value or "active").strip().lower()
    return status or "active"


def _validate_optional_iso_date(
    value: str | None, field_name: str, source_label: str
) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MedicationSyncError(
            f"{field_name} must be an ISO date or datetime in {source_label}, got: {value!r}"
        ) from exc
    return normalized


def medication_row_id(person_id: str, source_path: str, row_index: int) -> str:
    raw = f"{person_id}|{source_path}|{row_index}".encode()
    return hashlib.sha1(raw).hexdigest()


def _normalize_medication_type(value: str | None) -> MedicationType:
    normalized = (value or "prescription").strip().lower()
    if normalized not in VALID_MEDICATION_TYPES:
        raise MedicationSyncError(
            f"medication_type must be one of {sorted(VALID_MEDICATION_TYPES)}, got: {value!r}"
        )
    return normalized  # type: ignore[return-value]


_MEDICATION_CSV_NAME_FIELDS = ("medication_name", "medication", "name")
_CURRENT_ACTIVE_STATUS_SQL = "(status IS NULL OR TRIM(status) = '' OR LOWER(status) = 'active')"

# Vendor portal CSV column aliases → canonical field names.
# Normalised to casefold before lookup so mixed-case headers match.
_VENDOR_COLUMN_ALIASES: dict[str, str] = {
    "drug": "medication_name",
    "drug name": "medication_name",
    "drug_name": "medication_name",
    "product": "medication_name",
    "product name": "medication_name",
    "product_name": "medication_name",
    "dosage": "dose",
    "strength": "dose",
    "sig": "frequency",
    "directions": "frequency",
    "direction": "frequency",
}


def parse_medication_csv(source_file: Path, person_id: str) -> list[dict[str, Any]]:
    """Parse a deterministic medication CSV into canonical row dictionaries.

    Supported columns:
    - `medication_name` | `medication` | `name` (required)
    - `person_id` (optional, must match the resolved person when present)
    - `medication_type`, `rxnorm_cui`, `dose`, `frequency`, `status`,
      `start_date`, `end_date`, `route`, `evidence_snippet`
    """
    try:
        with source_file.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = [
                name.strip() for name in (reader.fieldnames or []) if name and name.strip()
            ]
            if not fieldnames:
                raise MedicationSyncError(f"CSV has no header row in {source_file}")

            lowered = {name.casefold() for name in fieldnames}
            # Remap vendor alias headers to canonical names for the header check.
            lowered = {_VENDOR_COLUMN_ALIASES.get(n, n) for n in lowered}
            if not any(field in lowered for field in _MEDICATION_CSV_NAME_FIELDS):
                raise MedicationSyncError(
                    f"CSV must include one of medication_name, medication, or name in {source_file}"
                )

            rows: list[dict[str, Any]] = []
            for row_index, raw_row in enumerate(reader, start=1):
                row = {
                    _VENDOR_COLUMN_ALIASES.get(k := str(key or "").strip().casefold(), k): value
                    for key, value in raw_row.items()
                }
                if not any(str(value or "").strip() for value in row.values()):
                    continue

                row_person_id = _normalize_optional_text(row.get("person_id"))
                if row_person_id and row_person_id != person_id:
                    raise MedicationSyncError(
                        "person_id column must match the resolved route person_id in "
                        f"{source_file} row {row_index}"
                    )

                medication_name = None
                for field in _MEDICATION_CSV_NAME_FIELDS:
                    medication_name = _normalize_optional_text(row.get(field))
                    if medication_name:
                        break
                if medication_name is None:
                    raise MedicationSyncError(
                        f"medication name is required in {source_file} row {row_index}"
                    )

                rows.append(
                    {
                        "person_id": person_id,
                        "medication_name": medication_name,
                        "medication_type": _normalize_medication_type(row.get("medication_type")),
                        "rxnorm_cui": _normalize_optional_text(row.get("rxnorm_cui")),
                        "dose": _normalize_optional_text(row.get("dose")),
                        "frequency": _normalize_optional_text(row.get("frequency")),
                        "start_date": _validate_optional_iso_date(
                            row.get("start_date"), "start_date", f"{source_file} row {row_index}"
                        ),
                        "end_date": _validate_optional_iso_date(
                            row.get("end_date"), "end_date", f"{source_file} row {row_index}"
                        ),
                        "status": _normalize_status(row.get("status")),
                        "route": _normalize_optional_text(row.get("route")),
                        "evidence_snippet": _normalize_optional_text(row.get("evidence_snippet")),
                    }
                )
    except csv.Error as exc:
        raise MedicationSyncError(f"invalid medication CSV in {source_file}: {exc}") from exc

    if not rows:
        raise MedicationSyncError(f"no medication rows found in {source_file}")
    return rows


_SUPPLEMENT_DOSE_RE = re.compile(r"\d+\s*(mg|mcg|µg|g|IU|kg)\b", re.IGNORECASE)
_SUPPLEMENT_DISCONTINUED_RE = re.compile(r"paus|stopp|discontin", re.IGNORECASE)
_NO_DOSE_SUPPLEMENT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9 &'()/+-]*\Z")
_MEDICATION_TABLE_HEADER_COLUMNS = (
    "medication_name",
    "medication_type",
    "rxnorm_cui",
    "dose",
    "frequency",
    "status",
    "start_date",
    "end_date",
    "evidence",
)


def _looks_like_no_dose_supplement_name(line: str) -> bool:
    return bool(_NO_DOSE_SUPPLEMENT_NAME_RE.fullmatch(line)) and len(line.split()) <= 5


def _split_medication_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def parse_medication_markdown_table(content: str) -> list[CandidateMedicationFact] | None:
    """Parse a retained ``| medication_name | ... |`` markdown table note.

    Returns ``None`` when *content* does not contain a recognizable medication
    table header, signalling callers to fall back to ``parse_supplement_text``
    for free-form prose sources. Column positions follow the fixed layout
    templates write: medication_name, medication_type, rxnorm_cui, dose,
    frequency, status, start_date, end_date, evidence.
    """
    lines = content.splitlines()
    header_index: int | None = None
    for index, line in enumerate(lines):
        cells = _split_medication_table_row(line)
        if cells and [cell.casefold() for cell in cells] == list(_MEDICATION_TABLE_HEADER_COLUMNS):
            header_index = index
            break
    if header_index is None:
        return None

    if header_index + 1 >= len(lines):
        raise MedicationSyncError("medication markdown table is missing its alignment row")
    alignment = _split_medication_table_row(lines[header_index + 1])
    if alignment is None or not all(set(cell) <= {"-", ":"} for cell in alignment):
        raise MedicationSyncError("medication markdown table alignment row is invalid")

    facts: list[CandidateMedicationFact] = []
    for line in lines[header_index + 2 :]:
        cells = _split_medication_table_row(line)
        if cells is None:
            break
        if len(cells) < len(_MEDICATION_TABLE_HEADER_COLUMNS) or not any(cells):
            continue
        name = cells[0].strip()
        if not name:
            continue
        facts.append(
            CandidateMedicationFact(
                name=name,
                dose=cells[3].strip() or None,
                frequency=cells[4].strip() or None,
                status=cells[5].strip() or None,
                start_date=cells[6].strip() or None,
                end_date=cells[7].strip() or None,
                medication_type=_normalize_medication_type(cells[1].strip() or None),
                confidence=1.0,
                evidence_snippet=cells[8].strip() or line.strip(),
            )
        )
    return facts


def parse_supplement_text(content: str) -> list[CandidateMedicationFact]:
    """Parse a free-form supplement text list into deterministic candidate facts.

    Heuristics:
    - Skip blank lines.
    - Lines ending with `:` are section headers; set status to ``discontinued`` when
      the header matches /paus|stopp|discontin/, else reset to ``active``.
    - Lines containing a dose pattern (``\\d+\\s*(mg|mcg|µg|g|IU|kg)``) are parsed as
      ``(name, dose)``; trailing parenthetical notes are stripped from dose.
    - Lines without a dose pattern are parsed as ``(name, None)`` only when they
      appear under a section header and look like a short supplement name. This
      keeps title/metadata/prose from becoming medication rows.
    - A line whose dose fragment contains more than one dose-pattern match (e.g.
      a narrative titration schedule stating several doses on several dates)
      is skipped rather than persisted as a single garbled combined-dose fact:
      collapsing several distinct, differently-dated dose facts into one string
      would silently misrepresent the source. Multi-fact narrative lines like
      this need semantic (AI-assisted) interpretation, not this deterministic
      parser (ADR-018); it is safer to record nothing than to record a wrong
      single fact.

    Each parsed entry becomes a deterministic ``CandidateMedicationFact`` with
    ``medication_type="supplement"`` and ``confidence=1.0``.
    """
    facts: list[CandidateMedicationFact] = []
    status: str = "active"
    in_list_section = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":"):
            label = line.rstrip(":").strip()
            status = "discontinued" if _SUPPLEMENT_DISCONTINUED_RE.search(label) else "active"
            in_list_section = True
            continue
        match = _SUPPLEMENT_DOSE_RE.search(line)
        if not match:
            if not in_list_section or not _looks_like_no_dose_supplement_name(line):
                continue
            name = line
            dose = None
        else:
            name = line[: match.start()].strip()
            dose = line[match.start() :].strip()
            paren = dose.find("(")
            if paren > 0:
                dose = dose[:paren].strip()
            if dose and len(_SUPPLEMENT_DOSE_RE.findall(dose)) > 1:
                continue
        if not name:
            continue
        facts.append(
            CandidateMedicationFact(
                name=name,
                dose=dose or None,
                status=status,
                medication_type="supplement",
                confidence=1.0,
                evidence_snippet=line,
            )
        )
    return facts


def build_supplement_extraction(
    person_id: str,
    source_file: Path,
) -> InterpreterFactExtraction | None:
    """Read a supplement source file and return a deterministic fact extraction.

    Returns ``None`` when no parseable supplement lines are found, signalling
    callers to fall back to placeholder handling.
    """
    content = source_file.read_text(encoding="utf-8", errors="replace")
    facts = parse_medication_markdown_table(content)
    if facts is None:
        facts = parse_supplement_text(content)
    if not facts:
        return None
    return InterpreterFactExtraction(
        schema_version=FACT_EXTRACTION_SCHEMA_VERSION,
        person_id=person_id,
        person_confidence=1.0,
        category="medications",
        category_confidence=1.0,
        facts=tuple(facts),  # type: ignore[arg-type]
        evidence=(
            InterpreterEvidence(
                field="source_filename",
                snippet=source_file.name,
                confidence=1.0,
            ),
        ),
        overall_confidence=1.0,
        document_date=None,
    )


def canonicalize_candidate_medication_facts(
    extraction: InterpreterFactExtraction,
    source_label: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact in extraction.facts:
        if not isinstance(fact, CandidateMedicationFact):
            continue
        name = _normalize_optional_text(fact.name)
        if not name:
            raise MedicationSyncError(f"medication name is required in {source_label}")
        rows.append(
            {
                "person_id": extraction.person_id,
                "rxnorm_cui": None,
                "medication_name": name,
                "medication_type": _normalize_medication_type(fact.medication_type),
                "dose": _normalize_optional_text(fact.dose),
                "frequency": _normalize_optional_text(fact.frequency),
                "start_date": _validate_optional_iso_date(
                    fact.start_date, "start_date", source_label
                ),
                "end_date": _validate_optional_iso_date(fact.end_date, "end_date", source_label),
                "status": _normalize_status(fact.status),
                "route": _normalize_optional_text(fact.route),
                "evidence_snippet": _normalize_optional_text(fact.evidence_snippet),
            }
        )

    if not rows:
        raise MedicationSyncError(f"no medication facts found in {source_label}")
    return rows


def fill_rxnorm_cuis(rows: list[dict[str, Any]], rxnorm_index: dict[str, str]) -> None:
    """Fill blank ``rxnorm_cui`` fields in *rows* from *rxnorm_index*.

    Existing non-blank values are never overwritten.  Operates in-place.
    A no-op when *rxnorm_index* is empty.
    """
    if not rxnorm_index:
        return
    for row in rows:
        if row.get("rxnorm_cui"):
            continue
        name = str(row.get("medication_name") or "")
        if name:
            row["rxnorm_cui"] = lookup_rxnorm_cui(name, rxnorm_index) or None


def medication_document_date(
    rows: list[dict[str, Any]],
    document_date: str | None,
    source_label: str,
) -> str | None:
    normalized_document_date = _validate_optional_iso_date(
        document_date, "document_date", source_label
    )
    if normalized_document_date:
        return normalized_document_date.split("T", 1)[0]
    dates = [str(row["start_date"]).split("T", 1)[0] for row in rows if row.get("start_date")]
    return min(dates) if dates else None


def processed_medication_source_path(
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
        / "Medications"
        / f"{date_prefix}-{source_stem}{source_file.suffix.lower()}"
    )


def retain_medication_source_for_import(
    root: Path,
    processed_root: Path,
    person_id: str,
    document_date: str | None,
    source_file: Path,
) -> tuple[str, str]:
    checksum = source_file_checksum(source_file)
    processed_source = processed_medication_source_path(
        processed_root, person_id, document_date, source_file
    )
    processed_source = retain_processed_source(source_file, processed_source, checksum)
    return checksum, str(processed_source.relative_to(root))


def quarantined_medication_source_path(
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
) -> Path:
    safe_person_id = person_id or "_unknown"
    stem = source_file.stem
    suffix = source_file.suffix.lower()
    return quarantine_root / safe_person_id / "Medications" / f"undated-{stem}{suffix}"


def quarantine_medication_source_for_import(
    root: Path,
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
) -> tuple[str, str]:
    checksum = source_file_checksum(source_file)
    quarantine_source = quarantined_medication_source_path(quarantine_root, person_id, source_file)
    quarantine_source = retain_source_file(source_file, quarantine_source, checksum)
    return checksum, str(quarantine_source.relative_to(root))


def quarantine_medication_import_error(
    conn: sqlite3.Connection | None,
    root: Path,
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
    source_type: str,
    message: str,
) -> str:
    checksum, quarantine_source_path = quarantine_medication_source_for_import(
        root,
        quarantine_root,
        person_id,
        source_file,
    )
    if conn is not None:
        log_ingestion_event(
            conn,
            source_type=source_type,
            source_path=quarantine_source_path,
            source_checksum=checksum,
            status=STATE_QUARANTINED,
            message=message,
        )
        log_audit_event(
            conn,
            event_type=AUDIT_INBOX_ROUTE_QUARANTINED,
            subject_type="medication_source",
            subject_id=person_id,
            source_path=quarantine_source_path,
            status=STATE_QUARANTINED,
            message=message,
        )
        from famed.pending_review import refresh_pending_review_report_if_configured

        refresh_pending_review_report_if_configured(
            conn,
            root=root,
            vault_root=root / "famed-vault",
            person_id=person_id,
        )
    return quarantine_source_path


def infer_person_id_from_processed_medication_path(path: Path, processed_root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(processed_root.resolve())
    except ValueError:
        return None
    if len(relative.parts) >= 3 and relative.parts[1] == "Medications":
        return relative.parts[0].strip() or None
    return None


def upsert_medication(conn: sqlite3.Connection, medication: dict[str, Any]) -> None:
    source_path = medication.get("source_path")
    if not source_path:
        raise MedicationSyncError("source_path is required when writing medications")
    source_type = str(medication.get("source_type") or "manual").strip().lower() or "manual"

    conn.execute(
        """
        INSERT INTO medications (
            id,
            person_id,
            rxnorm_cui,
            medication_name,
            medication_type,
            dose,
            frequency,
            start_date,
            end_date,
            authorized_through,
            status,
            source_type,
            source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            rxnorm_cui = excluded.rxnorm_cui,
            medication_name = excluded.medication_name,
            medication_type = excluded.medication_type,
            dose = excluded.dose,
            frequency = excluded.frequency,
            start_date = excluded.start_date,
            end_date = excluded.end_date,
            authorized_through = excluded.authorized_through,
            status = excluded.status,
            source_type = excluded.source_type,
            source_path = excluded.source_path
        """,
        (
            medication_row_id(
                str(medication["person_id"]), str(source_path), int(medication.get("row_index", 0))
            ),
            medication["person_id"],
            medication["rxnorm_cui"],
            medication["medication_name"],
            medication.get("medication_type", "prescription"),
            medication["dose"],
            medication["frequency"],
            medication["start_date"],
            medication["end_date"],
            medication.get("authorized_through"),
            medication["status"],
            source_type,
            source_path,
        ),
    )


def medication_name_key(name: str | None) -> str:
    """Normalise a medication name for cross-source matching (lowercase, single-spaced)."""
    if not name:
        return ""
    return " ".join(name.split()).strip().lower()


_FREQUENCY_CANONICAL: dict[str, str] = {
    "once daily": "once daily",
    "daily": "once daily",
    "once a day": "once daily",
    "every day": "once daily",
    "every morning": "once daily",
    "daily, morning": "once daily",
    "at bedtime": "once daily",
    "twice daily": "twice daily",
    "two times daily": "twice daily",
    "2 times daily": "twice daily",
    "bid": "twice daily",
    "three times daily": "three times daily",
    "3 times daily": "three times daily",
    "tid": "three times daily",
    "four times daily": "four times daily",
    "4 times daily": "four times daily",
    "qid": "four times daily",
    "every other day": "every other day",
    "weekly": "weekly",
    "once weekly": "weekly",
    "monthly": "monthly",
    "as needed": "as needed",
    "at night, as needed": "as needed",
    "prn": "as needed",
}

_PRN_QUALIFIER_RE = re.compile(r"\bas needed\b|\bprn\b")


def _normalize_frequency(raw: str | None) -> str:
    if not raw:
        return ""
    text = " ".join(raw.lower().split()).strip()
    text = text.rstrip(".;,")
    if text in _FREQUENCY_CANONICAL:
        return _FREQUENCY_CANONICAL[text]
    if re.search(r"\bbid\b", text, flags=re.IGNORECASE):
        return "twice daily"
    for prefix, canonical in (
        ("prn ", "as needed"),
        ("prn", "as needed"),
        ("as needed ", "as needed"),
        ("as directed", "as needed"),
        ("one by mouth, ", ""),
        ("one by mouth ", ""),
    ):
        if text.startswith(prefix):
            if canonical:
                return canonical
            text = text[len(prefix) :].strip()
            text = re.sub(
                r"\s*;\s*duration:\s*\d+\s*days?\s*$", "", text, flags=re.IGNORECASE
            ).strip()
            if text.endswith(" daily") or text == "daily":
                return "once daily"
    import re as _re

    for route_pattern, route_rest in (
        (
            r"place\s+\d+\s*\S*\s*(?:\([^)]*\)\s*)?(?:under\s+the\s+tongue|sublingually)",
            "as needed",
        ),
    ):
        if _re.match(route_pattern, text, flags=_re.IGNORECASE):
            return route_rest
    match = _re.match(
        r"(?:take|place|one|1)\s+"
        r"(?:(?:\d+|one)(?:\s*(?:tablet|tablets|capsule|capsules|caplet|caplets|pill|pills)|"
        r"\s+(?:mcg|mg|g|ug|iu|meq)\s+(?:tablet|tablets|capsule|capsules|caplet|caplets|pill|pills)?)"
        r"\s*(?:\([^)]*\)\s*)?by\s+mouth\s+)"
        r"(?P<freq>\d+\s*(?:\([^)]*\)\s*)?times?\s*daily|twice\s*daily|once\s*daily|daily)"
        r"\b",
        text,
        flags=_re.IGNORECASE,
    )
    if match:
        freq = match.group("freq").lower().rstrip(".")
        if "twice" in freq or "2 " in freq or " 2 " in freq:
            return "twice daily"
        if "three" in freq or "3 " in freq or " 3 " in freq:
            return "three times daily"
        if "four" in freq or "4 " in freq or " 4 " in freq:
            return "four times daily"
        return "once daily"
    # common patterns: once daily, daily, prn, etc. — return as-is after cleanup
    return text


def _medication_base_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(
        r"\s+"
        r"\d+(?:\.\d+)?\s*"
        r"(?:mcg|mg|g|ug|iu|meq)\.?\s*"
        r"(?:(?:delay|delayed|extended)\s+release\s+)?"
        r"(?:(?:\d+\s*hr|ec|dr|sl|sr|er|ir|xl|cr|orally\s+disintegrating)\s+)*"
        r"(?:tablet|tablets|capsule|capsules|caplet|solution|suspension|injection)?"
        r"\s*$",
        "",
        medication_name_key(name),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned)
    cleaned = cleaned.strip().rstrip(",").strip()
    stripped = re.sub(
        r"\s+(?:etexilate|hydrochloride|hcl|sodium|potassium|calcium|magnesium"
        r"|sulfate|mesylate|maleate|fumarate|succinate|tartrate|citrate|acetate)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    # Only apply salt stripping when the base name is long enough to be an organic drug
    # compound (≥8 chars). Short names like "calcium" or "iron" are often the active
    # ingredient itself, not a mere salt counterion — stripping would corrupt drug identity.
    if len(stripped) >= 8:
        cleaned = stripped
    return cleaned.strip()


def _normalize_dose_text(dose: str | None) -> str:
    if not dose:
        return ""
    text = " ".join(str(dose).strip().split())
    # Strip parenthetical explanations for deduplication
    # e.g., "0.8 mg (2 x 0.4 mg capsules)" -> "0.8 mg"
    text = re.sub(r"\s*\([^)]*\)", "", text)
    text = re.sub(r"(\d+)\s*-\s*(\d+)", r"\1-\2", text)
    for old, new in (
        ("ug", "mcg"),
        ("micrograms", "mcg"),
        ("microgram", "mcg"),
        ("milligrams", "mg"),
        ("milligram", "mg"),
        ("mgs", "mg"),
        ("grams", "g"),
        ("gram", "g"),
    ):
        text = re.sub(rf"\b{old}\b", new, text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip()


def _clean_medication_display_name(name: str | None) -> str:
    """Clean medication name for display by removing dose/formulation but keeping brand names.

    Examples:
    - "carvedilol (COREG) 3.125 mg tablet" -> "carvedilol (Coreg)"
    - "dabigatran (PRADAXA) 150 mg capsule" -> "dabigatran (Pradaxa)"
    - "tamsulosin (Flomax) 0.4 MG 24 hr capsule" -> "tamsulosin (Flomax)"
    - "pantoprazole (ProtoNix) 20 MG EC tablet" -> "pantoprazole (ProtoNix)"
    """
    if not name:
        return ""

    # Remove dose and formulation details
    # Pattern matches: dose + unit + optional modifiers + formulation
    cleaned = re.sub(
        r"\s+\d+(?:\.\d+)?\s*(?:mcg|ug|mg|g|iu|meq)\s+"
        r"(?:\d+\s*(?:hr|hour)s?\s+)?"  # Optional extended release hours (e.g., "24 hr")
        r"(?:(?:delayed|extended)\s+release\s+)?"
        r"(?:(?:ec|dr|sl|sr|er|ir|xl|cr)\s+)?"  # Optional formulation modifiers (EC, DR, etc.)
        r"(?:tablet|capsule|caplet|pill|solution|suspension|injection|ointment|cream)s?",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Normalize brand name capitalization: all caps -> Title case
    def normalize_brand(match):
        brand = match.group(1)
        if brand.isupper() and len(brand) > 2:
            # All caps brand -> title case
            return f"({brand.title()})"
        return match.group(0)

    cleaned = re.sub(r"\(([^)]+)\)", normalize_brand, cleaned)
    return " ".join(cleaned.split()).strip()


def _extract_dose_text(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(
        r"\b(?P<amount>\d+(?:\.\d+)?)\s*"
        r"(?P<unit>mcg|ug|micrograms?|mg|milligrams?|mgs?|g|grams?|iu|meq)\b",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return _normalize_dose_text(f"{match.group('amount')} {match.group('unit')}")


def _effective_dose_text(row: dict[str, Any]) -> str:
    explicit = _normalize_dose_text(row.get("dose"))
    if explicit:
        return explicit
    return _extract_dose_text(row.get("medication_name")) or _extract_dose_text(
        row.get("frequency")
    )


def _canonical_medication_type(value: str | None) -> str:
    normalized = (value or "prescription").strip().lower()
    if normalized in VALID_MEDICATION_TYPES:
        return normalized
    return "prescription"


def _dose_frequency_signature(row: dict[str, Any]) -> tuple[str, str]:
    return (_effective_dose_text(row), _normalize_frequency(row.get("frequency")))


def _frequency_duration_signature(frequency: str | None) -> str:
    if not frequency:
        return ""
    match = re.search(
        r"(?:duration:\s*|for\s+)(\d+)\s*days?\b",
        frequency,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _medication_period_signature(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    dose, frequency = _dose_frequency_signature(row)
    return (
        dose,
        frequency,
        _frequency_duration_signature(row.get("frequency")),
        str(row.get("start_date") or ""),
        str(row.get("end_date") or ""),
        str(row.get("status") or "active").strip().lower(),
    )


_CONFLICT_AXES: tuple[str, ...] = ("dose", "frequency")

# Values that record the absence of a fact rather than a fact. A row carrying one
# of these states nothing about that axis and cannot disagree about it (ADR-042).
_CONFLICT_DOSE_PLACEHOLDERS = frozenset({"", "unspecified", "unknown", "n/a", "none"})


def _conflict_axis_value(row: dict[str, Any], axis: str) -> str:
    """Return the row's normalized value for one conflict axis, or "" when the row
    states no value for it."""
    if axis == "dose":
        dose = _effective_dose_text(row)
        return "" if dose.strip().lower() in _CONFLICT_DOSE_PLACEHOLDERS else dose
    raw_frequency = row.get("frequency")
    # A PRN qualifier governs the whole instruction: "by mouth daily as needed" is
    # patient-directed dosing, not a scheduled daily dose. The shared normalizer
    # matches the "daily" and drops the trailing qualifier, which would make a PRN
    # medication read as contradicting a scheduled one. Collapsed here rather than
    # in _normalize_frequency so rendered reports keep the source's own wording.
    if raw_frequency and _PRN_QUALIFIER_RE.search(str(raw_frequency).lower()):
        return "as needed"
    return _normalize_frequency(raw_frequency)


def medication_conflict_axis_value_is_stated(axis: str, value: Any) -> bool:
    """Whether a proposed value states a fact under conflict-comparison semantics."""
    if axis not in _CONFLICT_AXES:
        raise ValueError(f"unsupported medication conflict axis: {axis}")
    return bool(_conflict_axis_value({axis: value}, axis))


def _medication_therapy_has_lapsed(row: dict[str, Any], as_of: str) -> bool:
    """Whether the row's therapy stopped before `as_of`.

    Only `end_date` terminates therapy. A lapsed `authorized_through` is a refill
    signal and leaves the medication current (ADR-041).
    """
    end_date = str(row.get("end_date") or "").strip()
    return bool(end_date) and end_date < as_of


def _is_temporal_axis_change(rows: list[dict[str, Any]], axis: str) -> bool:
    """Whether one axis's differing values are a dose change over time rather than a
    contradiction.

    Decided solely from an ordered sequence of dated rows. A missing `start_date` is
    absence of evidence, not evidence of recency, so an undated row never marks a
    value as superseded (ADR-041, ADR-042).
    """
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        start_date = str(row.get("start_date") or "").strip()
        if start_date:
            by_date.setdefault(start_date, []).append(row)

    ordered_dates = sorted(by_date, reverse=True)
    if len(ordered_dates) < 2:
        return False

    recent = {_conflict_axis_value(r, axis) for r in by_date[ordered_dates[0]]}
    older = {_conflict_axis_value(r, axis) for d in ordered_dates[1:] for r in by_date[d]}
    return recent.isdisjoint(older)


def _conflict_source_key(source_path: str) -> str:
    return split_archive_source_path(source_path)[0]


def _medication_conflict_name_key(name: str | None) -> str:
    if not name:
        return ""
    normalized = medication_name_key(name)
    parenthetical_aliases = [
        medication_name_key(match).strip()
        for match in re.findall(r"\(([^)]*)\)", normalized)
        if medication_name_key(match).strip()
    ]
    if parenthetical_aliases:
        return parenthetical_aliases[0]
    return _medication_base_name(normalized)


def medication_conflict_identity(
    medication_name: str | None,
    medication_type: str | None,
) -> tuple[str, str]:
    """Return the exact medication identity used by conflict detection."""
    return (
        _medication_conflict_name_key(medication_name),
        _canonical_medication_type(medication_type),
    )


def _prefer_combination_row(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Return whichever of existing/candidate better represents a combination-therapy dose.

    Preference order: explicit dose field > more recent start_date > cleaner name.
    """
    existing_has_dose = bool(existing.get("dose"))
    candidate_has_dose = bool(candidate.get("dose"))
    if candidate_has_dose and not existing_has_dose:
        return candidate
    if existing_has_dose and not candidate_has_dose:
        return existing
    existing_start = existing.get("start_date") or "0000-00-00"
    candidate_start = candidate.get("start_date") or "0000-00-00"
    if candidate_start > existing_start:
        return candidate
    if candidate_start == existing_start and _medication_name_is_cleaner(
        candidate.get("medication_name"), existing.get("medication_name")
    ):
        return candidate
    return existing


def _indicates_combination_therapy(frequency: str | None) -> bool:
    """Check if frequency text indicates this dose is part of a combination therapy."""
    if not frequency:
        return False
    text = frequency.lower()
    combination_indicators = [
        "add to",
        "along with",
        "total of",
        "combined with",
        "plus",
        "in addition to",
    ]
    return any(indicator in text for indicator in combination_indicators)


def _find_medications_from_other_sources(
    conn: sqlite3.Connection,
    person_id: str,
    current_medication_name: str,
    medication_type: str,
    current_source_path: str,
) -> list[dict[str, Any]]:
    """Return matching medication details from other sources."""
    rows = conn.execute(
        """
        SELECT medication_name, source_path, dose, frequency, start_date, end_date, status
        FROM medications
        WHERE person_id = ?
          AND medication_type = ?
          AND source_path != ?
        """,
        (person_id, medication_type, current_source_path),
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for row_medication_name, source_path, dose, frequency, start_date, end_date, status in rows:
        if not _medication_names_match(current_medication_name, row_medication_name):
            continue
        matches.append(
            {
                "medication_name": row_medication_name,
                "source_path": str(source_path),
                "dose": dose,
                "frequency": frequency,
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
            }
        )
    return matches


def _find_user_corrected_medication(
    conn: sqlite3.Connection,
    person_id: str,
    name_key: str,
    medication_type: str,
) -> tuple[str, str | None, str | None] | None:
    """Return (source_path, dose, frequency) for a matching user-authored medication row.

    Matches rows with ``source_type`` of ``'user_correction'`` or ``'user_update'`` so
    that dated update notes (REQ-024) receive the same re-import protection as explicit
    corrections (REQ-023).
    """
    rows = conn.execute(
        """
        SELECT medication_name, source_path, dose, frequency FROM medications
        WHERE person_id = ?
          AND medication_type = ?
          AND source_type IN ('user_correction', 'user_update')
        """,
        (person_id, medication_type),
    ).fetchall()
    for medication_name, source_path, dose, frequency in rows:
        if medication_name_key(medication_name) == name_key:
            return (str(source_path or ""), dose, frequency)
    return None


def _medication_row_is_user_correction(
    conn: sqlite3.Connection,
    person_id: str,
    source_path: str,
    row_index: int,
) -> bool:
    """Return True if the existing row at this ID was written by the user (correction or update)."""
    row_id = medication_row_id(person_id, source_path, row_index)
    row = conn.execute(
        "SELECT source_type FROM medications WHERE id = ? LIMIT 1", (row_id,)
    ).fetchone()
    return bool(row and row[0] in ("user_correction", "user_update"))


def remove_redundant_lower_authority_medications(
    conn: sqlite3.Connection,
    medication: dict[str, Any],
) -> None:
    """Audit and remove exact imported copies superseded by an authoritative row.

    Distinct dosage periods remain separate. User corrections and other user
    updates are never removed. Each removed row's stable canonical fields are
    serialized into an append-only audit event linked to the surviving
    authoritative row; volatile creation metadata is intentionally excluded.
    """
    person_id = str(medication.get("person_id") or "")
    medication_name = str(medication.get("medication_name") or "")
    medication_type = str(medication.get("medication_type") or "prescription")
    if not person_id or not medication_name:
        return

    authoritative_rows = conn.execute(
        """
        SELECT id, medication_name, dose, frequency, start_date, end_date, status
        FROM medications
        WHERE person_id = ?
          AND medication_type = ?
          AND source_type = 'user_update'
          AND source_path = ?
        """,
        (person_id, medication_type, str(medication.get("source_path") or "")),
    ).fetchall()
    incoming_signature = _medication_period_signature(medication)
    authoritative_id = next(
        (
            str(row_id)
            for row_id, name, dose, frequency, start_date, end_date, status in authoritative_rows
            if _medication_names_match(medication_name, name)
            and _medication_period_signature(
                {
                    "dose": dose,
                    "frequency": frequency,
                    "start_date": start_date,
                    "end_date": end_date,
                    "status": status,
                }
            )
            == incoming_signature
        ),
        None,
    )
    if authoritative_id is None:
        return

    columns = (
        "id",
        "person_id",
        "rxnorm_cui",
        "medication_name",
        "medication_type",
        "dose",
        "frequency",
        "start_date",
        "end_date",
        "status",
        "source_type",
        "source_path",
    )
    rows = conn.execute(
        """
        SELECT id, person_id, rxnorm_cui, medication_name, medication_type,
               dose, frequency, start_date, end_date, status, source_type, source_path
        FROM medications
        WHERE person_id = ?
          AND medication_type = ?
          AND source_type NOT IN ('user_correction', 'user_update')
        """,
        (person_id, medication_type),
    ).fetchall()
    redundant_rows = [
        dict(zip(columns, row, strict=True))
        for row in rows
        if _medication_names_match(medication_name, row[3])
        and _medication_period_signature(
            {
                "dose": row[5],
                "frequency": row[6],
                "start_date": row[7],
                "end_date": row[8],
                "status": row[9],
            }
        )
        == incoming_signature
    ]
    for redundant in redundant_rows:
        log_audit_event(
            conn,
            event_type=AUDIT_MEDICATION_REDUNDANT_ROW_SUPERSEDED,
            subject_type="medication",
            subject_id=authoritative_id,
            source_path=str(redundant["source_path"]),
            status=STATE_PROCESSED,
            message=json.dumps(
                {
                    "authoritative_medication_id": authoritative_id,
                    "removed_medication": redundant,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        conn.execute("DELETE FROM medications WHERE id = ?", (str(redundant["id"]),))


def _rematerialize_orphaned_medication_facts(
    conn: sqlite3.Connection, person_ids: set[str]
) -> None:
    """Restore skipped rows whose owning source no longer states the fact.

    A cross-source redundant row is skipped because an equivalent row already
    exists elsewhere. When that owning row is later removed — typically because
    its own source is reprocessed and no longer yields the fact — the fact would
    otherwise disappear from the canonical store while still rendering in every
    source-evidence note. Restoring it under the source that does state it keeps
    canonical data and retained evidence in agreement (ADR-038).
    """
    links = load_dedup_links(conn, subject_type=DEDUP_SUBJECT_MEDICATION, person_ids=person_ids)
    for link_id, person_id, suppressed_source_path, payload in links:
        medication = dict(payload)
        owners = _find_medications_from_other_sources(
            conn,
            person_id,
            str(medication.get("medication_name") or ""),
            str(medication.get("medication_type") or "prescription"),
            suppressed_source_path,
        )
        signature = _medication_period_signature(medication)
        if any(_medication_period_signature(owner) == signature for owner in owners):
            continue
        if (
            _find_user_corrected_medication(
                conn,
                person_id,
                medication_name_key(str(medication.get("medication_name") or "")),
                str(medication.get("medication_type") or "prescription"),
            )
            is not None
        ):
            # A user correction outranks a retained source (ADR-004). Leave the link
            # in place so the fact stays recoverable if that correction is revoked.
            continue
        upsert_medication(conn, medication)
        delete_dedup_link(conn, link_id)
        log_audit_event(
            conn,
            event_type=AUDIT_MEDICATION_DEDUPLICATED_ROW_RESTORED,
            subject_type="medication",
            subject_id=person_id,
            source_path=suppressed_source_path,
            status=STATE_PROCESSED,
            message=(
                "restored cross-source deduplicated medication "
                f"{medication.get('medication_name')!r} after its owning source stopped "
                "stating it"
            ),
        )


def replace_medications_for_source_path(
    conn: sqlite3.Connection,
    source_path: str,
    medications: list[dict[str, Any]],
    *,
    non_interactive: bool = True,
    conflict_resolver: (
        Callable[[str, str, str, str | None, str | None, str | None, str | None], ConflictChoice]
        | None
    ) = None,
) -> tuple[int, int]:
    """Replace rows for source_path, applying cross-source dedup against active rows.

    Within-source: existing rows for ``source_path`` are deleted first (idempotent
    re-import).
    Cross-source: a row whose (person_id, normalised name, medication_type)
    already exists from a different source is classified as:
      - redundant when dose, frequency, start, end, and status match → skipped
        to avoid repeated-source copies
      - distinct when any period field differs → retained so conflicts and
        lifecycle changes remain visible (REQ-004, ADR-006: no silent discard)

    When a user_correction row conflicts with an incoming import value:
    - non_interactive=True → skip + emit skipped_user_correction
    - non_interactive=False + conflict_resolver → call resolver(person_id, medication_name,
      medication_type, corrected_dose, corrected_frequency, import_dose, import_frequency)
      → ConflictChoice (same semantics as labs)
    - non_interactive=False + no resolver → raise MedicationSyncError

    Returns ``(inserted, skipped_redundant)``.
    """
    ensure_dedup_links_table(conn)
    # Only people this source touches can have a link orphaned by this call.
    affected_person_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT person_id FROM medications WHERE source_path = ?",
            (source_path,),
        )
        if row[0]
    }
    affected_person_ids.update(
        str(medication.get("person_id"))
        for medication in medications
        if medication.get("person_id")
    )
    # Keep explicit user corrections and user update notes authoritative across re-imports.
    conn.execute(
        """
        DELETE FROM medications
        WHERE source_path = ?
          AND source_type NOT IN ('user_correction', 'user_update')
        """,
        (source_path,),
    )
    # This source's own suppression links are re-derived below from the incoming rows.
    clear_dedup_links_for_source(
        conn, subject_type=DEDUP_SUBJECT_MEDICATION, suppressed_source_path=source_path
    )
    inserted = 0
    skipped = 0
    for row_index, medication in enumerate(medications):
        person_id = str(medication.get("person_id") or "")
        med_type = str(medication.get("medication_type") or "prescription")
        name_key = medication_name_key(medication.get("medication_name"))
        med_name = str(medication.get("medication_name") or "")
        corrected = _find_user_corrected_medication(conn, person_id, name_key, med_type)
        if corrected is not None:
            correction_source_path, corrected_dose, corrected_frequency = corrected
            if corrected_dose == medication.get("dose") and corrected_frequency == medication.get(
                "frequency"
            ):
                skipped += 1
                continue
            if non_interactive:
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="medication",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        "skipped import value for user-corrected medication "
                        f"{med_name!r} "
                        f"(correction source: {correction_source_path or 'unknown'})"
                    ),
                )
                skipped += 1
                continue
            if conflict_resolver is None:
                raise MedicationSyncError(
                    "interactive user-correction conflict encountered but no conflict_resolver "
                    "was provided; rerun non-interactively or resolve via fm-correct-fact"
                )
            choice = conflict_resolver(
                person_id,
                med_name,
                med_type,
                corrected_dose,
                corrected_frequency,
                medication.get("dose"),
                medication.get("frequency"),
            )
            if choice == "keep_correction":
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="medication",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        f"user chose keep_correction for medication {med_name!r}; import skipped"
                    ),
                )
                skipped += 1
                continue
            # accept_import or revoke_and_import: clear the correction flag
            conn.execute(
                "UPDATE medications SET source_type = 'manual' WHERE person_id = ? "
                "AND medication_type = ? AND source_type = 'user_correction'",
                (person_id, med_type),
            )
            if choice == "revoke_and_import":
                log_audit_event(
                    conn,
                    event_type=AUDIT_USER_CORRECTION_REVOKED,
                    subject_type="medication",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        f"user chose revoke_and_import for medication {med_name!r}; "
                        f"correction revoked; import value applied"
                    ),
                )
            # Fall through to insert.

        elif _medication_row_is_user_correction(conn, person_id, source_path, row_index):
            if non_interactive:
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="medication",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        "skipped import row-index overwrite of user-corrected medication "
                        f"{med_name!r}"
                    ),
                )
                skipped += 1
                continue
            if conflict_resolver is None:
                raise MedicationSyncError(
                    "interactive user-correction conflict encountered but no conflict_resolver "
                    "was provided; rerun non-interactively or resolve via fm-correct-fact"
                )
            corrected_row = conn.execute(
                "SELECT dose, frequency FROM medications WHERE person_id = ? AND source_path = ? "
                "AND source_type = 'user_correction' LIMIT 1",
                (person_id, source_path),
            ).fetchone()
            row_corrected_dose = corrected_row[0] if corrected_row else None
            row_corrected_frequency = corrected_row[1] if corrected_row else None
            choice = conflict_resolver(
                person_id,
                med_name,
                med_type,
                row_corrected_dose,
                row_corrected_frequency,
                medication.get("dose"),
                medication.get("frequency"),
            )
            if choice == "keep_correction":
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="medication",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        f"user chose keep_correction for row-index-corrected medication "
                        f"{med_name!r}; import skipped"
                    ),
                )
                skipped += 1
                continue
            conn.execute(
                "DELETE FROM medications WHERE person_id = ? AND source_path = ? "
                "AND source_type = 'user_correction'",
                (person_id, source_path),
            )
            if choice == "revoke_and_import":
                log_audit_event(
                    conn,
                    event_type=AUDIT_USER_CORRECTION_REVOKED,
                    subject_type="medication",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        f"user chose revoke_and_import for row-index-corrected medication "
                        f"{med_name!r}"
                    ),
                )
            # Fall through to insert.

        existing_rows = _find_medications_from_other_sources(
            conn, person_id, med_name, med_type, source_path
        )
        incoming_signature = _medication_period_signature(medication)
        medication_copy = dict(medication)
        medication_copy["source_path"] = source_path
        medication_copy["row_index"] = row_index
        medication_copy.setdefault("source_type", "manual")
        owner = next(
            (
                existing
                for existing in existing_rows
                if _medication_period_signature(existing) == incoming_signature
            ),
            None,
        )
        if owner is not None:
            record_dedup_link(
                conn,
                subject_type=DEDUP_SUBJECT_MEDICATION,
                person_id=person_id,
                suppressed_source_path=source_path,
                owner_source_path=str(owner.get("source_path") or ""),
                identity=json.dumps(list(incoming_signature)),
                payload=medication_copy,
            )
            skipped += 1
            continue
        upsert_medication(conn, medication_copy)
        inserted += 1
    _rematerialize_orphaned_medication_facts(conn, affected_person_ids)
    return inserted, skipped


def remove_imported_medications_for_source_prefix(
    conn: sqlite3.Connection,
    source_path_prefix: str,
) -> int:
    """Remove imported medication rows whose source path starts with a retained-source prefix."""
    if not source_path_prefix:
        raise MedicationSyncError("source_path_prefix is required")
    cursor = conn.execute(
        """
        DELETE FROM medications
        WHERE substr(source_path, 1, ?) = ?
          AND source_type NOT IN ('user_correction', 'user_update')
        """,
        (len(source_path_prefix), source_path_prefix),
    )
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def render_medication_note(
    medications: list[dict[str, Any]],
    *,
    source_file_name: str,
    source_path: str,
) -> str:
    if not medications:
        raise MedicationSyncError("at least one medication is required to render a note")
    person_ids = {row["person_id"] for row in medications}
    if len(person_ids) != 1:
        raise MedicationSyncError("all medications in one note must belong to one person_id")

    front_matter: dict[str, Any] = {
        "person_id": medications[0]["person_id"],
        "record_type": "medication",
        "source_file_name": source_file_name,
        "source_path": source_path,
        "projection_role": "source_evidence",
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    table_rows = [
        (
            "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
            "status | start_date | end_date | evidence |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in medications:
        table_rows.append(
            "| "
            + " | ".join(
                [
                    _markdown_table_cell(row.get("medication_name")),
                    _markdown_table_cell(row.get("medication_type", "prescription")),
                    _render_rxnorm_cell(row.get("rxnorm_cui")),
                    _markdown_table_cell(row.get("dose")),
                    _markdown_table_cell(row.get("frequency")),
                    _markdown_table_cell(row.get("status")),
                    _markdown_table_cell(row.get("start_date")),
                    _markdown_table_cell(row.get("end_date")),
                    _markdown_table_cell(row.get("evidence_snippet")),
                ]
            )
            + " |"
        )

    return (
        f"---\n{yaml_block}\n---\n\n"
        "# Medication List\n\n"
        "[[_index|← Medications]]\n\n"
        "## Medications\n\n"
        f"{chr(10).join(table_rows)}\n\n"
        "## Notes\n"
    )


def write_medication_note(
    note_path: Path,
    medications: list[dict[str, Any]],
    *,
    source_file_name: str,
    source_path: str,
) -> str:
    """Regenerate source evidence while preserving the user-editable Notes section."""
    fresh = render_medication_note(
        medications,
        source_file_name=source_file_name,
        source_path=source_path,
    )
    if not note_path.exists():
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    existing = note_path.read_text(encoding="utf-8")
    marker = "\n## Notes"
    existing_pos = existing.rfind(marker)
    fresh_pos = fresh.rfind(marker)
    updated = (
        fresh
        if existing_pos == -1 or fresh_pos == -1
        else fresh[:fresh_pos] + existing[existing_pos:]
    )
    note_path.write_text(updated, encoding="utf-8")
    return updated


def _empty_medication_source_note_candidates(
    *,
    vault_root: Path,
    person_id: str,
    source_paths: Sequence[str],
) -> dict[str, list[Path]]:
    medication_dir = vault_root / "Records" / person_id / "Medications"
    if not medication_dir.is_dir():
        return {}

    source_path_set = set(source_paths)
    candidates_by_source: dict[str, list[Path]] = {}
    for note_path in sorted(medication_dir.glob("*.md")):
        if note_path.name.startswith("_"):
            continue
        try:
            content = note_path.read_text(encoding="utf-8")
            front_matter = parse_front_matter(content)
        except (OSError, ValueError):
            continue
        if (
            front_matter.get("record_type") != "medication"
            or front_matter.get("source_path") not in source_path_set
            or front_matter.get("projection_role") not in {None, "source_evidence"}
        ):
            continue
        note_source_path = str(front_matter["source_path"])
        notes_marker = "\n## Notes"
        notes_position = content.rfind(notes_marker)
        user_notes = (
            content[notes_position + len(notes_marker) :].strip() if notes_position != -1 else ""
        )
        if user_notes:
            raise MedicationSyncError(
                "cannot retire empty medication source note with user-authored Notes content: "
                f"{note_path.relative_to(vault_root)}"
            )
        candidates_by_source.setdefault(note_source_path, []).append(note_path)
    return candidates_by_source


def validate_empty_medication_source_note_retirement(
    *,
    vault_root: Path,
    person_id: str,
    source_paths: Sequence[str],
) -> None:
    """Verify that exact source-evidence notes can be retired without data loss."""
    _empty_medication_source_note_candidates(
        vault_root=vault_root,
        person_id=person_id,
        source_paths=source_paths,
    )


def retire_empty_medication_source_notes(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    source_paths: Sequence[str],
) -> list[Path]:
    """Remove stale generated notes when a source yields no medication facts.

    Only exact source-bound medication notes are eligible. A legacy note without
    ``projection_role`` is compatible, but any content beneath ``## Notes``
    blocks retirement so reprocessing cannot silently discard user-authored
    context.
    """
    candidates_by_source = _empty_medication_source_note_candidates(
        vault_root=vault_root,
        person_id=person_id,
        source_paths=source_paths,
    )

    if not candidates_by_source:
        return []

    medication_dir = vault_root / "Records" / person_id / "Medications"
    candidates = [
        note_path
        for source_candidates in candidates_by_source.values()
        for note_path in source_candidates
    ]
    for note_path in candidates:
        note_path.unlink()
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        medication_dir,
        person_id,
        "Medications",
        display_name=display_name,
    )
    for source_path, source_candidates in candidates_by_source.items():
        note_paths = ", ".join(
            path.relative_to(vault_root).as_posix() for path in source_candidates
        )
        log_audit_event(
            conn,
            event_type=AUDIT_MEDICATION_SOURCE_NOTE_RETIRED,
            subject_type="medication",
            subject_id=person_id,
            source_path=source_path,
            status=STATE_PROCESSED,
            message=(
                "retired generated medication source note after exact source reprocessing "
                f"yielded no accepted medication facts: {note_paths}"
            ),
        )
    return candidates


def _markdown_table_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _render_rxnorm_cell(value: Any) -> str:
    normalized = _normalize_optional_text(str(value)) if value is not None else None
    return normalized or "unresolved"


def medication_note_path(
    person_medications_dir: Path,
    document_date: str | None,
    source_file: Path,
) -> Path:
    date_prefix = document_date or "undated"
    slug = slugify_filename(
        source_stem_without_redundant_date_prefix(source_file.stem, document_date)
    )
    suffix = "" if slug.endswith("-medications") else "-medications"
    return person_medications_dir / f"{date_prefix}-{slug}{suffix}.md"


def _user_suppressed_medications(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[tuple[str, str, str, str]]:
    """Return [(medication_name, canonical_type, source_path, dose), ...] for medications
    the user marked not-taken or discontinued.

    Only user-authored rows (user_correction, user_update) are authoritative overrides;
    imported rows with these statuses are not treated as suppressors. Matching against
    candidate active rows uses `_medication_names_match` (the same brand/generic alias
    overlap already used for import-time dedup), not exact base-name equality, so a
    discontinuation recorded under a brand name (e.g. "Zoloft") still suppresses an
    active row imported under its generic/source name (e.g. "sertraline (Zoloft) 50 mg
    tablet"). A user-reported discontinuation must suppress an older open-ended active
    row for the same medication recorded under a *different* dated note.

    Within a *single* note the same-source_path exception applies only when the doses
    differ, which is what distinguishes a titration (discontinue 37.5 mg, start 75 mg —
    both rows are true) from a contradiction (one row says a 25 mcg dose is active while
    another says the same 25 mcg dose was never started — only one can be true). See
    `_is_user_suppressed_medication_row`.
    """
    rows = conn.execute(
        """
        SELECT medication_name, medication_type, source_path, dose FROM medications
        WHERE person_id = ? AND LOWER(status) IN ('not-taken', 'discontinued')
          AND source_type IN ('user_correction', 'user_update')
        """,
        (person_id,),
    ).fetchall()
    return [
        (name, _canonical_medication_type(med_type), source_path or "", dose or "")
        for name, med_type, source_path, dose in rows
    ]


def _is_user_suppressed_medication_row(
    row: dict[str, Any], suppressing: list[tuple[str, str, str, str]]
) -> bool:
    row_type = _canonical_medication_type(row.get("medication_type"))
    row_source_path = row.get("source_path") or ""
    row_dose = _normalize_dose_text(row.get("dose"))
    for name, med_type, source_path, dose in suppressing:
        if med_type != row_type:
            continue
        if source_path == row_source_path and _normalize_dose_text(dose) != row_dose:
            # Same note, different dose: a titration recorded as one statement.
            continue
        if _medication_names_match(name, row.get("medication_name")):
            return True
    return False


def filter_user_suppressed_medication_rows(
    conn: sqlite3.Connection,
    person_id: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop rows the user has authoritatively marked not-taken/discontinued elsewhere.

    Shared by every report that derives "current medications" from the `medications`
    table (current-medications.md, PGx coverage/wallet-card, medication-safety-alerts),
    so a user-reported discontinuation suppresses matching rows consistently across all
    of them instead of only the report that happens to call `_query_current_medications`
    directly. Each row dict must carry medication_name, medication_type, and source_path.
    """
    suppressing = _user_suppressed_medications(conn, person_id)
    if not suppressing:
        return rows
    return [r for r in rows if not _is_user_suppressed_medication_row(r, suppressing)]


def _query_current_medications(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date().isoformat()
    cursor = conn.execute(
        """
        SELECT medication_name, medication_type, rxnorm_cui, dose, frequency, status,
               start_date, end_date, authorized_through, source_type, source_path
        FROM medications
        WHERE person_id = ? AND """
        + _CURRENT_ACTIVE_STATUS_SQL
        + """
          AND (start_date IS NULL OR start_date <= ?)
        ORDER BY medication_name COLLATE NOCASE, start_date, end_date
        """,
        (person_id, today),
    )
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]
    _annotate_source_list_currency(rows)
    rows = filter_user_suppressed_medication_rows(conn, person_id, rows)
    rows = _deduplicate_medication_rows(rows)
    _backfill_earliest_start_dates(conn, person_id, rows)
    return rows


def query_current_medications(conn: sqlite3.Connection, person_id: str) -> list[dict[str, Any]]:
    """The current-medication set shown in current-medications.md.

    Every report that reasons about what a person takes now (PGx coverage, the
    wallet card, medication-safety alerts) reads this one set, so stopped,
    aborted, completed, or user-suppressed rows never reach them.
    """
    return _query_current_medications(conn, person_id)


def _medication_recency_key(row: dict[str, Any]) -> str:
    """How recently this row's regimen was stated, for ranking contradictory rows (ADR-041).

    A row's own `start_date` is the signal whenever it has one. When it does not, stand in
    the currency of its source document's medication list, so a legacy export cannot
    outrank a current list merely by omitting a date.

    When the document offers no dating evidence either, keep the long-standing behaviour of
    treating the row as current. An undated row on an undated list is a list that simply
    does not record start dates — that is absence of evidence, not evidence the regimen is
    stale, and demoting it would drop hand-entered rows that carry the only real dose and
    frequency text.
    """
    start_date = row.get("start_date")
    if start_date:
        return str(start_date)
    return str(row.get("_source_list_currency") or "") or "9999-99-99"


def _annotate_source_list_currency(rows: list[dict[str, Any]]) -> None:
    """Tag each row with how current its source document's medication list is (ADR-041).

    A medication list states its own currency through the start dates of the drugs on it:
    an export whose every entry begins in 2019 is a legacy record, even when the file was
    produced last week. Import date cannot express this — archives are always imported
    later than the therapy they describe — so a row's own document is scored by the latest
    start_date any of that person's active medications carries from the same source.

    Used only to break ties between contradictory active rows; it never removes a row.
    """
    latest_by_source: dict[str, str] = {}
    for row in rows:
        source = str(row.get("source_path") or "")
        start = row.get("start_date")
        if not source or not start:
            continue
        start_text = str(start)
        if source not in latest_by_source or start_text > latest_by_source[source]:
            latest_by_source[source] = start_text
    for row in rows:
        row["_source_list_currency"] = latest_by_source.get(str(row.get("source_path") or ""), "")


def _backfill_earliest_start_dates(
    conn: sqlite3.Connection,
    person_id: str,
    rows: list[dict[str, Any]],
) -> None:
    """Replace each row's start_date with the earliest known date for the same dose.

    The active-row query only surfaces the current prescription period. Older records
    (aborted, completed) may show an earlier start that predates the current renewal —
    the user wants to see when they originally started the medication, not when the
    latest prescription was written. Grouping by dose ensures a prior titration step
    does not overwrite the start date of a later, different-dose period.
    """
    if not rows:
        return
    cursor = conn.execute(
        """
        SELECT medication_name, medication_type, dose, frequency, start_date
        FROM medications
        WHERE person_id = ? AND start_date IS NOT NULL
        """,
        (person_id,),
    )
    hist_cols = [d[0] for d in cursor.description]
    earliest: dict[tuple[str, str, str], str] = {}
    for hist_row_data in cursor.fetchall():
        hist_row = dict(zip(hist_cols, hist_row_data, strict=True))
        key = (
            _medication_base_name(hist_row.get("medication_name")),
            _canonical_medication_type(hist_row.get("medication_type")),
            _effective_dose_text(hist_row),
        )
        date = str(hist_row["start_date"])
        if key not in earliest or date < earliest[key]:
            earliest[key] = date

    # Also build a dose-agnostic index for rows whose dose cannot be determined.
    earliest_any_dose: dict[tuple[str, str], str] = {}
    for (base, med_type, _dose), date in earliest.items():
        key2 = (base, med_type)
        if key2 not in earliest_any_dose or date < earliest_any_dose[key2]:
            earliest_any_dose[key2] = date

    for row in rows:
        base = _medication_base_name(row.get("medication_name"))
        med_type = _canonical_medication_type(row.get("medication_type"))
        dose_sig = _effective_dose_text(row)
        if dose_sig:
            hist = earliest.get((base, med_type, dose_sig))
        else:
            # No dose known for this row; use the earliest record regardless of dose.
            hist = earliest_any_dose.get((base, med_type))
        current = row.get("start_date")
        if hist and (current is None or hist < current):
            row["start_date"] = hist


def _is_expired_short_term(
    frequency: str,
    start_date: str | None,
    end_date: str | None,
    today: str,
) -> bool:
    freq_lower = frequency.lower()
    duration_match = re.search(r"duration:\s*(\d+)\s*days?", freq_lower)
    if not duration_match:
        return False
    days = int(duration_match.group(1))
    if days > 90:
        return False
    if end_date and str(end_date) < today:
        return True
    if start_date:
        from datetime import date as _date

        try:
            ref = _date.fromisoformat(str(start_date))
            elapsed = (_date.fromisoformat(today) - ref).days
            if elapsed > days + 30:
                return True
        except ValueError:
            pass
    return False


def _deduplicate_medication_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date().isoformat()
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        frequency = _normalize_frequency(row.get("frequency"))
        if _is_expired_short_term(frequency, row.get("start_date"), row.get("end_date"), today):
            continue
        key = (
            _medication_base_name(row.get("medication_name")),
            _canonical_medication_type(row.get("medication_type")),
            _effective_dose_text(row),
            frequency,
        )
        existing = seen.get(key)
        if existing is None:
            seen[key] = row
        elif _indicates_combination_therapy(row.get("frequency")):
            if not _indicates_combination_therapy(existing.get("frequency")):
                seen[key] = row
            else:
                seen[key] = _prefer_combination_row(existing, row)
        elif _medication_name_is_cleaner(
            row.get("medication_name"), existing.get("medication_name")
        ):
            seen[key] = row
    deduplicated = list(seen.values())
    deduplicated = _collapse_dose_changes_over_time(deduplicated)
    deduplicated = _collapse_user_authored_same_therapy_rows(deduplicated)
    # Clean up display names and normalize frequencies for all final results
    deduplicated = _clean_medication_rows_for_display(deduplicated)
    return _sort_current_medication_rows(deduplicated)


def _clean_medication_rows_for_display(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean medication rows for display by:
    1. Removing dose/formulation from medication names
    2. Ensuring dose fields are populated
    3. Normalizing frequency text
    """
    cleaned = []
    for row in rows:
        cleaned_row = dict(row)  # Make a copy
        # Clean the medication name
        cleaned_row["medication_name"] = _clean_medication_display_name(row.get("medication_name"))
        # Ensure dose field is populated
        if not cleaned_row.get("dose"):
            cleaned_row["dose"] = _effective_dose_text(row)
        # Normalize frequency for display
        cleaned_row["frequency"] = _normalize_frequency(row.get("frequency"))
        cleaned.append(cleaned_row)
    return cleaned


def _deduplicate_conflict_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate conflict rows after display cleanup.

    After name cleaning and frequency normalization, some conflict rows may become
    exact duplicates. Keep each retained source and conflict axis visible because
    Human Resolver review depends on complete provenance.
    """
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("medication_name") or "",
            row.get("dose") or "",
            row.get("frequency") or "",
            row.get("source_path") or "",
            row.get("conflict_axis") or "",
        )
        if key not in seen:
            seen[key] = row
    return list(seen.values())


def _sort_current_medication_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            medication_name_key(row.get("medication_name")),
            str(row.get("start_date") or ""),
            str(row.get("end_date") or ""),
            str(row.get("dose") or ""),
        ),
    )


def _collapse_dose_changes_over_time(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse different doses of the same medication into the most recent dose,
    unless they represent combination therapy (indicated by 'add to', 'along with', etc.)
    or are part of a user-authored titration schedule with defined periods.

    Also cleans up display names and ensures dose fields are populated.

    For the same medication with different doses:
    - If any dose has combination therapy indicator → keep one row per unique dose value
      (but deduplicate same dose/frequency combinations, preferring better-formed rows)
    - Past-period rows (end_date < today) are filtered first — only current/future rows
      are kept.
    - If remaining rows have end_date values → future titration schedule, keep all
    - Otherwise → keep only the most recent dose (dosage change over time)
    """
    today = datetime.now(UTC).date().isoformat()
    # Group by base medication name and type
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            _medication_base_name(row.get("medication_name")),
            _canonical_medication_type(row.get("medication_type")),
        )
        grouped.setdefault(key, []).append(row)

    collapsed: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        if len(group_rows) == 1:
            collapsed.extend(group_rows)
            continue

        # Check if there are different doses/frequencies
        signatures = {_dose_frequency_signature(row) for row in group_rows}
        if len(signatures) == 1:
            # All same dose/frequency, just pick the cleanest name
            collapsed.append(min(group_rows, key=lambda r: len(r.get("medication_name") or "")))
            continue

        # Check for combination therapy indicators
        has_combination = any(
            _indicates_combination_therapy(row.get("frequency")) for row in group_rows
        )

        if has_combination:
            # Combination therapy: keep one row per unique dose value
            # Prefer rows with explicit dose, more recent start_date, and cleaner names
            by_dose: dict[str, dict[str, Any]] = {}
            no_dose_rows: list[dict[str, Any]] = []
            for row in group_rows:
                dose_key = _effective_dose_text(row)
                if not dose_key:
                    # No extractable dose — preserve rather than drop silently
                    no_dose_rows.append(row)
                    continue
                existing = by_dose.get(dose_key)
                if existing is None:
                    by_dose[dose_key] = row
                else:
                    by_dose[dose_key] = _prefer_combination_row(existing, row)

            collapsed.extend(by_dose.values())
            collapsed.extend(no_dose_rows)
            continue

        # Drop rows whose end_date is in the past — completed dose periods.
        current_rows = [
            row for row in group_rows if not row.get("end_date") or row["end_date"] >= today
        ]
        if not current_rows:
            # All periods ended; keep the most recent as a best-effort fallback.
            current_rows = [max(group_rows, key=lambda r: r.get("end_date") or "0000-00-00")]

        # Check if remaining rows form a future titration schedule with defined periods
        has_future_periods = any(row.get("end_date") for row in current_rows)

        if has_future_periods:
            # Future titration schedule: keep all current doses to show the full schedule
            collapsed.extend(current_rows)
        else:
            user_authored_rows = [row for row in current_rows if _row_is_user_authored(row)]
            if user_authored_rows:
                collapsed.append(
                    sorted(user_authored_rows, key=_medication_recency_key, reverse=True)[0]
                )
                continue
            # Dosage change over time: keep only the most recently stated dose.
            # See _medication_recency_key for how a row without a start_date is ranked.
            sorted_by_date = sorted(current_rows, key=_medication_recency_key, reverse=True)
            collapsed.append(sorted_by_date[0])

    return collapsed


def _collapse_user_authored_same_therapy_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    for row in rows:
        replacement_index: int | None = None
        for index, existing in enumerate(collapsed):
            if not _rows_represent_same_user_visible_therapy(existing, row):
                continue
            preferred = _prefer_user_visible_medication_row(existing, row)
            collapsed[index] = preferred
            replacement_index = index
            break
        if replacement_index is None:
            collapsed.append(row)
    return collapsed


def _rows_represent_same_user_visible_therapy(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    if _row_is_user_authored(left) and _row_is_user_authored(right):
        return False
    if _canonical_medication_type(left.get("medication_type")) != _canonical_medication_type(
        right.get("medication_type")
    ):
        return False
    left_aliases = _medication_name_aliases(left.get("medication_name"))
    right_aliases = _medication_name_aliases(right.get("medication_name"))
    if not left_aliases or not right_aliases or left_aliases.isdisjoint(right_aliases):
        return False
    if _dose_frequency_signature(left) == _dose_frequency_signature(right):
        return True
    return _row_is_user_authored(left) or _row_is_user_authored(right)


def _medication_name_aliases(name: str | None) -> set[str]:
    if not name:
        return set()
    normalized = medication_name_key(name)
    aliases: set[str] = set()
    outside = re.sub(r"\([^)]*\)", " ", normalized)
    outside_base = _medication_base_name(outside)
    if outside_base:
        aliases.add(outside_base)
    aliases.update(
        medication_name_key(match).strip()
        for match in re.findall(r"\(([^)]*)\)", normalized)
        if medication_name_key(match).strip()
    )
    return aliases


def _medication_names_match(name_a: str | None, name_b: str | None) -> bool:
    """Return True if two medication name strings refer to the same medication.

    Brand and generic names (e.g. "Zoloft" vs "sertraline (Zoloft) 50 mg tablet")
    share a parenthetical alias even though their stripped base names differ, so
    this compares alias sets rather than requiring exact base-name equality.
    """
    aliases_a = _medication_name_aliases(name_a)
    aliases_b = _medication_name_aliases(name_b)
    key_a = medication_name_key(name_a)
    key_b = medication_name_key(name_b)
    if aliases_a and aliases_b:
        return not aliases_a.isdisjoint(aliases_b)
    if aliases_a:
        return key_b in aliases_a or key_b == key_a
    if aliases_b:
        return key_a in aliases_b
    return key_a == key_b


def _row_is_user_authored(row: dict[str, Any]) -> bool:
    return str(row.get("source_type") or "") in {"user_update", "user_correction"}


def _prefer_user_visible_medication_row(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    left_user_authored = _row_is_user_authored(left)
    right_user_authored = _row_is_user_authored(right)
    if left_user_authored != right_user_authored:
        return left if left_user_authored else right
    if _medication_name_is_cleaner(right.get("medication_name"), left.get("medication_name")):
        return right
    return left


def _medication_name_is_cleaner(new_name: str | None, old_name: str | None) -> bool:
    new_len = len(new_name) if new_name else 999
    old_len = len(old_name) if old_name else 999
    if new_len < old_len:
        return True
    return False


def _render_authorized_through_cell(authorized_through: Any) -> str:
    """Render the refill-authorization date, flagging one that has already lapsed.

    A lapsed authorization means the prescription needs renewing, not that the person
    stopped taking the drug (ADR-041), so it is surfaced as an action rather than in the
    End column where it would read as a discontinuation.
    """
    text = _markdown_table_cell(authorized_through)
    if not text:
        return ""
    if text < datetime.now(UTC).date().isoformat():
        return f"{text} — renewal due"
    return text


def _render_medications_table(meds: list[dict[str, Any]], *, include_type: bool = False) -> str:
    if not meds:
        return "_None recorded._\n"
    headers = ["Medication"]
    if include_type:
        headers.append("Type")
    headers.extend(["Dose", "Frequency", "Status", "Start", "End", "Refills through", "RxNorm"])
    header = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    rows = [header, sep]
    for m in meds:
        cells = [_markdown_table_cell(m.get("medication_name"))]
        if include_type:
            cells.append(_medication_type_label(m.get("medication_type")))
        cells.extend(
            [
                _markdown_table_cell(m.get("dose")),
                _markdown_table_cell(m.get("frequency")),
                _markdown_table_cell(m.get("status")),
                _markdown_table_cell(m.get("start_date")),
                _markdown_table_cell(m.get("end_date")),
                _render_authorized_through_cell(m.get("authorized_through")),
                _render_rxnorm_cell(m.get("rxnorm_cui")),
            ]
        )
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


_MEDICATION_TYPE_HEADINGS: tuple[tuple[str, str], ...] = (
    ("prescription", "Prescriptions"),
    ("otc", "Over-the-counter"),
    ("supplement", "Supplements"),
    ("herbal", "Herbals"),
    ("vaccine", "Vaccines"),
)

_NON_PRN_SCHEDULE_HEADINGS: tuple[tuple[str, str], ...] = (
    ("regular", "Regular"),
    ("unspecified", "Schedule not recorded"),
)

_MEDICATION_TYPE_LABELS: dict[str, str] = {
    "prescription": "Prescription",
    "otc": "OTC",
    "supplement": "Supplement",
    "herbal": "Herbal",
    "vaccine": "Vaccine",
}


def _medication_type_label(value: str | None) -> str:
    return _MEDICATION_TYPE_LABELS[_canonical_medication_type(value)]


def _medication_schedule_group(row: dict[str, Any]) -> str:
    frequency = _normalize_frequency(row.get("frequency"))
    if re.search(r"\b(?:as needed|prn)\b", frequency, flags=re.IGNORECASE):
        return "as_needed"
    if frequency:
        return "regular"
    return "unspecified"


def _render_grouped_medication_sections(meds: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    as_needed_meds = [med for med in meds if _medication_schedule_group(med) == "as_needed"]
    for medication_type, type_heading in _MEDICATION_TYPE_HEADINGS:
        typed_meds = [
            med
            for med in meds
            if _canonical_medication_type(med.get("medication_type")) == medication_type
            and _medication_schedule_group(med) != "as_needed"
        ]
        if not typed_meds:
            continue
        sections.append(f"## {type_heading}\n")
        for schedule_group, schedule_heading in _NON_PRN_SCHEDULE_HEADINGS:
            scheduled_meds = [
                med for med in typed_meds if _medication_schedule_group(med) == schedule_group
            ]
            if not scheduled_meds:
                continue
            sections.append(f"### {schedule_heading}\n")
            sections.append(_render_medications_table(scheduled_meds))
    if as_needed_meds:
        sections.append("## As needed\n")
        sections.append(_render_medications_table(as_needed_meds, include_type=True))
    return "\n".join(sections)


def write_current_medications_report(
    vault_root: Path,
    person_id: str,
    conn: sqlite3.Connection,
) -> Path | None:
    """Generate current-medications.md per ADR-014 report-refresh hook.

    Returns None and removes any stale report when the person has no active
    medications — an empty 'None recorded.' report carries no information.
    """
    all_meds = _query_current_medications(conn, person_id)
    if not all_meds:
        stale = vault_root / "Records" / person_id / "Reports" / "current-medications.md"
        if stale.exists():
            stale.unlink()
        return None

    person_reports_dir = vault_root / "Records" / person_id / "Reports"
    person_reports_dir.mkdir(parents=True, exist_ok=True)

    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "current_medications",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    evidence_link = _medication_source_evidence_index_link(vault_root, person_id)
    sections: list[str] = [
        f"---\n{yaml_block}\n---\n",
        "# Current Medications\n",
        f"{evidence_link}\n" if evidence_link else "",
        "\n".join(render_allergy_section(vault_root, person_id)),
        _render_grouped_medication_sections(all_meds),
    ]

    report_path = person_reports_dir / "current-medications.md"
    report_path.write_text("\n".join(sections), encoding="utf-8")
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return report_path


def _medication_source_evidence_index_link(
    vault_root: Path,
    person_id: str,
) -> str | None:
    medication_dir = vault_root / "Records" / person_id / "Medications"
    if not medication_dir.exists():
        return None
    for note_path in medication_dir.glob("*.md"):
        if note_path.name.startswith("_"):
            continue
        try:
            front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if front_matter.get("projection_role") == "source_evidence":
            return f"[[Records/{person_id}/Medications/_index|Medication source evidence]]"
    return None


def build_medication_conflicts_from_db(
    conn: sqlite3.Connection,
    person_id: str,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Return rows where retained sources disagree about a current medication.

    Dose and frequency are compared independently (ADR-042): a row that states no
    value for an axis cannot disagree about it, and therapy that has already stopped
    cannot contradict therapy that is current. Non-current statuses are historical
    records and do not signal a current conflict.
    """
    as_of_date = as_of or datetime.now(UTC).date().isoformat()
    rows = conn.execute(
        """
        SELECT medication_name, medication_type, dose, frequency, source_path, start_date,
               end_date, source_type
        FROM medications
        WHERE person_id = ?
          AND """
        + _CURRENT_ACTIVE_STATUS_SQL
        + """
        ORDER BY medication_name COLLATE NOCASE, medication_type, source_path
        """,
        (person_id,),
    ).fetchall()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (
        medication_name,
        medication_type,
        dose,
        frequency,
        source_path,
        start_date,
        end_date,
        source_type,
    ) in rows:
        key = medication_conflict_identity(medication_name, medication_type)
        row = {
            "medication_name": str(medication_name),
            "medication_type": str(medication_type or ""),
            "dose": dose,
            "frequency": frequency,
            "source_path": str(source_path or ""),
            "start_date": start_date,
            "end_date": end_date,
            "source_type": str(source_type or ""),
            "conflict_name_key": key[0],
        }
        if _medication_therapy_has_lapsed(row, as_of_date):
            continue
        grouped.setdefault(key, []).append(row)

    conflicts: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        # Skip if this appears to be combination therapy (multiple doses taken together)
        # Look for indicators like "add to", "along with", "total of" in frequency text
        if any(_indicates_combination_therapy(row.get("frequency")) for row in group_rows):
            # If any source indicates combination therapy, assume all dose variations
            # for this medication are part of combination therapy, not conflicts
            continue

        for axis in _CONFLICT_AXES:
            # Only rows that actually state a value for this axis can disagree about it.
            stating = [row for row in group_rows if _conflict_axis_value(row, axis)]
            if len({_conflict_axis_value(row, axis) for row in stating}) <= 1:
                continue
            if any(row["source_type"] in {"user_correction", "user_update"} for row in stating):
                # An explicit user-authority row resolves this axis even though the
                # retained source facts remain available for provenance (ADR-004,
                # ADR-042).
                continue
            # The report's subject is disagreement between documents; a single source
            # contradicting itself is resolved upstream (ADR-042).
            if len({row["source_path"] for row in stating}) <= 1:
                continue
            if _is_temporal_axis_change(stating, axis):
                continue

            deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
            for row in stating:
                source_key = _conflict_source_key(row["source_path"])
                deduplicated.setdefault(
                    (source_key, _conflict_axis_value(row, axis)),
                    {**row, "source_path": source_key, "conflict_axis": axis},
                )
            conflicts.extend(deduplicated.values())
    return conflicts


_MEDICATION_CONFLICT_REVIEW_SCHEMA = "famed.medication-conflict-review.v1"


def medication_conflict_groups(
    conflict_rows: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in conflict_rows:
        key = (
            str(row.get("conflict_name_key") or ""),
            _canonical_medication_type(row.get("medication_type")),
            str(row.get("conflict_axis") or ""),
        )
        groups.setdefault(key, []).append(row)
    return groups


def _medication_conflict_event(
    person_id: str,
    group_key: tuple[str, str, str],
    rows: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    medication_key, medication_type, conflict_axis = group_key
    candidate_pairs = sorted(
        {
            (
                str(row.get("source_path") or ""),
                _conflict_axis_value(row, conflict_axis),
            )
            for row in rows
        }
    )
    candidates = [
        {"source_path": source_path, "value": value} for source_path, value in candidate_pairs
    ]
    identity_payload = {
        "person_id": person_id,
        "medication_key": medication_key,
        "medication_type": medication_type,
        "conflict_axis": conflict_axis,
        "candidates": candidates,
    }
    conflict_id = hashlib.sha1(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "schema_version": _MEDICATION_CONFLICT_REVIEW_SCHEMA,
        "conflict_id": conflict_id,
        "medication_name": _clean_medication_display_name(
            str(rows[0].get("medication_name") or medication_key)
        ),
        **identity_payload,
    }
    return conflict_id, payload


def _latest_medication_conflict_event(
    conn: sqlite3.Connection,
    conflict_id: str,
) -> str | None:
    latest = _latest_medication_conflict_event_record(conn, conflict_id)
    return latest[1] if latest else None


def _latest_medication_conflict_event_record(
    conn: sqlite3.Connection,
    conflict_id: str,
) -> tuple[int, str] | None:
    ensure_audit_events_table(conn)
    row = conn.execute(
        """
        SELECT rowid, event_type
        FROM audit_events
        WHERE subject_type = 'medication_conflict' AND subject_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (conflict_id,),
    ).fetchone()
    return (int(row[0]), str(row[1])) if row else None


def _has_later_medication_correction_revocation(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    medication_type: str,
    rows: list[dict[str, Any]],
    after_rowid: int,
) -> bool:
    ensure_audit_events_table(conn)
    for row in rows:
        medication_name = row.get("medication_name")
        if not medication_name:
            continue
        event = conn.execute(
            """
            SELECT 1
            FROM audit_events
            WHERE rowid > ?
              AND event_type = 'user_correction_revoked'
              AND subject_type = 'medication'
              AND subject_id = ?
              AND message LIKE ?
            LIMIT 1
            """,
            (
                after_rowid,
                person_id,
                f"%medication {str(medication_name)!r} ({medication_type})%",
            ),
        ).fetchone()
        if event is not None:
            return True
    return False


def _latest_medication_conflict_payload_for_identity(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    medication_key: str,
    medication_type: str,
) -> tuple[str, dict[str, Any]] | None:
    ensure_audit_events_table(conn)
    rows = conn.execute(
        """
        SELECT subject_id, message
        FROM audit_events
        WHERE subject_type = 'medication_conflict'
        ORDER BY rowid DESC
        """
    ).fetchall()
    for subject_id, message in rows:
        try:
            payload = json.loads(str(message or "{}"))
        except json.JSONDecodeError:
            continue
        if (
            payload.get("person_id") == person_id
            and payload.get("medication_key") == medication_key
            and payload.get("medication_type") == medication_type
        ):
            return str(subject_id), payload
    return None


def unresolved_medication_conflict_rows(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    """Return current medication conflict rows whose latest audit event is pending."""
    current_rows = build_medication_conflicts_from_db(conn, person_id)
    record_medication_conflict_detection_events(conn, person_id, current_rows)
    unresolved: list[dict[str, Any]] = []
    for group_key, rows in medication_conflict_groups(current_rows).items():
        conflict_id, _payload = _medication_conflict_event(person_id, group_key, rows)
        if (
            _latest_medication_conflict_event(conn, conflict_id)
            == AUDIT_MEDICATION_CONFLICT_DETECTED
        ):
            unresolved.extend(rows)
    return unresolved


def record_medication_conflict_detection_events(
    conn: sqlite3.Connection,
    person_id: str,
    conflict_rows: list[dict[str, Any]],
    *,
    reopen_resolved: bool = False,
) -> int:
    """Append one pending event for each newly current medication contradiction."""
    recorded = 0
    for group_key, rows in medication_conflict_groups(conflict_rows).items():
        conflict_id, payload = _medication_conflict_event(person_id, group_key, rows)
        latest = _latest_medication_conflict_event_record(conn, conflict_id)
        latest_event = latest[1] if latest else None
        if latest_event == AUDIT_MEDICATION_CONFLICT_DETECTED:
            continue
        was_revoked_after_resolution = (
            latest is not None
            and _has_later_medication_correction_revocation(
                conn,
                person_id=person_id,
                medication_type=group_key[1],
                rows=rows,
                after_rowid=latest[0],
            )
        )
        if latest_event is not None and not (reopen_resolved or was_revoked_after_resolution):
            continue
        log_audit_event(
            conn,
            event_type=AUDIT_MEDICATION_CONFLICT_DETECTED,
            subject_type="medication_conflict",
            subject_id=conflict_id,
            status=STATE_AWAITING_USER_DECISION,
            message=json.dumps(payload, sort_keys=True),
        )
        recorded += 1
    return recorded


def pending_medication_conflict_axes(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    medication_name: str,
    medication_type: str,
) -> frozenset[str]:
    """Return the currently conflicting axes for one medication."""
    name_key, canonical_type = medication_conflict_identity(medication_name, medication_type)
    return frozenset(
        group_key[2]
        for group_key in medication_conflict_groups(
            build_medication_conflicts_from_db(conn, person_id)
        )
        if group_key[0] == name_key and group_key[1] == canonical_type
    )


def record_medication_conflict_resolution_events(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    medication_name: str,
    medication_type: str,
    resolved_dose: str | None,
    resolved_frequency: str | None,
    actor: str,
    reason: str,
) -> int:
    """Resolve current pending contradiction events for one user-corrected medication."""
    name_key, canonical_type = medication_conflict_identity(medication_name, medication_type)
    current_rows = build_medication_conflicts_from_db(conn, person_id)
    record_medication_conflict_detection_events(conn, person_id, current_rows)
    matching_groups = {
        key: rows
        for key, rows in medication_conflict_groups(current_rows).items()
        if key[0] == name_key and key[1] == canonical_type
    }
    recorded = 0
    for group_key, rows in matching_groups.items():
        conflict_id, payload = _medication_conflict_event(person_id, group_key, rows)
        latest_event = _latest_medication_conflict_event(conn, conflict_id)
        if latest_event not in (
            AUDIT_MEDICATION_CONFLICT_DETECTED,
            AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED,
        ):
            continue
        payload["resolution"] = {
            "actor": actor,
            "reason": reason,
            "dose": resolved_dose,
            "frequency": resolved_frequency,
        }
        log_audit_event(
            conn,
            event_type=AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED,
            subject_type="medication_conflict",
            subject_id=conflict_id,
            status=STATE_PROCESSED,
            resolution=RESOLUTION_USER_RESOLVED,
            message=json.dumps(payload, sort_keys=True),
        )
        recorded += 1
    if recorded == 0:
        latest = _latest_medication_conflict_payload_for_identity(
            conn,
            person_id=person_id,
            medication_key=name_key,
            medication_type=canonical_type,
        )
        if latest is None:
            return recorded
        conflict_id, payload = latest
        latest_resolution = payload.get("resolution")
        if isinstance(latest_resolution, dict) and (
            latest_resolution.get("dose") == resolved_dose
            and latest_resolution.get("frequency") == resolved_frequency
        ):
            return recorded
        payload["resolution"] = {
            "actor": actor,
            "reason": reason,
            "dose": resolved_dose,
            "frequency": resolved_frequency,
        }
        log_audit_event(
            conn,
            event_type=AUDIT_MEDICATION_CONFLICT_RESOLUTION_RECORDED,
            subject_type="medication_conflict",
            subject_id=conflict_id,
            status=STATE_PROCESSED,
            resolution=RESOLUTION_USER_RESOLVED,
            message=json.dumps(payload, sort_keys=True),
        )
        recorded += 1
    return recorded


def render_medication_conflicts_report(person_id: str, conflict_rows: list[dict[str, Any]]) -> str:
    pending_review_count = len(medication_conflict_groups(conflict_rows))
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "medication_conflicts",
        "generated_at": datetime.now(UTC).date().isoformat(),
        "verification_status": "needs_review" if conflict_rows else "accepted",
        "pending_review_count": pending_review_count,
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    if not conflict_rows:
        conflicts_block = "_No conflicting medication entries found._"
    else:
        table_rows = [
            "| medication_name | medication_type | disagrees_on | review_status | "
            "dose | frequency | source_path |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in conflict_rows:
            table_rows.append(
                "| "
                + " | ".join(
                    [
                        _markdown_table_cell(row.get("medication_name")),
                        _markdown_table_cell(row.get("medication_type")),
                        _markdown_table_cell(row.get("conflict_axis")),
                        "pending",
                        _markdown_table_cell(row.get("dose")),
                        _markdown_table_cell(row.get("frequency")),
                        _markdown_table_cell(row.get("source_path")),
                    ]
                )
                + " |"
            )
        conflicts_block = "\n".join(table_rows)

    review_callout = (
        "> [!warning] Human Resolver review required\n"
        "> Both source facts remain preserved. Choose an authoritative dose or "
        "frequency through `fm-correct-fact`; unattended imports leave this "
        "contradiction pending and visible.\n\n"
        if conflict_rows
        else ""
    )
    return (
        f"---\n{yaml_block}\n---\n\n"
        "# Medication Conflicts\n\n"
        f"{review_callout}"
        "Rows appear here when retained source documents state different dose or "
        "frequency values for the same current medication. Dose and frequency are "
        "compared independently, and a source that records no value for one of them "
        "is not counted as disagreeing about it. Review the source files before "
        "resolving.\n\n"
        "## Conflicts\n\n"
        f"{conflicts_block}\n"
    )


def write_medication_conflicts_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> tuple[Path, int]:
    raw_conflict_rows = unresolved_medication_conflict_rows(conn, person_id)
    # Clean up display names and normalize frequencies
    conflict_rows = _clean_medication_rows_for_display(raw_conflict_rows)
    # Remove only exact same-source/axis display duplicates; preserve provenance.
    conflict_rows = _deduplicate_conflict_rows(conflict_rows)
    person_reports_dir = vault_root / "Records" / person_id / "Reports"
    person_reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = person_reports_dir / "medication-conflicts.md"
    report_path.write_text(
        render_medication_conflicts_report(person_id, conflict_rows), encoding="utf-8"
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return report_path, len(conflict_rows)


def persist_medication_rows(
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
    refresh_reports: bool = True,
) -> Path:
    ensure_person_record_scaffold(vault_root, person_id)
    person_medications_dir = vault_root / "Records" / person_id / "Medications"
    person_medications_dir.mkdir(parents=True, exist_ok=True)

    note_path = medication_note_path(person_medications_dir, document_date, source_file)
    write_medication_note(
        note_path,
        rows,
        source_file_name=source_file_name or display_source_file_name(source_file, document_date),
        source_path=processed_source_path,
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_medications_dir,
        person_id,
        "Medications",
        display_name=display_name,
    )

    note_source_path = str(note_path.relative_to(vault_root))
    persisted_rows = [{**row, "source_type": source_type} for row in rows]
    replace_medications_for_source_path(conn, processed_source_path, persisted_rows)
    if retained_source:
        for report_name in ("current-medications.md", "medication-conflicts.md"):
            mark_dependent_output_stale(
                conn,
                output_path=f"Records/{person_id}/Reports/{report_name}",
                reason="retained medication source reprocessed",
                source_path=processed_source_path,
                person_id=person_id,
            )
    message_prefix = "reprocessed note" if retained_source else "medication note"
    audit_event = (
        AUDIT_MEDICATION_SOURCE_REPROCESSED if retained_source else AUDIT_MEDICATION_IMPORT_ACCEPTED
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
        subject_type="medication",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_PROCESSED,
        message=f"{message_prefix}: {note_source_path}",
    )
    if refresh_reports:
        write_current_medications_report(vault_root, person_id, conn)
        write_medication_conflicts_report(conn, vault_root, person_id)
        refresh_medication_dependent_reports(conn, vault_root, person_id)
        refresh_fhir_dependent_export(conn, vault_root, person_id)
    return note_path


def _fill_rxnorm_in_table_line(line: str, rxnorm_index: dict[str, str]) -> tuple[str, bool]:
    """Return (updated_line, changed) with rxnorm_cui cell filled if it was blank.

    Expects a pipe-separated Markdown table row from a medication note.
    Table layout: | medication_name | medication_type | rxnorm_cui | ...
    Existing non-blank CUIs are never overwritten.
    """
    parts = line.split("|")
    # layout: ['', ' medication_name ', ' medication_type ', ' rxnorm_cui ', ..., '']
    if len(parts) < 11:
        return line, False
    medication_name = parts[1].strip()
    if not medication_name:
        return line, False
    if parts[3].strip():
        return line, False
    cui = lookup_rxnorm_cui(medication_name, rxnorm_index)
    if not cui:
        return line, False
    parts[3] = f" {cui} "
    return "|".join(parts), True


def repair_medication_rxnorm_cuis(
    vault_root: Path,
    rxnorm_index: dict[str, str],
    conn: sqlite3.Connection | None = None,
) -> tuple[int, int]:
    """Fill blank rxnorm_cui cells in medication vault notes and optionally the DB.

    Reads every non-index ``*.md`` note under ``Records/*/Medications/``, finds rows
    in the Medications table where the ``rxnorm_cui`` cell is empty, looks up the CUI
    from *rxnorm_index* by ``medication_name``, and writes the note back only when at
    least one cell changed.  Existing non-blank CUIs are never overwritten.

    When *conn* is provided, also updates null ``rxnorm_cui`` values in the
    ``medications`` DB table.

    Returns ``(notes_updated, cells_filled)``.
    """
    if not rxnorm_index:
        return 0, 0

    records_root = vault_root / "Records"
    if not records_root.is_dir():
        return 0, 0

    notes_updated = 0
    cells_filled = 0

    for note_path in sorted(records_root.glob("*/Medications/*.md")):
        if note_path.name.startswith("_"):
            continue

        try:
            content = note_path.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            front_matter = parse_front_matter(content)
        except ValueError:
            continue

        if front_matter.get("record_type") != "medication":
            continue

        lines = content.splitlines(keepends=True)
        in_table = False
        past_alignment = False
        updated_lines: list[str] = []
        note_changed = False

        for line in lines:
            line_content = line.rstrip("\r\n")
            line_ending = line[len(line_content) :]
            stripped = line_content.strip()

            if (
                not in_table
                and stripped.startswith("| medication_name ")
                and stripped.endswith("|")
            ):
                in_table = True
                past_alignment = False
                updated_lines.append(line)
                continue

            if in_table and not past_alignment:
                if stripped.startswith("| ---"):
                    past_alignment = True
                updated_lines.append(line)
                continue

            if in_table and past_alignment:
                if not stripped.startswith("|"):
                    in_table = False
                    updated_lines.append(line)
                    continue
                new_content, changed = _fill_rxnorm_in_table_line(line_content, rxnorm_index)
                if changed:
                    updated_lines.append(new_content + line_ending)
                    cells_filled += 1
                    note_changed = True
                else:
                    updated_lines.append(line)
                continue

            updated_lines.append(line)

        if note_changed:
            note_path.write_text("".join(updated_lines), encoding="utf-8")
            notes_updated += 1

    if conn is not None:
        cursor = conn.execute(
            "SELECT rowid, medication_name FROM medications WHERE rxnorm_cui IS NULL"
        )
        db_rows = cursor.fetchall()
        for rowid, medication_name in db_rows:
            if not medication_name:
                continue
            cui = lookup_rxnorm_cui(str(medication_name), rxnorm_index)
            if cui:
                conn.execute(
                    "UPDATE medications SET rxnorm_cui = ? WHERE rowid = ?",
                    (cui, rowid),
                )

    return notes_updated, cells_filled


def reprocess_retained_supplement_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    refresh_reports: bool = True,
    source_path_prefix: Path | None = None,
) -> ReprocessResult:
    """Re-parse an already-retained supplement file and update DB rows and vault reports.

    Mirrors ``reprocess_retained_lab_source`` for the supplement_list source type.
    Marks dependent reports stale before regenerating them (audit trail).
    Raises ``ReprocessError`` when the path is outside processed Medications or
    the file yields no parseable rows (REQ-001: no silent loss).
    """
    person_id = infer_person_id_from_processed_medication_path(source_path, processed_root)
    if not person_id:
        raise ReprocessError(
            "retained supplement source must be under data/processed/<person_id>/Medications"
        )

    extraction = build_supplement_extraction(person_id, source_path)
    if extraction is None:
        raise ReprocessError(
            f"no parseable supplement lines found in {source_path.name!r}; "
            "cannot reprocess without confirmed fact rows (REQ-001: no silent loss)"
        )

    rows = canonicalize_candidate_medication_facts(extraction, source_path.name)
    fill_rxnorm_cuis(rows, load_rxnorm_index(configured_storage_roots(root).reference))
    processed_source_path = retained_source_reference(
        root=root,
        processed_root=processed_root,
        source_path=source_path,
        source_path_prefix=source_path_prefix,
    )
    checksum = source_file_checksum(source_path)
    document_date = medication_document_date(rows, extraction.document_date, source_path.name)

    persist_medication_rows(
        conn=conn,
        vault_root=vault_root,
        person_id=person_id,
        source_file=source_path,
        processed_source_path=processed_source_path,
        checksum=checksum,
        source_type="supplement_list",
        rows=rows,
        document_date=document_date,
        retained_source=True,
        refresh_reports=refresh_reports,
    )
    return ReprocessResult(imported_rows=len(rows), affected_person_ids={person_id})
