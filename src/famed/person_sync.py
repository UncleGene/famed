#!/usr/bin/env python3
"""Shared helpers for person import and vault sync workflows."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from famed.storage_contract import (
    AUDIT_SYNC_RESOLUTION_RECORDED,
    AUTHORITY_AMBIGUOUS,
    AUTHORITY_NEW,
    AUTHORITY_VAULT_WINS,
    RESOLUTION_QUARANTINED,
    AuthorityDecision,
    Resolution,
    log_audit_event,
    validate_resolution,
)

PERSON_RECORD_SUBDIRS = [
    "Genes",
    "History",
    "Immunizations",
    "Insights",
    "Labs",
    "Medications",
    "Reports",
    "Vitals",
]
# Fields that are stored in both the vault note front matter and the DB row.
SYNCED_PERSON_FIELDS: tuple[str, ...] = ("display_name", "birth_year", "sex")
RELATIONSHIP_TYPE_ALIASES = {
    "mother": "parent",
    "father": "parent",
    "mom": "parent",
    "dad": "parent",
    "son": "child",
    "daughter": "child",
    "wife": "spouse",
    "husband": "spouse",
    "partner": "spouse",
}
ALLOWED_RELATIONSHIP_TYPES = {
    "parent",
    "child",
    "spouse",
    "sibling",
    "grandparent",
    "grandchild",
    "guardian",
}
RECIPROCAL_RELATIONSHIP_TYPE: dict[str, str | None] = {
    "parent": "child",
    "child": "parent",
    "spouse": "spouse",
    "sibling": "sibling",
    "grandparent": "grandchild",
    "grandchild": "grandparent",
    "guardian": None,
}
SOURCE_REFERENCE_FIELDS = ("source_path", "source_file_name")
RETAINED_SOURCE_PATH_PREFIX = ("data", "processed")
VAULT_SOURCE_PATH_ROOT = "Records"
COMMON_FRONT_MATTER_FIELDS = {
    "person_id",
    "record_type",
    "source_path",
    "source_file_name",
}
PERSON_FRONT_MATTER_FIELDS = {
    "display_name",
    "legal_name",
    "birth_year",
    "birth_date",
    "sex",
    "identifiers",
    "relationships",
}
LAB_FRONT_MATTER_FIELDS = {
    "observed_at",
    "loinc_code",
    "test_name",
    "value",
    "unit",
    "reference_range_low",
    "reference_range_high",
    "source_type",
    "projection_role",
    "tests",
}
REPORT_FRONT_MATTER_FIELDS = {
    "report_type",
    "generated_at",
    "source_reports",
    "missing_reports",
    "stale_reports",
    "unreviewed_reports",
    "active_findings_count",
    "family_history_sources",
    "synthesis_status",
    "synthesis_generated_at",
    "verification_status",
    "pending_review_count",
}
INSIGHT_FRONT_MATTER_FIELDS = {"date", "domain", "evidence_tier", "sources"}
MEDICATION_FRONT_MATTER_FIELDS = {
    "category",
    "date",
    "source",
    "projection_role",
    "superseded_by",
}
IMMUNIZATION_FRONT_MATTER_FIELDS = {"event_date"}
HISTORY_FRONT_MATTER_FIELDS = {
    "event_date",
    "title",
    "provider",
    "icd10_codes",
    "summary",
    "verification_status",
    "extraction_mode",
    "accepted_facts",
    "refuted_facts",
    "excluded_low_confidence_count",
}
GENETIC_NOTE_FRONT_MATTER_FIELDS = {"source", "summary"}
PLACEHOLDER_FRONT_MATTER_FIELDS = {"category", "review_status", "source_type"}
KNOWN_RECORD_TYPE_FIELDS: dict[str, set[str]] = {
    "person": COMMON_FRONT_MATTER_FIELDS | PERSON_FRONT_MATTER_FIELDS,
    "lab_result": COMMON_FRONT_MATTER_FIELDS | LAB_FRONT_MATTER_FIELDS,
    "lab_panel": COMMON_FRONT_MATTER_FIELDS | LAB_FRONT_MATTER_FIELDS,
    "report": COMMON_FRONT_MATTER_FIELDS | REPORT_FRONT_MATTER_FIELDS,
    "insight": COMMON_FRONT_MATTER_FIELDS | INSIGHT_FRONT_MATTER_FIELDS,
    "medication": COMMON_FRONT_MATTER_FIELDS | MEDICATION_FRONT_MATTER_FIELDS,
    "immunization": COMMON_FRONT_MATTER_FIELDS | IMMUNIZATION_FRONT_MATTER_FIELDS,
    "history": COMMON_FRONT_MATTER_FIELDS | HISTORY_FRONT_MATTER_FIELDS,
    "history_correction": COMMON_FRONT_MATTER_FIELDS | {"title", "refutations"},
    "genetic_note": COMMON_FRONT_MATTER_FIELDS | GENETIC_NOTE_FRONT_MATTER_FIELDS,
    "category_placeholder": COMMON_FRONT_MATTER_FIELDS | PLACEHOLDER_FRONT_MATTER_FIELDS,
    "protocol": COMMON_FRONT_MATTER_FIELDS,
}
INDEX_RECORD_TYPE_SUFFIX = "_index"
REQUIRED_RECORD_TYPE_FIELDS: dict[str, tuple[str, ...]] = {
    "lab_panel": ("observed_at", "source_type"),
    "lab_result": ("observed_at", "test_name", "source_type"),
    "category_placeholder": ("category", "review_status", "source_type", "source_path"),
    "report": ("report_type", "generated_at"),
}


class PersonSyncError(ValueError):
    """Raised when person payloads are invalid."""


@dataclass(frozen=True)
class NoteValidationIssue:
    """One vault note validation issue, with path context for CLI output."""

    path: Path
    message: str


def normalize_relationship_type(raw_relationship_type: Any) -> str:
    relationship_type = str(raw_relationship_type or "").strip().lower().replace(" ", "-")
    relationship_type = RELATIONSHIP_TYPE_ALIASES.get(relationship_type, relationship_type)

    if not relationship_type:
        raise PersonSyncError("relationship_type is required for each relationship")

    if relationship_type not in ALLOWED_RELATIONSHIP_TYPES:
        allowed = ", ".join(sorted(ALLOWED_RELATIONSHIP_TYPES))
        raise PersonSyncError(
            "unsupported relationship_type "
            f"{relationship_type!r}. Allowed values: {allowed}. "
            "Direction is from this person to target_person_id."
        )

    return relationship_type


def normalize_person_id(raw_person_id: Any) -> str:
    person_id = str(raw_person_id or "").strip().lower().replace(" ", "-")
    if not person_id:
        raise PersonSyncError("person_id is required")
    return person_id


def normalize_birth_year(raw_birth_year: Any) -> int | None:
    if raw_birth_year in (None, "", "null"):
        return None
    if isinstance(raw_birth_year, int):
        return raw_birth_year
    try:
        return int(str(raw_birth_year).strip())
    except ValueError as exc:
        raise PersonSyncError(f"birth_year must be an integer, got: {raw_birth_year!r}") from exc


def normalize_relationships(raw_relationships: Any) -> list[dict[str, str]]:
    if raw_relationships in (None, ""):
        return []

    if not isinstance(raw_relationships, list):
        raise PersonSyncError("relationships must be a list")

    items = cast(list[Any], raw_relationships)  # type: ignore[redundant-cast]
    normalized: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            if ":" not in item:
                raise PersonSyncError(
                    "string relationships must use '<relationship_type>:<target_person_id>' format"
                )
            relationship_type, target_person_id = item.split(":", 1)
        elif isinstance(item, dict):
            item_d = cast(dict[str, Any], item)
            relationship_type = str(
                item_d.get("relationship_type")
                or item_d.get("type")
                or item_d.get("relation")
                or ""
            )
            target_person_id = str(item_d.get("target_person_id") or item_d.get("person_id") or "")
        else:
            raise PersonSyncError("relationship entries must be strings or dictionaries")

        relationship_type = normalize_relationship_type(relationship_type)
        target_person_id = normalize_person_id(target_person_id)

        normalized.append(
            {
                "relationship_type": relationship_type,
                "target_person_id": target_person_id,
            }
        )

    return normalized


def relationship_key(relationship: dict[str, str]) -> tuple[str, str]:
    return relationship["relationship_type"], relationship["target_person_id"]


def merge_relationships(
    existing: list[dict[str, str]], incoming: list[dict[str, str]]
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for relationship in [*existing, *incoming]:
        key = relationship_key(relationship)
        if key in seen:
            continue
        seen.add(key)
        merged.append(relationship)

    return merged


def reciprocal_relationship(
    relationship: dict[str, str], source_person_id: str
) -> dict[str, str] | None:
    reciprocal_type = RECIPROCAL_RELATIONSHIP_TYPE.get(relationship["relationship_type"])
    if not reciprocal_type:
        return None

    return {
        "relationship_type": reciprocal_type,
        "target_person_id": source_person_id,
    }


def infer_parent_relationships_from_siblings(
    person: dict[str, Any],
    load_person_by_id: Callable[[str], dict[str, Any] | None],
) -> list[dict[str, str]]:
    existing = {(r["relationship_type"], r["target_person_id"]) for r in person["relationships"]}
    inferred: list[dict[str, str]] = []

    for relationship in person["relationships"]:
        if relationship["relationship_type"] != "sibling":
            continue

        sibling_id = relationship["target_person_id"]
        sibling_person = load_person_by_id(sibling_id)
        if not sibling_person:
            continue

        for sibling_rel in sibling_person.get("relationships", []):
            if sibling_rel.get("relationship_type") != "parent":
                continue

            parent_id = sibling_rel.get("target_person_id")
            key = ("parent", parent_id)
            if not parent_id or parent_id == person["person_id"] or key in existing:
                continue

            inferred.append({"relationship_type": "parent", "target_person_id": parent_id})
            existing.add(key)

    return inferred


def apply_relationship_closure(persons_by_id: dict[str, dict[str, Any]]) -> set[str]:
    """Add inferred and reciprocal relationships among already-known people.

    Unknown relationship targets are left unresolved; creating new people requires
    explicit human input because it changes the family graph's domain truth.
    """
    changed_ids: set[str] = set()

    def add_reciprocal_links() -> None:
        for source_id, source_person in list(persons_by_id.items()):
            for relationship in source_person["relationships"]:
                reciprocal = reciprocal_relationship(relationship, source_id)
                if reciprocal is None:
                    continue

                target_id = relationship["target_person_id"]
                target_person = persons_by_id.get(target_id)
                if target_person is None:
                    continue

                merged = merge_relationships(target_person["relationships"], [reciprocal])
                if merged != target_person["relationships"]:
                    target_person["relationships"] = merged
                    changed_ids.add(target_id)

    add_reciprocal_links()

    for person_id, person in list(persons_by_id.items()):
        inferred = infer_parent_relationships_from_siblings(person, persons_by_id.get)
        if inferred:
            merged = merge_relationships(person["relationships"], inferred)
            if merged != person["relationships"]:
                person["relationships"] = merged
                changed_ids.add(person_id)

    add_reciprocal_links()

    return changed_ids


_PARENT_CHILD_MIN_AGE_GAP = 12
_GRANDPARENT_MIN_AGE_GAP = 24

# Only check relationships that assert "the target is older than me" to avoid
# duplicate warnings from reciprocal pairs.
_OLDER_PERSON_RELATIONSHIP_TYPES: dict[str, int] = {
    "parent": _PARENT_CHILD_MIN_AGE_GAP,
    "grandparent": _GRANDPARENT_MIN_AGE_GAP,
}


def check_relationship_age_plausibility(
    persons_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Return warning strings for parent/grandparent relationships where birth years
    contradict the declared relationship type.

    Only checks relationships where the target is expected to be *older* than the
    subject (parent, grandparent), so each problematic pair produces exactly one
    warning even when both reciprocal links are present.  Skips checks when either
    party lacks a birth_year.
    """
    warnings: list[str] = []
    for person_id, person in persons_by_id.items():
        person_year: int | None = person.get("birth_year")
        if person_year is None:
            continue
        for rel in person.get("relationships", []):
            rel_type = rel.get("relationship_type", "")
            min_gap = _OLDER_PERSON_RELATIONSHIP_TYPES.get(rel_type)
            if min_gap is None:
                continue
            target_id = rel.get("target_person_id", "")
            target = persons_by_id.get(target_id)
            if target is None:
                continue
            target_year: int | None = target.get("birth_year")
            if target_year is None:
                continue
            # Target should be at least min_gap years older than person.
            actual_gap = person_year - target_year
            if actual_gap < min_gap:
                target_name = target.get("display_name") or target_id
                person_name = person.get("display_name") or person_id
                warnings.append(
                    f"{person_name} ({person_year}) lists {target_name} ({target_year}) "
                    f"as {rel_type}, but the age gap is {actual_gap} year(s) "
                    f"(expected ≥{min_gap}). Check relationship direction."
                )
    return warnings


