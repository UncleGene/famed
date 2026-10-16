#!/usr/bin/env python3
"""Free-form history candidate note persistence helpers."""

from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from famed.clinical_interpreter import (
    CandidateHistoryFact,
    FactInterpreter,
    InterpreterContractError,
    InterpreterFactExtraction,
    validate_fact_extraction,
)
from famed.dependent_reports import (
    has_fhir_export_surface,
    refresh_fhir_dependent_export,
    refresh_history_dependent_reports,
    refresh_lab_dependent_reports,
    refresh_medication_dependent_reports,
)
from famed.import_labs import (
    display_source_file_name,
    retain_processed_source,
    slugify_filename,
    source_stem_without_redundant_date_prefix,
)
from famed.lab_sync import (
    LabSyncError,
    is_non_result_lab_row,
    lab_payload_from_mapping,
    log_ingestion_event,
    normalize_unit,
    reviewed_unit_equivalence,
    source_file_checksum,
    write_category_index_note,
)
from famed.person_sync import (
    PersonSyncError,
    ensure_person_record_scaffold,
    load_person_display_name,
    load_person_from_note,
    parse_front_matter,
)
from famed.reprocess_sources import ReprocessResult, retained_source_reference
from famed.source_paths import (
    archive_member_source_path,
    display_source_file_name_from_path,
    split_archive_source_path,
)
from famed.storage_contract import (
    AUDIT_HISTORY_CANDIDATES_ACCEPTED,
    AUDIT_HISTORY_IMPORT_NEEDS_REVIEW,
    AUDIT_HISTORY_SOURCE_REPROCESSED,
    AUDIT_HISTORY_SOURCE_REVIEWED_NO_FACTS,
    AUDIT_LAB_OBSERVATION_REJECTED,
    AUDIT_LAB_OBSERVATION_RESOLUTION_RECORDED,
    PROCESSED_ROOT,
    RESOLUTION_QUARANTINED,
    RESOLUTION_USER_RESOLVED,
    STATE_AWAITING_USER_DECISION,
    STATE_PROCESSED,
    STATE_QUARANTINED,
    log_audit_event,
    mark_dependent_output_stale,
)


class HistorySyncError(ValueError):
    """Raised when history candidates cannot be safely written for review."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryReviewSection:
    title: str
    text: str


@dataclass(frozen=True)
class HistoryReviewDocument:
    title: str
    document_date: str | None
    patient_given_names: tuple[str, ...]
    patient_family_name: str | None
    patient_birth_year: int | None
    sections: tuple[HistoryReviewSection, ...]


LabObservationResolutionAction = Literal["accept_verbatim", "reject", "quarantine"]


@dataclass(frozen=True)
class LabObservationSelector:
    """Exact source fields identifying one rejected C-CDA lab observation."""

    code_system: str
    loinc_code: str
    test_name: str
    value_text: str
    value_type: str
    unit: str | None
    observed_at: str
    reference_range_low: str | None
    reference_range_high: str | None


@dataclass(frozen=True)
class LabObservationResolution:
    """Attended decision for an exact rejected C-CDA lab observation."""

    action: LabObservationResolutionAction
    selector: LabObservationSelector
    expected_occurrences: int
    resolved_by: str
    resolved_at: str
    reason: str


class LabObservationResolutionProvider(Protocol):
    """Provide source-bound attended resolutions for one archive member."""

    def lab_observation_resolutions_for_source(
        self,
        source_path: str,
    ) -> Sequence[LabObservationResolution]: ...

    def validate_lab_observation_resolution_archive(
        self,
        archive_source_path: str,
        source_sha256: str,
        member_source_paths: Sequence[str],
    ) -> None: ...


@dataclass(frozen=True)
class DiagnosisSupportReview:
    status: str
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistorySupportContext:
    facts: list[dict[str, Any]]
    history_text: str
    labs: list[dict[str, str]]
    medications: list[dict[str, str]]
    vitals: list[dict[str, str]]


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
        raise HistorySyncError(
            f"{field_name} must be an ISO date or datetime in {source_label}, got: {value!r}"
        ) from exc
    return normalized


def canonicalize_candidate_history_facts(
    extraction: InterpreterFactExtraction,
    source_label: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact in extraction.facts:
        if not isinstance(fact, CandidateHistoryFact):
            continue
        description = _normalize_optional_text(fact.description)
        if not description:
            raise HistorySyncError(f"history description is required in {source_label}")
        rows.append(
            {
                "person_id": extraction.person_id,
                "description": description,
                "category": _normalize_optional_text(fact.category) or "diagnosis",
                "icd_10_code": _normalize_optional_text(fact.icd_10_code),
                "recorded_date": _validate_optional_iso_date(
                    fact.recorded_date, "recorded_date", source_label
                ),
                "value": _normalize_optional_text(fact.value),
                "unit": _normalize_optional_text(fact.unit),
                "cvx_code": _normalize_optional_text(fact.cvx_code),
                "lot_number": _normalize_optional_text(fact.lot_number),
                "confidence": fact.confidence,
                "evidence_snippet": _normalize_optional_text(fact.evidence_snippet),
            }
        )

    if not rows:
        raise HistorySyncError(f"no history facts found in {source_label}")
    return rows


def parse_ccda_review_document(source_file: Path) -> HistoryReviewDocument:
    root = _parse_ccda_root(source_file)
    return _build_ccda_review_document(root, source_file)


def extract_ccda_lab_rows(
    source_file: Path,
    person_id: str,
    *,
    rejected: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Extract Results-section observations from a C-CDA document.

    ``rejected``, if given, is populated with one entry per observation that
    failed deterministic validation, carrying its test name, unit and the
    rejection reason. Rejections are a review signal, not a silent discard:
    callers with a database connection record them so an observation the source
    states can never disappear without a trace (REQ-005).
    """
    root = _parse_ccda_root(source_file)
    document_date = _ccda_document_date(root)
    rows: list[dict[str, Any]] = []
    for index, observation in enumerate(_iter_ccda_result_observations(root), start=1):
        raw = _ccda_lab_mapping(observation, person_id=person_id, document_date=document_date)
        if raw is None:
            continue
        if is_non_result_lab_row(raw.get("test_name"), raw.get("loinc_code")):
            continue
        try:
            rows.append(lab_payload_from_mapping(raw, f"{source_file.name}:ccda_lab[{index}]"))
        except LabSyncError as exc:
            if rejected is not None:
                rejected.append(
                    {
                        "code_system": raw.get("code_system"),
                        "loinc_code": raw.get("loinc_code"),
                        "test_name": raw.get("test_name"),
                        "value_text": raw.get("value_text"),
                        "value_type": raw.get("value_type"),
                        "unit": raw.get("unit"),
                        "observed_at": raw.get("observed_at"),
                        "reference_range_low": raw.get("reference_range_low"),
                        "reference_range_high": raw.get("reference_range_high"),
                        "reason": str(exc),
                        "raw_mapping": raw,
                    }
                )
    return rows


def extract_ccda_medication_rows(source_file: Path, person_id: str) -> list[dict[str, Any]]:
    root = _parse_ccda_root(source_file)
    local_offsets = _ccda_document_local_offsets(root)
    rows: list[dict[str, Any]] = []
    for section in _iter_ccda_section_elements(root):
        if not _is_ccda_medications_section(section):
            continue
        for administration in section.findall(".//hl7:entry/hl7:substanceAdministration", _HL7_NS):
            row = _ccda_medication_mapping(
                administration, section, person_id=person_id, local_offsets=local_offsets
            )
            if row is not None:
                rows.append(row)
    return rows


def extract_ccda_vital_rows(source_file: Path, person_id: str) -> list[dict[str, Any]]:
    root = _parse_ccda_root(source_file)
    document_date = _ccda_document_date(root)
    rows: list[dict[str, Any]] = []
    for observation in _iter_ccda_vital_observations(root):
        row = _ccda_vital_mapping(observation, person_id=person_id, document_date=document_date)
        if row is not None:
            rows.append(row)
    return rows


def extract_ccda_immunization_rows(source_file: Path, person_id: str) -> list[dict[str, Any]]:
    root = _parse_ccda_root(source_file)
    document_date = _ccda_document_date(root)
    rows: list[dict[str, Any]] = []
    for section in _iter_ccda_section_elements(root):
        if not _is_ccda_immunizations_section(section):
            continue
        for _index, administration in enumerate(
            section.findall(".//hl7:entry/hl7:substanceAdministration", _HL7_NS),
            start=1,
        ):
            row = _ccda_immunization_mapping(
                administration,
                section,
                person_id=person_id,
                document_date=document_date,
            )
            if row is not None:
                rows.append(row)
    return rows


def _parse_ccda_root(source_file: Path) -> ET.Element:
    try:
        return ET.parse(source_file).getroot()
    except ET.ParseError as exc:
        raise HistorySyncError(f"C-CDA XML could not be parsed: {source_file}") from exc


def _build_ccda_review_document(root: ET.Element, source_file: Path) -> HistoryReviewDocument:
    if _local_name(root.tag) != "ClinicalDocument":
        raise HistorySyncError(f"expected ClinicalDocument root in {source_file}")

    title = _normalize_optional_text(_first_text(root, ".//hl7:title")) or "Visit Summary"
    document_date = _ccda_document_date(root)
    patient = root.find(".//hl7:recordTarget/hl7:patientRole/hl7:patient", _HL7_NS)
    given_names: tuple[str, ...] = ()
    family_name: str | None = None
    birth_year: int | None = None
    if patient is not None:
        given_names = tuple(
            text
            for text in (
                _normalize_optional_text(element.text)
                for element in patient.findall(".//hl7:name/hl7:given", _HL7_NS)
            )
            if text is not None
        )
        family_name = _normalize_optional_text(_first_text(patient, ".//hl7:name/hl7:family"))
        birth_year = _ccda_birth_year(_first_attr(patient, "./hl7:birthTime", "value"))

    sections = tuple(_iter_ccda_sections(root))
    if not sections:
        raise HistorySyncError(f"no C-CDA sections found in {source_file}")

    return HistoryReviewDocument(
        title=title,
        document_date=document_date,
        patient_given_names=given_names,
        patient_family_name=family_name,
        patient_birth_year=birth_year,
        sections=sections,
    )


def parse_text_history_review_document(source_file: Path, text: str) -> HistoryReviewDocument:
    normalized = _normalize_optional_text(text)
    if not normalized:
        raise HistorySyncError(f"no extractable history text found in {source_file}")
    given_names, family_name = _parse_text_patient_name(normalized)
    birth_year = _parse_text_birth_year(normalized)
    document_date = _parse_text_document_date(normalized) or _date_from_source_stem(
        source_file.stem
    )
    return HistoryReviewDocument(
        title=source_file.stem,
        document_date=document_date,
        patient_given_names=given_names,
        patient_family_name=family_name,
        patient_birth_year=birth_year,
        sections=(HistoryReviewSection(title="Extracted Text", text=normalized),),
    )


def match_history_document_to_person(
    vault_root: Path, document: HistoryReviewDocument
) -> str | None:
    matches: list[str] = []
    for note_path in (vault_root / "Persons").glob("*.md"):
        if note_path.name.startswith("_"):
            continue
        try:
            person = load_person_from_note(note_path)
        except (OSError, PersonSyncError):
            continue
        if _history_document_person_matches(person, document):
            matches.append(str(person["person_id"]))
    return matches[0] if len(matches) == 1 else None


def history_document_date(
    rows: list[dict[str, Any]],
    document_date: str | None,
    source_label: str,
) -> str | None:
    normalized_document_date = _validate_optional_iso_date(
        document_date, "document_date", source_label
    )
    if normalized_document_date:
        return normalized_document_date.split("T", 1)[0]
    dates = [str(row["recorded_date"]).split("T", 1)[0] for row in rows if row.get("recorded_date")]
    return min(dates) if dates else None


def processed_history_source_path(
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
        / "History"
        / f"{date_prefix}-{source_stem}{source_file.suffix.lower()}"
    )


def retain_history_source_for_import(
    root: Path,
    processed_root: Path,
    person_id: str,
    document_date: str | None,
    source_file: Path,
) -> tuple[str, str]:
    checksum = source_file_checksum(source_file)
    processed_source = processed_history_source_path(
        processed_root, person_id, document_date, source_file
    )
    processed_source = retain_processed_source(source_file, processed_source, checksum)
    return checksum, str(processed_source.relative_to(root))


def render_history_review_note(
    rows: list[dict[str, Any]],
    *,
    source_file_name: str,
    source_path: str,
    document_date: str | None,
) -> str:
    if not rows:
        raise HistorySyncError("at least one history item is required to render a note")
    person_ids = {row["person_id"] for row in rows}
    if len(person_ids) != 1:
        raise HistorySyncError("all history items in one note must belong to one person_id")

    front_matter: dict[str, Any] = {
        "person_id": rows[0]["person_id"],
        "record_type": "history",
        "source_file_name": source_file_name,
        "source_path": source_path,
        "event_date": document_date,
        "title": rows[0]["description"],
        "provider": None,
        "icd10_codes": [row["icd_10_code"] for row in rows if row.get("icd_10_code")],
        "summary": rows[0]["description"],
        "verification_status": "needs_review",
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    table_rows = [
        "| description | category | icd10 | recorded_date | confidence | evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        table_rows.append(
            "| "
            + " | ".join(
                [
                    _markdown_table_cell(row.get("description")),
                    _markdown_table_cell(row.get("category")),
                    _markdown_table_cell(row.get("icd_10_code")),
                    _markdown_table_cell(row.get("recorded_date")),
                    _markdown_table_cell(row.get("confidence")),
                    _markdown_table_cell(row.get("evidence_snippet")),
                ]
            )
            + " |"
        )

    return (
        f"---\n{yaml_block}\n---\n\n"
        "# History Review\n\n"
        "[[_index|← History]]\n\n"
        "## Candidate Facts\n\n"
        f"{chr(10).join(table_rows)}\n\n"
        "## Review\n\n"
        "- Verification status: needs_review\n"
        f"- Source file: {source_file_name}\n"
        "- Confirm, edit, or reject candidate facts before treating them as authoritative.\n"
    )


def render_history_document_review_note(
    document: HistoryReviewDocument,
    *,
    person_id: str,
    source_file_name: str,
    source_path: str,
    document_kind: str,
) -> str:
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "history",
        "source_file_name": source_file_name,
        "source_path": source_path,
        "event_date": document.document_date,
        "title": document.title,
        "provider": None,
        "summary": document.title,
        "verification_status": "needs_review",
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    patient_name = " ".join(
        [*document.patient_given_names, document.patient_family_name or ""]
    ).strip()
    patient_details = [
        f"- Patient name in source: {patient_name or 'unknown'}",
        f"- Birth year in source: {document.patient_birth_year or 'unknown'}",
    ]
    narrative_sections = ["## Narrative Sections"]
    for section in document.sections:
        narrative_sections.extend(
            [
                "",
                f"### {section.title}",
                "",
                section.text,
            ]
        )
    return (
        f"---\n{yaml_block}\n---\n\n"
        "# History Review\n\n"
        "[[_index|← History]]\n\n"
        "## Source Patient\n\n"
        f"{chr(10).join(patient_details)}\n\n"
        f"{chr(10).join(narrative_sections)}\n\n"
        "## Review\n\n"
        "- Verification status: needs_review\n"
        f"- Source file: {source_file_name}\n"
        "- Run `fm-history-extract` to extract structured facts from this document.\n"
    )


def _markdown_table_cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _split_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]


def _iter_markdown_table_dicts(text: str, required_columns: set[str]) -> Iterator[dict[str, str]]:
    headers: list[str] | None = None
    for line in text.splitlines():
        cells = _split_markdown_table_row(line)
        if not cells:
            headers = None
            continue
        if all(set(cell) <= {"-", ":", " "} and "-" in cell for cell in cells):
            continue
        normalized = [cell.strip().casefold().replace(" ", "_") for cell in cells]
        if required_columns.issubset(set(normalized)):
            headers = normalized
            continue
        if headers is None:
            continue
        row = dict(zip(headers, cells, strict=False))
        if any(row.values()):
            yield row


_CATEGORY_DISPLAY = {
    "diagnosis": "Diagnoses",
    "vital_sign": "Vital Signs",
    "medication": "Medications",
    "allergy": "Allergies",
    "immunization": "Immunizations",
    "procedure": "Procedures",
    "lab": "Lab Results",
    "imaging_finding": "Imaging Findings",
    "screening": "Health Screenings",
    "social_history": "Social History",
    "follow_up": "Follow-up",
    "other": "Other",
}
_CATEGORY_ORDER = [
    "diagnosis",
    "imaging_finding",
    "allergy",
    "immunization",
    "vital_sign",
    "medication",
    "procedure",
    "lab",
    "screening",
    "social_history",
    "follow_up",
    "other",
]


def _render_facts_grouped(
    facts: list[dict[str, Any]],
    excluded_low_confidence: int = 0,
    note_date: str | None = None,
) -> str:
    """Render accepted facts grouped by category with adaptive column layout.

    - Date column omitted per group when all facts share the same recorded_date
      (date shown as an italic header line instead, omitted when it equals note_date).
    - Medium-confidence facts prefixed with ⚠ inline on the description.
    - Single-description groups (no ICD, uniform date) rendered as bullet lists.
    - Vital signs always rendered as a table; Date column dropped when uniform.
    - Trailing callout added when excluded_low_confidence > 0.
    """
    from collections import defaultdict as _defaultdict

    groups: dict[str, list[dict[str, Any]]] = _defaultdict(list)
    for f in facts:
        groups[f.get("category") or "other"].append(f)

    ordered = _CATEGORY_ORDER + [c for c in groups if c not in _CATEGORY_ORDER]
    lines: list[str] = []

    def _desc(f: dict[str, Any]) -> str:
        d = _markdown_table_cell(f.get("description"))
        return f"⚠ {d}" if f.get("confidence") == "medium" else d

    def _desc_valued(f: dict[str, Any]) -> str:
        # Non-vital facts have no dedicated Value/Unit columns, so append the
        # measurement to the description when present (e.g. an Agatston score) —
        # otherwise the rendered note shows only a label and no finding.
        base = _desc(f)
        value = f.get("value")
        if value in (None, ""):
            return base
        unit = f.get("unit")
        unit_text = f" {unit}" if unit not in (None, "") else ""
        return f"{base}: {value}{unit_text}"

    for cat in ordered:
        if cat not in groups:
            continue
        gfacts = groups[cat]
        label = _CATEGORY_DISPLAY.get(cat, cat.replace("_", " ").title())
        lines.append(f"**{label}**\n")

        dates = {f.get("recorded_date") for f in gfacts}
        all_same_date = len(dates) == 1
        uniform_date = next(iter(dates)) if all_same_date else None
        if all_same_date and uniform_date and uniform_date != note_date:
            lines.append(f"*{uniform_date}*\n")

        if cat == "vital_sign":
            if all_same_date:
                lines.append("| Measurement | Value | Unit |")
                lines.append("| --- | --- | --- |")
                for f in gfacts:
                    cells = [
                        _desc(f),
                        _markdown_table_cell(f.get("value")),
                        _markdown_table_cell(f.get("unit")),
                    ]
                    lines.append("| " + " | ".join(cells) + " |")
            else:
                lines.append("| Measurement | Value | Unit | Date |")
                lines.append("| --- | --- | --- | --- |")
                for f in gfacts:
                    cells = [
                        _desc(f),
                        _markdown_table_cell(f.get("value")),
                        _markdown_table_cell(f.get("unit")),
                        _markdown_table_cell(f.get("recorded_date")),
                    ]
                    lines.append("| " + " | ".join(cells) + " |")
        else:
            has_icd = any(f.get("icd_10_code") for f in gfacts)
            if has_icd:
                if all_same_date:
                    lines.append("| Description | ICD-10 |")
                    lines.append("| --- | --- |")
                    for f in gfacts:
                        cells = [
                            _desc_valued(f),
                            _markdown_table_cell(f.get("icd_10_code")),
                        ]
                        lines.append("| " + " | ".join(cells) + " |")
                else:
                    lines.append("| Description | ICD-10 | Date |")
                    lines.append("| --- | --- | --- |")
                    for f in gfacts:
                        cells = [
                            _desc_valued(f),
                            _markdown_table_cell(f.get("icd_10_code")),
                            _markdown_table_cell(f.get("recorded_date")),
                        ]
                        lines.append("| " + " | ".join(cells) + " |")
            else:
                if all_same_date:
                    for f in gfacts:
                        lines.append(f"- {_desc_valued(f)}")
                else:
                    lines.append("| Description | Date |")
                    lines.append("| --- | --- |")
                    for f in gfacts:
                        cells = [
                            _desc_valued(f),
                            _markdown_table_cell(f.get("recorded_date")),
                        ]
                        lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    if excluded_low_confidence > 0:
        lines.append(
            f"> _{excluded_low_confidence} low-confidence finding(s) not included. "
            "Re-running with a more capable AI model may extract additional facts._\n"
        )

    return "\n".join(lines)


def history_note_path(
    person_history_dir: Path,
    document_date: str | None,
    source_file: Path,
) -> Path:
    date_prefix = document_date or "undated"
    slug = slugify_filename(
        source_stem_without_redundant_date_prefix(source_file.stem, document_date)
    )
    return person_history_dir / f"{date_prefix}-{slug}-history-review.md"


