"""Helpers for classifying and rendering source-file paths."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

ARCHIVE_MEMBER_SEPARATOR = "#"
_ARCHIVE_DATE_RE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[_ -]*(\d{1,2})[_ -]*(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def is_hidden_source_path(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return any(part.startswith(".") for part in relative_parts)


def archive_member_source_path(archive_path: str, member_name: str) -> str:
    normalized_archive = archive_path.replace("\\", "/").strip()
    normalized_member = member_name.replace("\\", "/").strip()
    if not normalized_archive or not normalized_member:
        raise ValueError("archive path and member name are required")
    return f"{normalized_archive}{ARCHIVE_MEMBER_SEPARATOR}{normalized_member}"


def split_archive_source_path(source_path: str) -> tuple[str, str | None]:
    normalized = source_path.replace("\\", "/").strip()
    if ".zip#" not in normalized.casefold():
        return normalized, None
    archive_path, member_name = normalized.split(ARCHIVE_MEMBER_SEPARATOR, 1)
    if archive_path.casefold().endswith(".zip") and member_name.strip():
        return archive_path, member_name.strip()
    return normalized, None


def _healthsummary_archive_key(source_path: str) -> tuple[str, str] | None:
    archive_path, member_name = split_archive_source_path(source_path)
    path = Path(archive_path)
    parts = path.parts
    if not path.name.casefold().endswith(".zip"):
        return None
    if "HealthSummary" not in parts:
        return None
    if member_name is None and ARCHIVE_MEMBER_SEPARATOR in source_path:
        return None
    try:
        person_id = parts[parts.index("HealthSummary") - 1]
    except (ValueError, IndexError):
        return None
    provider = _ARCHIVE_DATE_RE.sub("", path.stem.casefold())
    provider = provider.replace("healthsummary", "").replace("health summary", "")
    provider = re.sub(r"\bkaiser\b", "", provider)
    provider = re.sub(r"[^a-z0-9]+", " ", provider).strip()
    return person_id, provider or "healthsummary"


def _healthsummary_archive_date(source_path: str) -> date | None:
    archive_path, _member_name = split_archive_source_path(source_path)
    match = _ARCHIVE_DATE_RE.search(Path(archive_path).name)
    if not match:
        return None
    month_name, day_text, year_text = match.groups()
    try:
        return date(int(year_text), _MONTHS[month_name[:3].casefold()], int(day_text))
    except ValueError:
        return None


def prune_superseded_healthsummary_source_paths(source_paths: list[str]) -> list[str]:
    """Drop older same-family HealthSummary archive paths from one fact's provenance."""
    normalized_paths = [path.replace("\\", "/").strip() for path in source_paths if path.strip()]
    latest_by_key: dict[tuple[str, str], date] = {}
    for source_path in normalized_paths:
        key = _healthsummary_archive_key(source_path)
        archive_date = _healthsummary_archive_date(source_path)
        if key is None or archive_date is None:
            continue
        latest_by_key[key] = max(archive_date, latest_by_key.get(key, date.min))

    pruned: list[str] = []
    for source_path in normalized_paths:
        key = _healthsummary_archive_key(source_path)
        archive_date = _healthsummary_archive_date(source_path)
        if key is not None and archive_date is not None and archive_date < latest_by_key[key]:
            continue
        pruned.append(source_path)
    return sorted(dict.fromkeys(pruned))


def compact_archive_source_paths(source_paths: list[str]) -> list[str]:
    compacted: list[str] = []
    member_groups: dict[str, list[str]] = {}
    archive_positions: dict[str, int] = {}
    seen_non_archive: set[str] = set()

    for raw_path in source_paths:
        normalized = raw_path.replace("\\", "/").strip()
        if not normalized:
            continue
        archive_path, member_name = split_archive_source_path(normalized)
        if member_name is None:
            if archive_path in seen_non_archive:
                continue
            seen_non_archive.add(archive_path)
            compacted.append(archive_path)
            if archive_path.casefold().endswith(".zip"):
                archive_positions[archive_path] = len(compacted) - 1
            continue
        if archive_path not in archive_positions:
            archive_positions[archive_path] = len(compacted)
            compacted.append(normalized)
            member_groups[archive_path] = [member_name]
            continue
        member_groups.setdefault(archive_path, []).append(member_name)
        compacted[archive_positions[archive_path]] = archive_path

    return compacted


def display_source_reference(source_path: str) -> str:
    archive_path, member_name = split_archive_source_path(source_path)
    if member_name is None:
        return archive_path
    return f"{archive_path}{ARCHIVE_MEMBER_SEPARATOR}{Path(member_name).name}"


def display_source_file_name_from_path(source_path: str) -> str:
    archive_path, member_name = split_archive_source_path(source_path)
    if member_name is None:
        return Path(archive_path).name
    return f"{Path(archive_path).name}{ARCHIVE_MEMBER_SEPARATOR}{Path(member_name).name}"