def person_payload_from_mapping(raw_data: dict[str, Any], source_label: str) -> dict[str, Any]:
    person_id = normalize_person_id(raw_data.get("person_id"))
    display_name = str(raw_data.get("display_name") or "").strip()
    if not display_name:
        raise PersonSyncError(f"display_name is required in {source_label}")

    birth_year = normalize_birth_year(raw_data.get("birth_year"))
    if birth_year is None:
        raise PersonSyncError(f"birth_year is required in {source_label}")

    sex = str(raw_data.get("sex_at_birth") or raw_data.get("sex") or "").strip().lower()
    if not sex:
        raise PersonSyncError(f"sex is required in {source_label}")

    payload: dict[str, Any] = {
        "person_id": person_id,
        "display_name": display_name,
        "birth_year": birth_year,
        "sex": sex,
        "relationships": normalize_relationships(raw_data.get("relationships") or []),
    }

    # Optional identity fields — preserved round-trip but not stored in the DB.
    legal_name = str(raw_data.get("legal_name") or "").strip()
    if legal_name:
        payload["legal_name"] = legal_name

    birth_date = str(raw_data.get("birth_date") or "").strip()
    if birth_date:
        payload["birth_date"] = birth_date

    identifiers = raw_data.get("identifiers")
    if identifiers and isinstance(identifiers, list) and len(identifiers) > 0:
        payload["identifiers"] = identifiers

    return payload


