#!/usr/bin/env python3
"""Import lab results from CSV files into vault notes and SQLite."""

from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from famed.clinical_interpreter import InterpreterFactExtraction
from famed.dependent_reports import (
    LAB_DEPENDENT_COMPOSITE_FILENAMES,
    LAB_DEPENDENT_REPORT_FILENAMES,
    has_composite_report_surface,
    has_fhir_export_surface,
    refresh_lab_dependent_reports,
)
from famed.lab_sync import (
    LOINC_CODE_ORIGIN_INFERRED,
    LabSyncError,
    build_latest_lab_results_from_notes,
    can_parse_optional_float,
    find_overlapping_panel_note,
    is_non_result_lab_row,
    iter_date_panel_note_paths,
    lab_payload_from_mapping,
    load_labs_from_csv,
    load_labs_from_note,
    log_ingestion_event,
    merge_lab_panel_rows,
    replace_lab_results_for_source_path,
    source_file_checksum,
    write_lab_panel_note,
    write_labs_index_note,
)
from famed.person_sync import (
    ensure_person_record_scaffold,
    load_person_display_name,
    parse_front_matter,
)
from famed.reference_cache import load_loinc_index, lookup_loinc_code
from famed.reprocess_sources import (
    ReprocessError,
    ReprocessRegistry,
    ReprocessResult,
    retained_source_reference,
)
from famed.storage_contract import (
    AUDIT_LAB_IMPORT_ACCEPTED,
    AUDIT_LAB_IMPORT_QUARANTINED,
    AUDIT_LAB_SOURCE_REPROCESSED,
    STATE_PROCESSED,
    STATE_QUARANTINED,
    configured_storage_roots,
    log_audit_event,
    mark_dependent_output_stale,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import lab CSV/PDF files into famed-vault and data/famed.db. "
            "Bulk scan mode reads data/inbox/<person_id>/labs/ folders. "
            "Internal helper invoked by skills (ADR-015); not the user-facing surface."
        )
    )
    parser.add_argument(
        "--inbox-dir",
        default="data/inbox",
        help=(
            "Inbox root (default: data/inbox). Bulk scan mode reads "
            "<person_id>/labs/ subfolders; use --csv-file/--pdf-file for a specific file."
        ),
    )
    parser.add_argument(
        "--vault-path",
        default="famed-vault",
        help="Path to Obsidian vault (default: famed-vault)",
    )
    parser.add_argument(
        "--db-path",
        default="data/famed.db",
        help="Path to SQLite database (default: data/famed.db)",
    )
    parser.add_argument(
        "--csv-file",
        help=(
            "Single CSV file path to import; defaults to bulk-scanning "
            "person-scoped lab inbox folders."
        ),
    )
    parser.add_argument(
        "--pdf-file",
        help=(
            "Single PDF file path to import; defaults to bulk-scanning "
            "person-scoped lab inbox folders."
        ),
    )
    parser.add_argument(
        "--person-id",
        help=(
            "Required for --pdf-file unless person_id can be inferred from filename "
            "prefix '<person_id>__<label>.pdf'."
        ),
    )
    parser.add_argument(
        "--approve-pdf",
        action="store_true",
        help="Skip interactive review prompt for extracted PDF values.",
    )
    parser.add_argument(
        "--review-only",
        action="store_true",
        help="Parse and review extracted rows without writing notes or database records.",
    )
    parser.add_argument(
        "--backfill-reports",
        action="store_true",
        help=(
            "Regenerate latest lab reports for all people with existing lab notes, "
            "even when no new inbox files are imported."
        ),
    )
    parser.add_argument(
        "--reprocess-source",
        help=(
            "Rebuild notes and database rows from an already retained lab source under "
            "data/processed/<person_id>/Labs without moving the source file."
        ),
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def collect_csv_files(inbox_dir: Path, single_file: str | None) -> list[Path]:
    if single_file:
        file_path = Path(single_file)
        if not file_path.is_absolute():
            file_path = (repo_root() / file_path).resolve()
        return [file_path]

    return sorted(inbox_dir.glob("*/labs/*.csv"))


def collect_pdf_files(inbox_dir: Path, single_file: str | None) -> list[Path]:
    if single_file:
        file_path = Path(single_file)
        if not file_path.is_absolute():
            file_path = (repo_root() / file_path).resolve()
        return [file_path]

    return sorted(inbox_dir.glob("*/labs/*.pdf"))


def preflight_pdf_dependencies_if_needed(pdf_files: list[Path]) -> list[str]:
    if not pdf_files:
        return []

    errors: list[str] = []

    try:
        import pypdf  # pyright: ignore[reportUnusedImport,reportMissingModuleSource]  # noqa: F401
    except ImportError:
        errors.append(
            "Missing dependency for PDF import: pypdf. "
            "Run: python -m pip install -e . or python scripts/setup.py --check"
        )

    return errors


def slugify_filename(value: str) -> str:
    """Sanitize a label for use in *vault note filenames* only.

    Source file relocation paths preserve the original filename stem
    (REQ-001 clinically meaningful filename preservation).  This
    function is kept for vault note name generation where consistent,
    safe labels are desirable.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return slug.strip("-") or "lab-import"


def source_stem_without_redundant_date_prefix(source_stem: str, document_date: str | None) -> str:
    """Remove leading storage/date prefixes that duplicate the canonical date.

    The processed path already carries the canonical date as its first segment.
    Keeping storage stems like ``undated-221031 visitSummary`` after that prefix
    creates confusing names such as
    ``2022-10-31-undated-221031-visitsummary``.
    """
    stem = source_stem.strip()
    while True:
        original = stem
        if document_date:
            stem = re.sub(
                r"^\d{4}-\d{2}-\d{2}[-_\s]+(?=undated[-_\s]+\d{6}[a-z]?[-_\s]+)",
                "",
                stem,
                flags=re.IGNORECASE,
            )
        stem = re.sub(r"^(?:undated[-_\s]+)+", "", stem, flags=re.IGNORECASE)
        if document_date:
            stem = re.sub(rf"^{re.escape(document_date)}[-_\s]+", "", stem)
            stem = re.sub(r"^\d{6}[a-z]?[-_\s]+", "", stem, flags=re.IGNORECASE)
        if stem == original:
            break
    return stem or source_stem


def display_source_file_name(source_file: Path, document_date: str | None) -> str:
    stem = source_stem_without_redundant_date_prefix(source_file.stem, document_date)
    return f"{stem}{source_file.suffix.lower()}"


def deterministic_panel_note_path(
    person_labs_dir: Path, observed_at: str, import_label: str
) -> Path:
    date_prefix = observed_at.split("T", 1)[0]
    slug = slugify_filename(import_label)
    suffix = "" if slug.endswith("-panel") else "-panel"
    return person_labs_dir / f"{date_prefix}-{slug}{suffix}.md"


def processed_lab_source_path(
    processed_root: Path,
    person_id: str,
    observed_at: str,
    source_file: Path,
) -> Path:
    date_prefix = observed_at.split("T", 1)[0]
    stem = source_stem_without_redundant_date_prefix(source_file.stem, date_prefix)
    suffix = source_file.suffix.lower()
    return processed_root / person_id / "Labs" / f"{date_prefix}-{stem}{suffix}"


def quarantined_lab_source_path(
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
) -> Path:
    safe_person_id = person_id or "_unknown"
    stem = source_file.stem
    suffix = source_file.suffix.lower()
    return quarantine_root / safe_person_id / "Labs" / f"undated-{stem}{suffix}"


def retain_source_file(source_file: Path, destination: Path, checksum: str) -> Path:
    """Move a source into retained storage without clobbering a different file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    final_destination = destination

    if final_destination.exists() and source_file_checksum(final_destination) != checksum:
        final_destination = destination.with_name(
            f"{destination.stem}-{checksum[:8]}{destination.suffix}"
        )
        suffix = 2
        while final_destination.exists() and source_file_checksum(final_destination) != checksum:
            final_destination = destination.with_name(
                f"{destination.stem}-{checksum[:8]}-{suffix}{destination.suffix}"
            )
            suffix += 1

    if source_file.resolve() == final_destination.resolve():
        return final_destination

    if final_destination.exists() and source_file_checksum(final_destination) == checksum:
        source_file.unlink()
        return final_destination

    shutil.move(str(source_file), str(final_destination))
    return final_destination


def retain_processed_source(source_file: Path, destination: Path, checksum: str) -> Path:
    """Move an accepted source into processed storage without clobbering a different file."""
    return retain_source_file(source_file, destination, checksum)


def retain_lab_source_for_import(
    root: Path,
    processed_root: Path,
    person_id: str,
    observed_at: str,
    source_file: Path,
) -> tuple[str, str]:
    checksum = source_file_checksum(source_file)
    processed_source = processed_lab_source_path(
        processed_root,
        person_id,
        observed_at,
        source_file,
    )
    processed_source = retain_processed_source(source_file, processed_source, checksum)
    return checksum, str(processed_source.relative_to(root))


def quarantine_lab_source_for_import(
    root: Path,
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
) -> tuple[str, str]:
    checksum = source_file_checksum(source_file)
    quarantine_source = quarantined_lab_source_path(quarantine_root, person_id, source_file)
    quarantine_source = retain_source_file(source_file, quarantine_source, checksum)
    return checksum, str(quarantine_source.relative_to(root))


def quarantine_lab_import_error(
    conn: sqlite3.Connection | None,
    root: Path,
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
    source_type: str,
    message: str,
) -> str:
    checksum, quarantine_source_path = quarantine_lab_source_for_import(
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
            event_type=AUDIT_LAB_IMPORT_QUARANTINED,
            subject_type="lab_source",
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


def record_lab_import_validation_error(
    errors: list[str],
    *,
    review_only: bool,
    conn: sqlite3.Connection | None,
    root: Path,
    quarantine_root: Path,
    person_id: str | None,
    source_file: Path,
    source_type: str,
    reason: str,
) -> None:
    if review_only:
        errors.append(f"{source_file.name}: {reason}")
        return

    message = f"validation failed: {reason}"
    try:
        quarantine_path = quarantine_lab_import_error(
            conn,
            root,
            quarantine_root,
            person_id,
            source_file,
            source_type,
            message,
        )
        errors.append(f"{source_file.name}: quarantined at {quarantine_path}: {reason}")
    except (OSError, sqlite3.Error, LabSyncError) as exc:
        errors.append(f"{source_file.name}: {reason}; failed to quarantine source: {exc}")


def infer_person_id_from_pdf_filename(path: Path) -> str | None:
    base = path.stem
    if "__" not in base:
        return None
    person_prefix = base.split("__", 1)[0].strip()
    if not person_prefix:
        return None
    return person_prefix


def infer_person_id_from_inbox_path(path: Path, inbox_dir: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(inbox_dir.resolve())
    except ValueError:
        return None

    if len(relative.parts) >= 3 and relative.parts[1] == "labs":
        return relative.parts[0].strip() or None

    return None


def infer_person_id_from_processed_lab_path(path: Path, processed_root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(processed_root.resolve())
    except ValueError:
        return None

    if len(relative.parts) >= 3 and relative.parts[1] == "Labs":
        return relative.parts[0].strip() or None

    return None


def detect_pdf_provider(text: str) -> str:
    lowered = text.lower()
    if "quest diagnostics" in lowered:
        return "quest"
    if "mychart" in lowered:
        return "mychart"
    if "labcorp" in lowered or "laboratory corporation of america" in lowered:
        return "labcorp"
    if "hospital" in lowered or "medical center" in lowered:
        return "hospital"
    return "generic-pdf"


_RANGE_BAR_LINE = re.compile(r"^[\d\s.]+$")
_DOUBLED_NUMBER = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(\1)(?![\d.])")


def clean_pdf_range_bar_artifacts(text: str) -> str:
    """Remove doubled-number artifacts produced by MyChart/LabCorp range-bar rendering.

    When pypdf extracts a PDF with a graphical reference-range bar, each boundary label
    is extracted twice with no separator, e.g. '3.43.4 10.810.8' instead of '3.4 10.8'.
    The deduplication is applied only to lines that contain nothing but digits, spaces,
    and dots — the exact signature of a range-bar line — so dates, times, phone numbers,
    and prose are never touched. It additionally requires at least two whitespace-
    separated tokens, because a range bar always carries both bounds while a lone
    number on its own line is the measured value.
    """
    cleaned: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # A range bar carries both bounds, so it always has at least two tokens.
        # A lone number on its own line is the measured value, never a range
        # bar: collapsing it turned real readings into wrong ones (33.5 -> 3.5,
        # 88.8 -> 8.8, 44.3 -> 4.3, 22 -> 2), because the doubled-number pattern
        # also matches a repeated leading digit inside a genuine number.
        if stripped and len(stripped.split()) >= 2 and _RANGE_BAR_LINE.match(stripped):
            line = _DOUBLED_NUMBER.sub(r"\1", line)
        cleaned.append(line)
    return "\n".join(cleaned)


def extract_pdf_text(path: Path) -> str:
    # First try embedded PDF text extraction.
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise LabSyncError(
            "PDF import requires 'pypdf'. Install with: python -m pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(str(path))
        parts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise LabSyncError(f"failed to read PDF {path}: {exc}") from exc

    text = "\n".join(parts).strip()
    if text:
        return clean_pdf_range_bar_artifacts(text)

    # Fallback OCR if the PDF appears image-based.
    try:
        pytesseract = importlib.import_module("pytesseract")
        pdf2image = importlib.import_module("pdf2image")
        convert_from_path = pdf2image.convert_from_path
    except ImportError as exc:
        raise LabSyncError(
            "PDF has no extractable text and OCR dependencies are missing. "
            "Install with: python -m pip install pdf2image pytesseract"
        ) from exc

    try:
        images: list[Any] = cast(list[Any], convert_from_path(str(path)))  # pyright: ignore[reportUnknownVariableType]
    except Exception as exc:
        raise LabSyncError(f"failed to render PDF pages for OCR in {path}: {exc}") from exc

    ocr_parts: list[str] = []
    for image in images:
        ocr_parts.append(str(pytesseract.image_to_string(image)))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]

    ocr_text = "\n".join(ocr_parts).strip()
    if not ocr_text:
        raise LabSyncError(f"could not extract text from PDF {path}")
    return ocr_text


def parse_date_fragment(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract_observed_at_from_text(text: str) -> str | None:
    month_name = (
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"(?:uary|ch|il|e|y|ust|tember|ober|ember)?"
    )
    collected_named = re.search(
        rf"Collected(?: on|:)?\s*({month_name}\s+\d{{1,2}},\s+\d{{4}})",
        text,
        flags=re.IGNORECASE,
    )
    if collected_named:
        named_value = collected_named.group(1)
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(named_value, fmt).date().isoformat()
            except ValueError:
                continue

    collected_inline = re.search(
        r"Collected:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.IGNORECASE
    )
    if collected_inline:
        parsed = parse_date_fragment(collected_inline.group(1))
        if parsed:
            return parsed

    collected_next_line = re.search(
        r"Collected:\s*\n\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, flags=re.IGNORECASE
    )
    if collected_next_line:
        parsed = parse_date_fragment(collected_next_line.group(1))
        if parsed:
            return parsed

    patterns = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        parsed = parse_date_fragment(match.group(1))
        if parsed:
            return parsed
    return None


def _normalize_grouped_numbers(value: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", value)


def _extract_first_float(value: str) -> float | None:
    normalized = _normalize_grouped_numbers(value)
    match = re.search(r"[+-]?\d+(?:\.\d+)?", normalized)
    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalize_qualitative_value(value_text: str) -> str:
    normalized = " ".join(value_text.split()).strip()
    normalized = re.sub(r"\s*\[[^\]]+\]\s*$", "", normalized).strip()
    return normalized


def _normalize_mychart_measured_value_text(value_text: str) -> str:
    normalized = _normalize_qualitative_value(value_text)
    normalized = re.sub(r"(?i)(?<=[0-9])\s*(?:low|high|critical)$", "", normalized).strip()

    numeric_match = re.search(r"[+-]?\d+(?:\.\d+)?", normalized)
    if numeric_match:
        token = numeric_match.group(0)
        collapsed = _collapse_repeated_numeric_artifact(token)
        if collapsed != token:
            normalized = (
                f"{normalized[: numeric_match.start()]}"
                f"{collapsed}"
                f"{normalized[numeric_match.end() :]}"
            )

    return normalized


def _collapse_repeated_numeric_artifact(raw_token: str) -> str:
    token = raw_token.strip()
    if not token.isdigit() or len(token) < 4 or len(token) % 2 != 0:
        return token

    midpoint = len(token) // 2
    left = token[:midpoint]
    right = token[midpoint:]
    if left != right:
        return token

    return left


def _collapse_repeated_numeric_token(raw_token: str) -> str:
    token = raw_token.strip().replace(" ", "")
    if len(token) < 4 or len(token) % 2 != 0:
        return token

    midpoint = len(token) // 2
    left = token[:midpoint]
    right = token[midpoint:]
    if left != right:
        return token

    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", left):
        return token

    return left


def _is_repeated_reference_marker_artifact(
    line: str,
    ref_low: float | None,
    ref_high: float | None,
) -> bool:
    candidate = _collapse_repeated_numeric_token(line)
    if candidate == line.strip().replace(" ", ""):
        return False

    try:
        value = float(candidate)
    except ValueError:
        return False

    for bound in (ref_low, ref_high):
        if bound is None:
            continue
        if abs(value - bound) < 1e-9:
            return True

    return False


def _extract_mychart_numeric_value(value_line: str) -> float | None:
    normalized = _normalize_grouped_numbers(value_line)
    match = re.match(r"\s*(?:[<>]=?\s*)?(?P<number>[+-]?\d+(?:\.\d+)?)", normalized)
    if not match:
        return None

    token = match.group("number")
    collapsed = _collapse_repeated_numeric_artifact(token)
    if collapsed != token:
        normalized = (
            f"{normalized[: match.start('number')]}{collapsed}{normalized[match.end('number') :]}"
        )

    return _extract_first_float(normalized)


def _parse_reference_range(reference_text: str) -> tuple[float | None, float | None]:
    normalized = " ".join(reference_text.lower().split())
    normalized = _normalize_grouped_numbers(normalized)

    interval = re.search(r"([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)", normalized)
    if interval:
        return float(interval.group(1)), float(interval.group(2))

    upper = re.search(r"<\s*(?:or\s*=\s*)?([+-]?\d+(?:\.\d+)?)", normalized)
    if upper:
        return None, float(upper.group(1))

    lower = re.search(r">\s*(?:or\s*=\s*)?([+-]?\d+(?:\.\d+)?)", normalized)
    if lower:
        return float(lower.group(1)), None

    return None, None


def _looks_like_quest_noise_line(line: str) -> bool:
    lowered = line.lower()
    if lowered in {"analyte", "value", "reference range:"}:
        return True

    noise_prefixes = [
        "dob:",
        "sex:",
        "phone:",
        "patient id:",
        "age:",
        "fasting:",
        "specimen:",
        "requisition:",
        "report status:",
        "collected:",
        "received:",
        "reported:",
        "client #:",
        "performing sites",
        "laboratory director:",
        "key",
        "priority out of range",
        "out of range",
        "these results have been sent",
        "your receipt of these results",
        "quest, quest diagnostics",
        "privacy policy",
    ]
    if any(lowered.startswith(prefix) for prefix in noise_prefixes):
        return True

    if re.match(r"^\d+\s*/\s*\d+$", line):
        return True
    if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", line):
        return True
    if re.match(r"^[A-Z]+,[A-Z]+ \([A-Z0-9]+\)$", line):
        return True

    return False


def _looks_like_probable_unit(line: str) -> bool:
    normalized = line.strip()
    if not normalized or len(normalized) > 20:
        return False
    if normalized.lower().startswith("http"):
        return False
    if ":" in normalized:
        return False
    if len(normalized.split()) > 3:
        return False
    return bool(re.search(r"[%A-Za-z/]", normalized))


def _is_mychart_noise_line(line: str) -> bool:
    lowered = line.lower()
    if not line:
        return True

    noise_prefixes = [
        "name:",
        "dob:",
        "mrn:",
        "pcp:",
        "legal name:",
        "results",
        "ordering provider:",
        "collection date:",
        "result date:",
        "result status:",
        "resulting lab:",
        "specimens:",
        "mychart",
        "reference range updated",
        "please note",
        # LabCorp/MyChart per-analyte provenance block lines
        "specimen received",
        "lab test performed",
        "labcorp",
        "laboratory corporation",
    ]
    if any(lowered.startswith(prefix) for prefix in noise_prefixes):
        return True

    if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", line):
        return True
    if re.match(r"^\d+\s*/\s*\d+$", line):
        return True

    # Chart axis labels often appear as multiple numeric tokens on one line.
    if re.match(r"^(?:[<>]=?\s*)?[\d\.]+(?:\s+[\d\.]+)+$", line):
        return True

    # Duplicated compact numeric sequences like "0.10.1 1.21.2" are graph guides, not values.
    if re.match(r"^[\d\.\s-]+$", line) and "." in line and len(re.findall(r"\d", line)) >= 6:
        return True

    # Standalone large integers (≥7 digits, no decimal) are phone numbers or zip codes, not values.
    if re.fullmatch(r"\d{7,}", line.strip()):
        return True

    return False


def _looks_like_mychart_test_name(line: str) -> bool:
    if _is_mychart_noise_line(line):
        return False
    if len(line) > 90:
        return False
    if line.lower().startswith("normal range:") or line.lower().startswith("normal value:"):
        return False
    if line.lower() in {"value", "comment", "see below"}:
        return False
    return bool(re.search(r"[A-Za-z]", line))


def _is_probable_address_or_org_line(line: str) -> bool:
    lowered = line.lower()
    if any(
        token in lowered
        for token in ["clia", "lab director", "issaquah", "wa ", "dr", "phone", "provider"]
    ):
        return True
    if re.search(r"\b\d{3,}\b", line) and any(
        token in lowered for token in ["blakely", "suite", "street", "ave"]
    ):
        return True
    return False


def _parse_normal_line(normal_line: str) -> tuple[float | None, float | None, str]:
    raw = normal_line.strip()
    is_normal_value = raw.lower().startswith("normal value:")
    value = raw.split(":", 1)[1].strip() if ":" in raw else raw
    value = _normalize_grouped_numbers(value)
    qualitative_unit_match = (
        re.search(r"\s(?P<unit>/[A-Za-z][A-Za-z0-9/*^.\-]*)$", value) if is_normal_value else None
    )

    if is_normal_value:
        has_interval = bool(re.search(r"[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?", value))
        scalar_match = None if has_interval else re.match(r"([+-]?\d+(?:\.\d+)?)\s+(.+)$", value)
        if scalar_match:
            scalar = float(scalar_match.group(1))
            unit = scalar_match.group(2).strip()
            if unit.lower().startswith("see ref"):
                unit = ""
            return scalar, scalar, unit

    ref_low, ref_high = _parse_reference_range(value)
    threshold_unit = ""
    if ref_low is None and ref_high is None:
        threshold = re.search(
            r"(?P<num>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%][A-Za-z0-9/%\-]*)?\s+or\s+(?P<dir>below|above)",
            value,
            flags=re.IGNORECASE,
        )
        if not threshold:
            threshold = re.search(
                r"(?P<dir>below|above)\s*(?P<num>[+-]?\d+(?:\.\d+)?)(?:\s*(?P<unit>[A-Za-z%][A-Za-z0-9/%\-]*))?",
                value,
                flags=re.IGNORECASE,
            )
        if threshold:
            threshold_unit = str(threshold.groupdict().get("unit") or "").strip()
            bound = float(str(threshold.group("num")))
            direction = str(threshold.group("dir")).lower()
            if direction == "below":
                ref_high = bound
            elif direction == "above":
                ref_low = bound
    unit = ""

    interval_match = re.search(
        r"[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?\s*(?P<unit>[^\d].*)", value
    )
    if interval_match:
        unit = interval_match.group("unit").strip()
    else:
        comparator_match = re.search(
            r"(?:<|>)\s*(?:or\s*=\s*)?[+-]?\d+(?:\.\d+)?\s*(?P<unit>.+)$", value
        )
        if comparator_match:
            unit = comparator_match.group("unit").strip()

    if not unit and " per " in value.lower() and not is_normal_value:
        unit = value

    if not unit and threshold_unit:
        unit = threshold_unit

    if not unit and qualitative_unit_match:
        unit = qualitative_unit_match.group("unit")

    qualitative_reference_without_unit = is_normal_value and bool(
        re.fullmatch(r"none seen(?:/few)?", value, flags=re.IGNORECASE)
    )
    if not unit and not qualitative_reference_without_unit:
        trailing_unit = re.search(r"(%|[A-Za-z][A-Za-z0-9/%\-]+)$", value)
        if trailing_unit:
            candidate = trailing_unit.group(1)
            is_bare_qualitative_reference = is_normal_value and trailing_unit.start() == 0
            if not is_bare_qualitative_reference and candidate.lower() not in {
                "notes",
                "below",
                "value",
                "range",
            }:
                unit = candidate

    if unit and not re.search(r"[A-Za-z%]", unit):
        unit = ""

    if unit.lower().startswith("see ref"):
        unit = ""

    if unit.lower() in {"above", "below"}:
        unit = ""

    return ref_low, ref_high, unit


def _is_reference_bounds_echo(
    line: str,
    ref_low: float | None,
    ref_high: float | None,
) -> bool:
    """True when *line* is the block's reference bounds repeated as bare numbers.

    A MyChart block echoes its stated range as a bare numeric pair immediately
    before the measured value (``Normal range: 47 - 123 U/L`` ... ``47 123`` ...
    ``65``). That echo is a precise anchor for the value, which matters when a
    long age-banded reference table sits between the two and pushes the value
    out of any fixed-size search window.
    """
    parts = line.split()
    if not parts or len(parts) > 2:
        return False
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return False
    bounds = [bound for bound in (ref_low, ref_high) if bound is not None]
    if not bounds or len(numbers) != len(bounds):
        return False
    return all(abs(number - bound) < 1e-9 for number, bound in zip(numbers, bounds, strict=True))


def _pick_mychart_value_line(
    lines: list[str],
    start_index: int,
    end_index: int,
    ref_low: float | None = None,
    ref_high: float | None = None,
) -> str | None:
    # MyChart graph exports often put the measured value immediately after one
    # or more axis-marker lines. Prefer that compact result before later
    # "Value" blocks, which may belong to a neighboring test.
    for index in range(start_index, min(start_index + 5, end_index, len(lines))):
        line = lines[index]
        lowered = line.lower()
        if _is_mychart_noise_line(line):
            continue
        if _is_repeated_reference_marker_artifact(line, ref_low, ref_high):
            continue
        if lowered == "value":
            continue
        if lowered in {"comment", "see below"}:
            continue
        if lowered.startswith("normal range:") or lowered.startswith("normal value:"):
            continue
        if _looks_like_direct_mychart_graph_value_line(line):
            return line

    # The reference-bounds echo anchors the value precisely. Preferred over the
    # positional scans below because an age-banded reference table can separate
    # the "Normal range:" line from the measurement by dozens of lines.
    for index in range(start_index, min(end_index, len(lines))):
        if not _is_reference_bounds_echo(lines[index], ref_low, ref_high):
            continue
        for value_index in range(index + 1, min(index + 4, end_index, len(lines))):
            candidate = lines[value_index]
            lowered_candidate = candidate.lower()
            if _is_mychart_noise_line(candidate):
                continue
            if lowered_candidate.startswith(("normal range:", "normal value:")):
                break
            if _looks_like_direct_mychart_graph_value_line(candidate):
                return candidate
        break

    # Prefer explicit "Value" sections when present. They are more reliable than
    # nearby risk-band or threshold lines such as "Low <1.00".
    for index in range(start_index, min(end_index, len(lines))):
        if lines[index].lower() != "value":
            continue
        for value_index in range(index + 1, min(end_index, len(lines))):
            candidate = lines[value_index]
            lowered_candidate = candidate.lower()
            if _is_mychart_noise_line(candidate):
                continue
            if _is_repeated_reference_marker_artifact(candidate, ref_low, ref_high):
                continue
            if lowered_candidate in {"comment", "see below"}:
                continue
            if lowered_candidate.startswith("normal range:") or lowered_candidate.startswith(
                "normal value:"
            ):
                continue
            if _is_explicit_mychart_value_candidate(candidate):
                return candidate

    # Next, prefer nearby direct values immediately following the normal-range line.
    for index in range(start_index, min(start_index + 5, end_index, len(lines))):
        line = lines[index]
        lowered = line.lower()
        if _is_mychart_noise_line(line):
            continue
        if _is_repeated_reference_marker_artifact(line, ref_low, ref_high):
            continue
        if _looks_like_narrative_mychart_value_line(line):
            continue
        if lowered == "value":
            continue
        if lowered in {"comment", "see below"}:
            continue
        if lowered.startswith("normal range:") or lowered.startswith("normal value:"):
            continue
        if ";" in line and any(
            token in lowered for token in ["negative", "positive", "indeterminate"]
        ):
            continue
        if _is_high_confidence_mychart_fallback_value_line(
            line,
            allow_plain_numeric=True,
        ):
            return line

    for index in range(start_index, min(end_index, len(lines))):
        line = lines[index]
        lowered = line.lower()
        if _is_mychart_noise_line(line):
            continue
        if _is_repeated_reference_marker_artifact(line, ref_low, ref_high):
            continue
        if _looks_like_narrative_mychart_value_line(line):
            continue
        if lowered == "value":
            continue
        if lowered in {"comment", "see below"}:
            continue
        if ";" in line and any(
            token in lowered for token in ["negative", "positive", "indeterminate"]
        ):
            # This is usually the threshold reference sentence, not the measured value.
            continue
        if lowered.startswith("normal range:") or lowered.startswith("normal value:"):
            continue
        if _is_high_confidence_mychart_fallback_value_line(
            line,
            allow_plain_numeric=True,
        ):
            return line
    return None


def _is_explicit_mychart_value_candidate(line: str) -> bool:
    lowered = line.lower().strip()
    if not lowered or len(line) > 240:
        return False
    if lowered in {"value", "comment", "see below"}:
        return False
    if lowered.startswith(("normal range:", "normal value:", "http://", "https://")):
        return False
    return not _is_mychart_noise_line(line)


def _looks_like_direct_mychart_graph_value_line(line: str) -> bool:
    lowered = line.lower().strip()
    if not lowered:
        return False
    if re.fullmatch(
        r"[<>]?\s*[+-]?\d+(?:\.\d+)?(?:\s+(?:low|high|critical))?",
        lowered,
    ):
        return True
    return lowered in {"negative", "positive", "indeterminate"}


def _is_compact_mychart_value_line(line: str) -> bool:
    lowered = line.lower().strip()
    if not lowered or _is_mychart_noise_line(line):
        return False
    if lowered in {"value", "comment", "see below"}:
        return False
    if len(line) > 24:
        return False
    if re.fullmatch(r"[<>]=?\s*[+-]?\d+(?:\.\d+)?(?:\s*[A-Za-z%][A-Za-z0-9/%\-]*)?", line):
        return True
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", line):
        return True
    return lowered in {"negative", "positive", "indeterminate"}


def _looks_like_narrative_mychart_value_line(line: str) -> bool:
    lowered = line.lower()
    if _is_mychart_noise_line(line):
        return False
    if re.fullmatch(
        r"[A-Za-z][A-Za-z /-]{0,40}:\s*(?:"
        r"[<>]=?\s*[+-]?\d+(?:\.\d+)?|"
        r"[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?)",
        line.strip(),
    ):
        return True
    narrative_tokens = [
        "diagnostic",
        "criteria",
        "considers",
        "goal of diabetic therapy",
        "equation",
    ]
    if any(token in lowered for token in narrative_tokens):
        return True
    if lowered.startswith("diabetes:"):
        return True
    if len(line) > 24:
        return True
    return len(re.findall(r"[+-]?\d+(?:\.\d+)?", line)) > 1


def _is_high_confidence_mychart_fallback_value_line(
    line: str, allow_plain_numeric: bool = False
) -> bool:
    lowered = line.lower().strip()
    if not lowered or _is_mychart_noise_line(line):
        return False
    if lowered in {"value", "comment", "see below"}:
        return False

    # Avoid stealing plain numeric lines from neighboring tests.
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", lowered):
        return allow_plain_numeric

    if any(token in lowered for token in {"negative", "positive", "indeterminate"}):
        return True

    if re.search(r"[<>]=?\s*[+-]?\d", lowered):
        return True

    return bool(re.search(r"[+-]?\d+(?:\.\d+)?\s*[a-z%]", lowered))


def _find_mychart_test_name(lines: list[str], normal_index: int) -> tuple[str, int] | None:
    for probe in range(normal_index - 1, max(normal_index - 6, -1), -1):
        candidate = lines[probe]
        if not _looks_like_mychart_test_name(candidate):
            continue
        lowered = candidate.lower()
        if "collected on" in lowered:
            continue
        if _is_probable_address_or_org_line(candidate):
            continue
        if lowered.startswith("normal range:") or lowered.startswith("normal value:"):
            continue
        return candidate, probe
    return None


def _looks_like_unreferenced_mychart_test_name(line: str) -> bool:
    if not _looks_like_mychart_test_name(line):
        return False
    stripped = line.strip()
    if (
        stripped.startswith("-")
        or stripped.endswith((".", "?", "!"))
        or ":" in stripped
        or len(stripped.split()) < 2
    ):
        return False
    if stripped == "%" or (
        " " not in stripped and re.fullmatch(r"[A-Za-z0-9.*^%]+/[A-Za-z0-9.*^%]+", stripped)
    ):
        return False
    return not any(token in line for token in (";", "<", ">"))


def _parse_mychart_bullet_result_line(line: str) -> tuple[str, str, str] | None:
    # Example: "- Example Test Negative unit 01"
    match = re.match(
        r"^-\s*(?P<test>.+?)\s+(?P<value>Negative|Positive(?:\s*\[[^\]]+\])?|Note:|[<>]=?\s*\d+(?:\.\d+)?)\s+(?P<unit>[A-Za-z/%][A-Za-z0-9/%\-]*)\s+\d+\s*$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    test_name = match.group("test").strip().lstrip("-").strip()
    value_text = _normalize_qualitative_value(match.group("value"))
    unit_text = match.group("unit").strip()
    return test_name, value_text, unit_text


def _resolve_mychart_followup_bullet_value(
    lines: list[str],
    start_index: int,
    test_name: str,
    current_value_text: str,
) -> str:
    if current_value_text.lower() not in {"note:", "see final results"}:
        return current_value_text

    base_token = test_name.split(",", 1)[0].strip().lower()
    leading_word = base_token.split()[0] if base_token.split() else base_token
    for probe in range(start_index + 1, min(start_index + 8, len(lines))):
        candidate = lines[probe]
        if not candidate.startswith("-"):
            continue
        match = re.match(
            r"^-\s*(?P<label>.+?)\s+(?P<value>Positive(?:\s*\[[^\]]+\])?|Negative(?:\s*\[[^\]]+\])?)(?:\s+[A-Za-z/%][A-Za-z0-9/%\-]*)?\s+\d+\s*$",
            candidate,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        label = match.group("label").strip().lower()
        if label.startswith(base_token) or (leading_word and label.startswith(leading_word)):
            return _normalize_qualitative_value(match.group("value"))

    return current_value_text


_POCT_TEST_LINE = re.compile(
    r"^POCT\s+(?P<name>[A-Za-z0-9][A-Za-z0-9 ().,/+&\-]+),\s*UA$", re.MULTILINE
)
_POCT_RESULT_PARTS = re.compile(r"^(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>\S.*)?$")


def _looks_like_poct_report(text: str) -> bool:
    """Return True when the text contains POCT test lines in the three-line MyChart format."""
    matches = _POCT_TEST_LINE.findall(text)
    return len({m.strip().casefold() for m in matches}) >= 2


def parse_labs_from_poct_text(text: str, person_id: str, source_label: str) -> list[dict[str, Any]]:
    observed_at = extract_observed_at_from_text(text)
    if not observed_at:
        raise LabSyncError(f"unable to infer observed_at date in {source_label}")

    lines = [" ".join(raw.split()) for raw in text.splitlines() if raw.strip()]
    labs: list[dict[str, Any]] = []
    seen: set[str] = set()

    i = 0
    while i < len(lines):
        match = _POCT_TEST_LINE.match(lines[i])
        if match and i + 2 < len(lines):
            test_name = match.group("name").strip()
            if lines[i + 1].casefold() == "value":
                result_line = lines[i + 2]
                if _POCT_TEST_LINE.match(result_line):
                    i += 1
                    continue
                test_key = test_name.casefold()
                if test_key in seen:
                    i += 3
                    continue
                seen.add(test_key)
                parsed = _parse_poct_result(result_line)
                labs.append(
                    lab_payload_from_mapping(
                        {
                            "person_id": person_id,
                            "test_name": f"POCT {test_name}, UA",
                            "observed_at": observed_at,
                            "value": parsed["value"],
                            "value_text": parsed["value_text"],
                            "unit": parsed["unit"],
                            "reference_range_low": None,
                            "reference_range_high": None,
                            "source_type": "pdf:mychart",
                        },
                        source_label,
                    )
                )
                i += 3
                continue
        i += 1

    if not labs:
        raise LabSyncError(f"no POCT lab rows extracted from PDF {source_label}")

    return labs


def _parse_poct_result(line: str) -> dict[str, Any]:
    match = _POCT_RESULT_PARTS.match(line)
    if match and match.group("value"):
        value = match.group("value")
        unit = (match.group("unit") or "").strip()
        return {"value": value, "value_text": line, "unit": unit}
    return {"value": None, "value_text": line, "unit": None}


_POCT_DOCUMENT_HEADER = re.compile(r"^POCT\s+\S", re.MULTILINE)
_POCT_LOT_OR_DATE = re.compile(r"^(\d+[A-Z]\d+[A-Z0-9]*|[A-Z]\d+[A-Z0-9]*|\d{1,2}/\d{1,2}/\d{4})$")
_POCT_SKIP_TEST_WORDS = frozenset(
    {
        "control",
        "cntrl",
        "intrnl",
        "lot",
        "expiration",
        "location",
        "bottle",
        "developer",
        "strip",
        "collection",
        "performing",
        "card",
    }
)


def _looks_like_poct_general_report(text: str) -> bool:
    """Return True when the text has a POCT document header and Value/result pairs."""
    if not _POCT_DOCUMENT_HEADER.search(text):
        return False
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    for i in range(len(lines) - 2):
        if lines[i + 1] == "Value" and lines[i] != "Value":
            return True
    return False


def _is_poct_skip_test(name: str) -> bool:
    """Return True when the test name looks like a QC, lot-number, or metadata entry."""
    lower = name.lower()
    return any(w in lower for w in _POCT_SKIP_TEST_WORDS)


def _is_poct_skip_result(result: str) -> bool:
    """Return True when the result value looks like a lot number, date, or address."""
    if len(result) > 40:
        return True
    return bool(_POCT_LOT_OR_DATE.match(result))


def parse_labs_from_poct_general_text(
    text: str, person_id: str, source_label: str
) -> list[dict[str, Any]]:
    """Parse POCT documents whose test lines don't follow the 'POCT <name>, UA' pattern.

    Handles formats like POCT OCCULT BLOOD STOOL where individual test names
    are plain strings (e.g. 'Fecal Occult Blood') followed by 'Value' and a
    result.  Lot numbers, expiration dates, location entries, and other
    non-clinical fields are excluded.
    """
    observed_at = extract_observed_at_from_text(text)
    if not observed_at:
        raise LabSyncError(f"unable to infer observed_at date in {source_label}")

    lines = [" ".join(raw.split()) for raw in text.splitlines() if raw.strip()]
    labs: list[dict[str, Any]] = []
    seen: set[str] = set()

    i = 0
    while i < len(lines):
        if (
            i + 2 < len(lines)
            and lines[i + 1] == "Value"
            and lines[i] != "Value"
            and not _is_poct_skip_test(lines[i])
            and not _is_poct_skip_result(lines[i + 2])
            and not _POCT_TEST_LINE.match(lines[i])  # already handled by UA parser
        ):
            test_name = lines[i]
            result = lines[i + 2]
            key = test_name.casefold()
            if key not in seen:
                seen.add(key)
                parsed = _parse_poct_result(result)
                labs.append(
                    lab_payload_from_mapping(
                        {
                            "person_id": person_id,
                            "test_name": test_name,
                            "observed_at": observed_at,
                            "value": parsed["value"],
                            "value_text": parsed["value_text"],
                            "unit": parsed["unit"],
                            "reference_range_low": None,
                            "reference_range_high": None,
                            "source_type": "pdf:mychart",
                        },
                        source_label,
                    )
                )
            i += 3
            continue
        i += 1

    if not labs:
        raise LabSyncError(f"no POCT general lab rows extracted from {source_label}")
    return labs


def _mychart_lab_payload(
    *,
    person_id: str,
    test_name: str,
    observed_at: str,
    value_line: str,
    unit: str,
    reference_range_low: float | None,
    reference_range_high: float | None,
    source_label: str,
) -> dict[str, Any]:
    value_text = _normalize_mychart_measured_value_text(value_line)
    return lab_payload_from_mapping(
        {
            "person_id": person_id,
            "test_name": test_name,
            "observed_at": observed_at,
            "value": _extract_mychart_numeric_value(value_line),
            "value_text": value_text,
            "unit": unit,
            "reference_range_low": reference_range_low,
            "reference_range_high": reference_range_high,
            "source_type": "pdf:mychart",
        },
        source_label,
    )


def parse_labs_from_mychart_text(
    text: str, person_id: str, source_label: str
) -> list[dict[str, Any]]:
    observed_at = extract_observed_at_from_text(text)
    if not observed_at:
        raise LabSyncError(f"unable to infer observed_at date in {source_label}")

    lines = [" ".join(raw.split()) for raw in text.splitlines() if raw.strip()]
    labs: list[dict[str, Any]] = []
    seen_tests: set[str] = set()

    # Primary pass: parse blocks anchored by explicit normal range/value lines.
    for normal_index, line in enumerate(lines):
        lowered = line.lower()
        if not (lowered.startswith("normal range:") or lowered.startswith("normal value:")):
            continue

        test_entry = _find_mychart_test_name(lines, normal_index)
        if not test_entry:
            continue
        test_name, test_index = test_entry

        # Wide enough to span an age-banded reference table; the scan still
        # stops at the next block anchor, and the bounds echo pins the value.
        block_window = 60
        search_end = min(normal_index + block_window, len(lines))
        for probe in range(normal_index + 1, min(normal_index + block_window, len(lines))):
            lowered_probe = lines[probe].lower()
            if lowered_probe.startswith("normal range:") or lowered_probe.startswith(
                "normal value:"
            ):
                search_end = probe
                break

        ref_low, ref_high, unit_text = _parse_normal_line(line)
        value_line = _pick_mychart_value_line(
            lines,
            normal_index + 1,
            search_end,
            ref_low=ref_low,
            ref_high=ref_high,
        )
        allow_plain_numeric_fallback = lowered.startswith("normal range:")
        if (
            not value_line
            and test_index > 0
            and _is_compact_mychart_value_line(lines[test_index - 1])
            and _is_high_confidence_mychart_fallback_value_line(
                lines[test_index - 1],
                allow_plain_numeric=allow_plain_numeric_fallback,
            )
        ):
            value_line = lines[test_index - 1]
        if not value_line:
            continue

        if len(value_line) > 240:
            continue

        test_key = test_name.lower()
        if test_key in seen_tests:
            continue
        seen_tests.add(test_key)

        try:
            labs.append(
                _mychart_lab_payload(
                    person_id=person_id,
                    test_name=test_name,
                    observed_at=observed_at,
                    value_line=value_line,
                    unit=unit_text,
                    reference_range_low=ref_low,
                    reference_range_high=ref_high,
                    source_label=source_label,
                )
            )
        except LabSyncError:
            continue

    # Some MyChart result pages omit a reference range but retain an explicit
    # three-line test-name / Value / result block.
    if not _looks_like_poct_report(text) and not _looks_like_poct_general_report(text):
        for value_index, line in enumerate(lines):
            if line.lower() != "value" or value_index == 0:
                continue
            test_name = lines[value_index - 1]
            if not _looks_like_unreferenced_mychart_test_name(test_name):
                continue
            test_key = test_name.lower()
            if test_key in seen_tests:
                continue
            value_line = _pick_mychart_value_line(
                lines,
                value_index,
                min(value_index + 3, len(lines)),
            )
            if (
                not value_line
                or value_line.lstrip().startswith("/")
                or _extract_mychart_numeric_value(value_line) is not None
            ):
                continue
            try:
                labs.append(
                    _mychart_lab_payload(
                        person_id=person_id,
                        test_name=test_name,
                        observed_at=observed_at,
                        value_line=value_line,
                        unit="",
                        reference_range_low=None,
                        reference_range_high=None,
                        source_label=source_label,
                    )
                )
                seen_tests.add(test_key)
            except LabSyncError:
                continue

    # Secondary pass: parse Labcorp-style bullet result lines found in some MyChart exports.
    for index, line in enumerate(lines):
        if not line.startswith("-"):
            continue
        parsed = _parse_mychart_bullet_result_line(line)
        if not parsed:
            continue

        test_name, value_text, unit_text = parsed
        value_text = _resolve_mychart_followup_bullet_value(lines, index, test_name, value_text)
        if test_name.lower() in {"please", "comment"}:
            continue
        if value_text.lower() == "note:" and unit_text.lower() == "comment":
            continue
        ref_low = None
        ref_high = None
        if index + 1 < len(lines) and lines[index + 1].lower().startswith("- reference range:"):
            ref_low, ref_high = _parse_reference_range(lines[index + 1].split(":", 1)[1].strip())

        test_key = test_name.lower()
        if test_key in seen_tests:
            continue
        seen_tests.add(test_key)

        try:
            labs.append(
                lab_payload_from_mapping(
                    {
                        "person_id": person_id,
                        "test_name": test_name,
                        "observed_at": observed_at,
                        "value": _extract_first_float(value_text),
                        "value_text": value_text,
                        "unit": unit_text,
                        "reference_range_low": ref_low,
                        "reference_range_high": ref_high,
                        "source_type": "pdf:mychart",
                    },
                    source_label,
                )
            )
        except LabSyncError:
            continue

    if not labs:
        raise LabSyncError(
            f"no lab rows extracted from PDF {source_label}; review format or import manually"
        )

    return labs


def _parse_quest_analyte_groups(
    lines: list[str],
    start_index: int,
    person_id: str,
    observed_at: str,
    source_label: str,
    seen_tests: set[str],
) -> tuple[list[dict[str, Any]], int]:
    parsed_rows: list[dict[str, Any]] = []
    index = start_index

    while index + 3 < len(lines):
        if lines[index] == "Analyte" and index + 1 < len(lines) and lines[index + 1] == "Value":
            break

        test_name = lines[index]
        value_text = lines[index + 1]
        ref_marker = lines[index + 2]
        reference_text = lines[index + 3]

        if _looks_like_quest_noise_line(test_name) or ref_marker != "Reference Range:":
            index += 1
            continue

        unit_text = ""
        if index + 4 < len(lines):
            candidate_unit = lines[index + 4]
            if _looks_like_probable_unit(candidate_unit) and not _looks_like_quest_noise_line(
                candidate_unit
            ):
                unit_text = candidate_unit

        ref_low, ref_high = _parse_reference_range(reference_text)
        numeric_value = _extract_first_float(value_text)
        qualitative_value = _normalize_qualitative_value(value_text)

        test_key = test_name.lower()
        if test_key not in seen_tests:
            parsed_rows.append(
                lab_payload_from_mapping(
                    {
                        "person_id": person_id,
                        "test_name": test_name,
                        "observed_at": observed_at,
                        "value": numeric_value,
                        "value_text": qualitative_value,
                        "unit": unit_text,
                        "reference_range_low": ref_low,
                        "reference_range_high": ref_high,
                        "source_type": "pdf:quest",
                    },
                    source_label,
                )
            )
            seen_tests.add(test_key)

        index += 5 if unit_text else 4

    return parsed_rows, index


def parse_labs_from_quest_text(
    text: str, person_id: str, source_label: str
) -> list[dict[str, Any]]:
    observed_at = extract_observed_at_from_text(text)
    if not observed_at:
        raise LabSyncError(f"unable to infer observed_at date in {source_label}")

    lines = [" ".join(raw.split()) for raw in text.splitlines() if raw.strip()]
    labs: list[dict[str, Any]] = []
    seen_tests: set[str] = set()

    index = 0
    while index < len(lines):
        if lines[index] != "Analyte" or index + 1 >= len(lines) or lines[index + 1] != "Value":
            index += 1
            continue

        parsed_rows, next_index = _parse_quest_analyte_groups(
            lines,
            start_index=index + 2,
            person_id=person_id,
            observed_at=observed_at,
            source_label=source_label,
            seen_tests=seen_tests,
        )
        labs.extend(parsed_rows)
        index = max(next_index, index + 2)

    if not labs:
        raise LabSyncError(
            f"no lab rows extracted from PDF {source_label}; review format or import manually"
        )

    return labs


_STRUCTURED_RESULT_HEADER = (
    "test current result and flag previous result and date units reference interval"
)
_STRUCTURED_VALUE_TOKEN = r"(?:N/?A|(?:[<>]=?)?\s*[+-]?\d+(?:\.\d+)?)"
_STRUCTURED_REFERENCE_TOKEN = (
    r"(?:Not Estab\.|[<>]=?\s*[+-]?\d+(?:\.\d+)?|"
    r"[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?)"
)
_STRUCTURED_UNIT_TOKEN = r"(?:%|[A-Za-z][A-Za-z0-9%/.^\-]*)"
# A unit may carry one trailing body-surface-area qualifier separated by a
# space, as in eGFR's "mL/min/1.73 m2". Without this the space-delimited scan
# kept only the final token and persisted "m2" as the unit. Deliberately
# bounded to the area qualifier (ADR-018): units are not allowed to absorb
# arbitrary trailing words.
_STRUCTURED_AREA_QUALIFIER = r"(?:m2|m\^2|m\*2|m²|sq\.?\s?m)"
_STRUCTURED_UNIT_WITH_AREA = rf"(?:{_STRUCTURED_UNIT_TOKEN}\s{_STRUCTURED_AREA_QUALIFIER})"
_STRUCTURED_REFERENCE_AT_END = re.compile(
    rf"\s+(?P<reference>{_STRUCTURED_REFERENCE_TOKEN})\s*$",
    flags=re.IGNORECASE,
)
_STRUCTURED_UNIT_AT_END = re.compile(
    rf"\s+(?P<unit>{_STRUCTURED_UNIT_WITH_AREA}|{_STRUCTURED_UNIT_TOKEN})\s*$",
    flags=re.IGNORECASE,
)
_STRUCTURED_PREVIOUS_DATE_AT_END = re.compile(r"\s+\d{1,2}/\d{1,2}/\d{2,4}\s*$")
_STRUCTURED_FLAG_AT_END = re.compile(r"\s+(?:high|low|critical|alert)\s*$", flags=re.IGNORECASE)
_STRUCTURED_VALUE_AT_END = re.compile(
    rf"\s+(?P<value>{_STRUCTURED_VALUE_TOKEN})\s*$", flags=re.IGNORECASE
)
_STRUCTURED_SITE_CODE_AT_END = re.compile(r"\s+(?:A,\s*)?\d{2}\s*$", flags=re.IGNORECASE)
_STRUCTURED_UNIT_REFERENCE_AT_END = re.compile(
    rf"\s+{_STRUCTURED_UNIT_TOKEN}\s+{_STRUCTURED_REFERENCE_TOKEN}\s*$",
    flags=re.IGNORECASE,
)
_STRUCTURED_PREVIOUS_DATE_REFERENCE_AT_END = re.compile(
    rf"\s+\d{{1,2}}/\d{{1,2}}/\d{{2,4}}(?:\s+{_STRUCTURED_UNIT_TOKEN})?\s+"
    rf"{_STRUCTURED_REFERENCE_TOKEN}\s*$",
    flags=re.IGNORECASE,
)
_STRUCTURED_SITE_ROW_WITH_UNIT_AT_END = re.compile(
    rf"\s+(?:A,\s*)?\d{{2}}\s+\S+(?:\s+(?:high|low|critical|alert))?\s+"
    rf"{_STRUCTURED_UNIT_TOKEN}\s*$",
    flags=re.IGNORECASE,
)
_STRUCTURED_NARRATIVE_COMPARISON = re.compile(
    r"(?P<operator><=|>=|<|>)\s*(?P<value>[+-]?\d+(?:\.\d+)?)"
)


def _strip_structured_result_suffix(value: str, pattern: re.Pattern[str]) -> tuple[str, str]:
    match = pattern.search(value)
    if not match:
        return value, ""
    captured = next((group for group in match.groups() if group is not None), "")
    return value[: match.start()].rstrip(), captured.strip()


def _parse_structured_result_line(line: str) -> dict[str, str] | None:
    body, reference_text = _strip_structured_result_suffix(line, _STRUCTURED_REFERENCE_AT_END)
    body, unit = _strip_structured_result_suffix(body, _STRUCTURED_UNIT_AT_END)
    if not reference_text and not unit:
        return None
    if not reference_text and (unit.endswith(".") or (unit != "%" and "/" not in unit)):
        return None

    previous_date = _STRUCTURED_PREVIOUS_DATE_AT_END.search(body)
    if previous_date:
        body = body[: previous_date.start()].rstrip()
        previous_value = re.search(
            rf"\s+{_STRUCTURED_VALUE_TOKEN}\*?\s*$", body, flags=re.IGNORECASE
        )
        if not previous_value:
            return None
        body = body[: previous_value.start()].rstrip()

    body = _STRUCTURED_FLAG_AT_END.sub("", body).rstrip()
    current_value = _STRUCTURED_VALUE_AT_END.search(body)
    if not current_value:
        return None

    test_name = body[: current_value.start()].strip()
    if not test_name or not re.search(r"[A-Za-z]", test_name):
        return None

    return {
        "test_name": test_name,
        "value_text": current_value.group("value").replace(" ", ""),
        "unit": unit,
        "reference_text": reference_text,
    }


def _looks_like_unparsed_structured_result(line: str) -> bool:
    without_na = re.sub(r"N/?A", "", line, flags=re.IGNORECASE)
    if not re.search(r"[A-Za-z]", without_na):
        return False
    return bool(
        _STRUCTURED_UNIT_REFERENCE_AT_END.search(line)
        or _STRUCTURED_PREVIOUS_DATE_REFERENCE_AT_END.search(line)
        or _STRUCTURED_SITE_ROW_WITH_UNIT_AT_END.search(line)
    )


def _looks_like_wrapped_structured_test_name(line: str) -> bool:
    if not (2 <= len(line) <= 80) or not re.search(r"[A-Za-z]", line):
        return False
    if ":" in line or line.endswith(".") or re.search(r"\d", line):
        return False
    return not _looks_like_unparsed_structured_result(line)


def _is_structured_test_name_continuation(test_name: str) -> bool:
    return bool(_STRUCTURED_SITE_CODE_AT_END.search(test_name)) and len(test_name.split()) <= 3


def _parse_labs_from_structured_result_tables(
    text: str,
    person_id: str,
    observed_at: str,
    source_label: str,
    *,
    provider: str,
) -> list[dict[str, Any]] | None:
    lines = [" ".join(raw.split()) for raw in text.splitlines() if raw.strip()]
    if not any(line.casefold() == _STRUCTURED_RESULT_HEADER for line in lines):
        return None

    labs: list[dict[str, Any]] = []
    failures: list[str] = []
    inside_result_tables = False
    pending_test_name = ""
    pending_reference_lab_index: int | None = None

    for line in lines:
        if line.casefold() == _STRUCTURED_RESULT_HEADER:
            inside_result_tables = True
            pending_test_name = ""
            pending_reference_lab_index = None
            continue
        if not inside_result_tables:
            continue
        if line.casefold() == "reference range:":
            pending_reference_lab_index = len(labs) - 1 if labs else None
            pending_test_name = ""
            continue
        if pending_reference_lab_index is not None:
            comparisons = list(_STRUCTURED_NARRATIVE_COMPARISON.finditer(line))
            if comparisons:
                comparison = comparisons[-1]
                ref_low, ref_high = _parse_reference_range(
                    f"{comparison.group('operator')}{comparison.group('value')}"
                )
                lab = labs[pending_reference_lab_index]
                if lab["reference_range_low"] is None and ref_low is not None:
                    lab["reference_range_low"] = ref_low
                if lab["reference_range_high"] is None and ref_high is not None:
                    lab["reference_range_high"] = ref_high
            pending_reference_lab_index = None
            pending_test_name = ""
            continue

        parsed = _parse_structured_result_line(line)
        if parsed:
            test_name = parsed["test_name"]
            if pending_test_name and _is_structured_test_name_continuation(test_name):
                test_name = f"{pending_test_name} {test_name}"
            test_name = _STRUCTURED_SITE_CODE_AT_END.sub("", test_name).strip()
            ref_low, ref_high = _parse_reference_range(parsed["reference_text"])
            value_text = parsed["value_text"]
            numeric_value = (
                None if value_text.startswith(("<", ">")) else _extract_first_float(value_text)
            )
            labs.append(
                lab_payload_from_mapping(
                    {
                        "person_id": person_id,
                        "test_name": test_name,
                        "observed_at": observed_at,
                        "value": numeric_value,
                        "value_text": value_text,
                        "unit": parsed["unit"],
                        "reference_range_low": ref_low,
                        "reference_range_high": ref_high,
                        "source_type": f"pdf:{provider}",
                    },
                    source_label,
                )
            )
            pending_test_name = ""
            continue

        if _looks_like_unparsed_structured_result(line):
            failures.append(line)
            pending_test_name = ""
        elif _looks_like_wrapped_structured_test_name(line):
            pending_test_name = line
        else:
            pending_test_name = ""

    if failures:
        raise LabSyncError(
            f"could not parse every structured result row in {source_label}: "
            + "; ".join(failures[:3])
        )
    if not labs:
        raise LabSyncError(f"no structured lab rows extracted from PDF {source_label}")
    return labs


def parse_labs_from_pdf_text(text: str, person_id: str, source_label: str) -> list[dict[str, Any]]:
    observed_at = extract_observed_at_from_text(text)
    if not observed_at:
        raise LabSyncError(f"unable to infer observed_at date in {source_label}")

    provider = detect_pdf_provider(text)
    structured_rows = _parse_labs_from_structured_result_tables(
        text,
        person_id,
        observed_at,
        source_label,
        provider=provider,
    )
    if structured_rows is not None:
        return structured_rows

    if provider == "quest":
        try:
            return parse_labs_from_quest_text(text, person_id, source_label)
        except LabSyncError:
            # Some Quest exports flatten rows onto single lines; fallback to generic line parser.
            pass
    if provider == "mychart":
        try:
            return parse_labs_from_mychart_text(text, person_id, source_label)
        except LabSyncError:
            # Fallback to generic parser for unexpected MyChart exports.
            pass

    if _looks_like_poct_report(text):
        try:
            return parse_labs_from_poct_text(text, person_id, source_label)
        except LabSyncError:
            pass

    if _looks_like_poct_general_report(text):
        try:
            return parse_labs_from_poct_general_text(text, person_id, source_label)
        except LabSyncError:
            pass

    labs: list[dict[str, Any]] = []

    line_pattern = re.compile(
        r"^(?P<test>[A-Za-z0-9][A-Za-z0-9 ()/,+\-\.]{2,}?)\s+"
        r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*"
        r"(?P<unit>%|[A-Za-z][A-Za-z0-9/\^]*)?\s+"
        r"(?P<low>[+-]?\d+(?:\.\d+)?)\s*-\s*(?P<high>[+-]?\d+(?:\.\d+)?)\s*$"
    )

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        match = line_pattern.match(line)
        if not match:
            continue

        payload = lab_payload_from_mapping(
            {
                "person_id": person_id,
                "test_name": match.group("test").strip(),
                "observed_at": observed_at,
                "value": match.group("value"),
                "value_text": _normalize_qualitative_value(match.group("value")),
                "unit": (match.group("unit") or "").strip(),
                "reference_range_low": match.group("low"),
                "reference_range_high": match.group("high"),
                "source_type": f"pdf:{provider}",
            },
            source_label,
        )
        labs.append(payload)

    if not labs:
        raise LabSyncError(
            f"no lab rows extracted from PDF {source_label}; review format or import manually"
        )

    return labs


def review_pdf_extraction(
    path: Path,
    person_id: str,
    rows: list[dict[str, Any]],
    skip_prompt: bool,
    review_only: bool,
    *,
    show_output: bool = True,
) -> bool:
    if show_output:
        print(f"\nReview extracted labs for {path.name} (person_id={person_id}):")
        for index, row in enumerate(rows, start=1):
            print(
                f"{index:>2}. {row['test_name']} = {row['value']} {row['unit']} "
                f"(ref {row['reference_range_low']} - {row['reference_range_high']}) "
                f"at {row['observed_at']}"
            )

    if skip_prompt:
        return True

    # Test runs should never block on interactive confirmation.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True

    if not os.isatty(0):
        raise LabSyncError(
            "PDF import requires confirmation prompt; rerun with --approve-pdf "
            "in non-interactive mode"
        )

    action_label = "continue review" if review_only else "write to vault/database"
    response = input(f"Approve extracted values and {action_label}? [y/N]: ").strip().lower()
    return response in {"y", "yes"}


def person_ids_with_existing_labs(vault_root: Path) -> set[str]:
    records_dir = vault_root / "Records"
    if not records_dir.exists():
        return set()

    person_ids: set[str] = set()
    for labs_dir in records_dir.glob("*/Labs"):
        if not labs_dir.is_dir():
            continue
        person_id = labs_dir.parent.name
        if person_id.startswith("_"):
            continue
        note_paths = [path for path in labs_dir.glob("*.md") if not path.name.startswith("_")]
        try:
            has_rows = bool(build_latest_lab_results_from_notes(note_paths))
        except LabSyncError:
            continue
        if has_rows:
            person_ids.add(person_id)

    return person_ids


def import_label_for_lab_note(source_file: Path, observed_at: str, retained_source: bool) -> str:
    label = source_file.stem
    if retained_source:
        date_prefix = observed_at.split("T", 1)[0]
        label = source_stem_without_redundant_date_prefix(label, date_prefix)
    return slugify_filename(label)


def validate_reprocessed_lab_person(rows: list[dict[str, Any]], person_id: str) -> None:
    row_person_ids = {row["person_id"] for row in rows}
    if row_person_ids != {person_id}:
        raise LabSyncError(
            "retained lab source person_id does not match processed path; "
            f"expected {person_id!r}, found {sorted(row_person_ids)}"
        )


def _drop_source_less_overlapping_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    incoming_keys = {
        (str(row.get("test_name") or ""), str(row.get("observed_at") or "")) for row in new_rows
    }
    return [
        row
        for row in existing_rows
        if (str(row.get("test_name") or ""), str(row.get("observed_at") or "")) not in incoming_keys
    ]


def _find_panel_note_for_source_path(
    person_labs_dir: Path,
    date_str: str,
    processed_source_path: str,
    preferred_label: str,
) -> Path | None:
    matches: list[Path] = []
    for note_path in iter_date_panel_note_paths(person_labs_dir, date_str):
        try:
            front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if front_matter.get("source_path") == processed_source_path:
            matches.append(note_path)

    if not matches:
        return None

    preferred_name = deterministic_panel_note_path(
        person_labs_dir,
        date_str,
        preferred_label,
    ).name
    for note_path in matches:
        if note_path.name == preferred_name:
            return note_path
    return matches[0]


def _persist_lab_panel_note_for_date_group(
    *,
    person_labs_dir: Path,
    vault_root: Path,
    date_str: str,
    rows: list[dict[str, Any]],
    source_file: Path,
    processed_source_path: str,
    retained_source: bool,
    source_file_name: str | None,
) -> tuple[Path, str | None]:
    """Merge or create the single-date panel note for one observed-date group.

    Returns the written note path and, if a source-less note was claimed and
    fully absorbed, its vault-relative path (for downstream lab_results cleanup).
    """
    observed_at = min(row["observed_at"] for row in rows)
    import_label = import_label_for_lab_note(source_file, observed_at, retained_source)
    test_names = frozenset(str(row.get("test_name") or "") for row in rows if row.get("test_name"))
    existing_note_path = (
        _find_panel_note_for_source_path(
            person_labs_dir,
            date_str,
            processed_source_path,
            import_label,
        )
        if retained_source
        else None
    )
    if existing_note_path is None:
        overlap_candidate = find_overlapping_panel_note(
            person_labs_dir,
            date_str,
            test_names,
            prefer_source_less=retained_source,
        )
        if overlap_candidate is not None:
            try:
                overlap_front_matter = parse_front_matter(
                    overlap_candidate.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                overlap_front_matter = {}
            existing_note_path = (
                overlap_candidate
                if not overlap_front_matter.get("source_path")
                or overlap_front_matter.get("source_path") == processed_source_path
                else None
            )
        else:
            existing_note_path = overlap_candidate
    migrated_note_source_path: str | None = None
    existing_rows: list[dict[str, Any]] = []
    existing_fm: dict[str, Any] = {}

    if existing_note_path is not None:
        try:
            existing_content = existing_note_path.read_text(encoding="utf-8")
            existing_rows = load_labs_from_note(existing_note_path)
            existing_fm = parse_front_matter(existing_content)
        except (LabSyncError, ValueError, OSError):
            pass
        # ADR-038: a note's filename date is its single source of truth. Drop any
        # rows for other dates so a legacy multi-date catch-all note self-heals
        # as each of its date groups gets reprocessed.
        existing_rows = [
            row for row in existing_rows if str(row.get("observed_at") or "")[:10] == date_str
        ]
        if retained_source and not existing_fm.get("source_path"):
            remaining_rows = _drop_source_less_overlapping_rows(existing_rows, rows)
            if remaining_rows:
                # The source-less note still carries rows this source doesn't
                # account for; claiming it would mis-attribute their provenance
                # to processed_source_path. Leave it untouched and write the
                # new rows to their own note instead.
                existing_note_path = None
            else:
                existing_rows = remaining_rows
                migrated_note_source_path = str(existing_note_path.relative_to(vault_root))
        elif retained_source and existing_fm.get("source_path") == processed_source_path:
            # ADR-038: a retained-source note is owned by exactly one source.
            # Reprocessing replaces that source's generated rows wholesale;
            # user-authored Notes content is preserved by write_lab_panel_note.
            existing_rows = []

    if existing_note_path is not None:
        merged_rows = merge_lab_panel_rows(existing_rows, rows)
        note_path = existing_note_path
        write_lab_panel_note(
            note_path,
            merged_rows,
            source_file_name=source_file_name or existing_fm.get("source_file_name"),
            source_path=processed_source_path
            if retained_source or source_file_name
            else existing_fm.get("source_path"),
            projection_role="source_evidence",
        )
    else:
        note_path = deterministic_panel_note_path(person_labs_dir, observed_at, import_label)
        write_lab_panel_note(
            note_path,
            rows,
            source_file_name=source_file_name or display_source_file_name(source_file, date_str),
            source_path=processed_source_path,
            projection_role="source_evidence",
        )

    return note_path, migrated_note_source_path


def persist_lab_rows(
    *,
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    source_file: Path,
    processed_source_path: str,
    checksum: str,
    source_type: str,
    rows: list[dict[str, Any]],
    retained_source: bool = False,
    source_file_name: str | None = None,
    refresh_reports: bool = True,
    dedup_stats: dict[str, int] | None = None,
) -> list[Path]:
    """Persist *rows* into per-observed-date lab panel notes (ADR-038).

    Rows are grouped by observed date and each group is merged into its own
    ``{date}-*-panel.md`` note via the existing overlap/merge/write path.
    Returns the note paths written, one per distinct observed date.

    ``refresh_reports=False`` is reserved for callers that batch several
    canonical mutations and invoke ``refresh_lab_dependent_reports`` once after
    the batch is complete.

    ``dedup_stats``, if given, is populated with ``{"inserted": N,
    "skipped_redundant": M}`` from ``replace_lab_results_for_source_path``.
    The vault note is written from *rows* regardless of how many of those
    rows were actually inserted vs. skipped as redundant duplicates of a
    fact already recorded under a different source_path (same person_id,
    test_name, observed_at, value) -- so a note can legitimately cite a
    source_path for which 0 lab_results rows exist. ``dedup_stats`` and the
    audit event below make that outcome discoverable instead of silent.
    """
    ensure_person_record_scaffold(vault_root, person_id)
    person_labs_dir = vault_root / "Records" / person_id / "Labs"
    person_labs_dir.mkdir(parents=True, exist_ok=True)

    # ADR-035: drop non-result narrative rows (instrument lot numbers,
    # kit expiration dates, internal-control flags) before persistence.
    rows = [
        row
        for row in rows
        if not is_non_result_lab_row(row.get("test_name"), row.get("loinc_code"))
    ]

    rows_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_date.setdefault(str(row["observed_at"]).split("T", 1)[0], []).append(row)

    note_paths: list[Path] = []
    migrated_note_source_paths: list[str] = []
    for date_str in sorted(rows_by_date):
        note_path, migrated_note_source_path = _persist_lab_panel_note_for_date_group(
            person_labs_dir=person_labs_dir,
            vault_root=vault_root,
            date_str=date_str,
            rows=rows_by_date[date_str],
            source_file=source_file,
            processed_source_path=processed_source_path,
            retained_source=retained_source,
            source_file_name=source_file_name,
        )
        note_paths.append(note_path)
        if migrated_note_source_path is not None:
            migrated_note_source_paths.append(migrated_note_source_path)

    display_name = load_person_display_name(vault_root, person_id)
    write_labs_index_note(person_labs_dir, person_id, display_name=display_name)

    for migrated_note_source_path in migrated_note_source_paths:
        if migrated_note_source_path != processed_source_path:
            conn.execute(
                "DELETE FROM lab_results WHERE person_id = ? AND source_path = ?",
                (person_id, migrated_note_source_path),
            )
    dedup_inserted, dedup_skipped = replace_lab_results_for_source_path(
        conn, processed_source_path, rows
    )
    if dedup_stats is not None:
        dedup_stats["inserted"] = dedup_inserted
        dedup_stats["skipped_redundant"] = dedup_skipped
    if retained_source:
        has_fhir_surface = has_fhir_export_surface(conn, vault_root, person_id)
        has_composite_surface = has_composite_report_surface(vault_root, person_id)
        for output_name in LAB_DEPENDENT_REPORT_FILENAMES:
            if output_name == "fhir-export.json" and not has_fhir_surface:
                continue
            if output_name in LAB_DEPENDENT_COMPOSITE_FILENAMES and not has_composite_surface:
                continue
            mark_dependent_output_stale(
                conn,
                output_path=f"Records/{person_id}/Reports/{output_name}",
                reason="retained lab source reprocessed",
                source_path=processed_source_path,
                person_id=person_id,
            )
    message_prefix = "reprocessed note" if retained_source else "note"
    note_source_paths = ", ".join(str(path.relative_to(vault_root)) for path in note_paths)
    message = f"{message_prefix}: {note_source_paths}"
    if dedup_skipped:
        message += (
            f"; {dedup_skipped} of {len(rows)} fact(s) already recorded via another "
            "source_path and not duplicated under this source"
        )
    log_ingestion_event(
        conn,
        source_type=source_type,
        source_path=processed_source_path,
        source_checksum=checksum,
        status=STATE_PROCESSED,
        message=message,
    )
    log_audit_event(
        conn,
        event_type=(AUDIT_LAB_SOURCE_REPROCESSED if retained_source else AUDIT_LAB_IMPORT_ACCEPTED),
        subject_type="lab_panel",
        subject_id=person_id,
        source_path=processed_source_path,
        status=STATE_PROCESSED,
        message=message,
    )
    if refresh_reports:
        refresh_lab_dependent_reports(conn, vault_root, person_id)
    return note_paths


def fill_loinc_codes(rows: list[dict[str, Any]], loinc_index: dict[str, str]) -> None:
    """Fill blank ``loinc_code`` fields in *rows* from *loinc_index*.

    Existing non-blank codes are never overwritten. Operates in-place.
    """
    if not loinc_index:
        return
    for row in rows:
        if not row.get("loinc_code"):
            filled = lookup_loinc_code(
                str(row.get("test_name") or ""),
                loinc_index,
                str(row.get("unit") or ""),
            )
            row["loinc_code"] = filled
            # A code produced here is inference, not source authority (ADR-034).
            if filled:
                row["loinc_code_origin"] = LOINC_CODE_ORIGIN_INFERRED


def collect_loinc_misses(rows: list[dict[str, Any]], loinc_index: dict[str, str]) -> list[str]:
    """Return sorted distinct test names whose loinc_code is empty after filling.

    Returns an empty list when *loinc_index* is absent — an unconfigured index is a
    setup gap, not a per-test miss, and should be reported separately.
    """
    if not loinc_index:
        return []
    return sorted({str(r.get("test_name") or "") for r in rows if not r.get("loinc_code")})


def canonicalize_candidate_lab_facts(
    extraction: InterpreterFactExtraction,
    source_label: str,
    loinc_index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Convert validated fact-extraction output to lab row dicts for persist_lab_rows.

    Uses document_date as a fallback when a fact has no observed_at.
    Raises LabSyncError when a date is entirely absent or normalization rejects a fact.
    Qualitative values (non-numeric strings) are stored in value_text with value=None.
    Only ``CandidateLabFact`` entries are processed; non-lab facts in the extraction
    are skipped (the caller should dispatch by category).
    When *loinc_index* is provided, blank ``loinc_code`` fields are filled from it.
    """
    from famed.clinical_interpreter import CandidateLabFact

    rows: list[dict[str, Any]] = []
    fallback_date = extraction.document_date
    for fact in extraction.facts:
        if not isinstance(fact, CandidateLabFact):
            continue
        observed_at = fact.observed_at or fallback_date
        if not observed_at:
            raise LabSyncError(
                f"no observed_at or document_date for fact {fact.test_name!r} in {source_label}"
            )
        value_text = fact.value or ""
        value: Any = fact.value if can_parse_optional_float(fact.value) else None
        ref_low: Any = (
            fact.reference_range_low if can_parse_optional_float(fact.reference_range_low) else None
        )
        ref_high: Any = (
            fact.reference_range_high
            if can_parse_optional_float(fact.reference_range_high)
            else None
        )
        raw: dict[str, Any] = {
            "person_id": extraction.person_id,
            "test_name": fact.test_name,
            "value": value,
            "value_text": value_text,
            "unit": fact.unit,
            "reference_range_low": ref_low,
            "reference_range_high": ref_high,
            "observed_at": observed_at,
            "source_type": "ai_extracted",
            "loinc_code": "",
        }
        rows.append(lab_payload_from_mapping(raw, source_label))
    if loinc_index:
        fill_loinc_codes(rows, loinc_index)
    return rows


def reprocess_retained_lab_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection | None,
    source_path: Path,
    requested_person_id: str | None,
    approve_pdf: bool,
    review_only: bool,
    refresh_reports: bool = True,
    show_review: bool = True,
    source_path_prefix: Path | None = None,
) -> ReprocessResult:
    source_type = source_path.suffix.lower().lstrip(".")
    if source_type not in {"csv", "pdf"}:
        raise LabSyncError("retained lab source must be a CSV or PDF file")

    processed_person_id = infer_person_id_from_processed_lab_path(source_path, processed_root)
    if not processed_person_id:
        raise LabSyncError("retained lab source must be under data/processed/<person_id>/Labs")
    if requested_person_id and requested_person_id != processed_person_id:
        raise LabSyncError(
            "retained lab source person_id does not match --person-id; "
            f"expected {processed_person_id!r}, got {requested_person_id!r}"
        )
    person_id = requested_person_id or processed_person_id

    if source_type == "csv":
        rows = load_labs_from_csv(source_path, default_person_id=person_id)
    else:
        text = extract_pdf_text(source_path)
        rows = parse_labs_from_pdf_text(text, person_id, str(source_path))
        fill_loinc_codes(rows, load_loinc_index(root / "data" / "reference"))
        approved = review_pdf_extraction(
            source_path,
            person_id,
            rows,
            skip_prompt=approve_pdf,
            review_only=review_only,
            show_output=show_review,
        )
        if not approved:
            raise LabSyncError("reprocess cancelled during review")

    if not rows:
        return ReprocessResult(imported_rows=0, affected_person_ids=set())

    validate_reprocessed_lab_person(rows, person_id)
    if review_only:
        return ReprocessResult(imported_rows=len(rows), affected_person_ids={person_id})

    if conn is None:
        raise LabSyncError("database connection not available")
    processed_source_path = retained_source_reference(
        root=root,
        processed_root=processed_root,
        source_path=source_path,
        source_path_prefix=source_path_prefix,
    )
    checksum = source_file_checksum(source_path)
    dedup_stats: dict[str, int] = {}
    persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id=person_id,
        source_file=source_path,
        processed_source_path=processed_source_path,
        checksum=checksum,
        source_type=source_type,
        rows=rows,
        retained_source=True,
        refresh_reports=refresh_reports,
        dedup_stats=dedup_stats,
    )
    # dedup_stats["inserted"] can be less than len(rows) when facts were already
    # recorded under a different source_path (same person/test/date/value); those
    # are skipped as redundant rather than re-inserted, but the vault note still
    # renders every extracted row. Report the true inserted count so a caller
    # cannot mistake "extracted" for "persisted to lab_results".
    return ReprocessResult(
        imported_rows=dedup_stats.get("inserted", len(rows)), affected_person_ids={person_id}
    )


def main() -> int:
    args = parse_args()
    root = repo_root()

    inbox_dir = (root / args.inbox_dir).resolve()
    vault_root = (root / args.vault_path).resolve()
    db_path = (root / args.db_path).resolve()
    storage_roots = configured_storage_roots(root)
    loinc_index = load_loinc_index(storage_roots.reference)

    inbox_dir.mkdir(parents=True, exist_ok=True)

    reprocess_source: Path | None = None
    if args.reprocess_source:
        reprocess_source = Path(args.reprocess_source)
        if not reprocess_source.is_absolute():
            reprocess_source = (root / reprocess_source).resolve()

    csv_files = [] if reprocess_source else collect_csv_files(inbox_dir, args.csv_file)
    pdf_files = [] if reprocess_source else collect_pdf_files(inbox_dir, args.pdf_file)
    reprocess_pdf_files = (
        [reprocess_source] if reprocess_source and reprocess_source.suffix.lower() == ".pdf" else []
    )
    if not csv_files and not pdf_files and not reprocess_source and not args.backfill_reports:
        print(f"No lab CSV or PDF files found in: {inbox_dir}")
        return 0

    preflight_errors = preflight_pdf_dependencies_if_needed([*pdf_files, *reprocess_pdf_files])
    if preflight_errors:
        print("Encountered errors:")
        for error in preflight_errors:
            print(f"- {error}")
        return 1

    imported_rows = 0
    reports_refreshed = 0
    conflict_reports_refreshed = 0
    errors: list[str] = []
    person_ids_to_refresh: set[str] = set()
    loinc_misses: set[str] = set()

    conn: sqlite3.Connection | None = None
    if not args.review_only:
        conn = sqlite3.connect(db_path)

    reprocess_registry = ReprocessRegistry()
    reprocess_registry.register(
        name="labs",
        category_parts=("Labs",),
        suffixes={".csv", ".pdf"},
        handler=lambda source_path: reprocess_retained_lab_source(
            root=root,
            vault_root=vault_root,
            processed_root=storage_roots.processed,
            conn=conn,
            source_path=source_path,
            requested_person_id=args.person_id,
            approve_pdf=args.approve_pdf,
            review_only=args.review_only,
        ),
    )

    try:
        for csv_file in csv_files:
            inferred_person_id = infer_person_id_from_inbox_path(csv_file, inbox_dir)
            try:
                rows = load_labs_from_csv(csv_file, default_person_id=inferred_person_id)
            except LabSyncError as exc:
                record_lab_import_validation_error(
                    errors,
                    review_only=args.review_only,
                    conn=conn,
                    root=root,
                    quarantine_root=storage_roots.quarantine,
                    person_id=inferred_person_id,
                    source_file=csv_file,
                    source_type="csv",
                    reason=str(exc),
                )
                continue

            if not rows:
                continue

            fill_loinc_codes(rows, loinc_index)
            loinc_misses.update(collect_loinc_misses(rows, loinc_index))
            person_ids = {row["person_id"] for row in rows}
            if len(person_ids) != 1:
                record_lab_import_validation_error(
                    errors,
                    review_only=args.review_only,
                    conn=conn,
                    root=root,
                    quarantine_root=storage_roots.quarantine,
                    person_id=inferred_person_id,
                    source_file=csv_file,
                    source_type="csv",
                    reason=(
                        "all rows in a CSV import must share one person_id; "
                        f"found {sorted(person_ids)}"
                    ),
                )
                continue

            person_id = rows[0]["person_id"]
            observed_at = min(row["observed_at"] for row in rows)

            try:
                if args.review_only:
                    imported_rows += len(rows)
                    continue

                checksum, processed_source_path = retain_lab_source_for_import(
                    root,
                    storage_roots.processed,
                    person_id,
                    observed_at,
                    csv_file,
                )
                if conn is None:
                    raise LabSyncError("database connection not available")
                persist_lab_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=csv_file,
                    processed_source_path=processed_source_path,
                    checksum=checksum,
                    source_type="csv",
                    rows=rows,
                    refresh_reports=False,
                )
                person_ids_to_refresh.add(person_id)
                imported_rows += len(rows)
            except (OSError, sqlite3.Error, LabSyncError) as exc:
                errors.append(f"{csv_file.name}: {exc}")

        for pdf_file in pdf_files:
            try:
                person_id = (
                    args.person_id
                    or infer_person_id_from_inbox_path(pdf_file, inbox_dir)
                    or infer_person_id_from_pdf_filename(pdf_file)
                )
                if not person_id:
                    raise LabSyncError(
                        "person_id is required for PDF import "
                        "(use person-scoped path, --person-id, or filename prefix "
                        "'<person_id>__...pdf')"
                    )

                try:
                    text = extract_pdf_text(pdf_file)
                    rows = parse_labs_from_pdf_text(text, person_id, str(pdf_file))
                except LabSyncError as exc:
                    record_lab_import_validation_error(
                        errors,
                        review_only=args.review_only,
                        conn=conn,
                        root=root,
                        quarantine_root=storage_roots.quarantine,
                        person_id=person_id,
                        source_file=pdf_file,
                        source_type="pdf",
                        reason=str(exc),
                    )
                    continue

                approved = review_pdf_extraction(
                    pdf_file,
                    person_id,
                    rows,
                    skip_prompt=args.approve_pdf,
                    review_only=args.review_only,
                )
                if not approved:
                    errors.append(f"{pdf_file.name}: import cancelled during review")
                    continue

                fill_loinc_codes(rows, loinc_index)
                loinc_misses.update(collect_loinc_misses(rows, loinc_index))
                observed_at = min(row["observed_at"] for row in rows)
                if args.review_only:
                    imported_rows += len(rows)
                    continue

                checksum, processed_source_path = retain_lab_source_for_import(
                    root,
                    storage_roots.processed,
                    person_id,
                    observed_at,
                    pdf_file,
                )
                if conn is None:
                    raise LabSyncError("database connection not available")
                persist_lab_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=pdf_file,
                    processed_source_path=processed_source_path,
                    checksum=checksum,
                    source_type="pdf",
                    rows=rows,
                    refresh_reports=False,
                )
                person_ids_to_refresh.add(person_id)
                imported_rows += len(rows)
            except (OSError, sqlite3.Error, LabSyncError) as exc:
                errors.append(f"{pdf_file.name}: {exc}")

        if reprocess_source:
            try:
                result = reprocess_registry.reprocess(reprocess_source, storage_roots.processed)
                imported_rows += result.imported_rows
                if not args.review_only:
                    person_ids_to_refresh.update(result.affected_person_ids)
            except (OSError, sqlite3.Error, LabSyncError, ReprocessError, ValueError) as exc:
                errors.append(f"{reprocess_source.name}: {exc}")

        if not args.review_only:
            if args.backfill_reports:
                person_ids_to_refresh.update(person_ids_with_existing_labs(vault_root))

            for person_id in sorted(person_ids_to_refresh):
                try:
                    if conn is None:
                        raise LabSyncError("database connection not available")
                    refresh_lab_dependent_reports(conn, vault_root, person_id)
                    reports_refreshed += 1
                    conflict_reports_refreshed += 1
                except (OSError, LabSyncError) as exc:
                    errors.append(f"{person_id}: failed to generate lab reports: {exc}")

        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

    if args.review_only:
        print(f"Reviewed {imported_rows} lab row(s). No notes or database records were written.")
    else:
        print(
            f"Imported {imported_rows} lab row(s). "
            f"Refreshed {reports_refreshed} latest lab report(s) and "
            f"{conflict_reports_refreshed} lab conflict report(s)."
        )
    if imported_rows > 0:
        if not loinc_index:
            print("LOINC index not loaded — LOINC codes not assigned.")
        elif loinc_misses:
            names = ", ".join(sorted(loinc_misses))
            print(f"{len(loinc_misses)} test name(s) have no LOINC code: {names}")
    if errors:
        print("Encountered errors:")
        for error in errors:
            print(f"- {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
