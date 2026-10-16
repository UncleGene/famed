"""Dated authoritative user-update notes and their DB persistence.

Implements REQ-024 / ADR-004.  Each category of user-authored facts is
persisted to a category-specific note under the matching subfolder:

* Medications → ``Records/{person_id}/Medications/{date}-{person_id}-medications.md``
* Labs        → ``Records/{person_id}/Labs/{date}-{person_id}-labs.md``
* History     → ``Records/{person_id}/History/{date}-{person_id}-history.md``

Notes land in their category subfolder so they appear in the category index
(``_index.md``) and are navigable from within Obsidian.

Same-day repeated updates to the same medication use snapshot semantics:
the later update overwrites the earlier entry for the same medication name.
Lab and history facts on the same date are merged into the same category note.

Re-import paths treat rows whose ``source_type`` is ``'user_update'`` identically
to ``'user_correction'`` rows: non-interactive re-imports skip them and emit
``skipped_user_correction``; interactive re-imports surface the conflict.

Public surface::

    record_user_update_medications(...) → int
    record_user_update_lab_facts(...) → int
    record_user_update_history_facts(...) → int

The AI never calls these functions autonomously; they are invoked only after the
user has confirmed the proposed facts through the fm-record-user-update skill.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from famed.dependent_reports import (
    refresh_fhir_dependent_export,
    refresh_lab_dependent_reports,
    refresh_medication_dependent_reports,
)
from famed.history_sync import infer_person_id_from_processed_history_path
from famed.import_labs import infer_person_id_from_processed_lab_path
from famed.lab_sync import write_category_index_note
from famed.medication_sync import (
    fill_rxnorm_cuis,
    infer_person_id_from_processed_medication_path,
    medication_conflict_axis_value_is_stated,
    medication_conflict_identity,
    medication_name_key,
    pending_medication_conflict_axes,
    record_medication_conflict_resolution_events,
    remove_redundant_lower_authority_medications,
    upsert_medication,
    write_current_medications_report,
    write_medication_conflicts_report,
)
from famed.person_sync import ensure_person_record_scaffold, load_person_display_name
from famed.reference_cache import load_rxnorm_index
from famed.reprocess_sources import ReprocessError, ReprocessResult
from famed.storage_contract import (
    AUDIT_USER_UPDATE_RECORDED,
    STATE_PROCESSED,
    log_audit_event,
)

_USER_UPDATE_SOURCE_TYPE = "user_update"


def _read_retained_front_matter(source_path: Path) -> dict[str, Any]:
    """Parse the YAML front matter block of a retained user-update source file."""
    content = source_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise ReprocessError(
            f"retained user-update source is missing YAML front matter: {source_path.name}"
        )
    end = content.find("\n---", 3)
    if end == -1:
        raise ReprocessError(
            f"retained user-update source front matter is not closed: {source_path.name}"
        )
    parsed = yaml.safe_load(content[3:end]) or {}
    if not isinstance(parsed, dict):
        raise ReprocessError(
            f"retained user-update source front matter is not a mapping: {source_path.name}"
        )
    return parsed


def _category_note_path(person_id: str, date_str: str, vault_root: Path, category: str) -> Path:
    """Return the category-subfolder path for a dated user-update note.

    Routes:
      * ``"medications"`` → ``Records/{person_id}/Medications/{date}-{person_id}-medications.md``
      * ``"labs"``        → ``Records/{person_id}/Labs/{date}-{person_id}-labs.md``
      * ``"history"``     → ``Records/{person_id}/History/{date}-{person_id}-history.md``
    """
    _CAT: dict[str, tuple[str, str]] = {
        "medications": ("Medications", "medications"),
        "labs": ("Labs", "labs"),
        "history": ("History", "history"),
    }
    folder, suffix = _CAT[category]
    return vault_root / "Records" / person_id / folder / f"{date_str}-{person_id}-{suffix}.md"


def _retained_source_path(
    processed_root: Path,
    person_id: str,
    date_str: str,
    category: str,
) -> Path:
    """Return the retained source file path for a user update.

    Creates a retained source file under the active processed-storage root so
    user updates can be re-imported if the vault is rebuilt from scratch. This
    ensures user-authored facts persist across system rebuilds without allowing
    tests or temporary vaults to write into live storage (ADR-003, ADR-004,
    REQ-021, REQ-024).

    Routes:
      * "medications" → {processed_root}/{person_id}/Medications/{date}-{person_id}-medications
      * "labs"        → {processed_root}/{person_id}/Labs/{date}-{person_id}-labs
      * "history"     → {processed_root}/{person_id}/History/{date}-{person_id}-history
    """
    _CAT_FOLDER = {
        "medications": "Medications",
        "labs": "Labs",
        "history": "History",
    }
    folder = _CAT_FOLDER[category]
    # Note: no .md extension for retained source files
    return processed_root / person_id / folder / f"{date_str}-{person_id}-{category}"


def _user_update_processed_root(vault_root: Path, processed_root: Path | None) -> Path:
    """Resolve retained storage without depending on the process working directory."""
    if processed_root is not None:
        return processed_root
    return vault_root.parent / "data" / "processed"


def _user_update_reference_root(vault_root: Path, reference_root: Path | None) -> Path:
    """Resolve reference storage without depending on the process working directory."""
    if reference_root is not None:
        return reference_root
    return vault_root.parent / "data" / "reference"


def _write_retained_source_file(
    processed_root: Path,
    person_id: str,
    date_str: str,
    category: str,
    content: str,
) -> None:
    """Write a retained source file for a user update.

    Ensures the user update can be re-imported from retained sources if needed.
    The file contains the same structured data as the vault note, allowing
    deterministic re-import (REQ-024, ADR-004).
    """
    source_path = _retained_source_path(processed_root, person_id, date_str, category)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(content, encoding="utf-8")


def _refresh_user_update_category_index(
    vault_root: Path,
    note_path: Path,
    person_id: str,
    category: str,
    *,
    note_label: str | None = None,
) -> None:
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        note_path.parent,
        person_id,
        category,
        note_label=note_label,
        display_name=display_name,
    )


# ---------------------------------------------------------------------------
# Medication user-updates
# ---------------------------------------------------------------------------


def record_user_update_medications(
    person_id: str,
    date_str: str,
    medications: list[dict[str, Any]],
    vault_root: Path,
    conn: sqlite3.Connection,
    *,
    processed_root: Path | None = None,
    reference_root: Path | None = None,
    refresh_reports: bool = True,
    write_retained_source: bool = True,
    record_audit: bool = True,
) -> int:
    """Persist user-authored medication updates for *person_id* on *date_str*.

    Same-day snapshot semantics: when a medication with the same name key already
    exists in the same-date note, the existing DB row is updated with the new
    values rather than being skipped.  The note is rewritten with the merged set.

    The note is written to ``Records/{person_id}/Medications/`` so it appears in
    the Medications category index.

    Returns the number of newly persisted rows (0 when no new facts are given).
    """
    medications = [dict(med) for med in medications]
    ensure_person_record_scaffold(vault_root, person_id)
    note_path = _category_note_path(person_id, date_str, vault_root, "medications")
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_relative = str(note_path.relative_to(vault_root))

    medications_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for med in medications:
        medication_name = str(med.get("medication_name") or "")
        identity = medication_conflict_identity(
            medication_name,
            str(med.get("medication_type") or "prescription"),
        )
        if not identity[0]:
            continue
        medications_by_identity.setdefault(identity, []).append(med)

    pending_updates: list[dict[str, Any]] = []
    for grouped_medications in medications_by_identity.values():
        med = grouped_medications[0]
        medication_name = str(med.get("medication_name") or "")
        medication_type = str(med.get("medication_type") or "prescription")
        pending_axes = pending_medication_conflict_axes(
            conn,
            person_id=person_id,
            medication_name=medication_name,
            medication_type=medication_type,
        )
        supplied_axes = frozenset(
            axis
            for axis, value in (("dose", med.get("dose")), ("frequency", med.get("frequency")))
            if medication_conflict_axis_value_is_stated(axis, value)
        )
        missing_axes = pending_axes - supplied_axes
        if missing_axes:
            missing_list = ", ".join(sorted(missing_axes))
            raise ReprocessError(
                "all pending medication conflict axes require explicit user values "
                f"before applying row-level authority; missing: {missing_list}"
            )
        if pending_axes and len(grouped_medications) != 1:
            raise ReprocessError(
                "a pending medication contradiction requires exactly one "
                "authoritative row for that medication"
            )
        if pending_axes:
            pending_updates.append(med)

    for med in pending_updates:
        record_medication_conflict_resolution_events(
            conn,
            person_id=person_id,
            medication_name=str(med.get("medication_name") or ""),
            medication_type=str(med.get("medication_type") or "prescription"),
            resolved_dose=med.get("dose"),
            resolved_frequency=med.get("frequency"),
            actor="user",
            reason=f"dated authoritative medication update on {date_str}",
        )

    existing = _query_user_update_medication_rows(conn, note_relative)
    existing_by_key: dict[str, dict[str, Any]] = {}
    for r in existing:
        key = _user_update_medication_key(r)
        existing_by_key[key] = r

    for med in medications:
        incoming_cui = str(med.get("rxnorm_cui") or "").strip()
        existing_row = existing_by_key.get(_user_update_medication_key(med))
        existing_cui = str(existing_row.get("rxnorm_cui") or "").strip() if existing_row else ""
        if not incoming_cui:
            med["rxnorm_cui"] = existing_cui or None
    fill_rxnorm_cuis(
        medications,
        load_rxnorm_index(_user_update_reference_root(vault_root, reference_root)),
    )

    updated = 0
    inserted = 0
    incoming_keys_by_name: dict[str, set[str]] = {}
    scheduled_names: set[str] = set()
    for med in medications:
        name_key = medication_name_key(med.get("medication_name"))
        if not name_key:
            continue
        incoming_keys_by_name.setdefault(name_key, set()).add(_user_update_medication_key(med))
        if _medication_period_is_dated(med):
            scheduled_names.add(name_key)
    for name_key, incoming_keys in incoming_keys_by_name.items():
        if len(incoming_keys) > 1:
            scheduled_names.add(name_key)

    for name_key in scheduled_names:
        incoming_keys = incoming_keys_by_name[name_key]
        for key, existing_row in list(existing_by_key.items()):
            if medication_name_key(existing_row.get("medication_name")) != name_key:
                continue
            if key in incoming_keys:
                continue
            _delete_user_update_medication_row(
                conn,
                note_relative,
                str(existing_row.get("medication_name") or ""),
                person_id,
                existing_row,
            )
            del existing_by_key[key]

    for med in medications:
        key = _user_update_medication_key(med)
        if key in existing_by_key:
            existing_row = existing_by_key[key]
            if _medication_update_changed(existing_row, med):
                db_name = existing_row.get("medication_name") or med.get("medication_name") or ""
                _update_user_update_medication_row(
                    conn, note_relative, db_name, med, person_id, existing_row
                )
                existing_row.update(med)
                updated += 1
            remove_redundant_lower_authority_medications(conn, existing_row)
            continue
        row_index = len(existing_by_key)
        row = _build_med_row_dict(med, person_id, note_relative, row_index)
        existing_by_key[key] = row
        upsert_medication(conn, row)
        remove_redundant_lower_authority_medications(conn, row)
        inserted += 1

    all_rows = list(existing_by_key.values())
    note_content = _render_medications_note(person_id, date_str, all_rows)
    note_path.write_text(note_content, encoding="utf-8")

    # Also write retained source file for re-importability
    if write_retained_source:
        _write_retained_source_file(
            _user_update_processed_root(vault_root, processed_root),
            person_id,
            date_str,
            "medications",
            note_content,
        )

    total_new = inserted + updated
    if total_new and record_audit:
        log_audit_event(
            conn,
            event_type=AUDIT_USER_UPDATE_RECORDED,
            subject_type="medication",
            subject_id=person_id,
            source_path=note_relative,
            status=STATE_PROCESSED,
            message=(
                f"recorded {inserted} new + {updated} updated "
                f"user-update medication row(s) on {date_str}"
            ),
        )

    _refresh_user_update_category_index(vault_root, note_path, person_id, "Medications")
    if refresh_reports:
        write_current_medications_report(vault_root, person_id, conn)
        write_medication_conflicts_report(conn, vault_root, person_id)
        refresh_medication_dependent_reports(conn, vault_root, person_id)
        refresh_fhir_dependent_export(conn, vault_root, person_id)

    return total_new


def _build_med_row_dict(
    med: dict[str, Any], person_id: str, note_relative: str, row_index: int
) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "medication_name": med.get("medication_name", ""),
        "medication_type": med.get("medication_type", "prescription"),
        "rxnorm_cui": med.get("rxnorm_cui"),
        "dose": med.get("dose"),
        "frequency": med.get("frequency"),
        "start_date": med.get("start_date"),
        "end_date": med.get("end_date"),
        "status": med.get("status", "active"),
        "source_type": _USER_UPDATE_SOURCE_TYPE,
        "source_path": note_relative,
        "row_index": row_index,
    }


def _user_update_medication_key(med: dict[str, Any]) -> str:
    name_key = medication_name_key(med.get("medication_name"))
    if not _medication_period_is_dated(med):
        return name_key
    return "|".join(
        [
            name_key,
            str(med.get("start_date") or ""),
            str(med.get("end_date") or ""),
        ]
    )


def _medication_period_is_dated(med: dict[str, Any]) -> bool:
    return bool(med.get("start_date") or med.get("end_date"))


def _medication_update_changed(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    fields = (
        "medication_name",
        "medication_type",
        "rxnorm_cui",
        "dose",
        "frequency",
        "status",
        "start_date",
        "end_date",
    )
    return any(
        str(existing.get(field) or "").strip() != str(incoming.get(field) or "").strip()
        for field in fields
    )


def _delete_user_update_medication_row(
    conn: sqlite3.Connection,
    source_path: str,
    med_name: str,
    person_id: str,
    med: dict[str, Any],
) -> None:
    conn.execute(
        """
        DELETE FROM medications
        WHERE source_path = ?
          AND source_type = ?
          AND person_id = ?
          AND medication_name = ?
          AND COALESCE(start_date, '') = ?
          AND COALESCE(end_date, '') = ?
        """,
        [
            source_path,
            _USER_UPDATE_SOURCE_TYPE,
            person_id,
            med_name,
            str(med.get("start_date") or ""),
            str(med.get("end_date") or ""),
        ],
    )


def _update_user_update_medication_row(
    conn: sqlite3.Connection,
    source_path: str,
    med_name: str,
    med: dict[str, Any],
    person_id: str,
    existing_row: dict[str, Any],
) -> None:
    updateable = [
        "medication_type",
        "dose",
        "frequency",
        "start_date",
        "end_date",
        "status",
        "rxnorm_cui",
    ]
    provided = [(c, med.get(c)) for c in updateable if c in med]
    if not provided:
        return
    set_clause = ", ".join(f"{c} = ?" for c, _ in provided)
    values = [v for _, v in provided]
    conn.execute(
        f"""
        UPDATE medications
        SET {set_clause}
        WHERE source_path = ?
          AND source_type = ?
          AND person_id = ?
          AND medication_name = ?
          AND COALESCE(start_date, '') = ?
          AND COALESCE(end_date, '') = ?
        """,
        values
        + [
            source_path,
            _USER_UPDATE_SOURCE_TYPE,
            person_id,
            med_name,
            str(existing_row.get("start_date") or ""),
            str(existing_row.get("end_date") or ""),
        ],
    )


def _query_user_update_medication_rows(
    conn: sqlite3.Connection,
    source_path: str,
) -> list[dict[str, Any]]:
    """Return existing user-update rows for *source_path* ordered by insertion order."""
    cursor = conn.execute(
        """
        SELECT person_id, medication_name, medication_type, rxnorm_cui, dose,
               frequency, start_date, end_date, status, source_path
        FROM medications
        WHERE source_path = ? AND source_type = 'user_update'
        ORDER BY rowid
        """,
        (source_path,),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]


def _read_medication_facts_from_note(note_path: Path) -> list[dict[str, Any]]:
    content = note_path.read_text(encoding="utf-8")
    in_medications = False
    facts: list[dict[str, Any]] = []
    columns = (
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
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Medications"):
            in_medications = True
            continue
        if in_medications and stripped.startswith("## "):
            break
        if (
            not in_medications
            or not stripped.startswith("| ")
            or stripped.startswith("| ---")
            or stripped.startswith("| medication_name")
        ):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        fact = {
            column: cells[index]
            for index, column in enumerate(columns)
            if index < len(cells) and cells[index]
        }
        facts.append(fact)
    return facts


def reprocess_retained_user_update_medication_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    refresh_reports: bool = True,
) -> ReprocessResult:
    """Replay one retained dated medication update snapshot."""
    person_id = infer_person_id_from_processed_medication_path(source_path, processed_root)
    if not person_id:
        raise ReprocessError(
            "retained user-update medication source must be under "
            "data/processed/<person_id>/Medications"
        )
    front_matter = _read_retained_front_matter(source_path)
    date_str = str(front_matter.get("date") or "").strip()
    if not date_str:
        raise ReprocessError(
            f"retained user-update medication source is missing date: {source_path.name}"
        )
    fm_person_id = str(front_matter.get("person_id") or "").strip()
    if fm_person_id and fm_person_id != person_id:
        raise ReprocessError(
            f"retained medication source person_id mismatch: path says {person_id!r}, "
            f"front matter says {fm_person_id!r}"
        )
    facts = _read_medication_facts_from_note(source_path)
    inserted = record_user_update_medications(
        person_id,
        date_str,
        facts,
        vault_root,
        conn,
        processed_root=processed_root,
        reference_root=root / "data" / "reference",
        refresh_reports=refresh_reports,
        write_retained_source=False,
        record_audit=False,
    )
    return ReprocessResult(imported_rows=inserted, affected_person_ids={person_id})


# ---------------------------------------------------------------------------
# Lab user-updates
# ---------------------------------------------------------------------------


def record_user_update_lab_facts(
    person_id: str,
    date_str: str,
    lab_facts: list[dict[str, Any]],
    vault_root: Path,
    conn: sqlite3.Connection,
    *,
    processed_root: Path | None = None,
    refresh_reports: bool = True,
    write_retained_source: bool = True,
) -> int:
    """Persist user-authored lab observations on *date_str* for *person_id*.

    Each fact requires at minimum ``test_name`` and ``observed_at``.
    Optional: ``value``, ``unit``, ``reference_range_low``,
    ``reference_range_high``, ``loinc_code``.

    Same-date dedup: a fact with the same ``(test_name, observed_at)`` that
    already exists in the same update note is skipped (snapshot via update).

    The note is written to ``Records/{person_id}/Labs/`` so it appears in the
    Labs category index.
    """
    ensure_person_record_scaffold(vault_root, person_id)
    note_path = _category_note_path(person_id, date_str, vault_root, "labs")
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_relative = str(note_path.relative_to(vault_root))

    existing = _query_user_update_lab_rows(conn, note_relative, person_id)
    existing_keys = {
        (r["test_name"].strip().lower(), str(r.get("observed_at", ""))) for r in existing
    }

    inserted = 0
    for fact in lab_facts:
        dedup_key = (
            str(fact.get("test_name", "")).strip().lower(),
            str(fact.get("observed_at", date_str)),
        )
        if dedup_key in existing_keys:
            continue
        _insert_lab_row(conn, fact, person_id, note_relative)
        existing_keys.add(dedup_key)
        inserted += 1

    all_labs = _query_user_update_lab_rows(conn, note_relative, person_id)
    note_content = _render_labs_note(person_id, date_str, all_labs)
    note_path.write_text(note_content, encoding="utf-8")

    # Also write retained source file for re-importability
    if write_retained_source:
        _write_retained_source_file(
            _user_update_processed_root(vault_root, processed_root),
            person_id,
            date_str,
            "labs",
            note_content,
        )

    if inserted:
        log_audit_event(
            conn,
            event_type=AUDIT_USER_UPDATE_RECORDED,
            subject_type="lab",
            subject_id=person_id,
            source_path=note_relative,
            status=STATE_PROCESSED,
            message=f"recorded {inserted} user-update lab row(s) on {date_str}",
        )

    _refresh_user_update_category_index(
        vault_root,
        note_path,
        person_id,
        "Labs",
        note_label="lab",
    )
    if refresh_reports:
        refresh_lab_dependent_reports(conn, vault_root, person_id)

    return inserted


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _insert_lab_row(
    conn: sqlite3.Connection,
    fact: dict[str, Any],
    person_id: str,
    source_path: str,
) -> None:
    row_id = str(uuid.uuid4())
    raw_value = fact.get("value")
    value = _to_float(raw_value)
    value_text = str(fact.get("value_text") or raw_value or "").strip()
    ref_low = _to_float(fact.get("reference_range_low"))
    ref_high = _to_float(fact.get("reference_range_high"))
    conn.execute(
        """
        INSERT INTO lab_results
          (id, person_id, loinc_code, test_name, value, value_text, unit,
           reference_range_low, reference_range_high, observed_at,
           source_type, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            person_id,
            fact.get("loinc_code"),
            fact.get("test_name", ""),
            value,
            value_text or None,
            fact.get("unit"),
            ref_low,
            ref_high,
            fact.get("observed_at", date.today().isoformat()),
            _USER_UPDATE_SOURCE_TYPE,
            source_path,
        ),
    )