def load_person_from_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw_parsed: Any = yaml.safe_load(handle)

    raw_data: dict[str, Any] = (
        cast(dict[str, Any], raw_parsed) if isinstance(raw_parsed, dict) else {}
    )
    if not raw_parsed or not isinstance(raw_parsed, dict):
        raise PersonSyncError(f"YAML in {path} must be a mapping")

    return person_payload_from_mapping(raw_data, str(path))


def parse_front_matter(markdown_content: str) -> dict[str, Any]:
    if not markdown_content.startswith("---\n"):
        raise PersonSyncError("missing YAML front matter")

    end_marker_index = markdown_content.find("\n---\n", 4)
    if end_marker_index == -1:
        raise PersonSyncError("front matter is not terminated with '---'")

    raw_front_matter = markdown_content[4:end_marker_index]
    raw_parsed = yaml.safe_load(raw_front_matter)
    front_matter: dict[str, Any] = (
        cast(dict[str, Any], raw_parsed) if isinstance(raw_parsed, dict) else {}
    )

    return front_matter


def load_person_from_note(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        content = handle.read()

    front_matter = parse_front_matter(content)
    if not front_matter.get("person_id"):
        front_matter["person_id"] = path.stem

    return person_payload_from_mapping(front_matter, str(path))


def render_person_note(person: dict[str, Any]) -> str:
    front_matter: dict[str, Any] = {
        "person_id": person["person_id"],
        "record_type": "person",
        "display_name": person["display_name"],
    }
    if person.get("legal_name"):
        front_matter["legal_name"] = person["legal_name"]
    front_matter["birth_year"] = person["birth_year"]
    if person.get("birth_date"):
        front_matter["birth_date"] = person["birth_date"]
    front_matter["sex"] = person["sex"]
    if person.get("identifiers"):
        front_matter["identifiers"] = person["identifiers"]
    front_matter["relationships"] = person["relationships"]
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    person_id = person["person_id"]
    record_links = "\n".join(
        f"- [[Records/{person_id}/{subdir}/_index|{subdir}]]" for subdir in PERSON_RECORD_SUBDIRS
    )

    return (
        f"---\n{yaml_block}\n---\n\n"
        f"# {person['display_name']}\n\n"
        "## Records\n\n"
        f"{record_links}\n\n"
        "## Notes\n\n"
        "- Add context about this family member here.\n"
    )


def load_person_display_name(vault_root: Path, person_id: str) -> str:
    """Return the display_name from a person note, falling back to person_id."""
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.exists():
        return person_id
    try:
        content = person_note.read_text(encoding="utf-8")
        front_matter = parse_front_matter(content)
        return str(front_matter.get("display_name") or person_id)
    except (OSError, PersonSyncError):
        return person_id


def _extract_notes_section(content: str) -> str:
    """Return the ``## Notes`` heading and everything after it in *content*.

    Returns an empty string if there is no ``## Notes`` heading.
    """
    marker = "\n## Notes"
    pos = content.rfind(marker)
    if pos == -1:
        return ""
    return content[pos:]


def write_person_note_idempotent(note_path: Path, person: dict[str, Any]) -> str:
    """Create or update a person profile note.

    * If the note does not exist, render it fresh and write it.
    * If the note already exists, regenerate the front matter and ``## Records``
      section while preserving the user-editable ``## Notes`` section verbatim.

    Returns the final content written.
    """
    fresh = render_person_note(person)

    if not note_path.exists():
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    existing = note_path.read_text(encoding="utf-8")
    user_notes = _extract_notes_section(existing)

    if not user_notes:
        # No Notes section — safe to overwrite entirely.
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    fresh_marker = "\n## Notes"
    fresh_pos = fresh.rfind(fresh_marker)
    if fresh_pos == -1:
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    updated = fresh[:fresh_pos] + user_notes
    note_path.write_text(updated, encoding="utf-8")
    return updated


def _field_path(parent: str, child: str | int) -> str:
    if isinstance(child, int):
        return f"{parent}[{child}]"
    if not parent:
        return child
    return f"{parent}.{child}"


def _json_blob_violation(field_name: str, value: str) -> str | None:
    import json as _json

    stripped = value.strip()
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return None

    try:
        _json.loads(stripped)
    except _json.JSONDecodeError:
        return None

    return (
        f"field '{field_name}' contains an embedded JSON blob; use Markdown body sections instead"
    )


def _validate_no_embedded_json(value: Any, field_name: str) -> list[str]:
    if isinstance(value, str):
        violation = _json_blob_violation(field_name, value)
        return [violation] if violation else []

    if isinstance(value, list):
        violations: list[str] = []
        for index, item in enumerate(value):
            violations.extend(_validate_no_embedded_json(item, _field_path(field_name, index)))
        return violations

    if isinstance(value, dict):
        violations = []
        for key, item in value.items():
            violations.extend(_validate_no_embedded_json(item, _field_path(field_name, str(key))))
        return violations

    return []


def _is_compact_front_matter_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool, date, datetime)):
        return True
    if isinstance(value, list):
        return all(
            item is None or isinstance(item, (str, int, float, bool, dict)) for item in value
        )
    return False