def _history_note_path_avoiding_collision(
    note_path: Path,
    *,
    processed_source_path: str,
    qualifier: str | None,
    legacy_source_paths: set[str] | None = None,
) -> Path:
    if not note_path.exists():
        return note_path
    try:
        _, _, fm = _read_history_review_front_matter(note_path)
    except (OSError, HistorySyncError):
        fm = {}
    existing_source_path = str(fm.get("source_path") or "")
    if existing_source_path == processed_source_path:
        return note_path
    if legacy_source_paths and existing_source_path in legacy_source_paths:
        return note_path

    suffix = "-history-review.md"
    stem = note_path.name.removesuffix(suffix)
    candidates = [qualifier] if qualifier else []
    candidates.extend(str(index) for index in range(2, 100))
    for candidate in candidates:
        if not candidate:
            continue
        deduplicated = note_path.with_name(f"{stem}-{slugify_filename(candidate)}{suffix}")
        if not deduplicated.exists():
            return deduplicated
    raise HistorySyncError(f"could not choose unique history note path for {note_path}")


def _source_stem_without_date_prefix(source_stem: str) -> str:
    return source_stem_without_redundant_date_prefix(source_stem, None)


def persist_history_review_note(
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
) -> Path:
    ensure_person_record_scaffold(vault_root, person_id)
    person_history_dir = vault_root / "Records" / person_id / "History"
    person_history_dir.mkdir(parents=True, exist_ok=True)

    note_path = _history_note_path_avoiding_collision(
        history_note_path(person_history_dir, document_date, source_file),
        processed_source_path=processed_source_path,
        qualifier=source_file.suffix.lstrip("."),
    )
    note_path.write_text(
        render_history_review_note(
            rows,
            source_file_name=display_source_file_name(source_file, document_date),
            source_path=processed_source_path,
            document_date=document_date,
        ),
        encoding="utf-8",
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_history_dir,
        person_id,
        "History",
        display_name=display_name,
    )

    note_source_path = str(note_path.relative_to(vault_root))
    log_ingestion_event(
        conn,
        source_type=source_type,
        source_path=processed_source_path,
        source_checksum=checksum,
        status=STATE_PROCESSED,
        message=f"history review note: {note_source_path}",
    )
    log_audit_event(
        conn,
        event_type=AUDIT_HISTORY_IMPORT_NEEDS_REVIEW,
        subject_type="history",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_PROCESSED,
        message=f"history review note: {note_source_path}",
    )
    write_medical_history_summary_report(vault_root, person_id)
    _refresh_history_pending_review(conn, vault_root, person_id)
    return note_path


def persist_history_document_review_note(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    source_file: Path,
    processed_source_path: str,
    checksum: str,
    source_type: str,
    document: HistoryReviewDocument,
    document_kind: str,
    retained_source: bool = False,
    source_file_name: str | None = None,
    legacy_source_paths: set[str] | None = None,
) -> Path:
    ensure_person_record_scaffold(vault_root, person_id)
    person_history_dir = vault_root / "Records" / person_id / "History"
    person_history_dir.mkdir(parents=True, exist_ok=True)

    note_path = _history_note_path_avoiding_collision(
        history_note_path(person_history_dir, document.document_date, source_file),
        processed_source_path=processed_source_path,
        qualifier=source_file.suffix.lstrip("."),
        legacy_source_paths=legacy_source_paths or set(),
    )
    note_path.write_text(
        render_history_document_review_note(
            document,
            person_id=person_id,
            source_file_name=source_file_name
            or display_source_file_name(source_file, document.document_date),
            source_path=processed_source_path,
            document_kind=document_kind,
        ),
        encoding="utf-8",
    )
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_history_dir,
        person_id,
        "History",
        display_name=display_name,
    )

    note_source_path = str(note_path.relative_to(vault_root))
    message_prefix = (
        "reprocessed note" if retained_source else f"{document_kind} history review note"
    )
    audit_event = (
        AUDIT_HISTORY_SOURCE_REPROCESSED if retained_source else AUDIT_HISTORY_IMPORT_NEEDS_REVIEW
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
        subject_type="history",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_PROCESSED,
        message=f"{message_prefix}: {note_source_path}",
    )
    write_medical_history_summary_report(vault_root, person_id)
    _refresh_history_pending_review(conn, vault_root, person_id)
    return note_path


def remove_linked_category_placeholder_note(
    *,
    vault_root: Path,
    person_id: str,
    category_dir_name: str,
    source_path: str,
) -> None:
    category_dir = vault_root / "Records" / person_id / category_dir_name
    if not category_dir.is_dir():
        return
    for candidate in category_dir.glob("*-placeholder.md"):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if "record_type: category_placeholder" in text and source_path in text:
            candidate.unlink(missing_ok=True)


def _read_history_note_summary(note_path: Path) -> dict[str, Any] | None:
    """Return relevant front-matter fields and facts from a history note.

    Supports two note formats:

    1. History-review notes (``record_type: history``) — facts in
       ``accepted_facts`` front-matter field.
    2. User-update history notes (``record_type: user_update``,
       ``category: history``) — facts as markdown table rows in the
       ``## History`` section.

    Returns None when the note cannot be read or is not a recognized
    history note type.
    """
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return None

    record_type = fm.get("record_type")
    if record_type not in ("history", "user_update"):
        return None

    # History-review notes: facts in front-matter accepted_facts
    if record_type == "history":
        raw_facts = fm.get("accepted_facts")
        accepted_facts: list[dict[str, Any]] = raw_facts if isinstance(raw_facts, list) else []
    else:
        # User-update notes: facts as markdown table rows
        if fm.get("category") != "history":
            return None
        accepted_facts = _read_user_update_history_facts(text)

    excl = fm.get("excluded_low_confidence_count") or 0
    event_date = (
        str(fm["event_date"])
        if fm.get("event_date")
        else (str(fm["date"]) if fm.get("date") else None)
    )
    if record_type == "user_update":
        title = f"History Update — {event_date}" if event_date else "History Update"
    else:
        title = str(fm.get("title") or "")

    return {
        "record_type": str(record_type),
        "event_date": event_date,
        "title": title,
        "source_file_name": str(fm.get("source_file_name") or fm.get("source_document") or ""),
        "source_path": str(fm.get("source_path") or fm.get("source_document") or ""),
        "note_stem": note_path.stem,
        "verification_status": str(fm.get("verification_status") or "accepted"),
        "accepted_facts": accepted_facts,
        "excluded_low_confidence_count": int(excl) if excl else 0,
    }


def _read_user_update_history_facts(text: str) -> list[dict[str, Any]]:
    """Parse history facts from the ``## History`` markdown table in a user-update note.

    Returns a list of fact dicts with keys compatible with the history-review
    fact schema: description, category, recorded_date, details.
    """
    facts: list[dict[str, Any]] = []
    in_history = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## History"):
            in_history = True
            continue
        if in_history and stripped.startswith("## "):
            break
        if (
            in_history
            and stripped.startswith("| ")
            and "---" not in stripped
            and not stripped.startswith("| Description ")
        ):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not cells or not cells[0]:
                continue
            description = _clean_history_fact_description(cells[0])
            if not description:
                continue
            fact: dict[str, Any] = {
                "description": description,
                "category": _map_user_update_category(cells[1] if len(cells) > 1 else "other"),
                "recorded_date": cells[2] if len(cells) > 2 and cells[2] else None,
                "confidence": "high",
            }
            if len(cells) > 3 and cells[3]:
                fact["value"] = cells[3]
            facts.append(fact)
    return facts


def _extract_markdown_heading_section(markdown: str, heading: str) -> str | None:
    pattern = rf"\n### {re.escape(heading)}\n(.*?)(?=\n### |\n## |\Z)"
    match = re.search(pattern, markdown, flags=re.DOTALL)
    if not match:
        return None
    section = match.group(1).strip()
    return section or None


def _iter_markdown_table_rows(section: str) -> Iterator[list[str]]:
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells:
            continue
        if all(re.fullmatch(r"-+", cell) for cell in cells if cell):
            continue
        if cells[0].casefold() == "diagnosis":
            continue
        yield cells


def _iso_date_from_slash_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _strip_history_section_footnotes(section: str) -> str:
    cleaned = re.sub(
        r"\bdocumented(?: (?:in|as of))? (?:this encounter|today)\b",
        "",
        section,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\bas of\s+\d{1,2}/\d{1,2}/\d{4}\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split()).strip()