def _query_user_update_lab_rows(
    conn: sqlite3.Connection,
    source_path: str,
    person_id: str,
) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT test_name, value, value_text, unit, reference_range_low, reference_range_high,
               observed_at, loinc_code
        FROM lab_results
        WHERE source_path = ? AND source_type = ? AND person_id = ?
        ORDER BY rowid
        """,
        (source_path, _USER_UPDATE_SOURCE_TYPE, person_id),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]


def _read_lab_facts_from_note(note_path: Path) -> list[dict[str, Any]]:
    """Parse the ``## Results`` table of a retained user-update Labs note.

    Mirrors ``_read_history_facts_from_note``'s table-scraping approach, over
    the column order ``_render_labs_note`` writes: test_name, loinc_code,
    value, unit, reference_range_low, reference_range_high, observed_at.
    """
    if not note_path.exists():
        return []
    content = note_path.read_text(encoding="utf-8")
    in_results = False
    facts: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Results"):
            in_results = True
            continue
        if in_results and stripped.startswith("## "):
            break
        if (
            in_results
            and stripped.startswith("| ")
            and not stripped.startswith("| ---")
            and not stripped.startswith("| test_name")
        ):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not cells or not cells[0]:
                continue
            fact: dict[str, Any] = {"test_name": cells[0]}
            if len(cells) > 1 and cells[1]:
                fact["loinc_code"] = cells[1]
            if len(cells) > 2 and cells[2]:
                fact["value"] = cells[2]
            if len(cells) > 3 and cells[3]:
                fact["unit"] = cells[3]
            if len(cells) > 4 and cells[4]:
                fact["reference_range_low"] = cells[4]
            if len(cells) > 5 and cells[5]:
                fact["reference_range_high"] = cells[5]
            if len(cells) > 6 and cells[6]:
                fact["observed_at"] = cells[6]
            facts.append(fact)
    return facts


def reprocess_retained_user_update_lab_source(
    *,
    root: Path,  # noqa: ARG001 -- kept for interface parity with other reprocess handlers
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    refresh_reports: bool = True,
) -> ReprocessResult:
    """Re-parse a retained user-update Labs snapshot and refresh its DB rows.

    These retained files are the exact rendered content of a dated
    ``fm-record-user-update`` Labs note (see ``_render_labs_note``) -- the
    authored update *is* the source. Reprocessing re-reads the retained
    snapshot's ``## Results`` table and replays it through
    ``record_user_update_lab_facts``, the same function the live authoring
    flow uses, so dedup, note rendering, and report refresh all behave
    identically to a fresh user-update call. Raises ``ReprocessError`` when
    the path is outside processed Labs or front matter is missing/invalid.
    """
    person_id = infer_person_id_from_processed_lab_path(source_path, processed_root)
    if not person_id:
        raise ReprocessError(
            "retained user-update lab source must be under data/processed/<person_id>/Labs"
        )
    front_matter = _read_retained_front_matter(source_path)
    date_str = str(front_matter.get("observed_at") or "").strip()
    if not date_str:
        raise ReprocessError(
            f"retained user-update lab source is missing observed_at: {source_path.name}"
        )
    fm_person_id = str(front_matter.get("person_id") or "").strip()
    if fm_person_id and fm_person_id != person_id:
        raise ReprocessError(
            f"retained lab source person_id mismatch: path says {person_id!r}, "
            f"front matter says {fm_person_id!r}"
        )

    facts = _read_lab_facts_from_note(source_path)
    inserted = record_user_update_lab_facts(
        person_id,
        date_str,
        facts,
        vault_root,
        conn,
        processed_root=processed_root,
        refresh_reports=refresh_reports,
        write_retained_source=False,
    )
    return ReprocessResult(imported_rows=inserted, affected_person_ids={person_id})


# ---------------------------------------------------------------------------
# History user-updates
# ---------------------------------------------------------------------------


def record_user_update_history_facts(
    person_id: str,
    date_str: str,
    history_facts: list[dict[str, Any]],
    vault_root: Path,
    conn: sqlite3.Connection,
    *,
    processed_root: Path | None = None,
    write_retained_source: bool = True,
) -> int:
    """Persist user-authored history facts on *date_str* for *person_id*.

    History facts are stored in the dated update note text (``## History``
    table section).  There is no separate DB table for history facts — the
    authority is the vault note itself.  Each fact requires ``description``;
    optional: ``category``, ``date``.

    Same-date dedup: facts with the same normalized description are skipped.

    The note is written to ``Records/{person_id}/History/`` so it appears in
    the History category index.
    """
    ensure_person_record_scaffold(vault_root, person_id)
    note_path = _category_note_path(person_id, date_str, vault_root, "history")
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_relative = str(note_path.relative_to(vault_root))

    existing = _read_history_facts_from_note(note_path)
    existing_descs = {str(f.get("description", "")).strip().lower() for f in existing}

    inserted = 0
    for fact in history_facts:
        desc = str(fact.get("description", "")).strip().lower()
        if not desc or desc in existing_descs:
            continue
        existing.append(fact)
        existing_descs.add(desc)
        inserted += 1

    note_content = _render_history_note(person_id, date_str, existing)
    note_path.write_text(note_content, encoding="utf-8")

    # Also write retained source file for re-importability
    if write_retained_source:
        _write_retained_source_file(
            _user_update_processed_root(vault_root, processed_root),
            person_id,
            date_str,
            "history",
            note_content,
        )

    if inserted:
        log_audit_event(
            conn,
            event_type=AUDIT_USER_UPDATE_RECORDED,
            subject_type="history",
            subject_id=person_id,
            source_path=note_relative,
            status=STATE_PROCESSED,
            message=f"recorded {inserted} user-update history fact(s) on {date_str}",
        )

    _refresh_user_update_category_index(vault_root, note_path, person_id, "History")
    return inserted


def _read_history_facts_from_note(note_path: Path) -> list[dict[str, Any]]:
    if not note_path.exists():
        return []
    content = note_path.read_text(encoding="utf-8")
    in_history = False
    facts: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## History"):
            in_history = True
            continue
        if in_history and stripped.startswith("## "):
            break
        if (
            in_history
            and stripped.startswith("| ")
            and not stripped.startswith("| ---")
            and not stripped.startswith("| Description")
        ):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 1 and cells[0]:
                fact: dict[str, Any] = {"description": cells[0]}
                if len(cells) > 1:
                    fact["category"] = cells[1]
                if len(cells) > 2:
                    fact["date"] = cells[2]
                if len(cells) > 3:
                    fact["details"] = cells[3]
                facts.append(fact)
    return facts


def reprocess_retained_user_update_history_source(
    *,
    root: Path,  # noqa: ARG001 -- kept for interface parity with other reprocess handlers
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
) -> ReprocessResult:
    """Re-parse a retained user-update History snapshot and refresh its note.

    Mirrors ``reprocess_retained_user_update_lab_source`` for the History
    category. Reuses ``_read_history_facts_from_note`` (already generic over
    any note with a ``## History`` table) and replays facts through
    ``record_user_update_history_facts``.
    """
    person_id = infer_person_id_from_processed_history_path(source_path, processed_root)
    if not person_id:
        raise ReprocessError(
            "retained user-update history source must be under data/processed/<person_id>/History"
        )
    front_matter = _read_retained_front_matter(source_path)
    date_str = str(front_matter.get("date") or "").strip()
    if not date_str:
        raise ReprocessError(
            f"retained user-update history source is missing date: {source_path.name}"
        )
    fm_person_id = str(front_matter.get("person_id") or "").strip()
    if fm_person_id and fm_person_id != person_id:
        raise ReprocessError(
            f"retained history source person_id mismatch: path says {person_id!r}, "
            f"front matter says {fm_person_id!r}"
        )

    facts = _read_history_facts_from_note(source_path)
    inserted = record_user_update_history_facts(
        person_id,
        date_str,
        facts,
        vault_root,
        conn,
        processed_root=processed_root,
        write_retained_source=False,
    )
    return ReprocessResult(imported_rows=inserted, affected_person_ids={person_id})


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _render_medications_note(
    person_id: str, date_str: str, medications: list[dict[str, Any]]
) -> str:
    """Render a medications-only update note using the standard medication template
    columns (matching the standard ``record_type: medication`` format)."""
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "medication",
        "category": "medications",
        "date": date_str,
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    rows: list[str] = [
        f"---\n{yaml_block}\n---\n",
        "# Medication List\n",
        "[[_index|← Medications]]\n",
        "## Medications\n",
        "| medication_name | medication_type | rxnorm_cui | dose | frequency |"
        " status | start_date | end_date | evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in medications:
        rxnorm = m.get("rxnorm_cui")
        rxnorm_display = rxnorm if rxnorm else ""
        rows.append(
            f"| {_cell(m.get('medication_name'))} "
            f"| {_cell(m.get('medication_type'))} "
            f"| {_cell(rxnorm_display)} "
            f"| {_cell(m.get('dose'))} "
            f"| {_cell(m.get('frequency'))} "
            f"| {_cell(m.get('status'))} "
            f"| {_cell(m.get('start_date'))} "
            f"| {_cell(m.get('end_date'))} "
            f"|  |"
        )
    return "\n".join(rows) + "\n"


def _render_labs_note(person_id: str, date_str: str, lab_facts: list[dict[str, Any]]) -> str:
    """Render a labs-only update note using the standard lab panel template
    (matching ``record_type: lab_panel`` format used by PDF imports)."""
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "lab_panel",
        "observed_at": date_str,
        "source_type": _USER_UPDATE_SOURCE_TYPE,
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    rows: list[str] = [
        f"---\n{yaml_block}\n---\n",
        f"# Lab Panel — {date_str}\n",
        "[[_index|← Labs]]\n",
        "## Results\n",
        "| test_name | loinc_code | value | unit |"
        " reference_range_low | reference_range_high | observed_at |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for lab in lab_facts:
        rows.append(
            f"| {_cell(lab.get('test_name'))} "
            f"| {_cell(lab.get('loinc_code'))} "
            f"| {_cell(lab.get('value'))} "
            f"| {_cell(lab.get('unit'))} "
            f"| {_cell(lab.get('reference_range_low'))} "
            f"| {_cell(lab.get('reference_range_high'))} "
            f"| {_cell(lab.get('observed_at', date_str))} |"
        )
    return "\n".join(rows) + "\n"


def _render_history_note(person_id: str, date_str: str, history_facts: list[dict[str, Any]]) -> str:
    """Render a history-only update note for the History subfolder."""
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "user_update",
        "category": "history",
        "date": date_str,
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    rows: list[str] = [
        f"---\n{yaml_block}\n---\n",
        f"# History Update — {date_str}\n",
        "[[_index|← History]]\n",
        "## History\n",
        "| Description | Category | Date | Details |",
        "| --- | --- | --- | --- |",
    ]
    for h in history_facts:
        rows.append(
            f"| {_cell(h.get('description'))} "
            f"| {_cell(h.get('category'))} "
            f"| {_cell(h.get('date'))} "
            f"| {_cell(h.get('details'))} |"
        )
    return "\n".join(rows) + "\n"