def _allowed_front_matter_fields(record_type: str | None) -> set[str] | None:
    if not record_type:
        return None
    if record_type.endswith(INDEX_RECORD_TYPE_SUFFIX):
        return COMMON_FRONT_MATTER_FIELDS
    return KNOWN_RECORD_TYPE_FIELDS.get(record_type)


def _validate_front_matter_contract(front_matter: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    raw_record_type = front_matter.get("record_type")
    record_type = raw_record_type.strip() if isinstance(raw_record_type, str) else None
    allowed_fields = _allowed_front_matter_fields(record_type)

    for key, value in front_matter.items():
        if not isinstance(key, str):
            violations.append("front matter keys must be strings")
            continue
        if allowed_fields is not None and key not in allowed_fields:
            violations.append(f"field '{key}' is not allowed for record_type '{record_type}'")
        if not _is_compact_front_matter_value(value):
            violations.append(
                f"field '{key}' is too structured for front matter; use Markdown body sections"
            )

    return violations


def _validate_category_front_matter(front_matter: dict[str, Any]) -> list[str]:
    raw_record_type = front_matter.get("record_type")
    record_type = raw_record_type.strip() if isinstance(raw_record_type, str) else None
    if not record_type:
        return []

    required_fields = REQUIRED_RECORD_TYPE_FIELDS.get(record_type, ())
    violations = _missing_required_field_messages(front_matter, record_type, required_fields)
    if record_type.endswith(INDEX_RECORD_TYPE_SUFFIX) and not front_matter.get("person_id"):
        violations.append(f"missing required field for record_type '{record_type}': person_id")

    return violations


def _missing_required_field_messages(
    front_matter: dict[str, Any],
    record_type: str,
    required_fields: tuple[str, ...],
) -> list[str]:
    return [
        f"missing required field for record_type '{record_type}': {field}"
        for field in required_fields
        if front_matter.get(field) in (None, "")
    ]


def _validate_source_reference(field_name: str, value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"field '{field_name}' must be a non-empty string when present"]

    if field_name != "source_path":
        return []

    source_path = Path(value)
    if source_path.is_absolute() or ".." in source_path.parts:
        return [f"field '{field_name}' must be a relative vault or data path"]

    if source_path.parts[:2] == RETAINED_SOURCE_PATH_PREFIX:
        if len(source_path.parts) > 2:
            return []
        return [f"field '{field_name}' must point to a retained source file"]

    if source_path.parts and source_path.parts[0] == VAULT_SOURCE_PATH_ROOT:
        if source_path.suffix == ".md":
            return []
        return [f"field '{field_name}' vault-note fallback must point to a Markdown note"]

    allowed = "data/processed/... or Records/.../*.md"
    return [f"field '{field_name}' must point to {allowed}"]


def _requires_record_type(note_path: Path) -> bool:
    # Legacy person profile notes predate record_type. New renders include it, but
    # vault sync should not reject existing profiles before a re-import/backfill.
    return "Persons" not in note_path.parts


def validate_note_front_matter(note_path: Path) -> list[str]:
    """Return a list of schema violation messages for *note_path*'s front matter.

    Checks:
    - Front matter is parseable YAML.
    - No field value is a raw JSON string (embedded blob).
    - ``record_type`` is present and compact for generated record notes.
    - ``person_id`` is present.
    - Source reference fields are relative, non-empty strings when present.
    """
    try:
        content = note_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read file: {exc}"]

    try:
        front_matter = parse_front_matter(content)
    except (PersonSyncError, yaml.YAMLError) as exc:
        return [f"invalid front matter: {exc}"]

    if not front_matter:
        return ["no front matter found"]

    violations: list[str] = []

    if not front_matter.get("person_id"):
        violations.append("missing required field: person_id")

    record_type = front_matter.get("record_type")
    if _requires_record_type(note_path) and (
        not isinstance(record_type, str) or not record_type.strip()
    ):
        violations.append("missing required field: record_type")

    for key, value in front_matter.items():
        if key in SOURCE_REFERENCE_FIELDS:
            violations.extend(_validate_source_reference(key, value))
        violations.extend(_validate_no_embedded_json(value, str(key)))
    violations.extend(_validate_front_matter_contract(front_matter))
    violations.extend(_validate_category_front_matter(front_matter))

    return violations


def _is_vault_note_candidate(vault_root: Path, note_path: Path) -> bool:
    if note_path.suffix != ".md":
        return False

    relative = note_path.relative_to(vault_root)
    if "_person_id" in relative.parts or note_path.name == "_template.md":
        return False

    if relative.parts == ("Persons", "_index.md"):
        return False

    return relative.parts[0] in {"Persons", "Records"}


def _has_wiki_link(content: str, target: str) -> bool:
    return re.search(rf"\[\[{re.escape(target)}(?:\|[^\]]*)?\]\]", content) is not None


def _read_note_for_validation(note_path: Path) -> tuple[str | None, str | None]:
    try:
        return note_path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, f"cannot read file: {exc}"


def _validate_record_note_reachability(
    vault_root: Path,
    note_path: Path,
) -> list[NoteValidationIssue]:
    relative = note_path.relative_to(vault_root)
    if len(relative.parts) != 4 or relative.parts[0] != "Records":
        return []

    person_id = relative.parts[1]
    category = relative.parts[2]
    issues: list[NoteValidationIssue] = []

    if note_path.name == "_index.md":
        person_note = vault_root / "Persons" / f"{person_id}.md"
        person_content, read_error = _read_note_for_validation(person_note)
        target = f"Records/{person_id}/{category}/_index"

        if read_error:
            issues.append(NoteValidationIssue(person_note.relative_to(vault_root), read_error))
        elif person_content is not None and not _has_wiki_link(person_content, target):
            issues.append(
                NoteValidationIssue(relative, f"person hub does not link to [[{target}]]")
            )
        return issues

    category_index = note_path.parent / "_index.md"
    index_relative = category_index.relative_to(vault_root)
    if not category_index.exists():
        issues.append(
            NoteValidationIssue(index_relative, "missing category index for generated record notes")
        )
        return issues

    index_content, read_error = _read_note_for_validation(category_index)
    if read_error:
        issues.append(
            NoteValidationIssue(index_relative, f"cannot read category index: {read_error}")
        )
    elif index_content is not None and not _has_wiki_link(index_content, note_path.stem):
        issues.append(
            NoteValidationIssue(
                relative,
                f"category index {index_relative} does not link to [[{note_path.stem}]]",
            )
        )

    return issues


def validate_vault_notes(vault_root: Path) -> list[NoteValidationIssue]:
    """Validate generated vault notes under Persons and Records."""
    if not vault_root.exists():
        return [NoteValidationIssue(vault_root, "vault path does not exist")]

    issues: list[NoteValidationIssue] = []
    seen: set[tuple[Path, str]] = set()
    for note_path in sorted(vault_root.rglob("*.md")):
        if not _is_vault_note_candidate(vault_root, note_path):
            continue
        for message in validate_note_front_matter(note_path):
            issue = NoteValidationIssue(note_path.relative_to(vault_root), message)
            key = (issue.path, issue.message)
            if key not in seen:
                seen.add(key)
                issues.append(issue)
        for issue in _validate_record_note_reachability(vault_root, note_path):
            key = (issue.path, issue.message)
            if key not in seen:
                seen.add(key)
                issues.append(issue)
    return issues


def ensure_person_record_scaffold(vault_root: Path, person_id: str) -> None:
    base_path = vault_root / "Records" / person_id
    for subdir in PERSON_RECORD_SUBDIRS:
        (base_path / subdir).mkdir(parents=True, exist_ok=True)


def ensure_person_inbox_scaffold(inbox_root: Path, person_id: str) -> None:
    (inbox_root / person_id).mkdir(parents=True, exist_ok=True)


def create_person_interactive(
    vault_root: Path,
    inbox_root: Path,
    conn: sqlite3.Connection,
    existing_person_ids: list[str],
    *,
    _stdin: object = None,
) -> dict[str, Any] | None:
    """Interactively create a new person via stdin prompts.

    Asks for display_name, birth_year, and sex; generates a person_id slug;
    creates the vault note, DB row, and directory scaffolds; then walks
    through existing persons to offer relationship assignment.

    Returns the created person dict, or ``None`` if the user cancels at
    any prompt.
    """
    stdin = _stdin or sys.stdin

    display_name = _prompt_required(stdin, "Display name (e.g. Jane Doe)")
    if display_name is None:
        return None

    person_id = normalize_person_id(display_name)
    if person_id in _load_person_ids(vault_root):
        print(f"person_id '{person_id}' already exists; pick a different name.", file=sys.stderr)
        return None

    birth_year = _prompt_birth_year(stdin)
    if birth_year is None:
        return None

    sex = _prompt_sex(stdin)
    if sex is None:
        return None

    relationships = _prompt_relationships(stdin, person_id, vault_root, existing_person_ids)
    if relationships is None:
        return None

    person: dict[str, Any] = {
        "person_id": person_id,
        "display_name": display_name,
        "birth_year": birth_year,
        "sex": sex,
        "relationships": relationships,
    }

    try:
        ensure_person_record_scaffold(vault_root, person_id)
        ensure_person_inbox_scaffold(inbox_root, person_id)

        persons_dir = vault_root / "Persons"
        persons_dir.mkdir(parents=True, exist_ok=True)
        note_path = persons_dir / f"{person_id}.md"
        write_person_note_idempotent(note_path, person)

        assert_unique_display_name(conn, person)
        upsert_person(conn, person)
        replace_relationships(conn, person)
        conn.commit()

        person_records_dir = vault_root / "Records" / person_id
        for subdir in PERSON_RECORD_SUBDIRS:
            category_dir = person_records_dir / subdir
            from famed.lab_sync import write_category_index_note

            write_category_index_note(
                category_dir,
                person_id,
                subdir,
                display_name=display_name,
            )

        print(f"Created person: {display_name} ({person_id})")
        return person
    except (OSError, PersonSyncError, sqlite3.Error) as exc:
        print(f"Failed to create person: {exc}", file=sys.stderr)
        return None


def _prompt_required(stdin: object, label: str) -> str | None:
    """Prompt for a non-empty string; ``q`` cancels."""
    while True:
        try:
            value = input(f"{label} (q to cancel): ").strip()
        except EOFError:
            return None
        if value.lower() == "q":
            return None
        if value:
            return value
        print("Value is required.", file=sys.stderr)


def _prompt_birth_year(stdin: object) -> int | None:
    while True:
        try:
            value = input("Birth year (e.g. 1990, q to cancel): ").strip()
        except EOFError:
            return None
        if value.lower() == "q":
            return None
        try:
            year = int(value)
            if 1900 <= year <= date.today().year:
                return year
            print("Birth year must be between 1900 and current year.", file=sys.stderr)
        except ValueError:
            print("Enter a valid year.", file=sys.stderr)


def _prompt_sex(stdin: object) -> str | None:
    while True:
        try:
            value = input("Sex (male/female, q to cancel): ").strip().lower()
        except EOFError:
            return None
        if value == "q":
            return None
        if value in {"male", "female"}:
            return value
        print("Enter 'male' or 'female'.", file=sys.stderr)


def _load_person_ids(vault_root: Path) -> set[str]:
    persons_dir = vault_root / "Persons"
    if not persons_dir.is_dir():
        return set()
    ids: set[str] = set()
    for note_path in persons_dir.glob("*.md"):
        if note_path.name == "_index.md":
            continue
        ids.add(note_path.stem)
    return ids


def _prompt_relationships(
    stdin: object,
    person_id: str,
    vault_root: Path,
    existing_person_ids: list[str],
) -> list[dict[str, str]] | None:
    if not existing_person_ids:
        return []

    print("\nFor each existing person, you can set a relationship with the new person.")
    print("Options: parent, child, spouse, sibling, none")
    relationships: list[dict[str, str]] = []
    for other_id in existing_person_ids:
        if other_id == person_id:
            continue
        other_name = load_person_display_name(vault_root, other_id)
        while True:
            try:
                value = (
                    input(
                        f"  Relationship to '{other_name}' ({other_id})"
                        f" [parent/child/spouse/sibling/none]: "
                    )
                    .strip()
                    .lower()
                )
            except EOFError:
                return None
            if value in {"none", "", "n"}:
                break
            if value in {"parent", "child", "spouse", "sibling"}:
                relationships.append({"relationship_type": value, "target_person_id": other_id})
                break
            print("  Enter one of: parent, child, spouse, sibling, none", file=sys.stderr)
    return relationships


def relation_row_id(source_person_id: str, target_person_id: str, relationship_type: str) -> str:
    raw = f"{source_person_id}|{target_person_id}|{relationship_type}".encode()
    return hashlib.sha1(raw).hexdigest()


def _values_equal(a: Any, b: Any) -> bool:
    """Return True when two field values represent the same data.

    Normalises both sides to stripped strings so that, e.g., the integer
    birth_year 1990 stored in the DB compares equal to the incoming int 1990
    or the string "1990".
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def detect_person_sync_authority(
    conn: sqlite3.Connection,
    person_id: str,
    field_name: str,
    incoming_value: Any,
) -> AuthorityDecision:
    """Classify authority for one field of a person row before a vault sync.

    Returns one of:
    - ``"new"``        — person does not yet exist in the DB.
    - ``"vault_wins"`` — DB is unchanged since the last vault sync, or the values
                         agree; the incoming vault value should be written.
    - ``"ambiguous"``  — the DB row was modified after the last vault sync *and*
                         the incoming value differs; both sides changed and the
                         winner cannot be determined automatically.
    """
    row = conn.execute(
        "SELECT display_name, birth_year, sex, updated_at, vault_synced_at "
        "FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()

    if row is None:
        return AUTHORITY_NEW

    col_names = (
        "display_name",
        "birth_year",
        "sex",
        "updated_at",
        "vault_synced_at",
    )
    rec: dict[str, Any] = dict(zip(col_names, row, strict=True))

    db_value = rec.get(field_name)
    updated_at = rec["updated_at"]
    vault_synced_at = rec["vault_synced_at"]

    # If the DB has never been vault-synced, treat vault as authoritative.
    if vault_synced_at is None or updated_at is None:
        return AUTHORITY_VAULT_WINS

    # DB was modified after the last vault sync.
    if updated_at > vault_synced_at:
        if not _values_equal(incoming_value, db_value):
            return AUTHORITY_AMBIGUOUS

    return AUTHORITY_VAULT_WINS


def upsert_person(conn: sqlite3.Connection, person: dict[str, Any]) -> None:
    """Insert or update a person record from a vault sync.

    Sets ``vault_synced_at`` to the current time.  Does **not** touch
    ``updated_at`` so that any out-of-band DB modifications remain
    detectable by divergence checks.
    """
    conn.execute(
        """
        INSERT INTO persons (id, display_name, birth_year, sex, vault_synced_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            display_name = excluded.display_name,
            birth_year = excluded.birth_year,
            sex = excluded.sex,
            vault_synced_at = CURRENT_TIMESTAMP
        """,
        (
            person["person_id"],
            person["display_name"],
            person["birth_year"],
            person["sex"],
        ),
    )


def assert_unique_display_name(conn: sqlite3.Connection, person: dict[str, Any]) -> None:
    row = conn.execute(
        "SELECT id FROM persons WHERE lower(display_name) = lower(?) AND id != ? LIMIT 1",
        (person["display_name"], person["person_id"]),
    ).fetchone()
    if row:
        raise PersonSyncError(
            "display_name must be unique across persons "
            f"(conflicts with existing person_id={row[0]!r})"
        )


def replace_relationships(conn: sqlite3.Connection, person: dict[str, Any]) -> None:
    source_person_id = person["person_id"]
    conn.execute("DELETE FROM family_links WHERE source_person_id = ?", (source_person_id,))

    for relationship in person["relationships"]:
        target_person_id = relationship["target_person_id"]
        relationship_type = relationship["relationship_type"]

        conn.execute(
            """
            INSERT OR REPLACE INTO family_links (
                id,
                source_person_id,
                target_person_id,
                relationship_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                relation_row_id(source_person_id, target_person_id, relationship_type),
                source_person_id,
                target_person_id,
                relationship_type,
            ),
        )


def sync_person_to_db(conn: sqlite3.Connection, person: dict[str, Any]) -> None:
    assert_unique_display_name(conn, person)

    # Detect and log divergence for every shared field before writing.
    for field in SYNCED_PERSON_FIELDS:
        incoming = person.get(field)
        authority = detect_person_sync_authority(conn, person["person_id"], field, incoming)
        if authority == AUTHORITY_AMBIGUOUS:
            existing = conn.execute(
                f"SELECT {field} FROM persons WHERE id = ?",  # noqa: S608 – field is a constant
                (person["person_id"],),
            ).fetchone()
            db_value = existing[0] if existing else None
            log_sync_conflict(
                conn,
                table_name="persons",
                record_id=person["person_id"],
                field_name=field,
                vault_value=str(incoming) if incoming is not None else None,
                db_value=str(db_value) if db_value is not None else None,
                resolution=RESOLUTION_QUARANTINED,
            )

    upsert_person(conn, person)
    replace_relationships(conn, person)


def log_sync_conflict(
    conn: sqlite3.Connection,
    table_name: str,
    record_id: str,
    field_name: str | None,
    vault_value: str | None,
    db_value: str | None,
    resolution: Resolution | str,
) -> None:
    """Append a row to ``sync_conflicts`` and set resolved_at immediately.

    ``resolution`` must be one of the shared storage-contract resolution labels.
    """
    resolution = validate_resolution(resolution)
    conflict_id = hashlib.sha1(f"{table_name}|{record_id}|{field_name}".encode()).hexdigest()
    conn.execute(
        """
        INSERT OR REPLACE INTO sync_conflicts (
            id, table_name, record_id, field_name,
            vault_value, db_value, resolution, resolved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (conflict_id, table_name, record_id, field_name, vault_value, db_value, resolution),
    )
    log_audit_event(
        conn,
        event_type=AUDIT_SYNC_RESOLUTION_RECORDED,
        subject_type=table_name,
        subject_id=record_id,
        resolution=resolution,
        message=f"field: {field_name}" if field_name else None,
    )