def _extract_dated_history_problem_list(
    section: str,
    *,
    category: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    cleaned = _strip_history_section_footnotes(section)
    cleaned = re.sub(
        r"^(Problem Noted Date Diagnosed Date|Diagnosis Start Date)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    matches = re.findall(
        (
            r"([A-Za-z0-9][A-Za-z0-9(),/'&+\- ]+?)\s+"
            r"(\d{1,2}/\d{1,2}/\d{4})"
            r"(?:\s+\d{1,2}/\d{1,2}/\d{4})?"
            r"(?=\s+[A-Z0-9]|\Z)"
        ),
        cleaned,
    )
    for label, raw_date in matches:
        description = " ".join(label.split()).strip(" -")
        if not description:
            continue
        facts.append(
            {
                "description": description,
                "category": category,
                "recorded_date": _iso_date_from_slash_date(raw_date),
                "confidence": "medium",
            }
        )
    return facts


def _visit_diagnosis_requires_semantic_interpretation(text: str) -> bool:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9()/'+-]*", text)
    return len(words) > 7


def _clean_visit_diagnosis_label(value: str) -> str:
    normalized = " ".join(value.split()).strip(" -")
    normalized = re.sub(r"\s+-\s+Primary$", "", normalized, flags=re.IGNORECASE)
    return normalized.strip(" -")


def _extract_undated_visit_diagnoses(
    section: str,
    *,
    event_date: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    table_rows = list(_iter_markdown_table_rows(section))
    if table_rows:
        facts: list[dict[str, Any]] = []
        for cells in table_rows:
            description = _clean_visit_diagnosis_label(cells[0] if cells else "")
            if not description:
                continue
            lowered = description.casefold()
            category = "screening" if "screening" in lowered else "diagnosis"
            facts.append(
                {
                    "description": description,
                    "category": category,
                    "recorded_date": event_date,
                    "confidence": "medium",
                }
            )
        return (facts, False)

    cleaned = _strip_history_section_footnotes(section)
    cleaned = re.sub(r"^Diagnosis\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return ([], False)
    parts: list[str] = []
    for raw_part in re.split(r"\s+- Primary\s+", cleaned):
        normalized_part = _clean_visit_diagnosis_label(raw_part)
        if normalized_part:
            parts.append(normalized_part)
    facts: list[dict[str, Any]] = []
    requires_semantic_interpretation = False
    for description in parts:
        if _visit_diagnosis_requires_semantic_interpretation(description):
            requires_semantic_interpretation = True
            continue
        lowered = description.casefold()
        category = "screening" if "screening" in lowered else "diagnosis"
        facts.append(
            {
                "description": description,
                "category": category,
                "recorded_date": event_date,
                "confidence": "medium",
            }
        )
    return (facts, requires_semantic_interpretation)


def _resolve_note_source_path(
    note_path: Path,
    source_path: str,
    *,
    processed_root: Path | None,
) -> tuple[Path, str | None] | None:
    archive_path, member_name = split_archive_source_path(source_path)
    if processed_root is not None:
        source = Path(archive_path)
        relative = None
        for prefix in (PROCESSED_ROOT, Path(processed_root.name)):
            try:
                relative = source.relative_to(prefix)
                break
            except ValueError:
                continue
        if relative is None:
            raise HistorySyncError(
                f"retained history provenance is outside {PROCESSED_ROOT}: {archive_path}"
            )
        resolved_root = processed_root.resolve()
        candidate = (resolved_root / relative).resolve()
        if candidate != resolved_root and resolved_root not in candidate.parents:
            raise HistorySyncError(
                f"retained history provenance escapes configured processed root: {archive_path}"
            )
        if candidate.exists():
            return candidate, member_name
        return None

    for parent in note_path.parents:
        candidate = (parent / archive_path).resolve()
        if candidate.exists():
            return (candidate, member_name)
    return None


def _read_ccda_root_for_history_note(
    note_path: Path,
    source_path: str,
    *,
    processed_root: Path | None,
) -> ET.Element | None:
    archive_path, member_name = split_archive_source_path(source_path)
    expects_ccda = member_name is not None or Path(archive_path).suffix.lower() == ".xml"
    if not expects_ccda:
        return None
    resolved = _resolve_note_source_path(
        note_path,
        source_path,
        processed_root=processed_root,
    )
    if resolved is None:
        if processed_root is not None:
            raise HistorySyncError(
                f"retained C-CDA source is unavailable under configured processed root: "
                f"{source_path}"
            )
        return None
    source_file, member_name = resolved
    try:
        if member_name is None:
            if source_file.suffix.lower() != ".xml":
                return None
            return ET.parse(source_file).getroot()
        with zipfile.ZipFile(source_file) as archive:
            return ET.fromstring(archive.read(member_name))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        if processed_root is not None:
            raise HistorySyncError(
                f"retained C-CDA source could not be parsed: {source_path}"
            ) from exc
        return None


def _extract_ccda_visit_diagnoses_from_root(
    root: ET.Element,
    *,
    event_date: str | None,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for section in _iter_ccda_section_elements(root):
        title = _normalize_optional_text(_first_text(section, "./hl7:title"))
        if title != "Visit Diagnoses":
            continue
        text_node = section.find("./hl7:text", _HL7_NS)
        if text_node is None:
            continue
        parsed_rows: list[tuple[str, str | None]] = []
        for row in text_node.iter():
            if _local_name(row.tag) != "tr":
                continue
            cells = [c for c in row if _local_name(c.tag) in {"td", "th"}]
            if not cells or _local_name(cells[0].tag) == "th":
                continue  # skip header rows
            description = _clean_visit_diagnosis_label(
                " ".join(" ".join(cells[0].itertext()).split()).strip()
            )
            if not description:
                continue
            date_text = " ".join(cells[1].itertext()).strip() if len(cells) > 1 else ""
            row_date = _iso_date_from_slash_date(date_text) if date_text else None
            parsed_rows.append((description, row_date))
        if not parsed_rows:
            continue
        for description, row_date in parsed_rows:
            lowered = description.casefold()
            facts.append(
                {
                    "description": description,
                    "category": "screening" if "screening" in lowered else "diagnosis",
                    "recorded_date": row_date or event_date,
                    "confidence": "medium",
                }
            )
        break
    return facts


def _extract_structured_history_facts_from_note(
    note_path: Path,
    *,
    processed_root: Path | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    text, end, fm = _read_history_review_front_matter(note_path)
    body = text[end + 4 :]
    event_date = str(fm.get("event_date") or "") or None
    source_path = str(fm.get("source_path") or "")
    facts: list[dict[str, Any]] = []
    requires_semantic_interpretation = False

    simple_sections = (
        ("Allergies", "No known allergies documented", "allergy"),
        ("Medications", "No medications documented", "medication"),
        ("Active Problems", "No active problems documented", "diagnosis"),
    )
    for heading, description, category in simple_sections:
        section = _extract_markdown_heading_section(body, heading)
        if not section:
            continue
        if "no known" in section.casefold() or "not on file" in section.casefold():
            facts.append(
                {
                    "description": description,
                    "category": category,
                    "recorded_date": event_date,
                    "confidence": "medium",
                }
            )

    vitals_section = _extract_markdown_heading_section(body, "Last Filed Vital Signs")
    if vitals_section:
        vital_patterns = (
            (r"Pulse[: ]+(\d+(?:\.\d+)?)", "Pulse", "/min"),
            (r"Respiratory Rate[: ]+(\d+(?:\.\d+)?)", "Respiratory rate", "/min"),
            (r"Oxygen Saturation[: ]+(\d+(?:\.\d+)?)%", "O2 saturation", "%"),
            (r"Weight[: ]+(\d+(?:\.\d+)?)\s*kg", "Weight", "kg"),
            (r"Height[: ]+(\d+(?:\.\d+)?)\s*cm", "Height", "cm"),
            (r"Body Mass Index[: ]+(\d+(?:\.\d+)?)", "BMI", "kg/m2"),
            (
                r"Systolic Blood Pressure[: ]+(\d+(?:\.\d+)?)\s*mmHg",
                "Blood pressure systolic",
                "mmHg",
            ),
            (
                r"Diastolic Blood Pressure[: ]+(\d+(?:\.\d+)?)\s*mmHg",
                "Blood pressure diastolic",
                "mmHg",
            ),
        )
        for pattern, description, unit in vital_patterns:
            match = re.search(pattern, vitals_section, flags=re.IGNORECASE)
            if match:
                facts.append(
                    {
                        "description": description,
                        "category": "vital_sign",
                        "recorded_date": event_date,
                        "value": match.group(1),
                        "unit": unit,
                        "confidence": "medium",
                    }
                )

    diagnoses_section = _extract_markdown_heading_section(body, "Visit Diagnoses")
    if diagnoses_section:
        ccda_root = _read_ccda_root_for_history_note(
            note_path,
            source_path,
            processed_root=processed_root,
        )
        ccda_visit_diagnoses = []
        if ccda_root is not None:
            ccda_visit_diagnoses = _extract_ccda_visit_diagnoses_from_root(
                ccda_root,
                event_date=event_date,
            )
        if ccda_visit_diagnoses:
            facts.extend(ccda_visit_diagnoses)
        else:
            diagnoses_section = re.sub(r"^Diagnosis Start Date\s+", "", diagnoses_section)
            diagnoses_section = _strip_history_section_footnotes(diagnoses_section)
            dated_diagnoses = _extract_dated_history_problem_list(
                diagnoses_section,
                category="diagnosis",
            )
            if dated_diagnoses:
                for fact in dated_diagnoses:
                    lowered = str(fact["description"]).casefold()
                    if lowered in {
                        "as of",
                        "documented in this encounter",
                        "documented as of this encounter",
                    }:
                        continue
                    if "screening" in lowered:
                        fact["category"] = "screening"
                    if not fact.get("recorded_date"):
                        fact["recorded_date"] = event_date
                    facts.append(fact)
            else:
                undated_rows, undated_requires_semantic_interpretation = (
                    _extract_undated_visit_diagnoses(
                        diagnoses_section,
                        event_date=event_date,
                    )
                )
                facts.extend(undated_rows)
                requires_semantic_interpretation = (
                    requires_semantic_interpretation or undated_requires_semantic_interpretation
                )

    active_problems_section = _extract_markdown_heading_section(body, "Active Problems")
    if active_problems_section and "no known" not in active_problems_section.casefold():
        facts.extend(
            _extract_dated_history_problem_list(
                active_problems_section,
                category="diagnosis",
            )
        )

    unique_facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fact in facts:
        key = (
            str(fact.get("description") or "").casefold(),
            str(fact.get("category") or "").casefold(),
            str(fact.get("recorded_date") or ""),
            str(fact.get("value") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_facts.append(fact)
    return (unique_facts, requires_semantic_interpretation)


def extract_structured_history_facts_from_note(
    note_path: Path,
    *,
    processed_root: Path | None = None,
) -> list[dict[str, Any]]:
    facts, _ = _extract_structured_history_facts_from_note(
        note_path,
        processed_root=processed_root,
    )
    return facts


def _history_auto_accept_rows(
    note_path: Path,
    *,
    fact_interpreter: FactInterpreter | None,
    expected_person_id: str,
    processed_root: Path | None,
) -> tuple[list[dict[str, Any]], int, bool]:
    extracted_rows, _ = _extract_structured_history_facts_from_note(
        note_path,
        processed_root=processed_root,
    )
    accepted_rows = [
        row
        for row in extracted_rows
        if str(row.get("confidence") or "").lower() in {"high", "medium"}
    ]
    excluded_low_confidence = len(extracted_rows) - len(accepted_rows)

    if fact_interpreter is None:
        return (accepted_rows, excluded_low_confidence, False)

    ai_rows = _extract_history_facts_from_note_via_interpreter(
        note_path,
        fact_interpreter=fact_interpreter,
        expected_person_id=expected_person_id,
    )
    if not ai_rows:
        return (accepted_rows, excluded_low_confidence, False)
    return (
        _merge_history_auto_accept_rows(accepted_rows, ai_rows),
        excluded_low_confidence,
        True,
    )


def _extract_history_facts_from_note_via_interpreter(
    note_path: Path,
    *,
    fact_interpreter: FactInterpreter,
    expected_person_id: str,
) -> list[dict[str, Any]] | None:
    try:
        extraction = validate_fact_extraction(fact_interpreter.extract_facts(note_path))
    except InterpreterContractError:
        return None
    if extraction.category != "history" or extraction.person_id != expected_person_id:
        return None
    if extraction.below_threshold():
        return None
    try:
        rows = canonicalize_candidate_history_facts(extraction, str(note_path))
    except HistorySyncError:
        return None

    accepted_rows: list[dict[str, Any]] = []
    for row in rows:
        confidence_label = _history_confidence_label(row.get("confidence"))
        if confidence_label == "low":
            continue
        accepted_row = {key: value for key, value in row.items() if key != "person_id"}
        accepted_row["confidence"] = confidence_label
        accepted_rows.append(accepted_row)
    return accepted_rows or None


def _history_confidence_label(value: Any) -> str:
    if isinstance(value, (int, float)):
        return "high" if float(value) >= 0.9 else "medium"
    normalized = str(value or "").strip().lower()
    if normalized in {"high", "medium"}:
        return normalized
    return "low"


def _history_auto_accept_merge_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("description") or "").casefold(),
        str(row.get("category") or "").casefold(),
        str(row.get("recorded_date") or ""),
        str(row.get("value") or ""),
        str(row.get("unit") or ""),
    )


def _merge_history_auto_accept_rows(
    deterministic_rows: list[dict[str, Any]],
    ai_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, str, str], dict[str, Any]] = {
        _history_auto_accept_merge_key(row): row for row in deterministic_rows
    }
    for row in ai_rows:
        merged[_history_auto_accept_merge_key(row)] = row
    return list(merged.values())


def _clean_history_fact_description(raw: str) -> str:
    """Strip common noisy prefixes from history fact descriptions."""
    raw = raw.strip()
    noisy_prefixes = (
        "Active problem: ",
        "Active problem:",
        "History of ",
        "current use of ",
        "Long term ",
        "Taking ",
    )
    lowered = raw.lower()
    for prefix in noisy_prefixes:
        if lowered.startswith(prefix.lower()):
            return raw[len(prefix) :].strip()
    return raw


# Map user-update category labels to clinical categories recognized by the report renderer
_USER_UPDATE_CATEGORY_MAP = {
    "condition": "diagnosis",
    "diagnosis": "diagnosis",
    "allergy": "allergy",
    "immunization": "immunization",
    "screening": "screening",
    "social_history": "social_history",
    "vital_signs": "vital_sign",
    "hospitalization": "diagnosis",
    "procedure": "procedure",
    "imaging": "imaging_finding",
    "lab": "lab",
    "pathology": "diagnosis",
}


def _map_user_update_category(raw: str) -> str:
    normalized = raw.strip().lower().replace(" ", "_")
    return _USER_UPDATE_CATEGORY_MAP.get(normalized, "other")


HISTORY_CORRECTIONS_NOTE = "history-corrections.md"


def history_corrections_note_path(vault_root: Path, person_id: str) -> Path:
    return vault_root / "Records" / person_id / "History" / HISTORY_CORRECTIONS_NOTE


def history_refutation_key(description: str, recorded_date: object) -> tuple[str, str]:
    """Identity of a refuted fact: whitespace/case-normalized label plus its date."""
    return " ".join(description.split()).casefold(), str(recorded_date or "")


def load_history_refutations(vault_root: Path, person_id: str) -> list[dict[str, str]]:
    """Return the person's user refutations of history facts (ADR-004)."""
    note_path = history_corrections_note_path(vault_root, person_id)
    if not note_path.is_file():
        return []
    _, _, fm = _read_history_review_front_matter(note_path)
    if fm.get("record_type") != "history_correction":
        raise HistorySyncError(f"not a history correction note: {note_path}")
    raw = fm.get("refutations")
    if not isinstance(raw, list):
        return []
    return [
        {key: str(value) for key, value in item.items() if value is not None}
        for item in raw
        if isinstance(item, dict) and item.get("description") and item.get("recorded_date")
    ]


def write_history_refutations(
    vault_root: Path,
    person_id: str,
    refutations: list[dict[str, str]],
) -> None:
    """Write the person's refutation note, or remove it when none remain.

    The History category index is regenerated so the note never becomes an orphan.
    """
    note_path = history_corrections_note_path(vault_root, person_id)
    if refutations:
        _render_history_refutations_note(note_path, person_id, refutations)
    else:
        note_path.unlink(missing_ok=True)
    refresh_history_category_index(vault_root, person_id)


def refresh_history_category_index(vault_root: Path, person_id: str) -> None:
    write_category_index_note(
        vault_root / "Records" / person_id / "History",
        person_id,
        "History",
        display_name=load_person_display_name(vault_root, person_id),
    )


def _render_history_refutations_note(
    note_path: Path,
    person_id: str,
    refutations: list[dict[str, str]],
) -> None:
    fm = {
        "person_id": person_id,
        "record_type": "history_correction",
        "title": "History Corrections",
        "refutations": refutations,
    }
    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
    rows = "\n".join(
        f"| {r['description']} | {r['recorded_date']} | {r.get('reason', '')} "
        f"| {r.get('refuted_at', '')} |"
        for r in refutations
    )
    body = (
        "# History Corrections\n\n"
        "[[_index|← History]]\n\n"
        "User-refuted history facts. A fact from any source with the same label and "
        "date is kept out of accepted facts and every report until the refutation "
        "is revoked through `fm-correct-fact`.\n\n"
        "| Description | Date | Reason | Refuted |\n"
        "| --- | --- | --- | --- |\n"
        f"{rows}\n"
    )
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(f"---\n{yaml_block}\n---\n\n{body}", encoding="utf-8")


def _partition_refuted_facts(
    facts: list[dict[str, Any]],
    refutations: list[dict[str, str]],
    *,
    note_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keys = {history_refutation_key(r["description"], r["recorded_date"]) for r in refutations}
    accepted: list[dict[str, Any]] = []
    refuted: list[dict[str, Any]] = []
    for fact in facts:
        key = history_refutation_key(
            str(fact.get("description") or ""), fact.get("recorded_date") or note_date
        )
        (refuted if key in keys else accepted).append(fact)
    return accepted, refuted


def _write_history_review_facts(
    note_path: Path,
    text: str,
    end: int,
    fm: dict[str, Any],
    accepted: list[dict[str, Any]],
    refuted: list[dict[str, Any]],
) -> None:
    fm["accepted_facts"] = accepted
    if refuted:
        fm["refuted_facts"] = refuted
    else:
        fm.pop("refuted_facts", None)
    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
    body_after_fm = _strip_semantic_review_sections(text[end + 4 :])
    facts_section = "## Accepted Structured Facts\n\n" + _render_facts_grouped(
        accepted,
        excluded_low_confidence=int(fm.get("excluded_low_confidence_count") or 0),
        note_date=str(fm["event_date"]) if fm.get("event_date") else None,
    )
    if refuted:
        facts_section += (
            "\n\n## Refuted by User\n\n"
            "Kept out of accepted facts and reports by [[history-corrections]].\n\n"
            + "\n".join(
                f"- {fact.get('description')} ({fact.get('recorded_date') or ''})"
                for fact in refuted
            )
            + "\n"
        )
    note_path.write_text(
        f"---\n{yaml_block}\n---\n{body_after_fm}\n{facts_section}",
        encoding="utf-8",
    )


def _accepted_history_review_notes(
    vault_root: Path, person_id: str
) -> Iterator[tuple[Path, str, int, dict[str, Any]]]:
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name.startswith("_") or note_path.name == HISTORY_CORRECTIONS_NOTE:
            continue
        text, end, fm = _read_history_review_front_matter(note_path)
        if fm.get("record_type") == "history" and fm.get("verification_status") == "accepted":
            yield note_path, text, end, fm


def history_fact_exists(vault_root: Path, person_id: str, key: tuple[str, str]) -> bool:
    """Whether any accepted history-review note holds a fact with this refutation key."""
    for _, _, _, fm in _accepted_history_review_notes(vault_root, person_id):
        note_date = str(fm.get("event_date") or "")
        for fact in [*(fm.get("accepted_facts") or []), *(fm.get("refuted_facts") or [])]:
            fact_date = fact.get("recorded_date") or note_date
            if history_refutation_key(str(fact.get("description") or ""), fact_date) == key:
                return True
    return False


def apply_history_refutations_to_notes(vault_root: Path, person_id: str) -> int:
    """Re-partition accepted history-review notes against current refutations.

    Facts matching a refutation move to ``refuted_facts``; previously refuted
    facts whose refutation was revoked move back. Returns the number of notes
    rewritten.
    """
    refutations = load_history_refutations(vault_root, person_id)
    changed = 0
    for note_path, text, end, fm in _accepted_history_review_notes(vault_root, person_id):
        current_accepted = fm.get("accepted_facts") or []
        current_refuted = fm.get("refuted_facts") or []
        accepted, refuted = _partition_refuted_facts(
            [*current_accepted, *current_refuted],
            refutations,
            note_date=str(fm.get("event_date") or ""),
        )
        if accepted == current_accepted and refuted == current_refuted:
            continue
        _write_history_review_facts(note_path, text, end, fm, accepted, refuted)
        changed += 1
    return changed


def accept_history_candidates(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    note_path: Path,
    accepted_rows: list[dict[str, Any]],
    person_id: str,
    source_path: str,
    excluded_low_confidence: int = 0,
    extraction_mode: str | None = None,
    refresh_dependents: bool = True,
) -> None:
    """Persist accepted structured candidates into the vault note and regenerate the report.

    Updates the note's YAML front matter with ``accepted_facts`` and
    ``verification_status: accepted``, appends an ``## Accepted Structured Facts``
    section to the note body, and refreshes ``medical-history-summary.md``.
    Unless ``refresh_dependents`` is false (a batch caller refreshes once at the
    end), it also refreshes every composite that reads this history.

    ``accepted_rows`` must be non-empty dicts with at least a ``description`` field.
    Raises ``HistorySyncError`` on validation failure; the caller is responsible for
    rolling back and quarantining on persistence errors.
    """
    if not accepted_rows:
        raise HistorySyncError("accepted_rows must contain at least one fact")
    for row in accepted_rows:
        if not row.get("description"):
            raise HistorySyncError("each accepted fact must have a non-empty description")

    text, end, fm = _read_history_review_front_matter(note_path)

    serializable_facts = [
        {
            "description": str(r.get("description") or ""),
            "category": str(r.get("category") or "diagnosis"),
            "icd_10_code": r.get("icd_10_code"),
            "recorded_date": r.get("recorded_date"),
            "value": r.get("value"),
            "unit": r.get("unit"),
            "confidence": r.get("confidence"),
            **({"cvx_code": r["cvx_code"]} if r.get("cvx_code") else {}),
            **({"lot_number": r["lot_number"]} if r.get("lot_number") else {}),
        }
        for r in accepted_rows
    ]
    fm["verification_status"] = "accepted"
    if extraction_mode is not None:
        fm["extraction_mode"] = extraction_mode
    if excluded_low_confidence > 0:
        fm["excluded_low_confidence_count"] = excluded_low_confidence
    elif "excluded_low_confidence_count" in fm:
        del fm["excluded_low_confidence_count"]

    accepted, refuted = _partition_refuted_facts(
        serializable_facts,
        load_history_refutations(vault_root, person_id),
        note_date=str(fm.get("event_date") or ""),
    )
    _write_history_review_facts(note_path, text, end, fm, accepted, refuted)

    log_audit_event(
        conn,
        event_type=AUDIT_HISTORY_CANDIDATES_ACCEPTED,
        subject_type="history",
        subject_id=person_id,
        source_path=source_path,
        status=STATE_PROCESSED,
        message=f"accepted {len(accepted_rows)} history candidate(s) in {note_path.name}",
    )
    write_medical_history_summary_report(vault_root, person_id)
    if refresh_dependents:
        refresh_history_dependent_reports(conn, vault_root, person_id)
    refresh_fhir_dependent_export(conn, vault_root, person_id)
    _refresh_history_pending_review(conn, vault_root, person_id)


def auto_accept_history_review_notes_for_source_prefix(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    source_path_prefix: str,
    refresh_existing: bool = False,
    fact_interpreter: FactInterpreter | None = None,
    processed_root: Path | None = None,
    refresh_reports: bool = True,
) -> tuple[int, int]:
    """Run deterministic extraction/acceptance for matching history review notes.

    Returns ``(accepted_notes, unresolved_notes)`` for notes whose ``source_path``
    starts with ``source_path_prefix`` and are either still marked ``needs_review``
    or, when ``refresh_existing`` is true, already ``accepted``. When any note is
    accepted and ``refresh_reports`` is true, composites that read the history
    refresh once after the batch.
    """
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return (0, 0)

    accepted_notes = 0
    unresolved_notes = 0
    for note_path in sorted(history_dir.glob("*-history-review.md")):
        _, _, fm = _read_history_review_front_matter(note_path)
        verification_status = str(fm.get("verification_status") or "")
        if verification_status != "needs_review" and not (
            refresh_existing and verification_status == "accepted"
        ):
            continue
        source_path = str(fm.get("source_path") or "")
        if not source_path.startswith(source_path_prefix):
            continue

        accepted_rows, excluded_low_confidence, used_interpreter = _history_auto_accept_rows(
            note_path,
            fact_interpreter=fact_interpreter,
            expected_person_id=person_id,
            processed_root=processed_root,
        )
        if not accepted_rows:
            unresolved_notes += 1
            continue

        _persist_semantic_history_immunizations(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            person_id=person_id,
            source_path=source_path,
            accepted_rows=accepted_rows,
            processed_root=processed_root,
        )
        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=accepted_rows,
            person_id=person_id,
            source_path=source_path,
            excluded_low_confidence=excluded_low_confidence,
            extraction_mode="clinical_interpreter" if used_interpreter else None,
            refresh_dependents=False,
        )
        accepted_notes += 1

    if accepted_notes and refresh_reports:
        refresh_history_dependent_reports(conn, vault_root, person_id)
    return (accepted_notes, unresolved_notes)


def _persist_semantic_history_immunizations(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    note_path: Path,
    person_id: str,
    source_path: str,
    accepted_rows: list[dict[str, Any]],
    processed_root: Path | None,
) -> None:
    immunization_rows = [
        {
            "person_id": person_id,
            "vaccine_name": str(row.get("description") or ""),
            "cvx_code": row.get("cvx_code"),
            "administered_date": row.get("recorded_date"),
            "status": "completed",
            "lot_number": row.get("lot_number"),
            "evidence_snippet": row.get("evidence_snippet"),
        }
        for row in accepted_rows
        if str(row.get("category") or "").casefold() == "immunization"
    ]
    if not immunization_rows:
        return

    resolved = _resolve_note_source_path(
        note_path,
        source_path,
        processed_root=processed_root,
    )
    if resolved is None:
        raise HistorySyncError(f"retained immunization evidence is unavailable: {source_path}")
    source_file, _member_name = resolved
    _, _, front_matter = _read_history_review_front_matter(note_path)

    from famed.immunization_sync import persist_immunization_rows

    persist_immunization_rows(
        conn=conn,
        vault_root=vault_root,
        person_id=person_id,
        source_file=source_file,
        processed_source_path=source_path,
        checksum=source_file_checksum(source_file),
        source_type="history_extract",
        rows=immunization_rows,
        document_date=str(front_matter.get("event_date") or "") or None,
        retained_source=True,
        source_file_name=str(front_matter.get("source_file_name") or "") or None,
        refresh_export=False,
    )


def remove_history_review_note_without_facts(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    note_path: Path,
    person_id: str,
    source_path: str,
    excluded_low_confidence: int = 0,
) -> None:
    """Remove a no-use review note after confirming it has no accepted facts."""
    _, _, fm = _read_history_review_front_matter(note_path)
    if fm.get("person_id") != person_id:
        raise HistorySyncError(f"person_id mismatch in {note_path}")

    note_name = note_path.name
    note_path.unlink(missing_ok=True)
    person_history_dir = vault_root / "Records" / person_id / "History"
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_history_dir,
        person_id,
        "History",
        display_name=display_name,
    )

    log_audit_event(
        conn,
        event_type=AUDIT_HISTORY_SOURCE_REVIEWED_NO_FACTS,
        subject_type="history",
        subject_id=person_id,
        source_path=source_path,
        status=STATE_PROCESSED,
        message=(
            f"removed no-fact history review note {note_name}; "
            f"{excluded_low_confidence} low-confidence candidate(s) excluded"
        ),
    )
    write_medical_history_summary_report(vault_root, person_id)
    refresh_fhir_dependent_export(conn, vault_root, person_id)
    _refresh_history_pending_review(conn, vault_root, person_id)


def _refresh_history_pending_review(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> None:
    """Refresh the derived current-review projection after history mutations."""
    from famed.pending_review import refresh_pending_review_report_if_configured

    refresh_pending_review_report_if_configured(
        conn,
        root=vault_root.parent,
        vault_root=vault_root,
        person_id=person_id,
    )


def _read_history_review_front_matter(note_path: Path) -> tuple[str, int, dict[str, Any]]:
    text = note_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise HistorySyncError(f"note does not start with YAML front matter: {note_path}")
    end = text.find("\n---", 3)
    if end == -1:
        raise HistorySyncError(f"note front matter is not closed: {note_path}")
    try:
        loaded = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as exc:
        raise HistorySyncError(f"could not parse front matter in {note_path}") from exc
    if not isinstance(loaded, dict):
        raise HistorySyncError(f"history note front matter must be a mapping: {note_path}")
    fm: dict[str, Any] = loaded
    return text, end, fm


def _require_semantic_replay_for_accepted_history(
    *,
    vault_root: Path,
    person_id: str,
    source_path_prefix: str,
    fact_interpreter: FactInterpreter | None,
    only_interpreted: bool = False,
    equivalent_source_paths: dict[str, str] | None = None,
) -> None:
    """Prevent retained-source replay from silently discarding accepted facts."""
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return

    protected_notes: list[tuple[str, str]] = []
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        try:
            _, _, front_matter = _read_history_review_front_matter(note_path)
        except OSError as exc:
            raise HistorySyncError(
                f"could not preflight retained history note: {note_path}"
            ) from exc
        except HistorySyncError as exc:
            try:
                raw_note = note_path.read_text(encoding="utf-8")
            except OSError as read_exc:
                raise HistorySyncError(
                    f"could not preflight retained history note: {note_path}"
                ) from read_exc
            if source_path_prefix in raw_note or any(
                source_path in raw_note for source_path in (equivalent_source_paths or {})
            ):
                raise HistorySyncError(
                    f"could not validate matching retained history note: {note_path}"
                ) from exc
            continue
        source_path = str(front_matter.get("source_path") or "")
        replay_source_path = (equivalent_source_paths or {}).get(source_path, source_path)
        accepted_facts = front_matter.get("accepted_facts")
        source_matches = replay_source_path == source_path_prefix or (
            source_path_prefix.endswith("#") and replay_source_path.startswith(source_path_prefix)
        )
        is_protected = not only_interpreted or front_matter.get("extraction_mode") == (
            "clinical_interpreter"
        )
        if source_matches and is_protected and isinstance(accepted_facts, list) and accepted_facts:
            protected_notes.append((note_path.name, replay_source_path))

    if not protected_notes:
        return

    decision_checker = getattr(fact_interpreter, "has_history_decision_for_source", None)
    uncovered_notes = [
        note_name
        for note_name, source_path in protected_notes
        if not callable(decision_checker) or not decision_checker(source_path)
    ]
    if not uncovered_notes:
        return
    joined = ", ".join(uncovered_notes)
    raise HistorySyncError(
        "retained history reprocessing would overwrite accepted facts in "
        f"{joined}; supply source-bound retained decisions for semantic replay"
    )


def _strip_semantic_review_sections(body: str) -> str:
    for heading in (
        "Narrative Sections",
        "Extracted Text",
        "Review",
        "Accepted Structured Facts",
        "Refuted by User",
    ):
        body = re.sub(
            rf"\n## {re.escape(heading)}\n.*?(?=\n## |\Z)",
            "",
            body,
            flags=re.DOTALL,
        )
    return body


def infer_person_id_from_processed_history_path(
    path: Path,
    processed_root: Path,
    *,
    source_category: str = "History",
) -> str | None:
    """Return the person_id encoded in a processed History source path, or None."""
    try:
        relative = path.resolve().relative_to(processed_root.resolve())
    except ValueError:
        return None
    if len(relative.parts) >= 3 and relative.parts[1] == source_category:
        return relative.parts[0].strip() or None
    return None


def _iter_history_archive_members(archive_path: Path) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(archive_path) as archive:
        for member_name in sorted(archive.namelist()):
            member_basename = Path(member_name).name
            if Path(member_name).suffix.lower() != ".xml":
                continue
            if not re.fullmatch(r"DOC\d+\.XML", member_basename, flags=re.IGNORECASE):
                continue
            yield member_name, archive.read(member_name)


def _legacy_history_source_paths_for_member(
    *,
    root: Path,
    processed_root: Path,
    person_id: str,
    member_name: str,
) -> list[str]:
    history_dir = processed_root / person_id / "History"
    if not history_dir.is_dir():
        return []
    member_base = Path(member_name).name.casefold()
    matches: list[str] = []
    for candidate in history_dir.glob("*"):
        if candidate.suffix.lower() != ".xml":
            continue
        if not candidate.name.casefold().endswith(member_base):
            continue
        matches.append(str(candidate.relative_to(root)))
    return sorted(dict.fromkeys(matches))


def _history_fact_date(fact: dict[str, Any], entry: dict[str, Any]) -> str:
    raw_date = fact.get("recorded_date") or entry.get("event_date") or "undated"
    return str(raw_date)


def _history_fact_label(fact: dict[str, Any]) -> str:
    label = _markdown_table_cell(fact.get("description")) or "Unlabeled history fact"
    icd_10_code = _markdown_table_cell(fact.get("icd_10_code"))
    if icd_10_code:
        label = f"{label} (ICD-10 {icd_10_code})"
    return label


def _history_fact_review_marker(fact: dict[str, Any]) -> str:
    if fact.get("confidence") != "medium":
        return ""
    reason = str(fact.get("review_reason") or "").strip()
    if reason:
        return f" (needs source check: {reason})"
    return ""


def _history_fact_description_text(fact: dict[str, Any]) -> str:
    return _markdown_table_cell(fact.get("description")) or "Unlabeled history fact"


def _history_fact_value(fact: dict[str, Any]) -> str | None:
    value = _markdown_table_cell(fact.get("value"))
    unit = _markdown_table_cell(fact.get("unit"))
    if value and unit:
        return f"{value} {unit}"
    return value or unit or None


def _history_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _history_fact_date(item["fact"], item["entry"]),
        str(item["fact"].get("description") or "").casefold(),
        str(item["entry"].get("title") or "").casefold(),
    )


def _history_fact_dedup_description(fact: dict[str, Any]) -> str:
    description = " ".join(_history_fact_description_text(fact).split()).casefold()
    category = str(fact.get("category") or "").casefold()
    if category == "follow_up":
        description = re.sub(r"\s*\([^)]*\)\s*$", "", description)
        description = re.sub(r"\brecommended\b", "", description)
        description = " ".join(description.split())
    return description


def _history_fact_render_dedup_description(fact: dict[str, Any]) -> str:
    summary_condition_key = _history_summary_condition_key(fact)
    if summary_condition_key is not None:
        return summary_condition_key
    description = _history_fact_dedup_description(fact)
    category = str(fact.get("category") or "").casefold()
    if category != "screening":
        return description

    words = set(re.findall(r"[a-z0-9]+", description))
    if "mammogram" in words and (
        "breast" in words or "tomosynthesis" in words or "mammographic" in words
    ):
        return "breast cancer mammogram screening"
    if "cervical" in words and ("pap" in words or "hpv" in words):
        return "cervical cancer pap hpv screening"
    return description


_HISTORY_DIGEST_CHRONIC_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Hypertension", ("htn", "hypertension", "high blood pressure")),
    ("Atrial fibrillation", ("atrial fibrillation", "paf")),
    ("Coronary artery disease", ("cad", "coronary artery disease")),
    (
        "Hyperlipidemia / high cholesterol",
        ("cholesterol", "hypercholesterolemia", "hyperlipidemia"),
    ),
    ("Diabetes / prediabetes", ("diabetes", "prediabetes")),
    ("GERD / reflux", ("gerd", "gastroesophageal reflux", "heartburn", "reflux")),
    ("Osteoarthritis", ("osteoarthritis",)),
    ("Asthma", ("asthma",)),
    ("Chronic kidney disease", ("chronic kidney disease", "ckd")),
    ("Benign prostatic hyperplasia", ("benign prostatic hyperplasia", "bph")),
    ("Depression", ("depression",)),
    ("Anxiety", ("anxiety", "gad")),
    ("Alcohol use disorder", ("alcohol dependence", "alcohol use disorder")),
    ("Varicose veins", ("varicose vein", "varicose veins")),
    (
        "Urinary incontinence",
        ("urinary incontinence", "stress incontinence", "incontinence of urine"),
    ),
)

_HISTORY_SUMMARY_CONDITION_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "key": "paroxysmal-atrial-fibrillation",
        "preferred_label": "Paroxysmal atrial fibrillation",
        "aliases": ("paf", "paroxysmal atrial fibrillation"),
    },
    {
        "key": "prostate-cancer",
        "preferred_label": "Prostate cancer",
        "aliases": ("prostate cancer", "malignant neoplasm of prostate"),
    },
)


def _history_term_matches(normalized: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized) is not None


def _history_summary_condition_family_for_text(text: str, category: str) -> dict[str, Any] | None:
    if category != "diagnosis":
        return None
    normalized = " ".join(text.split()).casefold()
    for family in _HISTORY_SUMMARY_CONDITION_FAMILIES:
        if any(_history_term_matches(normalized, alias) for alias in family["aliases"]):
            return family
    return None


def _history_summary_condition_family(fact: dict[str, Any]) -> dict[str, Any] | None:
    return _history_summary_condition_family_for_text(
        _history_fact_description_text(fact),
        str(fact.get("category") or "").casefold(),
    )


def _history_summary_condition_key(fact: dict[str, Any]) -> str | None:
    family = _history_summary_condition_family(fact)
    if family is None:
        return None
    return str(family["key"])


def _history_summary_issue_key(text: str, category: str) -> str:
    family = _history_summary_condition_family_for_text(text, category)
    if family is not None:
        return str(family["key"])
    normalized = re.sub(r"\s+\(ICD-10 [^)]+\)$", "", text).strip()
    return " ".join(normalized.split()).casefold()


def _history_summary_label_rank(item: dict[str, Any], preferred_label: str) -> tuple[int, int, str]:
    label = _history_fact_label(item["fact"])
    description = _history_fact_description_text(item["fact"])
    if description.casefold() == preferred_label.casefold():
        return (2, len(label), label.casefold())
    if "icd-10" not in label.casefold():
        return (1, len(label), label.casefold())
    return (0, len(label), label.casefold())


def _build_history_summary_condition_labels(items: list[dict[str, Any]]) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    families: dict[str, dict[str, Any]] = {}
    for item in items:
        family = _history_summary_condition_family(item["fact"])
        if family is None:
            continue
        key = str(family["key"])
        grouped.setdefault(key, []).append(item)
        families[key] = family

    labels: dict[str, str] = {}
    for key, family_items in grouped.items():
        family = families[key]
        preferred_label = str(family["preferred_label"])
        matching_preferred = [
            item
            for item in family_items
            if _history_fact_description_text(item["fact"]).casefold() == preferred_label.casefold()
        ]
        candidate_items = matching_preferred or family_items
        chosen = max(
            candidate_items,
            key=lambda item: _history_summary_label_rank(item, preferred_label),
        )
        labels[key] = _history_fact_label(chosen["fact"])
    return labels


def _history_summary_fact_label(
    fact: dict[str, Any],
    summary_condition_labels: dict[str, str],
) -> str:
    key = _history_summary_condition_key(fact)
    if key is not None and key in summary_condition_labels:
        return summary_condition_labels[key]
    return _history_fact_label(fact)


# Background events the Digest must show even though they are not "chronic":
# a cancer or a stroke changes how every later fact is read (ADR-029).
_HISTORY_DIGEST_CEREBROVASCULAR_LABEL = "Stroke / cerebrovascular disease"
_HISTORY_DIGEST_CEREBROVASCULAR_RE = re.compile(
    r"\b(?:stroke|cva|cerebrovascular|cerebral infarction|transient ischemic attack|tia)\b"
)
_HISTORY_DIGEST_NAMED_MALIGNANCIES = ("lymphoma", "leukemia", "melanoma", "myeloma", "sarcoma")
_HISTORY_DIGEST_CANCER_SITE_RE = re.compile(
    r"\bmalignant neoplasm of (?:the )?(?P<of_site>[a-z]+)"
    r"|\b(?P<site>[a-z]+) (?:cancer|carcinoma|adenocarcinoma)\b"
    r"|\b(?:cancer|carcinoma|adenocarcinoma|malignancy)\b"
)
_HISTORY_DIGEST_NON_SITE_WORDS = frozenset(
    {"a", "an", "and", "history", "no", "of", "the", "with", "metastatic", "invasive"}
)
# A clause that negates, screens for, or rules out a condition before naming it.
_HISTORY_DIGEST_NEGATION_RE = re.compile(
    r"\b(?:no|not|negative for|rule out|r/o|ruled out|screen(?:ing)? for)\b"
)


def _history_digest_affirmed(normalized: str, match: re.Match[str]) -> bool:
    """Return False when the matched term sits in a negated or screening clause."""
    clause_start = normalized.rfind(";", 0, match.start()) + 1
    return _HISTORY_DIGEST_NEGATION_RE.search(normalized[clause_start : match.end()]) is None


def _history_digest_event_label(normalized: str) -> str | None:
    if normalized.startswith(("family history of ", "fhx ")):
        return None
    cerebrovascular = _HISTORY_DIGEST_CEREBROVASCULAR_RE.search(normalized)
    if cerebrovascular and _history_digest_affirmed(normalized, cerebrovascular):
        return _HISTORY_DIGEST_CEREBROVASCULAR_LABEL
    for name in _HISTORY_DIGEST_NAMED_MALIGNANCIES:
        named = re.search(rf"\b{name}\b", normalized)
        if named and _history_digest_affirmed(normalized, named):
            return name.capitalize()
    cancer = _HISTORY_DIGEST_CANCER_SITE_RE.search(normalized)
    if cancer is None or not _history_digest_affirmed(normalized, cancer):
        return None
    site = cancer.group("of_site") or cancer.group("site")
    if site is None or site in _HISTORY_DIGEST_NON_SITE_WORDS:
        return "Cancer"
    return f"{site.capitalize()} cancer"


def _history_digest_chronic_label(fact: dict[str, Any]) -> str | None:
    if str(fact.get("category") or "").casefold() not in ("diagnosis", "imaging_finding"):
        return None
    normalized = _history_fact_dedup_description(fact)
    for label, terms in _HISTORY_DIGEST_CHRONIC_LABELS:
        if any(_history_term_matches(normalized, term) for term in terms):
            return label
    return None


def _history_digest_label(fact: dict[str, Any]) -> str | None:
    """Digest label: chronic allowlist, else a cancer or cerebrovascular event.

    Event labels feed only the Digest. Chronic-repeat suppression keeps using
    ``_history_digest_chronic_label`` so a stroke's distinct sequelae stay in the
    report body.
    """
    chronic = _history_digest_chronic_label(fact)
    if chronic is not None:
        return chronic
    if str(fact.get("category") or "").casefold() not in ("diagnosis", "imaging_finding"):
        return None
    return _history_digest_event_label(_history_fact_dedup_description(fact))


def _support_review_key(text: str) -> str:
    return _normalize_history_issue_text(text)


_ANTIHYPERTENSIVE_TERMS = (
    "amlodipine",
    "atenolol",
    "benazepril",
    "carvedilol",
    "chlorthalidone",
    "diltiazem",
    "enalapril",
    "hydrochlorothiazide",
    "irbesartan",
    "lisinopril",
    "losartan",
    "metoprolol",
    "olmesartan",
    "propranolol",
    "telmisartan",
    "valsartan",
    "verapamil",
)

_CAD_STRONG_SUPPORT_TERMS = (
    "angioplasty",
    "cardiac catheterization",
    "catheterization",
    "myocardial infarction",
    "nstemi",
    "pci",
    "stemi",
    "stent",
    "troponin",
)


def _numeric_prefix(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _load_markdown_table_rows(
    vault_root: Path,
    person_id: str,
    category: str,
) -> list[dict[str, str]]:
    directory = vault_root / "Records" / person_id / category
    if not directory.is_dir():
        return []

    required_columns = {
        "Labs": {"test_name", "value"},
        "Medications": {"medication_name", "status"},
        "Vitals": {"vital_name", "value"},
    }[category]
    rows: list[dict[str, str]] = []
    for note_path in sorted(directory.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if category == "Vitals":
            try:
                front_matter = parse_front_matter(text)
            except PersonSyncError:
                front_matter = {}
            if str(front_matter.get("verification_status") or "").casefold() == "needs_review":
                continue
        for row in _iter_markdown_table_dicts(text, required_columns):
            row["source_note"] = note_path.name
            rows.append(row)
    return rows


def _history_support_context(
    vault_root: Path,
    person_id: str,
    entries: list[dict[str, Any]],
) -> HistorySupportContext:
    facts = [
        fact
        for entry in entries
        for fact in entry.get("accepted_facts") or []
        if isinstance(fact, dict)
    ]
    return HistorySupportContext(
        facts=facts,
        history_text=" | ".join(
            " ".join(
                str(value)
                for key, value in fact.items()
                if key in {"description", "value", "details", "evidence_snippet"} and value
            ).casefold()
            for fact in facts
        ),
        labs=_load_markdown_table_rows(vault_root, person_id, "Labs"),
        medications=_load_markdown_table_rows(vault_root, person_id, "Medications"),
        vitals=_load_markdown_table_rows(vault_root, person_id, "Vitals"),
    )


def _active_medication_names(context: HistorySupportContext) -> list[str]:
    names: list[str] = []
    for row in context.medications:
        status = row.get("status", "").casefold()
        if status and status not in {"active", "current"}:
            continue
        name = row.get("medication_name", "")
        if name:
            names.append(name.casefold())
    return names


def _has_active_medication(context: HistorySupportContext, names: tuple[str, ...]) -> bool:
    active_names = _active_medication_names(context)
    return any(any(name in medication for name in names) for medication in active_names)


def _blood_pressure_readings(context: HistorySupportContext) -> list[tuple[str, float, float]]:
    readings: list[tuple[str, float, float]] = []
    pending_systolic: dict[str, float] = {}
    for row in context.vitals:
        label = row.get("vital_name", "").casefold()
        value = _numeric_prefix(row.get("value"))
        if value is None:
            continue
        date = row.get("observed_at") or row.get("source_note") or "undated"
        if "systolic" in label:
            pending_systolic[date] = value
        elif "diastolic" in label and date in pending_systolic:
            readings.append((date, pending_systolic[date], value))
    return readings


def _high_blood_pressure_evidence(context: HistorySupportContext) -> tuple[int, str | None]:
    readings = _blood_pressure_readings(context)
    elevated = [(date, sys, dia) for date, sys, dia in readings if sys >= 140 or dia >= 90]
    if readings:
        highest = max(readings, key=lambda reading: (reading[1], reading[2]))
        highest_text = f"highest local BP {highest[1]:g}/{highest[2]:g} on {highest[0]}"
    else:
        highest_text = None
    return len({date for date, _, _ in elevated}), highest_text


def _lab_values(
    context: HistorySupportContext,
    test_terms: tuple[str, ...],
) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    for row in context.labs:
        test_name = row.get("test_name", "").casefold()
        if not any(term in test_name for term in test_terms):
            continue
        value = _numeric_prefix(row.get("value"))
        if value is not None:
            values.append((row.get("observed_at") or row.get("source_note") or "undated", value))
    return values


def _has_history_text(context: HistorySupportContext, terms: tuple[str, ...]) -> bool:
    text = context.history_text
    for term in terms:
        if term.isalnum() and len(term) <= 5:
            if re.search(rf"\b{re.escape(term)}\b", text):
                return True
            continue
        if term in text:
            return True
    return False


def _support_review_for_description(
    description: str,
    category: str,
    context: HistorySupportContext,
) -> DiagnosisSupportReview | None:
    desc = description.casefold()
    if category not in {"diagnosis", "problem", "condition", "social_history"}:
        return None
    if desc.startswith(("family history of ", "fhx ")):
        return None

    if any(term in desc for term in ("hypertension", "high blood pressure", "htn")):
        if _has_active_medication(context, _ANTIHYPERTENSIVE_TERMS):
            return None
        elevated_dates, highest_bp = _high_blood_pressure_evidence(context)
        if elevated_dates >= 2:
            return None
        evidence = (highest_bp,) if highest_bp else ()
        return DiagnosisSupportReview(
            "source asserted only",
            "No repeated BP >=140/90 or active antihypertensive medication was found locally.",
            evidence,
        )

    if any(term in desc for term in ("coronary", "cad", "atherosclerotic heart")):
        if _has_history_text(context, _CAD_STRONG_SUPPORT_TERMS):
            return None
        if _has_history_text(context, ("agatston", "calcium score", "cac")):
            return DiagnosisSupportReview(
                "partial objective support",
                "Local evidence is limited to coronary calcium scoring; no MI, stent, "
                "catheterization, or troponin support was found.",
                ("coronary calcium score present",),
            )
        return DiagnosisSupportReview(
            "source asserted only",
            "No local MI, stent, catheterization, troponin, or coronary calcium evidence "
            "was found.",
        )

    if "alcohol dependence" in desc:
        if _has_history_text(
            context,
            ("audit-c score: 0", "low risk alcohol", "monthly or less", "0 drinks per week"),
        ):
            return DiagnosisSupportReview(
                "conflicting local evidence",
                "Local alcohol screening/social-history facts describe low-risk or "
                "minimal alcohol use.",
                ("low-risk alcohol history present",),
            )
        return DiagnosisSupportReview(
            "source asserted only",
            "No local alcohol-use-disorder evidence was found beyond the diagnosis label.",
        )

    if "diabetes" in desc or "prediabetes" in desc:
        a1c_values = _lab_values(context, ("hemoglobin a1c", "a1c"))
        if "prediabetes" in desc and any(5.7 <= value < 6.5 for _, value in a1c_values):
            return None
        if "diabetes" in desc and any(value >= 6.5 for _, value in a1c_values):
            return None
        if a1c_values:
            latest_date, latest_value = sorted(a1c_values)[-1]
            return DiagnosisSupportReview(
                "partial objective support",
                "A1c values are present but do not clearly support this diagnosis label.",
                (f"latest A1c {latest_value:g} on {latest_date}",),
            )
        return DiagnosisSupportReview("source asserted only", "No local A1c evidence was found.")

    if "hypothyroid" in desc or "tsh excess" in desc:
        if _has_active_medication(context, ("levothyroxine", "synthroid")):
            return None
        tsh_values = _lab_values(context, ("thyroid stimulating hormone", "tsh"))
        if any(value > 4.5 for _, value in tsh_values):
            return None
        return DiagnosisSupportReview(
            "source asserted only",
            "No elevated local TSH value or active thyroid replacement medication was found.",
        )

    if "anemia" in desc:
        hgb_values = _lab_values(context, ("hemoglobin", "hgb"))
        if any(value < 12.0 for _, value in hgb_values):
            return None
        return DiagnosisSupportReview(
            "source asserted only",
            "No low local hemoglobin value was found.",
        )

    if "depression" in desc or "anxiety" in desc:
        if _has_history_text(context, ("phq-2 score: 0", "no depression", "gad-2 score: 0")):
            return DiagnosisSupportReview(
                "conflicting local evidence",
                "Local screening facts include a negative depression/anxiety screen.",
                ("negative mental-health screen present",),
            )
        return None

    if "smoker" in desc or "nicotine" in desc or "tobacco use" in desc:
        if any(term in desc for term in ("never smoker", "never used tobacco", "no tobacco use")):
            return None
        if _has_history_text(context, ("never smoker", "never used tobacco")):
            return DiagnosisSupportReview(
                "conflicting local evidence",
                "Local social-history facts say never smoker/never used tobacco.",
                ("never-smoker history present",),
            )
        return None

    return None


def _build_diagnosis_support_reviews(
    vault_root: Path,
    person_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, DiagnosisSupportReview]:
    context = _history_support_context(vault_root, person_id, entries)
    reviews: dict[str, DiagnosisSupportReview] = {}
    for fact in context.facts:
        description = _history_fact_description_text(fact)
        category = str(fact.get("category") or "").casefold()
        review = _support_review_for_description(description, category, context)
        if review is None:
            continue
        reviews.setdefault(_support_review_key(description), review)
    return reviews


def _history_fact_support_marker(
    fact: dict[str, Any],
    support_reviews: dict[str, DiagnosisSupportReview],
) -> str:
    review = support_reviews.get(_support_review_key(_history_fact_description_text(fact)))
    if review is None:
        return ""
    return f" [[#Diagnosis Support Review|support review: {review.status}]]"


def _issue_support_marker(
    issue: str,
    support_reviews: dict[str, DiagnosisSupportReview],
) -> str:
    review = support_reviews.get(_support_review_key(issue))
    if review is None:
        return ""
    return f" [[#Diagnosis Support Review|support review: {review.status}]]"


def _best_history_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        items,
        key=lambda item: (
            len(_history_fact_description_text(item["fact"])),
            _history_fact_description_text(item["fact"]).casefold(),
        ),
    )


def _history_render_dedup_key(
    item: dict[str, Any],
    *,
    include_label: bool,
) -> tuple[str, ...]:
    fact = item["fact"]
    entry = item["entry"]
    key = [
        _history_fact_date(fact, entry),
        _history_fact_value(fact) or "",
        _history_fact_review_marker(fact),
    ]
    if include_label:
        key.append(_history_fact_render_dedup_description(fact))
    return tuple(key)


def _deduplicate_history_render_items(
    items: list[dict[str, Any]],
    *,
    include_label: bool,
) -> list[list[dict[str, Any]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in sorted(items, key=_history_sort_key):
        key = _history_render_dedup_key(item, include_label=include_label)
        grouped.setdefault(key, []).append(item)
    return list(grouped.values())


def _history_issue_reported_dates(items: list[dict[str, Any]]) -> str:
    return ", ".join(_history_digest_sorted_dates(items))


def _normalize_history_issue_text(text: str) -> str:
    normalized = re.sub(r"\s+\(ICD-10 [^)]+\)$", "", text).strip()
    return " ".join(normalized.split()).casefold()


def _history_digest_sorted_dates(items: list[dict[str, Any]]) -> list[str]:
    return sorted({_history_fact_date(item["fact"], item["entry"]) for item in items})


def _format_digest_date_summary(dates: list[str]) -> str:
    if not dates:
        return "Date not recorded."
    if len(dates) == 1:
        return f"First noted: {dates[0]}."
    return f"First noted: {dates[0]}. Last mentioned: {dates[-1]}."


def _history_digest_date_summary(items: list[dict[str, Any]]) -> str:
    return _format_digest_date_summary(_history_digest_sorted_dates(items))


def _render_medical_history_digest(
    items: list[dict[str, Any]],
    support_reviews: dict[str, DiagnosisSupportReview],
) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    markers: dict[str, str] = {}
    for deduplicated_items in _deduplicate_history_render_items(items, include_label=True):
        best_item = _best_history_item(deduplicated_items)
        label = _history_digest_label(best_item["fact"])
        if label is None:
            continue
        grouped.setdefault(label, []).append(best_item)
        markers[label] = _history_fact_support_marker(best_item["fact"], support_reviews)

    lines = ["## Digest", ""]
    if not grouped:
        lines.append(
            "- No active chronic background conditions identified"
            " from the current conservative allowlist."
        )
        lines.append("")
        return lines

    dates_by_label = {
        label: _history_digest_sorted_dates(label_items) for label, label_items in grouped.items()
    }

    for label in sorted(grouped, key=lambda label: (dates_by_label[label][0], label.casefold())):
        lines.append(
            f"- {label}{markers[label]}. {_format_digest_date_summary(dates_by_label[label])}"
        )
    lines.append("")
    return lines


def _history_fact_hidden_from_summary(fact: dict[str, Any]) -> bool:
    return (
        _history_fact_description_text(fact)
        .casefold()
        .startswith(("no known", "no active problems", "no medications"))
    )


def _is_encounter_metadata_fact(fact: dict[str, Any]) -> bool:
    description = _history_fact_description_text(fact).casefold()
    category = str(fact.get("category") or "").casefold()
    medical_procedure_terms = (
        "biopsy",
        "emb ",
        "surgery",
        "resection",
        "excision",
        "implant",
        "repair",
        "reconstruction",
        "transplant",
        "cryo",
        "ablation",
        "dilation",
        "aspiration",
    )
    encounter_starters = (
        "external films for comparison",
        "historical visit scan",
        "office visit",
        "procedure visit",
        "virtual office visit",
        "virtual visit",
        "hospital encounter",
        "establish care",
        "reason for visit",
        "annual exam",
        "annual gyn exam",
        "annual wellness",
        "follow-up for",
        "follow up for",
    )
    is_pure_encounter = any(description.startswith(p) for p in encounter_starters)
    if is_pure_encounter and not any(t in description for t in medical_procedure_terms):
        return True
    if category == "other" and any(
        m in description for m in ("npi:", "pcp", "provider", ", md ", ", arnp ", ", do ", ", pa ")
    ):
        return True
    return False


def _suppress_subsumed_same_day_history_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        date = _history_fact_date(item["fact"], item["entry"])
        if _history_fact_value(item["fact"]) is not None:
            continue
        by_date.setdefault(date, []).append(item)

    subsumed_indices: set[int] = set()
    for same_day in by_date.values():
        for idx, item in enumerate(same_day):
            desc = _history_fact_dedup_description(item["fact"])
            desc_normalized = _normalize_punctuation_for_subsumption(desc)
            if len(desc_normalized) < 8:
                continue
            for other_idx, other in enumerate(same_day):
                if other_idx == idx:
                    continue
                other_desc = _history_fact_dedup_description(other["fact"])
                other_normalized = _normalize_punctuation_for_subsumption(other_desc)
                if len(other_normalized) < 8 or other_normalized == desc_normalized:
                    continue
                if desc_normalized in other_normalized:
                    actual_idx = items.index(item)
                    subsumed_indices.add(actual_idx)
                    break

    return [item for i, item in enumerate(items) if i not in subsumed_indices]


def _normalize_punctuation_for_subsumption(text: str) -> str:
    return text.replace(" — ", " ").replace(" —", " ").replace("— ", " ").replace(" / ", " ")


def _history_fact_is_same_day_composite(
    item: dict[str, Any],
    same_day_items: list[dict[str, Any]],
) -> bool:
    """Return True when `item`'s description is a composite concatenation of
    2+ DISTINCT shorter same-day facts (e.g. "Mixed hyperlipidemia Encounter
    for screening..." composed of two separate facts also present that day).

    Counts distinct component texts, not raw match occurrences: a source
    extraction defect that repeats one short fragment many times on the same
    date (e.g. a truncated duplicate of a longer fact) must not let
    that single repeated fragment masquerade as 2+ distinct components and
    wrongly mark the longer, correct fact as composite/redundant."""
    fact = item["fact"]
    if _history_fact_value(fact) is not None:
        return False

    composite = _history_fact_dedup_description(fact)
    if not composite or len(composite) < 24:
        return False

    distinct_component_matches: set[str] = set()
    for candidate in same_day_items:
        if candidate is item:
            continue
        candidate_fact = candidate["fact"]
        if _history_fact_value(candidate_fact) is not None:
            continue
        candidate_text = _history_fact_dedup_description(candidate_fact)
        if len(candidate_text) < 8 or candidate_text == composite:
            continue
        if candidate_text in composite:
            distinct_component_matches.add(candidate_text)
    return len(distinct_component_matches) >= 2


def _suppress_composite_same_day_history_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_date.setdefault(_history_fact_date(item["fact"], item["entry"]), []).append(item)

    filtered: list[dict[str, Any]] = []
    for same_day_items in by_date.values():
        for item in same_day_items:
            if _history_fact_is_same_day_composite(item, same_day_items):
                continue
            filtered.append(item)
    return filtered


def _history_is_retrospective_context(fact: dict[str, Any]) -> bool:
    description = _history_fact_description_text(fact).casefold()
    category = str(fact.get("category") or "").casefold()
    context_markers = (
        "history of ",
        "personal history of ",
        "former ",
        "pmh",
        "s/p ",
        "status post ",
        "long term use of ",
        "long term current use of ",
        "current smoker",
        "current use of ",
        "no known ",
        "no active problems documented",
        "no medications documented",
    )
    if description.startswith(context_markers):
        return True
    if "(history)" in description or "history)" in description:
        return True
    if "pmh" in description:
        return True
    if category in {"social_history", "allergy"}:
        return True
    return False


def _history_render_detail(
    *,
    issue: str,
    fact: dict[str, Any],
    support_reviews: dict[str, DiagnosisSupportReview],
    summary_condition_labels: dict[str, str],
) -> str | None:
    label = _history_summary_fact_label(fact, summary_condition_labels)
    value = _history_fact_value(fact)
    review_marker = _history_fact_review_marker(fact)
    support_marker = _history_fact_support_marker(fact, support_reviews)
    fact_issue_key = _history_summary_issue_key(
        _history_fact_description_text(fact),
        str(fact.get("category") or "").casefold(),
    )
    issue_key = _history_summary_issue_key(issue, str(fact.get("category") or "").casefold())
    if fact_issue_key == issue_key:
        if value:
            return f"{value}{review_marker}{support_marker}"
        return None
    detail = f" — {value}" if value else ""
    return f"{label}{review_marker}{support_marker}{detail}"


def _render_history_date_grouped_details(
    *,
    issue: str,
    items: list[dict[str, Any]],
    support_reviews: dict[str, DiagnosisSupportReview],
    summary_condition_labels: dict[str, str],
) -> list[str]:
    by_date: dict[str, list[str]] = {}
    for item in sorted(items, key=_history_sort_key):
        rendered = _history_render_detail(
            issue=issue,
            fact=item["fact"],
            support_reviews=support_reviews,
            summary_condition_labels=summary_condition_labels,
        )
        if rendered is None:
            continue
        by_date.setdefault(_history_fact_date(item["fact"], item["entry"]), []).append(rendered)
    lines: list[str] = []
    for date in sorted(by_date):
        details = by_date[date]
        lines.append(f"- {date}")
        for detail in details:
            lines.append(f"  - {detail}")
    return lines


def _history_medical_area(
    item: dict[str, Any],
    summary_condition_labels: dict[str, str],
) -> tuple[str, str]:
    fact = item["fact"]
    description = _history_fact_description_text(fact).casefold()
    category = str(fact.get("category") or "").casefold()
    issue_rules = (
        # Bilateral knee before right/left so shared findings don't get mis-routed.
        (
            ("bilateral knee", "patellofemoral", "both knees"),
            "Musculoskeletal",
            "Bilateral Knee",
        ),
        (
            ("right knee", "patella", "popliteal", "quadriceps tendon"),
            "Musculoskeletal",
            "Right Knee",
        ),
        (("left knee",), "Musculoskeletal", "Left Knee"),
        (("left wrist",), "Musculoskeletal", "Left Wrist"),
        (("left shoulder",), "Musculoskeletal", "Left Shoulder"),
        (("left plantar", "heel", "achilles", "plantaris"), "Musculoskeletal", "Left Foot / Heel"),
        (
            (
                "colonoscopy",
                "polypectomy",
                "tubular adenoma",
                "hyperplastic polyp",
                "colon polyp",
                "sigmoid",
                "splenic flexure",
                "rectum",
            ),
            "Gastroenterology",
            "Colonoscopy and Colon Polyps",
        ),
        (
            (
                "egd",
                "barrett",
                "gastritis",
                "hiatal hernia",
                "gerd",
                "esophagus",
                "stomach",
                "heartburn",
                "reflux",
            ),
            "Gastroenterology",
            "Upper GI / Reflux",
        ),
        (
            ("familial hypercholesterolemia", "hyperlipidemia", "cholesterol"),
            "Cardiovascular",
            "Lipids and Vascular Risk",
        ),
        (
            (
                "preventative health care",
                "preventive health care",
                "encounter for breast and pelvic examination",
                "well woman exam with routine gynecological exam",
                "physical exam",
            ),
            "Prevention",
            "Exam History",
        ),
        (
            ("postprocedural states", "post procedural states"),
            "Procedures",
            "Exam History",
        ),
        (("screening", "phq-2", "audit-c"), "Prevention", "Screening"),
        (
            (
                "coronary",
                "cardiac",
                "stemi",
                "atrial fibrillation",
                "carotid",
                "vascular",
                "aortic",
                "rvsp",
            ),
            "Cardiovascular",
            "Cardiovascular",
        ),
        (("varicose vein", "venous"), "Cardiovascular", "Venous Disease"),
        (("prostate", "bladder", "urinary", "hematuria", "bph"), "Genitourinary", "Genitourinary"),
        (("postnasal drip", "sinus", "rhinitis", "nasal"), "ENT", "Upper Airway / Sinus"),
        (("rash", "dermatitis", "cellulitis", "skin"), "Dermatology", "Skin and Soft Tissue"),
        (("cataract", "optic", "retina", "visual field", "eye"), "Ophthalmology", "Ophthalmology"),
        (("hearing", "cerumen"), "ENT", "ENT"),
        (
            ("smoker", "tobacco", "alcohol", "exercise", "marital status", "language", "address"),
            "Social / Preventive",
            "Social History",
        ),
        (
            (
                "encounter for general adult medical examination without abnormal findings",
                "annual wellness visit",
                "well adult exam",
                "general medical exam",
            ),
            "Prevention",
            "Exam History",
        ),
        (
            ("immunization", "vaccination", "influenza", "moderna", "vaccine"),
            "Prevention",
            "Immunizations",
        ),
        (
            ("anxiety", "attention deficit", "adhd", "suicidal", "psychotic", "depressive"),
            "Psychiatry",
            "Mental Health",
        ),
        (("fracture of", "foreign body", "injury of"), "Musculoskeletal", "Injury"),
        (("cough", "dyspnea", "shortness of breath"), "Respiratory", "Respiratory Symptoms"),
        (("lab", "hemoglobin", "hematocrit", "troponin", "psa"), "Diagnostics", "Lab Findings"),
    )
    for keywords, area, issue in issue_rules:
        if any(keyword in description for keyword in keywords):
            return area, issue
    # "depression" alone is ambiguous with unrelated clinical phrases ("ST
    # depression" on an EKG, "respiratory depression"); only route to
    # Psychiatry when neither false-positive phrase is present.
    if "depression" in description and not any(
        phrase in description for phrase in ("st depression", "respiratory depression")
    ):
        return "Psychiatry", "Mental Health"
    # "fall" as a bare substring would also match unrelated terms like
    # "fallopian"; require it as a standalone word.
    if _history_term_matches(description, "fall"):
        return "Musculoskeletal", "Injury"
    if category == "vital_sign":
        return "Measurements", "Vital Signs"
    if category == "medication":
        return "Medications", "Medication Changes"
    if category == "follow_up":
        return "Plans", "Follow-up"
    if category == "procedure":
        return "Procedures", _history_fact_label(fact)
    if category == "diagnosis":
        return "Diagnoses", _history_summary_fact_label(fact, summary_condition_labels)
    if category == "imaging_finding":
        return "Diagnostics", "Imaging Findings"
    if category == "lab":
        return "Diagnostics", "Lab Findings"
    return "Other", _history_fact_label(fact)


def _history_area_order(area: str) -> tuple[int, str]:
    order = {
        "Gastroenterology": 1,
        "Musculoskeletal": 2,
        "Cardiovascular": 3,
        "Respiratory": 4,
        "Genitourinary": 5,
        "Psychiatry": 6,
        "Diagnoses": 7,
        "Medications": 8,
        "Measurements": 9,
        "Diagnostics": 10,
        "Prevention": 11,
        "Social / Preventive": 12,
        "Plans": 13,
        "Ophthalmology": 14,
        "ENT": 15,
        "Other": 16,
    }
    return order.get(area, 99), area.casefold()


def _group_history_items_by_description(
    items: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for item in sorted(items, key=_history_sort_key):
        icd_10_code = str(item["fact"].get("icd_10_code") or "")
        key = "|".join((_history_fact_dedup_description(item["fact"]), icd_10_code.casefold()))
        best_item = item if key not in grouped else _best_history_item(grouped[key] + [item])
        labels[key] = _history_fact_label(best_item["fact"])
        grouped.setdefault(key, []).append(item)
    sorted_keys = sorted(grouped, key=lambda key: labels[key].casefold())
    return [(labels[key], grouped[key]) for key in sorted_keys]


def _render_unreviewed_history_sources(entries: list[dict[str, Any]]) -> list[str]:
    unreviewed = [
        entry
        for entry in entries
        if entry.get("record_type") == "history"
        and entry.get("verification_status") != "reviewed_no_facts"
        and (entry.get("verification_status") != "accepted" or not entry.get("accepted_facts"))
    ]
    if not unreviewed:
        return []

    lines = [
        "## Sources Still Needing Review",
        "",
        "> [!warning]",
        "> These retained source notes either have no accepted structured history facts or "
        "still carry an unresolved review status.",
        "",
    ]
    for entry in sorted(
        unreviewed,
        key=lambda e: (e.get("event_date") or "9999", e.get("title") or ""),
    ):
        date = entry.get("event_date") or "undated"
        title = _markdown_table_cell(entry.get("title")) or "History note"
        status = _markdown_table_cell(entry.get("verification_status")) or "needs_review"
        accepted_facts = entry.get("accepted_facts")
        fact_count = len(accepted_facts) if isinstance(accepted_facts, list) else 0
        detail = (
            f"{fact_count} accepted fact{'s' if fact_count != 1 else ''}; review unresolved"
            if fact_count
            else "no accepted facts"
        )
        lines.append(f"- {date}: {title} ({status}; {detail})")
    lines.append("")
    return lines


def _history_vital_numeric_value(fact: dict[str, Any]) -> float | None:
    raw_value = fact.get("value")
    if raw_value is None:
        return None
    try:
        return float(str(raw_value).strip())
    except ValueError:
        return None


def _format_history_vital_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


_BMI_PLAUSIBLE_RANGE = (10.0, 50.0)


def _plausible_history_vital_items(
    measurement_key: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if measurement_key != "bmi":
        return items

    filtered: list[dict[str, Any]] = []
    low, high = _BMI_PLAUSIBLE_RANGE
    for item in items:
        number = _history_vital_numeric_value(item["fact"])
        if number is None:
            filtered.append(item)
            continue
        if low <= number <= high:
            filtered.append(item)
            continue
        logger.warning(
            "Ignoring implausible BMI value %s from %s while computing summary range",
            _history_fact_value(item["fact"]) or number,
            item["entry"].get("source_path") or item["entry"].get("note_stem") or "unknown source",
        )
    return filtered


def _history_vital_range(measurement_key: str, items: list[dict[str, Any]]) -> str:
    items = _plausible_history_vital_items(measurement_key, items)
    if not items:
        return "-"

    units = {
        _markdown_table_cell(item["fact"].get("unit")) or ""
        for item in items
        if _history_fact_value(item["fact"]) is not None
    }
    if len(units) == 1:
        numbers = [_history_vital_numeric_value(item["fact"]) for item in items]
        if all(number is not None for number in numbers):
            unit = next(iter(units))
            min_value = min(number for number in numbers if number is not None)
            max_value = max(number for number in numbers if number is not None)
            if min_value == max_value:
                value = _format_history_vital_number(min_value)
            else:
                value = (
                    f"{_format_history_vital_number(min_value)}-"
                    f"{_format_history_vital_number(max_value)}"
                )
            return f"{value} {unit}".strip()

    unique_values = sorted(
        {value for item in items if (value := _history_fact_value(item["fact"])) is not None}
    )
    if not unique_values:
        return f"{len(items)} reading(s)"
    if len(unique_values) == 1:
        return unique_values[0]
    return f"{len(unique_values)} values"


def _render_vital_signs_snapshot(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return []

    grouped: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for deduplicated_items in _deduplicate_history_render_items(items, include_label=True):
        best_item = _best_history_item(deduplicated_items)
        key = _history_fact_dedup_description(best_item["fact"])
        grouped.setdefault(key, []).append(best_item)
        labels[key] = _history_fact_label(best_item["fact"])

    lines = [
        "## Vital Signs Snapshot",
        "",
        "| Measurement | Latest | Date | Earlier range | Readings |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key in sorted(grouped, key=lambda grouped_key: labels[grouped_key].casefold()):
        measurement_items = sorted(grouped[key], key=_history_sort_key)
        latest = measurement_items[-1]
        latest_date = _history_fact_date(latest["fact"], latest["entry"])
        earlier_items = [
            item
            for item in measurement_items[:-1]
            if _history_fact_date(item["fact"], item["entry"]) != latest_date
        ]
        latest_value = _history_fact_value(latest["fact"]) or "recorded"
        latest_cell = f"{latest_value}{_history_fact_review_marker(latest['fact'])}"
        lines.append(
            "| "
            f"{_markdown_table_cell(labels[key])} | "
            f"{_markdown_table_cell(latest_cell)} | "
            f"{_markdown_table_cell(latest_date)} | "
            f"{_markdown_table_cell(_history_vital_range(key, earlier_items))} | "
            f"{len(measurement_items)} |"
        )
    lines.append("")
    return lines


def _render_diagnosis_support_review_section(
    support_reviews: dict[str, DiagnosisSupportReview],
) -> list[str]:
    if not support_reviews:
        return []

    lines = [
        "## Diagnosis Support Review",
        "",
        "> [!warning]",
        "> These are derived review prompts only. They do not change canonical facts.",
        "",
        "| Fact | Review | Why | Local evidence |",
        "| --- | --- | --- | --- |",
    ]
    for fact_key, review in sorted(support_reviews.items()):
        evidence = "; ".join(review.evidence) if review.evidence else "-"
        lines.append(
            "| "
            f"{_markdown_table_cell(fact_key)} | "
            f"{_markdown_table_cell(review.status)} | "
            f"{_markdown_table_cell(review.reason)} | "
            f"{_markdown_table_cell(evidence)} |"
        )
    lines.append("")
    return lines


def _is_pre_procedure_order_fact(fact: dict[str, Any]) -> bool:
    """Return True for follow-up orders and prep-kit medications that precede a procedure."""
    cat = str(fact.get("category") or "").casefold()
    desc = _history_fact_description_text(fact).casefold()
    if cat == "follow_up" and re.search(r"\bordered\b", desc):
        return True
    if cat == "medication" and (
        "prep kit" in desc or ("prescribed for" in desc and "prep" in desc)
    ):
        return True
    return False


def _suppress_superseded_procedure_orders(
    grouped_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Suppress orders/prep-kits when a later non-order result exists in the same group."""
    order_indices: set[int] = set()
    non_order_dates: list[str] = []
    for idx, item in enumerate(grouped_items):
        if _is_pre_procedure_order_fact(item["fact"]):
            order_indices.add(idx)
        else:
            non_order_dates.append(_history_fact_date(item["fact"], item["entry"]))

    if not order_indices or not non_order_dates:
        return grouped_items

    latest_non_order = max(non_order_dates)
    suppressed: set[int] = set()
    for idx in order_indices:
        item = grouped_items[idx]
        order_date = _history_fact_date(item["fact"], item["entry"])
        if order_date < latest_non_order:
            suppressed.add(idx)

    if not suppressed:
        return grouped_items
    return [item for i, item in enumerate(grouped_items) if i not in suppressed]


def _issue_is_chronic_allowlisted(items: list[dict[str, Any]]) -> bool:
    """Return True if any item in the group maps to an ADR-029 chronic allowlist label."""
    return any(_history_digest_chronic_label(item["fact"]) is not None for item in items)


def _history_bare_chronic_repeat_label(
    item: dict[str, Any], issue: str, chronic_label: str | None
) -> str | None:
    """Return `chronic_label` when `item` is a bare repeated mention of that
    condition inside a differently-named, multi-condition issue bucket (e.g. "High
    cholesterol" inside the "Lipids and Vascular Risk" issue). Returns None when
    `chronic_label` is None, the fact carries a value, or the issue bucket is
    already named after the condition itself (already handled by the existing
    same-name detail suppression in `_history_render_detail`)."""
    if chronic_label is None:
        return None
    fact = item["fact"]
    if _history_fact_value(fact) is not None:
        return None
    category = str(fact.get("category") or "").casefold()
    fact_issue_key = _history_summary_issue_key(_history_fact_description_text(fact), category)
    if fact_issue_key == _history_summary_issue_key(issue, category):
        return None
    return chronic_label


def _rollup_chronic_event_repeats(
    issue: str,
    event_items: list[dict[str, Any]],
    support_reviews: dict[str, DiagnosisSupportReview],
) -> tuple[list[dict[str, Any]], list[str], set[str]]:
    """Collapse 2+ bare repeated mentions of the same chronic condition within an
    issue bucket into one first/last-mention summary line. Single
    occurrences and concrete (value-bearing or non-chronic) items still render
    individually via the normal date-grouped path. Also returns every chronic
    label evidenced anywhere in `event_items` (rolled up or not), so callers can
    detect Historical-context rows that restate a condition already covered by
    a rendered Event in this issue."""
    by_label: dict[str, list[dict[str, Any]]] = {}
    individual: list[dict[str, Any]] = []
    event_chronic_labels: set[str] = set()
    for item in event_items:
        chronic_label = _history_digest_chronic_label(item["fact"])
        if chronic_label is not None:
            event_chronic_labels.add(chronic_label)
        label = _history_bare_chronic_repeat_label(item, issue, chronic_label)
        if label is None:
            individual.append(item)
        else:
            by_label.setdefault(label, []).append(item)

    rollup_lines: list[str] = []
    for label in sorted(by_label, key=str.casefold):
        items = by_label[label]
        dates = {_history_fact_date(item["fact"], item["entry"]) for item in items}
        if len(dates) < 2:
            individual.extend(items)
            continue
        marker = _history_fact_support_marker(items[0]["fact"], support_reviews)
        rollup_lines.append(f"- {label}{marker}. {_history_digest_date_summary(items)}")
    return individual, rollup_lines, event_chronic_labels


_IDENTICAL_REPEAT_MIN_DATES = 3


def _rollup_identical_repeats(
    issue: str,
    items: list[dict[str, Any]],
    support_reviews: dict[str, DiagnosisSupportReview],
    summary_condition_labels: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collapse a value-free fact restated verbatim on 3+ dates into one line.

    Identity is the exact render-dedup description, not a semantic match
    (ADR-018), so only word-for-word restatements collapse. The line keeps the
    first and last date and the mention count; fewer than three dates stay
    individually dated. A fact that restates its own issue heading is left to
    the existing same-name suppression, so Part B can still drop that section.
    """
    by_key: dict[str, list[dict[str, Any]]] = {}
    individual: list[dict[str, Any]] = []
    for item in items:
        fact = item["fact"]
        category = str(fact.get("category") or "").casefold()
        restates_issue = _history_summary_issue_key(
            _history_fact_description_text(fact), category
        ) == _history_summary_issue_key(issue, category)
        if restates_issue or _history_fact_value(fact) is not None:
            individual.append(item)
            continue
        by_key.setdefault(_history_fact_render_dedup_description(item["fact"]), []).append(item)

    rollup_lines: list[str] = []
    for key_items in by_key.values():
        dates = _history_digest_sorted_dates(key_items)
        if len(dates) < _IDENTICAL_REPEAT_MIN_DATES:
            individual.extend(key_items)
            continue
        fact = key_items[0]["fact"]
        label = _history_summary_fact_label(fact, summary_condition_labels)
        marker = _history_fact_support_marker(fact, support_reviews)
        rollup_lines.append(
            f"- {label}{marker}. {_format_digest_date_summary(dates)} ({len(dates)} mentions)"
        )
    individual.sort(key=lambda item: items.index(item))
    return individual, sorted(rollup_lines, key=str.casefold)


# ADR-018 boundary note: this is a deliberately narrow, closed
# exception, not a pattern to extend. ADR-018 prohibits deterministic code
# from growing source-specific clinical phrase lists or keyword taxonomies
# over free-text descriptions. The " — " split below reuses this file's own
# existing label/value rendering convention (see `_history_render_detail`'s
# `f" — {value}"`), and this is the only status-equivalence pair this
# function will ever recognize. If a future need arises to treat additional
# status words (e.g. "stable", "unchanged", "ongoing", "same dose") as
# equivalent, route that through the Clinical Interpreter / fm-history-extract
# skill emitting a structured status field on the fact — do not add more
# synonyms here.
_MEDICATION_ONGOING_STATUS_TERMS = frozenset({"active", "continued"})


def _medication_identity_and_status(fact: dict[str, Any]) -> tuple[str, str | None]:
    """Split a medication fact's description on the last " — " separator into
    (identity, status), e.g. "Omeprazole 20 mg daily — active" -> ("omeprazole
    20 mg daily", "active"). Descriptions with no separator have no status; the
    whole normalized description is the identity. A fact with no description at
    all gets an identity unique to that fact object rather than a shared
    placeholder, so two distinct medications missing a description are never
    mistaken for repeats of each other."""
    raw_description = _markdown_table_cell(fact.get("description"))
    if not raw_description:
        return f"__no_description_{id(fact)}__", None
    identity, separator, status = raw_description.rpartition(" — ")
    if not separator:
        return " ".join(raw_description.split()).casefold(), None
    return " ".join(identity.split()).casefold(), " ".join(status.split()).casefold()


def _medication_status_class(status: str | None) -> str | None:
    """Collapse the closed active/continued synonym pair (see boundary note
    above `_MEDICATION_ONGOING_STATUS_TERMS`) into one "no change from the
    prior mention" class; every other status (PRN, discontinued, etc.) stays
    distinct so a real status change is never suppressed."""
    if status in _MEDICATION_ONGOING_STATUS_TERMS:
        return "ongoing"
    return status


def _suppress_unchanged_medication_repeats(
    event_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Suppress medication-category Events that just restate the same medication
    identity and status class as that medication's immediately preceding mention.
    A dose/frequency change produces a different identity string and is never
    suppressed; a real status change (e.g. to discontinued) is kept too."""
    medication_indices = [
        idx
        for idx, item in enumerate(event_items)
        if str(item["fact"].get("category") or "").casefold() == "medication"
    ]
    if len(medication_indices) < 2:
        return event_items

    # `event_items` is already date-ascending (it's built from
    # `_deduplicate_history_render_items`, which sorts by `_history_sort_key`
    # before grouping), so `medication_indices` needs no re-sort.
    last_status_by_identity: dict[str, str | None] = {}
    suppressed: set[int] = set()
    for idx in medication_indices:
        identity, status = _medication_identity_and_status(event_items[idx]["fact"])
        status_class = _medication_status_class(status)
        if (
            identity in last_status_by_identity
            and last_status_by_identity[identity] == status_class
        ):
            suppressed.add(idx)
        else:
            last_status_by_identity[identity] = status_class

    if not suppressed:
        return event_items
    return [item for i, item in enumerate(event_items) if i not in suppressed]


def _render_issue_grouped_history_sections(
    items: list[dict[str, Any]],
    support_reviews: dict[str, DiagnosisSupportReview],
    summary_condition_labels: dict[str, str],
) -> list[str]:
    if not items:
        return []

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        area, issue = _history_medical_area(item, summary_condition_labels)
        grouped.setdefault((area, issue), []).append(item)

    by_area: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {}
    for (area, issue), grouped_items in grouped.items():
        by_area.setdefault(area, []).append((issue, grouped_items))

    lines: list[str] = ["## Medical Areas", ""]
    for area in sorted(by_area, key=_history_area_order):
        lines.append(f"### {area}")
        for issue, grouped_items in sorted(
            by_area[area],
            key=lambda item: (
                min(_history_sort_key(grouped_item)[0] for grouped_item in item[1]),
                item[0].casefold(),
            ),
        ):
            # Part A: remove pre-procedure orders superseded by later results.
            filtered_items = _suppress_superseded_procedure_orders(grouped_items)

            event_items: list[dict[str, Any]] = []
            context_items: list[dict[str, Any]] = []
            for deduplicated_items in _deduplicate_history_render_items(
                filtered_items,
                include_label=True,
            ):
                best_item = _best_history_item(deduplicated_items)
                if _history_is_retrospective_context(best_item["fact"]):
                    context_items.append(best_item)
                else:
                    event_items.append(best_item)

            # suppress medication Events that just restate the same
            # medication identity and active/continued/PRN status as the
            # immediately preceding mention; keep additions, stops, and
            # dose/frequency or real status changes. Part B below still counts
            # the original `event_items`/`context_items` (unaffected).
            medication_filtered_event_items = _suppress_unchanged_medication_repeats(event_items)

            # roll up bare repeated chronic-condition mentions inside
            # multi-condition issue buckets into one first/last-mention summary
            # line instead of one bullet per visit date; concrete findings keep
            # their individual dates. `event_chronic_labels` covers every
            # chronic condition evidenced by an Event in this issue (rolled up
            # or not), reused below to drop redundant Historical context rows.
            individual_event_items, rollup_lines, event_chronic_labels = (
                _rollup_chronic_event_repeats(
                    issue, medication_filtered_event_items, support_reviews
                )
            )
            individual_event_items, repeat_lines = _rollup_identical_repeats(
                issue, individual_event_items, support_reviews, summary_condition_labels
            )
            rendered_events = (
                rollup_lines
                + repeat_lines
                + list(
                    _render_history_date_grouped_details(
                        issue=issue,
                        items=individual_event_items,
                        support_reviews=support_reviews,
                        summary_condition_labels=summary_condition_labels,
                    )
                )
            )

            # scoped to ADR-029 chronic-allowlist conditions only (same
            # boundary as the rollup above, not general redundancy detection).
            # A "Historical context" row that only restates a chronic condition
            # for which this same issue already has concrete Event evidence adds
            # no distinct information; drop it. Context stays unfiltered when
            # there is no Event evidence at all (it is then the only available
            # evidence for the issue).
            context_candidates = (
                [
                    item
                    for item in context_items
                    if _history_digest_chronic_label(item["fact"]) not in event_chronic_labels
                ]
                if rendered_events
                else context_items
            )
            rendered_context = list(
                _render_history_date_grouped_details(
                    issue=issue,
                    items=context_candidates,
                    support_reviews=support_reviews,
                    summary_condition_labels=summary_condition_labels,
                )
            )

            # Part B: suppress noisy per-visit chronic repeats (3+) already in the Digest.
            if (
                not rendered_events
                and not rendered_context
                and len(event_items) + len(context_items) >= 3
                and _issue_is_chronic_allowlisted(filtered_items)
            ):
                continue

            lines.append(f"#### {issue}{_issue_support_marker(issue, support_reviews)}")
            lines.append(f"Reported on: {_history_issue_reported_dates(grouped_items)}")
            lines.append("")
            if rendered_events:
                lines.append("Events")
                lines.extend(rendered_events)
                lines.append("")
            if rendered_context:
                lines.append("Historical context")
                lines.extend(rendered_context)
                lines.append("")
    return lines


def _render_medical_history_report_body(
    entries: list[dict[str, Any]],
    support_reviews: dict[str, DiagnosisSupportReview] | None = None,
) -> str:
    support_reviews = support_reviews or {}
    fact_items: list[dict[str, Any]] = []
    vital_items: list[dict[str, Any]] = []
    excluded_low_confidence = 0
    for entry in entries:
        excluded_low_confidence += int(entry.get("excluded_low_confidence_count") or 0)
        for fact in entry.get("accepted_facts") or []:
            if _history_fact_hidden_from_summary(fact):
                continue
            if _is_encounter_metadata_fact(fact):
                continue
            item = {"entry": entry, "fact": fact}
            if str(fact.get("category") or "").casefold() == "vital_sign":
                vital_items.append(item)
            else:
                fact_items.append(item)
    fact_items = _suppress_composite_same_day_history_items(fact_items)
    fact_items = _suppress_subsumed_same_day_history_items(fact_items)
    vital_items = _suppress_composite_same_day_history_items(vital_items)
    summary_condition_labels = _build_history_summary_condition_labels(fact_items)

    lines: list[str] = []
    if not fact_items and not vital_items:
        lines.extend(
            [
                "## Accepted Medical History",
                "",
                "_No accepted structured history facts yet._",
                "",
            ]
        )
    else:
        lines.extend(_render_medical_history_digest(fact_items, support_reviews))
        lines.extend(
            _render_issue_grouped_history_sections(
                fact_items,
                support_reviews,
                summary_condition_labels,
            )
        )

    lines.extend(_render_diagnosis_support_review_section(support_reviews))
    lines.extend(_render_vital_signs_snapshot(vital_items))

    if excluded_low_confidence > 0:
        lines.extend(
            [
                "> [!note]",
                f"> {excluded_low_confidence} low-confidence finding(s) were not included.",
                "",
            ]
        )

    lines.extend(_render_unreviewed_history_sources(entries))
    return "\n".join(lines).rstrip()


def _record_rejected_lab_observations(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    processed_source_path: str,
    rejected: list[dict[str, Any]],
) -> None:
    """Record C-CDA observations that failed validation so none disappear silently."""
    if not rejected:
        return
    summary = "; ".join(
        f"{item['test_name']!r} ({item['unit']}): {item['reason']}" for item in rejected[:10]
    )
    log_audit_event(
        conn,
        event_type=AUDIT_LAB_OBSERVATION_REJECTED,
        subject_type="lab_result",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_AWAITING_USER_DECISION,
        message=(
            f"{len(rejected)} C-CDA lab observation(s) failed validation and were not "
            f"imported: {summary}"
        ),
    )


def _rejected_lab_matches_selector(
    rejected: dict[str, Any],
    selector: LabObservationSelector,
) -> bool:
    return all(
        rejected.get(field) == getattr(selector, field)
        for field in (
            "code_system",
            "loinc_code",
            "test_name",
            "value_text",
            "value_type",
            "unit",
            "observed_at",
            "reference_range_low",
            "reference_range_high",
        )
    )


def validate_rejected_lab_observation_resolutions(
    *,
    rejected: list[dict[str, Any]],
    resolutions: Sequence[LabObservationResolution],
    source_label: str,
) -> tuple[tuple[LabObservationResolution, tuple[int, ...]], ...]:
    """Match all attended decisions exactly before any replay mutation."""
    matched_resolutions: list[tuple[LabObservationResolution, tuple[int, ...]]] = []
    claimed_indexes: set[int] = set()
    for resolution in resolutions:
        matched_indexes = tuple(
            index
            for index, item in enumerate(rejected)
            if _rejected_lab_matches_selector(item, resolution.selector)
        )
        if len(matched_indexes) != resolution.expected_occurrences:
            raise HistorySyncError(
                "retained lab observation resolution expected "
                f"{resolution.expected_occurrences} exact match(es), found "
                f"{len(matched_indexes)} in {source_label}"
            )
        if claimed_indexes.intersection(matched_indexes):
            raise HistorySyncError(
                f"retained lab observation resolution overlaps another decision in {source_label}"
            )
        claimed_indexes.update(matched_indexes)
        matched_resolutions.append((resolution, matched_indexes))
    return tuple(matched_resolutions)


def _apply_rejected_lab_observation_resolutions(
    *,
    conn: sqlite3.Connection,
    person_id: str,
    processed_source_path: str,
    rejected: list[dict[str, Any]],
    resolutions: Sequence[LabObservationResolution],
    source_checksum: str,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Apply exact attended decisions without retyping source medical fields."""
    from famed.lab_rejections import lab_decision_message

    accepted_rows: list[dict[str, Any]] = []
    matched_resolutions = validate_rejected_lab_observation_resolutions(
        rejected=rejected,
        resolutions=resolutions,
        source_label=processed_source_path,
    )
    claimed_indexes = {
        index for _, matched_indexes in matched_resolutions for index in matched_indexes
    }
    for resolution, matched_indexes in matched_resolutions:
        if resolution.action == "accept_verbatim":
            for index in matched_indexes:
                raw_mapping = rejected[index].get("raw_mapping")
                if not isinstance(raw_mapping, dict):
                    raise HistorySyncError(
                        f"rejected observation source fields are unavailable in "
                        f"{processed_source_path}"
                    )
                reviewed_mapping = dict(raw_mapping)
                reviewed_mapping["value"] = None
                reviewed_row = lab_payload_from_mapping(
                    reviewed_mapping,
                    f"{processed_source_path}:human_resolver",
                )
                if reviewed_row not in accepted_rows:
                    accepted_rows.append(reviewed_row)

        status = STATE_QUARANTINED if resolution.action == "quarantine" else STATE_PROCESSED
        audit_resolution = (
            RESOLUTION_QUARANTINED
            if resolution.action == "quarantine"
            else RESOLUTION_USER_RESOLVED
        )
        log_audit_event(
            conn,
            event_type=AUDIT_LAB_OBSERVATION_RESOLUTION_RECORDED,
            subject_type="lab_result",
            subject_id=person_id,
            source_path=processed_source_path,
            status=status,
            resolution=audit_resolution,
            message=lab_decision_message(
                resolution,
                person_id=person_id,
                source_checksum=source_checksum,
                summary=(
                    f"Human Resolver action {resolution.action!r} applied to "
                    f"{len(matched_indexes)} exact rejected C-CDA observation(s) for "
                    f"{resolution.selector.test_name!r}; "
                    f"value_text={resolution.selector.value_text!r}; "
                    f"loinc_code={resolution.selector.loinc_code!r}; "
                    f"actor={resolution.resolved_by!r}; "
                    f"resolved_at={resolution.resolved_at}; reason={resolution.reason}"
                ),
            ),
        )
    return accepted_rows, claimed_indexes


def reprocess_retained_history_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    fact_interpreter: FactInterpreter | None = None,
    source_path_prefix: Path | None = None,
    refresh_reports: bool = True,
    source_category: str = "History",
) -> ReprocessResult:
    """Re-parse an already-retained history source file and refresh the vault note and report.

    Mirrors ``reprocess_retained_supplement_source`` for the History category.
    Supports .pdf (text-extractable), .xml (C-CDA), and .tif/.tiff (OCR) retained sources.
    Refuses to overwrite existing accepted facts unless a source-bound semantic
    interpreter is supplied, then replays extraction against the retained source.
    Marks ``medical-history-summary.md`` stale before regenerating it.
    Records ``history_source_reprocessed`` + ``dependent_output_marked_stale`` audit events.
    Raises ``HistorySyncError`` on parse failure or path mismatch.
    """
    from famed.immunization_sync import (
        immunization_document_date,
        persist_immunization_rows,
        remove_immunization_artifacts_for_source_path,
    )
    from famed.import_labs import extract_pdf_text, fill_loinc_codes, persist_lab_rows
    from famed.medication_sync import (
        fill_rxnorm_cuis,
        medication_document_date,
        persist_medication_rows,
        replace_medications_for_source_path,
        retire_empty_medication_source_notes,
        validate_empty_medication_source_note_retirement,
        write_current_medications_report,
        write_medication_conflicts_report,
    )
    from famed.reference_cache import load_loinc_index, load_rxnorm_index
    from famed.storage_contract import configured_storage_roots
    from famed.vital_sync import persist_vital_rows, vital_document_date

    suffix = source_path.suffix.lower()
    if suffix not in {".pdf", ".tif", ".tiff", ".xml"}:
        raise HistorySyncError(
            f"retained history source must be a .pdf, .tif/.tiff, or .xml file, "
            f"got: {source_path.name!r}"
        )

    person_id = infer_person_id_from_processed_history_path(
        source_path,
        processed_root,
        source_category=source_category,
    )
    if not person_id:
        raise HistorySyncError(
            f"retained history source must be under data/processed/<person_id>/{source_category}"
        )
    processed_source_path = retained_source_reference(
        root=root,
        processed_root=processed_root,
        source_path=source_path,
        source_path_prefix=source_path_prefix,
    )
    _require_semantic_replay_for_accepted_history(
        vault_root=vault_root,
        person_id=person_id,
        source_path_prefix=processed_source_path,
        fact_interpreter=fact_interpreter,
    )
    storage_roots = configured_storage_roots(root)
    lab_rows: list[dict[str, Any]] = []
    immunization_rows: list[dict[str, Any]] = []
    medication_rows: list[dict[str, Any]] = []
    vital_rows: list[dict[str, Any]] = []

    if suffix == ".xml":
        document = parse_ccda_review_document(source_path)
        document_kind = "C-CDA visit summary"
    elif suffix in {".tif", ".tiff"}:
        from famed.ocr import is_ocr_available, ocr_image_file

        if not is_ocr_available():
            raise HistorySyncError(
                "OCR support is not available. Install with: "
                "brew install tesseract && pip install pytesseract"
            )
        text = ocr_image_file(source_path)
        if not text.strip():
            raise HistorySyncError(f"OCR produced no text from {source_path.name}")
        document = parse_text_history_review_document(source_path, text)
        document_kind = "scanned image"
    else:
        text = extract_pdf_text(source_path)
        document = parse_text_history_review_document(source_path, text)
        document_kind = "visit PDF"

    checksum = source_file_checksum(source_path)
    if suffix == ".xml":
        medication_rows = extract_ccda_medication_rows(source_path, person_id)
        if not medication_rows:
            validate_empty_medication_source_note_retirement(
                vault_root=vault_root,
                person_id=person_id,
                source_paths=(processed_source_path,),
            )

    report_rel = f"Records/{person_id}/Reports/medical-history-summary.md"
    mark_dependent_output_stale(
        conn,
        output_path=report_rel,
        reason="retained history source reprocessed",
        source_path=processed_source_path,
        person_id=person_id,
    )
    if has_fhir_export_surface(conn, vault_root, person_id):
        mark_dependent_output_stale(
            conn,
            output_path=f"Records/{person_id}/Reports/fhir-export.json",
            reason="retained history source reprocessed",
            source_path=processed_source_path,
            person_id=person_id,
        )
    if suffix == ".xml":
        rejected_labs: list[dict[str, Any]] = []
        lab_rows = extract_ccda_lab_rows(source_path, person_id, rejected=rejected_labs)
        _record_rejected_lab_observations(
            conn,
            person_id=person_id,
            processed_source_path=processed_source_path,
            rejected=rejected_labs,
        )
        if lab_rows:
            fill_loinc_codes(lab_rows, load_loinc_index(storage_roots.reference))
            persist_lab_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=source_path,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type="xml",
                rows=lab_rows,
                retained_source=True,
                refresh_reports=False,
            )
        if medication_rows:
            fill_rxnorm_cuis(medication_rows, load_rxnorm_index(storage_roots.reference))
            persist_medication_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=source_path,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type="xml",
                rows=medication_rows,
                document_date=medication_document_date(
                    medication_rows,
                    document.document_date,
                    source_path.name,
                ),
                retained_source=True,
                refresh_reports=refresh_reports,
            )
        else:
            retire_empty_medication_source_notes(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_paths=(processed_source_path,),
            )
            replace_medications_for_source_path(conn, processed_source_path, [])
        vital_rows = extract_ccda_vital_rows(source_path, person_id)
        if vital_rows:
            persist_vital_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=source_path,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type="xml",
                rows=vital_rows,
                document_date=vital_document_date(
                    vital_rows,
                    document.document_date,
                    source_path.name,
                ),
            )
        immunization_rows = extract_ccda_immunization_rows(source_path, person_id)
        if immunization_rows:
            persist_immunization_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=source_path,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type="xml",
                rows=immunization_rows,
                document_date=immunization_document_date(
                    immunization_rows,
                    document.document_date,
                    source_path.name,
                ),
                retained_source=True,
                refresh_export=False,
            )
        else:
            remove_immunization_artifacts_for_source_path(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                processed_source_path=processed_source_path,
                refresh_export=False,
            )

    note_path = persist_history_document_review_note(
        conn=conn,
        vault_root=vault_root,
        person_id=person_id,
        source_file=source_path,
        processed_source_path=processed_source_path,
        checksum=checksum,
        source_type=suffix.lstrip("."),
        document=document,
        document_kind=document_kind,
        retained_source=True,
    )
    if suffix == ".xml":
        remove_linked_category_placeholder_note(
            vault_root=vault_root,
            person_id=person_id,
            category_dir_name="Immunizations",
            source_path=processed_source_path,
        )
    if refresh_reports:
        if suffix == ".xml" and lab_rows:
            refresh_lab_dependent_reports(conn, vault_root, person_id)
        else:
            refresh_fhir_dependent_export(conn, vault_root, person_id)
        if suffix == ".xml" and not medication_rows:
            write_current_medications_report(vault_root, person_id, conn)
            write_medication_conflicts_report(conn, vault_root, person_id)
            refresh_medication_dependent_reports(conn, vault_root, person_id)
    _ = note_path
    if fact_interpreter is not None:
        auto_accept_history_review_notes_for_source_prefix(
            conn=conn,
            vault_root=vault_root,
            person_id=person_id,
            source_path_prefix=processed_source_path,
            refresh_existing=refresh_reports,
            fact_interpreter=fact_interpreter,
            processed_root=processed_root,
            refresh_reports=refresh_reports,
        )
    return ReprocessResult(imported_rows=1, affected_person_ids={person_id})


def reprocess_retained_history_archive(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    archive_path: Path,
    fact_interpreter: FactInterpreter | None = None,
    lab_resolution_provider: LabObservationResolutionProvider | None = None,
    source_path_prefix: Path | None = None,
    refresh_reports: bool = True,
) -> ReprocessResult:
    from famed.immunization_sync import (
        immunization_document_date,
        persist_immunization_rows,
        remove_immunization_artifacts_for_source_path,
    )
    from famed.import_labs import (
        fill_loinc_codes,
        persist_lab_rows,
    )
    from famed.lab_rejections import (
        lab_resolutions_for_replay,
        validate_recorded_lab_archive_members,
    )
    from famed.lab_sync import (
        replace_lab_results_for_source_path,
    )
    from famed.medication_sync import (
        fill_rxnorm_cuis,
        medication_document_date,
        persist_medication_rows,
        remove_imported_medications_for_source_prefix,
        replace_medications_for_source_path,
        retire_empty_medication_source_notes,
        validate_empty_medication_source_note_retirement,
        write_current_medications_report,
        write_medication_conflicts_report,
    )
    from famed.reference_cache import load_loinc_index, load_rxnorm_index
    from famed.storage_contract import configured_storage_roots
    from famed.vital_sync import persist_vital_rows, vital_document_date

    if archive_path.suffix.lower() != ".zip":
        raise HistorySyncError(
            f"retained history archive must be a .zip file, got: {archive_path.name!r}"
        )
    try:
        relative = archive_path.resolve().relative_to(processed_root.resolve())
    except ValueError as exc:
        raise HistorySyncError(
            "retained history archive must be under data/processed/<person_id>/HealthSummary"
        ) from exc
    if len(relative.parts) < 3 or relative.parts[1] != "HealthSummary":
        raise HistorySyncError(
            "retained history archive must be under data/processed/<person_id>/HealthSummary"
        )
    person_id = relative.parts[0].strip()
    if not person_id:
        raise HistorySyncError("could not infer person_id from retained history archive path")

    storage_roots = configured_storage_roots(root)
    loinc_index = load_loinc_index(storage_roots.reference)
    rxnorm_index = load_rxnorm_index(storage_roots.reference)
    archive_source_path = retained_source_reference(
        root=root,
        processed_root=processed_root,
        source_path=archive_path,
        source_path_prefix=source_path_prefix,
    )
    archive_checksum = source_file_checksum(archive_path)
    imported_rows = 0
    legacy_file_paths: set[Path] = set()
    members = list(_iter_history_archive_members(archive_path))
    validate_recorded_lab_archive_members(
        conn,
        archive_source=archive_source_path,
        person_id=person_id,
        members={name for name, _ in members},
    )
    all_legacy_source_paths: set[str] = set()
    equivalent_source_paths: dict[str, str] = {}
    for member_name, _ in members:
        legacy_source_paths = _legacy_history_source_paths_for_member(
            root=root,
            processed_root=processed_root,
            person_id=person_id,
            member_name=member_name,
        )
        all_legacy_source_paths.update(legacy_source_paths)
        for legacy_source_path in legacy_source_paths:
            equivalent_source_paths[legacy_source_path] = archive_member_source_path(
                archive_source_path, member_name
            )
    _require_semantic_replay_for_accepted_history(
        vault_root=vault_root,
        person_id=person_id,
        source_path_prefix=f"{archive_source_path}#",
        fact_interpreter=fact_interpreter,
        only_interpreted=True,
        equivalent_source_paths=equivalent_source_paths,
    )
    if lab_resolution_provider is not None:
        lab_resolution_provider.validate_lab_observation_resolution_archive(
            archive_source_path,
            archive_checksum,
            tuple(
                archive_member_source_path(archive_source_path, member_name)
                for member_name, _ in members
            ),
        )
    archive_resolutions: dict[str, tuple[LabObservationResolution, ...]] = {}
    with tempfile.TemporaryDirectory(prefix="famed-history-archive-preflight-") as temp_dir:
        temp_root = Path(temp_dir)
        preflight_empty_source_paths: set[str] = set()
        for member_index, (member_name, xml_bytes) in enumerate(members):
            temp_path = temp_root / f"{member_index}-{Path(member_name).name}"
            temp_path.write_bytes(xml_bytes)
            member_source = archive_member_source_path(archive_source_path, member_name)
            rejected: list[dict[str, Any]] = []
            extract_ccda_lab_rows(temp_path, person_id, rejected=rejected)
            archive_resolutions[member_source] = lab_resolutions_for_replay(
                conn,
                person_id=person_id,
                source_path=member_source,
                source_checksum=archive_checksum,
                rejected=rejected,
                supplied=(
                    lab_resolution_provider.lab_observation_resolutions_for_source(member_source)
                    if lab_resolution_provider is not None
                    else ()
                ),
            )
            if extract_ccda_medication_rows(temp_path, person_id):
                continue
            preflight_empty_source_paths.add(
                archive_member_source_path(archive_source_path, member_name)
            )
            preflight_empty_source_paths.update(
                _legacy_history_source_paths_for_member(
                    root=root,
                    processed_root=processed_root,
                    person_id=person_id,
                    member_name=member_name,
                )
            )
        validate_empty_medication_source_note_retirement(
            vault_root=vault_root,
            person_id=person_id,
            source_paths=tuple(sorted(preflight_empty_source_paths)),
        )
    remove_imported_medications_for_source_prefix(conn, f"{archive_source_path}#")
    for legacy_source_path in all_legacy_source_paths:
        replace_medications_for_source_path(conn, legacy_source_path, [])

    with tempfile.TemporaryDirectory(prefix="famed-history-archive-") as temp_dir:
        temp_root = Path(temp_dir)
        for member_name, xml_bytes in members:
            temp_path = temp_root / Path(member_name).name
            temp_path.write_bytes(xml_bytes)

            document = parse_ccda_review_document(temp_path)
            processed_source_path = archive_member_source_path(archive_source_path, member_name)
            source_file_name = display_source_file_name_from_path(processed_source_path)
            legacy_source_paths = set(
                _legacy_history_source_paths_for_member(
                    root=root,
                    processed_root=processed_root,
                    person_id=person_id,
                    member_name=member_name,
                )
            )
            legacy_file_paths.update(
                root / legacy_source_path for legacy_source_path in legacy_source_paths
            )

            rejected_labs: list[dict[str, Any]] = []
            lab_rows = extract_ccda_lab_rows(temp_path, person_id, rejected=rejected_labs)
            resolved_indexes: set[int] = set()
            if archive_resolutions[processed_source_path]:
                resolved_rows, resolved_indexes = _apply_rejected_lab_observation_resolutions(
                    conn=conn,
                    person_id=person_id,
                    processed_source_path=processed_source_path,
                    rejected=rejected_labs,
                    resolutions=archive_resolutions[processed_source_path],
                    source_checksum=archive_checksum,
                )
                lab_rows.extend(resolved_rows)
            _record_rejected_lab_observations(
                conn,
                person_id=person_id,
                processed_source_path=processed_source_path,
                rejected=[
                    item
                    for index, item in enumerate(rejected_labs)
                    if index not in resolved_indexes
                ],
            )
            for legacy_source_path in legacy_source_paths:
                replace_lab_results_for_source_path(conn, legacy_source_path, [])
            replace_lab_results_for_source_path(conn, processed_source_path, [])
            if lab_rows:
                fill_loinc_codes(lab_rows, loinc_index)
                persist_lab_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=temp_path,
                    processed_source_path=processed_source_path,
                    checksum=archive_checksum,
                    source_type="zip_xml",
                    rows=lab_rows,
                    retained_source=True,
                    source_file_name=source_file_name,
                    refresh_reports=False,
                )

            medication_rows = extract_ccda_medication_rows(temp_path, person_id)
            if medication_rows:
                for legacy_source_path in legacy_source_paths:
                    replace_medications_for_source_path(conn, legacy_source_path, [])
                replace_medications_for_source_path(conn, processed_source_path, [])
                fill_rxnorm_cuis(medication_rows, rxnorm_index)
                persist_medication_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=temp_path,
                    processed_source_path=processed_source_path,
                    checksum=archive_checksum,
                    source_type="zip_xml",
                    rows=medication_rows,
                    document_date=medication_document_date(
                        medication_rows,
                        document.document_date,
                        member_name,
                    ),
                    retained_source=True,
                    source_file_name=source_file_name,
                    refresh_reports=False,
                )
            else:
                empty_source_paths = (*sorted(legacy_source_paths), processed_source_path)
                retire_empty_medication_source_notes(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_paths=empty_source_paths,
                )
                for empty_source_path in empty_source_paths:
                    replace_medications_for_source_path(conn, empty_source_path, [])

            vital_rows = extract_ccda_vital_rows(temp_path, person_id)
            if vital_rows:
                persist_vital_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=temp_path,
                    processed_source_path=processed_source_path,
                    checksum=archive_checksum,
                    source_type="zip_xml",
                    rows=vital_rows,
                    document_date=vital_document_date(
                        vital_rows,
                        document.document_date,
                        member_name,
                    ),
                    source_file_name=source_file_name,
                )

            immunization_rows = extract_ccda_immunization_rows(temp_path, person_id)
            for legacy_source_path in legacy_source_paths:
                remove_immunization_artifacts_for_source_path(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    processed_source_path=legacy_source_path,
                    refresh_export=False,
                )
            if immunization_rows:
                persist_immunization_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=temp_path,
                    processed_source_path=processed_source_path,
                    checksum=archive_checksum,
                    source_type="zip_xml",
                    rows=immunization_rows,
                    document_date=immunization_document_date(
                        immunization_rows,
                        document.document_date,
                        member_name,
                    ),
                    retained_source=True,
                    source_file_name=source_file_name,
                    refresh_export=False,
                )
            else:
                remove_immunization_artifacts_for_source_path(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    processed_source_path=processed_source_path,
                    refresh_export=False,
                )

            note_path = persist_history_document_review_note(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=temp_path,
                processed_source_path=processed_source_path,
                checksum=archive_checksum,
                source_type="zip",
                document=document,
                document_kind="C-CDA visit summary archive",
                retained_source=True,
                source_file_name=source_file_name,
                legacy_source_paths=legacy_source_paths,
            )
            remove_linked_category_placeholder_note(
                vault_root=vault_root,
                person_id=person_id,
                category_dir_name="Immunizations",
                source_path=processed_source_path,
            )
            _ = note_path
            imported_rows += 1

    if refresh_reports:
        write_medical_history_summary_report(vault_root, person_id)
        refresh_lab_dependent_reports(conn, vault_root, person_id)
        write_current_medications_report(vault_root, person_id, conn)
        write_medication_conflicts_report(conn, vault_root, person_id)
        refresh_medication_dependent_reports(conn, vault_root, person_id)

    for candidate in legacy_file_paths:
        candidate.unlink(missing_ok=True)

    auto_accept_history_review_notes_for_source_prefix(
        conn=conn,
        vault_root=vault_root,
        person_id=person_id,
        source_path_prefix=f"{archive_source_path}#",
        refresh_existing=refresh_reports,
        fact_interpreter=fact_interpreter,
        processed_root=processed_root,
        refresh_reports=refresh_reports,
    )

    return ReprocessResult(imported_rows=imported_rows, affected_person_ids={person_id})


def write_medical_history_summary_report(vault_root: Path, person_id: str) -> int:
    """Generate medical-history-summary.md per ADR-014 report-refresh hook.

    Returns the number of history notes included in the report.
    Accepted facts are rendered as a medical-history view grouped by clinical
    content. Source visits are retained as provenance rather than used as the
    primary report structure.
    """
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return 0

    entries: list[dict[str, Any]] = []
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        summary = _read_history_note_summary(note_path)
        if summary is not None:
            entries.append(summary)

    if not entries:
        return 0

    # Sort dated entries first (ascending), undated entries at the end.
    dated = sorted(
        (e for e in entries if e["event_date"]),
        key=lambda e: e["event_date"] or "",
    )
    undated = [e for e in entries if not e["event_date"]]
    sorted_entries = dated + undated

    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "medical_history_summary",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    display_name = load_person_display_name(vault_root, person_id)
    heading = f"Medical History — {display_name}" if display_name else "Medical History"

    support_reviews = _build_diagnosis_support_reviews(vault_root, person_id, sorted_entries)
    body = _render_medical_history_report_body(sorted_entries, support_reviews)
    content = f"---\n{yaml_block}\n---\n\n# {heading}\n\n[[_index|← Reports]]\n{body}\n"

    person_reports_dir = vault_root / "Records" / person_id / "Reports"
    person_reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = person_reports_dir / "medical-history-summary.md"
    report_path.write_text(content, encoding="utf-8")
    write_category_index_note(
        person_reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return len(sorted_entries)


def backfill_history_reports(vault_root: Path) -> dict[str, int]:
    """Generate medical-history-summary.md for every person with history notes.

    Returns a mapping of person_id -> note count written.
    """
    records_dir = vault_root / "Records"
    results: dict[str, int] = {}
    if not records_dir.is_dir():
        return results
    for person_dir in sorted(records_dir.iterdir()):
        if not person_dir.is_dir() or person_dir.name.startswith("_"):
            continue
        person_id = person_dir.name
        count = write_medical_history_summary_report(vault_root, person_id)
        if count > 0:
            results[person_id] = count
    return results


_HL7_NS = {"hl7": "urn:hl7-org:v3"}
_XSI_TYPE_ATTR = "{http://www.w3.org/2001/XMLSchema-instance}type"
_CCDA_ST_QUANTITY_RE = re.compile(
    r"^(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s+"
    r"(?P<unit>(?=\S*[A-Za-zµμ%/])[A-Za-z0-9µμ%*/^._-]+)$"
)
_CCDA_COMPARATOR_TEXT_RE = re.compile(
    r"^(?:(?:<=|>=|<|>)\s*|"
    r"(?:above|below|less\s+than|greater\s+than|up\s+to|at\s+least|at\s+most)\s+)"
    r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"(?:\s+(?P<culture_unit>CFU/mL)\s+[A-Za-z]+(?:\s+[A-Za-z]+)+)?$",
    re.IGNORECASE,
)
_CCDA_INTERVAL_TEXT_RE = re.compile(r"^(?P<low>\d+(?:\.\d+)?)\s*-\s*(?P<high>\d+(?:\.\d+)?)$")
_CCDA_ORDINAL_TEXT_RE = re.compile(r"^[1-9]\d*\+$")
_CCDA_LABELED_ORDINAL_TEXT_RE = re.compile(r"^(?P<label>.+?)\s+\([1-9]\d*\+\)$")


def _ccda_document_date(root: ET.Element) -> str | None:
    return (
        _ccda_date(_first_attr(root, ".//hl7:componentOf//hl7:effectiveTime/hl7:low", "value"))
        or _ccda_date(_first_attr(root, "./hl7:effectiveTime", "value"))
        or _ccda_date(
            _first_attr(root, ".//hl7:documentationOf//hl7:effectiveTime/hl7:low", "value")
        )
    )


def _iter_ccda_section_elements(root: ET.Element) -> Iterator[ET.Element]:
    yield from root.findall(".//hl7:structuredBody/hl7:component/hl7:section", _HL7_NS)


def _iter_ccda_sections(root: ET.Element) -> Iterator[HistoryReviewSection]:
    for section in _iter_ccda_section_elements(root):
        title = _normalize_optional_text(_first_text(section, "./hl7:title")) or "Untitled Section"
        text_element = section.find("./hl7:text", _HL7_NS)
        if text_element is None:
            continue
        section_text = _normalize_optional_text(_element_text(text_element))
        if not section_text:
            continue
        yield HistoryReviewSection(title=title, text=section_text)


def _is_ccda_results_section(section: ET.Element) -> bool:
    title = (_normalize_optional_text(_first_text(section, "./hl7:title")) or "").casefold()
    code = section.find("./hl7:code", _HL7_NS)
    section_code = str(code.get("code") or "").strip() if code is not None else ""
    return title == "results" or section_code == "30954-2"


def _is_ccda_medications_section(section: ET.Element) -> bool:
    title = (_normalize_optional_text(_first_text(section, "./hl7:title")) or "").casefold()
    code = section.find("./hl7:code", _HL7_NS)
    section_code = str(code.get("code") or "").strip() if code is not None else ""
    return title == "medications" or section_code == "10160-0"


def _is_ccda_vitals_section(section: ET.Element) -> bool:
    title = (_normalize_optional_text(_first_text(section, "./hl7:title")) or "").casefold()
    code = section.find("./hl7:code", _HL7_NS)
    section_code = str(code.get("code") or "").strip() if code is not None else ""
    return title in {"vital signs", "vitals"} or section_code == "8716-3"


def _is_ccda_immunizations_section(section: ET.Element) -> bool:
    title = (_normalize_optional_text(_first_text(section, "./hl7:title")) or "").casefold()
    code = section.find("./hl7:code", _HL7_NS)
    section_code = str(code.get("code") or "").strip() if code is not None else ""
    return title in {"immunizations", "immunization"} or section_code == "11369-6"


def _iter_ccda_result_observations(root: ET.Element) -> Iterator[ET.Element]:
    for section in _iter_ccda_section_elements(root):
        if not _is_ccda_results_section(section):
            continue
        for observation in section.findall(".//hl7:entry//hl7:observation", _HL7_NS):
            # A companion "units" observation carries its parent result's unit,
            # not a result of its own; _ccda_companion_unit reads it in place.
            if _is_ccda_units_companion(observation):
                continue
            yield observation


def _is_ccda_units_companion(observation: ET.Element) -> bool:
    code = observation.find("./hl7:code", _HL7_NS)
    if code is None:
        return False
    return str(code.get("code") or "").strip() == _CCDA_UNITS_OBSERVATION_CODE


def _iter_ccda_vital_observations(root: ET.Element) -> Iterator[ET.Element]:
    for section in _iter_ccda_section_elements(root):
        if not _is_ccda_vitals_section(section):
            continue
        yield from section.findall(".//hl7:entry//hl7:observation", _HL7_NS)


_CCDA_UNITS_OBSERVATION_CODE = "246514001"  # SNOMED CT "Units" (companion observation)


def _ccda_companion_unit(observation: ET.Element) -> str | None:
    """Return the unit text from a companion "units" observation, if present.

    Some CCDA exports (e.g. Epic) represent a result's unit as a sibling
    observation (SNOMED 246514001 "units") instead of a `unit` attribute on
    the result's own `<value>` element, typically when the unit has no UCUM
    code (e.g. µIU/mL).
    """
    for related in observation.findall("./hl7:entryRelationship/hl7:observation", _HL7_NS):
        related_code = related.find("./hl7:code", _HL7_NS)
        if related_code is None or related_code.get("code") != _CCDA_UNITS_OBSERVATION_CODE:
            continue
        related_value = related.find("./hl7:value", _HL7_NS)
        if related_value is not None:
            return _normalize_optional_text(_element_text(related_value))
    return None


def _ccda_reference_bound(element: ET.Element | None) -> str | None:
    """Return a reference-range bound's numeric value, including nullFlavor="OTH" bounds.

    CCDA represents bounds with no UCUM unit using `nullFlavor="OTH"` and
    nests the actual value in a `<translation value="...">` child instead of
    the `<low>`/`<high>` element's own `value` attribute.
    """
    if element is None:
        return None
    value = element.get("value")
    if value:
        return value
    translation = element.find("./hl7:translation", _HL7_NS)
    return translation.get("value") if translation is not None else None


def _ccda_st_quantity(value_element: ET.Element) -> tuple[str, str] | None:
    if value_element.get(_XSI_TYPE_ATTR) != "ST":
        return None
    value_text = _normalize_optional_text(_element_text(value_element))
    if not value_text:
        return None
    match = _CCDA_ST_QUANTITY_RE.fullmatch(value_text)
    if match is None or match.group("unit").endswith((".", ",", ";", ":")):
        return None
    return match.group("value"), match.group("unit")


def _is_ccda_bounded_text_result(
    value_element: ET.Element,
    value_text: str,
    unit: str | None,
) -> bool:
    """Return whether an ST result is one bounded ordinal or interval (ADR-036)."""
    if value_element.get(_XSI_TYPE_ATTR) != "ST":
        return False
    if _CCDA_ORDINAL_TEXT_RE.fullmatch(value_text) is not None:
        return True
    labeled_ordinal = _CCDA_LABELED_ORDINAL_TEXT_RE.fullmatch(value_text)
    if labeled_ordinal is not None:
        label = labeled_ordinal.group("label")
        return any(char.isalpha() for char in label) and all(
            char.isalpha() or char in {" ", "-"} for char in label
        )
    interval = _CCDA_INTERVAL_TEXT_RE.fullmatch(value_text)
    if interval is None or not unit:
        return False
    return Decimal(interval.group("low")) <= Decimal(interval.group("high"))


def _ccda_interpretation_code(observation: ET.Element) -> str:
    """Return the source's own abnormality flag for one observation.

    HL7 v3 ObservationInterpretation: ``H`` high, ``L`` low, ``A`` abnormal,
    ``AA`` critically abnormal, ``N`` normal. The lab states this per result,
    and it is not always reproducible from the reference range: a value inside
    its own stated bounds can still carry a non-normal flag. Discarding it lost
    the lab's judgement on individual results.
    """
    element = observation.find("./hl7:interpretationCode", _HL7_NS)
    if element is None:
        return ""
    return str(element.get("code") or "").strip().upper()


def _ccda_lab_mapping(
    observation: ET.Element,
    *,
    person_id: str,
    document_date: str | None,
) -> dict[str, Any] | None:
    code = observation.find("./hl7:code", _HL7_NS)
    if code is None:
        return None
    test_name = _ccda_coded_display_text(code, observation)
    value_element = observation.find("./hl7:value", _HL7_NS)
    if not test_name or value_element is None:
        return None
    value_text = (
        _normalize_optional_text(value_element.get("value"))
        or _normalize_optional_text(value_element.get("displayName"))
        or _normalize_optional_text(_element_text(value_element))
    )
    if not value_text:
        return None
    reference_value = observation.find(
        "./hl7:referenceRange/hl7:observationRange/hl7:value", _HL7_NS
    )
    reference_low_element = (
        reference_value.find("./hl7:low", _HL7_NS) if reference_value is not None else None
    )
    reference_high_element = (
        reference_value.find("./hl7:high", _HL7_NS) if reference_value is not None else None
    )
    reference_low = _ccda_reference_bound(reference_low_element)
    reference_high = _ccda_reference_bound(reference_high_element)
    observed_at = (
        _ccda_datetime(_first_attr(observation, "./hl7:effectiveTime", "value"))
        or _ccda_datetime(_first_attr(observation, "./hl7:effectiveTime/hl7:low", "value"))
        or document_date
    )
    if not observed_at:
        return None
    loinc_code = ""
    if code.get("codeSystem") == "2.16.840.1.113883.6.1":
        loinc_code = str(code.get("code") or "").strip()
    numeric_value = value_element.get("value")
    unit = _normalize_optional_text(value_element.get("unit")) or _ccda_companion_unit(observation)
    comparator_match = _CCDA_COMPARATOR_TEXT_RE.fullmatch(value_text)
    bounded_text_result = _is_ccda_bounded_text_result(value_element, value_text, unit)
    comparator_unit = (
        comparator_match.group("culture_unit") if comparator_match is not None else None
    )
    # A vendor unit spelling that a reviewed entry declares equivalent for this
    # exact analyte resolves to the canonical unit (ADR-035).
    unit = reviewed_unit_equivalence(test_name, unit) or unit
    comparator_unit_conflict = bool(
        comparator_unit
        and unit
        and normalize_unit(comparator_unit).casefold() != normalize_unit(unit).casefold()
    )
    if comparator_unit and not comparator_unit_conflict:
        unit = comparator_unit
    if numeric_value in (None, "") and not unit:
        st_quantity = _ccda_st_quantity(value_element)
        if st_quantity is not None:
            numeric_value, unit = st_quantity
    # Digit-free, comparator, ordinal, and interval results remain textual so
    # reports never trend them as exact measurements. Other digit-bearing text
    # stays unparsed and reviewable (ADR-018, ADR-036).
    candidate = numeric_value if numeric_value not in (None, "") else value_text
    try:
        float(str(candidate))
    except (TypeError, ValueError):
        candidate_text = str(candidate or "").strip()
        if candidate_text and (
            not any(char.isdigit() for char in candidate_text)
            or (comparator_match is not None and not comparator_unit_conflict)
            or bounded_text_result
        ):
            candidate = None
    return {
        "person_id": person_id,
        "code_system": str(code.get("codeSystem") or "").strip(),
        "loinc_code": loinc_code,
        "test_name": test_name,
        "value": candidate,
        "value_text": value_text,
        "value_type": str(value_element.get(_XSI_TYPE_ATTR) or "").strip(),
        "unit": unit,
        "reference_range_low": reference_low,
        "reference_range_high": reference_high,
        "interpretation_code": _ccda_interpretation_code(observation),
        "observed_at": observed_at,
        "source_type": "source_import",
    }


def _ccda_medication_mapping(
    administration: ET.Element,
    section: ET.Element,
    *,
    person_id: str,
    local_offsets: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    medication_name = None
    material_code = administration.find(
        "./hl7:consumable/hl7:manufacturedProduct/hl7:manufacturedMaterial/hl7:code",
        _HL7_NS,
    )
    if material_code is not None:
        medication_name = _ccda_coded_display_text(material_code, section)
    if not medication_name:
        text_element = administration.find("./hl7:text", _HL7_NS)
        if text_element is not None:
            medication_name = _ccda_reference_text(
                section,
                _first_attr(text_element, "./hl7:reference", "value"),
            )
    medication_name = _normalize_optional_text(medication_name)
    if not medication_name or medication_name.casefold().startswith(
        ("no known medication", "no known active medication", "no medications")
    ):
        return None
    start_date = _ccda_date(
        _first_attr(administration, "./hl7:effectiveTime/hl7:low", "value"),
        local_offsets=local_offsets,
    )
    high_date = _ccda_date(
        _first_attr(administration, "./hl7:effectiveTime/hl7:high", "value"),
        local_offsets=local_offsets,
    )
    # ADR-041: on an INT-mood (prescription intent) entry the effectiveTime high is the
    # date the order stops being dispensable — when refills run out — not a therapy stop.
    # Only an EVN-mood (administration event) high ends the therapy.
    if _normalize_optional_text(administration.get("moodCode")) == "INT":
        end_date = None
        authorized_through = high_date
    else:
        end_date = high_date
        authorized_through = None
    status = (
        _normalize_optional_text(_first_attr(administration, "./hl7:statusCode", "code"))
        or "active"
    )
    instruction = None
    for relationship in administration.findall("./hl7:entryRelationship", _HL7_NS):
        child_text = relationship.find(".//hl7:text", _HL7_NS)
        reference = (
            _first_attr(child_text, "./hl7:reference", "value") if child_text is not None else None
        )
        if reference:
            instruction = _ccda_reference_text(section, reference)
        if instruction:
            break
    dose = _ccda_dose_text(administration)
    frequency = _ccda_medication_frequency_text(instruction)
    rxnorm_cui = None
    if material_code is not None and material_code.get("codeSystem") == "2.16.840.1.113883.6.88":
        rxnorm_cui = _normalize_optional_text(material_code.get("code"))
    return {
        "person_id": person_id,
        "rxnorm_cui": rxnorm_cui,
        "medication_name": medication_name,
        "medication_type": "prescription",
        "dose": dose,
        "frequency": frequency,
        "start_date": start_date,
        "end_date": end_date,
        "authorized_through": authorized_through,
        "status": status,
        "route": _normalize_optional_text(
            _first_attr(administration, "./hl7:routeCode", "displayName")
        ),
        "evidence_snippet": frequency or medication_name,
    }


def _ccda_immunization_mapping(
    administration: ET.Element,
    section: ET.Element,
    *,
    person_id: str,
    document_date: str | None,
) -> dict[str, Any] | None:
    from famed.immunization_sync import normalize_vaccine_name

    text_element = administration.find("./hl7:text", _HL7_NS)
    vaccine_name = None
    if text_element is not None:
        vaccine_name = _ccda_reference_text(
            section,
            _first_attr(text_element, "./hl7:reference", "value"),
        )
    material_code = administration.find(
        "./hl7:consumable/hl7:manufacturedProduct/hl7:manufacturedMaterial/hl7:code",
        _HL7_NS,
    )
    if material_code is not None:
        vaccine_name = vaccine_name or _ccda_coded_display_text(material_code, section)
    vaccine_name = normalize_vaccine_name(_normalize_optional_text(vaccine_name))
    if not vaccine_name:
        return None
    administered_date = (
        _ccda_date(_first_attr(administration, "./hl7:effectiveTime", "value"))
        or _ccda_date(_first_attr(administration, "./hl7:effectiveTime/hl7:low", "value"))
        or document_date
    )
    cvx_code = None
    if material_code is not None and material_code.get("codeSystem") == "2.16.840.1.113883.12.292":
        cvx_code = _normalize_optional_text(material_code.get("code"))
    return {
        "person_id": person_id,
        "vaccine_name": vaccine_name,
        "cvx_code": cvx_code,
        "administered_date": administered_date,
        "status": _normalize_optional_text(_first_attr(administration, "./hl7:statusCode", "code"))
        or "completed",
        "lot_number": _normalize_optional_text(
            _first_text(
                administration,
                "./hl7:consumable/hl7:manufacturedProduct/hl7:manufacturedMaterial/hl7:lotNumberText",
            )
        ),
        "source_type": "source_import",
        "evidence_snippet": vaccine_name,
    }


_CCDA_VITAL_CODE_MAP = {
    "8310-5": "temperature",
    "8480-6": "blood pressure systolic",
    "8462-4": "blood pressure diastolic",
    "8302-2": "height",
    "8306-3": "height",
    "8308-9": "height",
    "29463-7": "weight",
    "3141-9": "weight",
    "39156-5": "bmi",
    "8867-4": "pulse",
    "2708-6": "oxygen saturation",
}

_CCDA_VITAL_NAME_MAP = {
    "temperature": "temperature",
    "body temperature": "temperature",
    "bp systolic": "blood pressure systolic",
    "systolic blood pressure": "blood pressure systolic",
    "blood pressure systolic": "blood pressure systolic",
    "bp diastolic": "blood pressure diastolic",
    "diastolic blood pressure": "blood pressure diastolic",
    "blood pressure diastolic": "blood pressure diastolic",
    "height": "height",
    "body height": "height",
    "weight": "weight",
    "body weight": "weight",
    "bmi": "bmi",
    "body mass index": "bmi",
    "heart rate": "pulse",
    "pulse": "pulse",
    "oxygen saturation": "oxygen saturation",
    "o2 saturation": "oxygen saturation",
    "spo2": "oxygen saturation",
}


def _ccda_vital_mapping(
    observation: ET.Element,
    *,
    person_id: str,
    document_date: str | None,
) -> dict[str, Any] | None:
    code = observation.find("./hl7:code", _HL7_NS)
    if code is None:
        return None
    vital_name = _ccda_vital_name(code)
    if vital_name is None:
        return None
    value_element = observation.find("./hl7:value", _HL7_NS)
    if value_element is None:
        return None
    value_text = (
        _normalize_optional_text(value_element.get("value"))
        or _normalize_optional_text(value_element.get("displayName"))
        or _normalize_optional_text(_element_text(value_element))
    )
    if not value_text:
        return None
    observed_at = (
        _ccda_datetime(_first_attr(observation, "./hl7:effectiveTime", "value"))
        or _ccda_datetime(_first_attr(observation, "./hl7:effectiveTime/hl7:low", "value"))
        or document_date
    )
    if not observed_at:
        return None
    reference_value = observation.find(
        "./hl7:referenceRange/hl7:observationRange/hl7:value", _HL7_NS
    )
    reference_low_element = (
        reference_value.find("./hl7:low", _HL7_NS) if reference_value is not None else None
    )
    reference_high_element = (
        reference_value.find("./hl7:high", _HL7_NS) if reference_value is not None else None
    )
    reference_low = (
        _normalize_optional_text(reference_low_element.get("value"))
        if reference_low_element is not None
        else None
    )
    reference_high = (
        _normalize_optional_text(reference_high_element.get("value"))
        if reference_high_element is not None
        else None
    )
    unit = _normalize_optional_text(value_element.get("unit"))
    display_name = _ccda_coded_display_text(code, observation) or vital_name
    evidence = f"{display_name}: {value_text}"
    if unit:
        evidence = f"{evidence} {unit}"
    return {
        "person_id": person_id,
        "vital_name": vital_name,
        "value": value_text,
        "unit": unit,
        "reference_range_low": reference_low,
        "reference_range_high": reference_high,
        "observed_at": observed_at,
        "confidence": 1.0,
        "evidence_snippet": evidence,
    }


def _ccda_vital_name(code_element: ET.Element) -> str | None:
    code = _normalize_optional_text(code_element.get("code"))
    if code and code in _CCDA_VITAL_CODE_MAP:
        return _CCDA_VITAL_CODE_MAP[code]
    display_name = _ccda_coded_display_text(code_element, code_element)
    if not display_name:
        return None
    return _CCDA_VITAL_NAME_MAP.get(display_name.casefold())


def _first_text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, _HL7_NS)
    return found.text if found is not None else None


def _first_attr(element: ET.Element, path: str, attr: str) -> str | None:
    found = element.find(path, _HL7_NS)
    return found.get(attr) if found is not None else None


def _ccda_reference_text(container: ET.Element, reference: str | None) -> str | None:
    normalized = _normalize_optional_text(reference)
    if not normalized:
        return None
    target = normalized.lstrip("#")
    for element in container.iter():
        if element.get("ID") == target:
            return _normalize_optional_text(_element_text(element))
    return None


def _ccda_coded_display_text(
    code_element: ET.Element, reference_container: ET.Element
) -> str | None:
    original_text = code_element.find("./hl7:originalText", _HL7_NS)
    if original_text is not None:
        referenced = _ccda_reference_text(
            reference_container,
            _first_attr(original_text, "./hl7:reference", "value"),
        )
        if referenced:
            return referenced
        inline_text = _normalize_optional_text(_element_text(original_text))
        if inline_text:
            return inline_text
    return _normalize_optional_text(code_element.get("displayName")) or _normalize_optional_text(
        code_element.get("code")
    )


_CCDA_NON_SCHEDULE_MEDICATION_TEXT_RE = re.compile(
    r"^(?:take\s+)?(?:by\s+mouth|orally|oral|tablet|tablets|capsule|capsules|"
    r"caplet|caplets|pill|pills)\.?$",
    re.IGNORECASE,
)


def _ccda_medication_frequency_text(instruction: str | None) -> str | None:
    text = _normalize_optional_text(instruction)
    if not text:
        return None
    if _CCDA_NON_SCHEDULE_MEDICATION_TEXT_RE.fullmatch(text):
        return None
    return text


def _ccda_dose_text(administration: ET.Element) -> str | None:
    dose = administration.find("./hl7:doseQuantity", _HL7_NS)
    if dose is None:
        return None
    value = _normalize_optional_text(dose.get("value"))
    unit = _normalize_optional_text(dose.get("unit"))
    if unit and unit.startswith("{") and unit.endswith("}"):
        unit = None
    if value and unit:
        return f"{value} {unit}"
    return value or unit


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_CCDA_OFFSET_TIMESTAMP = re.compile(r"^\d{8,14}([+-]\d{4})$")


def _ccda_document_local_offsets(root: ET.Element) -> tuple[str, ...]:
    """Return the non-UTC UTC-offsets this document states for its own timestamps.

    An HL7 timestamp written `YYYYMMDDhhmmss-0700` carries local wall-clock digits, so
    truncating it yields the right local date. One written `...+0000` carries a UTC
    instant, and truncating that can land on the following day — a value meaning 23:59
    local reads as the next date. Recovering the local date needs the document's own
    zone, which the document supplies wherever it writes a non-UTC offset.
    """
    offsets: set[str] = set()
    for element in root.iter():
        value = element.get("value")
        if not value:
            continue
        match = _CCDA_OFFSET_TIMESTAMP.match(value.strip())
        if match and match.group(1) != "+0000":
            offsets.add(match.group(1))
    return tuple(sorted(offsets))


def _ccda_date(value: str | None, *, local_offsets: tuple[str, ...] = ()) -> str | None:
    normalized = _normalize_optional_text(value)
    if not normalized or len(normalized) < 8:
        return None
    local = _ccda_utc_instant_local_date(normalized, local_offsets)
    if local is not None:
        return local
    return f"{normalized[0:4]}-{normalized[4:6]}-{normalized[6:8]}"


def _ccda_utc_instant_local_date(
    normalized: str,
    local_offsets: tuple[str, ...],
) -> str | None:
    """Re-express a UTC-stamped instant as the document's local calendar date.

    Returns None — leaving the caller with the raw UTC date — unless every offset the
    document declares agrees on the answer. A document spanning a daylight-saving change
    states more than one offset, and picking the wrong one shifts an end-of-day value
    across midnight, so disagreement fails closed rather than guessing (ADR-018, ADR-041).
    """
    if not local_offsets or len(normalized) < 19 or not normalized.endswith("+0000"):
        return None
    try:
        instant = datetime.strptime(normalized[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    candidates = set()
    for offset in local_offsets:
        try:
            delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        except ValueError:
            return None
        shifted = instant - delta if offset[0] == "-" else instant + delta
        candidates.add(shifted.date().isoformat())
    if len(candidates) != 1:
        return None
    return candidates.pop()


def _ccda_datetime(value: str | None) -> str | None:
    """Parse a raw HL7 timestamp into ISO datetime, preserving time when present.

    HL7 CCDA effectiveTime values use the format ``YYYYMMDDhhmmss[+zzzz]``.
    This function preserves the time component so that multiple observations
    on the same day (e.g. pre- and post-procedure draws) are distinguishable
    rather than collapsed into a date-only conflict.
    """
    date_part = _ccda_date(value)
    if date_part is None:
        return None
    normalized = _normalize_optional_text(value)
    if normalized is None or len(normalized) < 12:
        return date_part
    clean = re.split(r"[+-]", normalized, maxsplit=1)[0]
    if len(clean) < 12:
        return date_part
    return f"{date_part}T{clean[8:10]}:{clean[10:12]}:{clean[12:14] if len(clean) >= 14 else '00'}"


def _ccda_birth_year(value: str | None) -> int | None:
    normalized = _normalize_optional_text(value)
    if not normalized or len(normalized) < 4:
        return None
    try:
        return int(normalized[0:4])
    except ValueError:
        return None


def _parse_text_patient_name(text: str) -> tuple[tuple[str, ...], str | None]:
    for pattern in (
        r"\bName:\s*([A-Za-z' -]+?)\s+([A-Za-z' -]+?)\s*\|\s*DOB:",
        r"\bPatient:\s*([A-Za-z' -]+),\s*([A-Za-z' -]+?)\s+DOB:",
        r"\bPatient name\s+([A-Za-z' -]+?)\s+([A-Za-z' -]+?)\s+Contact info",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        if "Patient:" in pattern:
            family_name, given = match.groups()
            return (given.strip(),), family_name.strip()
        given, family_name = match.groups()
        return (given.strip(),), family_name.strip()
    return (), None


def _parse_text_birth_year(text: str) -> int | None:
    match = re.search(r"\bDOB:\s*(\d{1,2})/(\d{1,2})/(\d{4})\b", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(3))
    match = re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},\s+(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


_MONTH_NAME_TO_NUMBER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _parse_text_document_date(text: str) -> str | None:
    for pattern in (
        r"(?<!BIRTH\s)\bDates?:\s*(\d{1,2})/(\d{1,2})/(\d{4})\b",
        r"(?<!BIRTH\s)\bDOS:\s*(\d{1,2})/(\d{1,2})/(\d{4})\b",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            month, day, year = (int(part) for part in match.groups())
            return f"{year:04d}-{month:02d}-{day:02d}"
    # Spelled-out month dates (e.g. "Dose given on June 22, 2026"). Anchored on a
    # preceding "on" and excluding "birth"/"born" so a birth-date mention elsewhere
    # in the same document (typically spelled out without a preceding "on") isn't
    # mistaken for the document date.
    month_match = re.search(
        r"(?<!birth\s)(?<!born\s)\bon\s+(January|February|March|April|May|June|July|"
        r"August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_match:
        month_name, day, year = month_match.groups()
        month = _MONTH_NAME_TO_NUMBER[month_name.casefold()]
        return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    return None


def _date_from_source_stem(source_stem: str) -> str | None:
    # Prefer an explicit ISO date in the stem (e.g. "2024-01-15-...") over the
    # compact YYMMDD form, so source filenames that already carry a full date are
    # not treated as undated.
    iso_match = re.search(r"(?:^|[-_\s])(\d{4})-(\d{2})-(\d{2})(?:[-_\s]|$)", source_stem)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
        try:
            datetime(year, month, day)
        except ValueError:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = re.search(r"(?:^|[-_\s])(\d{2})(\d{2})(\d{2})(?:[-_\s]|$)", source_stem)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    full_year = 2000 + year if year < 70 else 1900 + year
    try:
        datetime(full_year, month, day)
    except ValueError:
        return None
    return f"{full_year:04d}-{month:02d}-{day:02d}"


def _element_text(element: ET.Element) -> str:
    return " ".join(part for part in element.itertext() if part and part.strip())


def _history_document_person_matches(
    person: dict[str, Any], document: HistoryReviewDocument
) -> bool:
    if (
        document.patient_birth_year is not None
        and person.get("birth_year") != document.patient_birth_year
    ):
        return False
    display_name = str(person.get("display_name") or "").casefold()
    if not display_name:
        return False
    given_names = {name.casefold() for name in document.patient_given_names}
    full_name = " ".join(
        [*document.patient_given_names, document.patient_family_name or ""]
    ).casefold()
    return display_name in given_names or display_name == full_name


def _escape_heading(value: str) -> str:
    return value.replace("\n", " ").strip() or "Untitled Section"
