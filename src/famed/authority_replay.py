"""Ordered replay of user-authored update and correction authority overlays."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from famed.dependent_reports import (
    refresh_fhir_dependent_export,
    refresh_lab_dependent_reports,
    refresh_medication_dependent_reports,
)
from famed.history_sync import (
    HISTORY_CORRECTIONS_NOTE,
    apply_history_refutations_to_notes,
    refresh_history_category_index,
    write_medical_history_summary_report,
)
from famed.lab_sync import write_category_index_note
from famed.medication_sync import (
    write_current_medications_report,
    write_medication_conflicts_report,
)
from famed.person_sync import load_person_display_name
from famed.reprocess_sources import (
    ReprocessError,
    restore_person_vault_tree,
    snapshot_person_vault_tree,
)
from famed.retained_replay import is_user_update_snapshot
from famed.storage_contract import (
    AUDIT_USER_FACT_CORRECTED,
    STATE_PROCESSED,
    log_audit_event,
)
from famed.user_update_sync import (
    reprocess_retained_user_update_history_source,
    reprocess_retained_user_update_lab_source,
    reprocess_retained_user_update_medication_source,
)


@dataclass(frozen=True)
class AuthorityCorrection:
    category: str
    values: dict[str, Any]


@dataclass(frozen=True)
class AuthorityReplaySummary:
    update_sources: int
    recovered_update_sources: int
    update_rows: int
    correction_rows: int
    affected_person_ids: tuple[str, ...]


_LAB_COLUMNS = (
    "id",
    "person_id",
    "loinc_code",
    "test_name",
    "value",
    "unit",
    "reference_range_low",
    "reference_range_high",
    "observed_at",
    "source_type",
    "source_path",
    "created_at",
    "loinc_code_origin",
)
_MEDICATION_COLUMNS = (
    "id",
    "person_id",
    "rxnorm_cui",
    "medication_name",
    "dose",
    "frequency",
    "start_date",
    "end_date",
    "status",
    "source_path",
    "created_at",
    "medication_type",
    "source_type",
)


def _read_active_corrections(source_db: Path) -> list[AuthorityCorrection]:
    corrections: list[AuthorityCorrection] = []
    try:
        with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            for category, table, columns in (
                ("labs", "lab_results", _LAB_COLUMNS),
                ("medications", "medications", _MEDICATION_COLUMNS),
            ):
                selected = ", ".join(columns)
                rows = conn.execute(
                    f"SELECT {selected} FROM {table} "
                    "WHERE source_type = 'user_correction' ORDER BY person_id, id"
                )
                corrections.extend(
                    AuthorityCorrection(category, dict(row)) for row in rows.fetchall()
                )
    except sqlite3.Error as exc:
        raise ReprocessError(f"cannot read active corrections from source database: {exc}") from exc
    return corrections


def _read_db_user_update_note_paths(source_db: Path) -> set[str]:
    try:
        with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_path FROM medications "
                "WHERE source_type = 'user_update' AND source_path LIKE 'Records/%' "
                "UNION SELECT DISTINCT source_path FROM lab_results "
                "WHERE source_type = 'user_update' AND source_path LIKE 'Records/%' "
                "ORDER BY source_path"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ReprocessError(f"cannot read user-update paths from source database: {exc}") from exc
    return {str(row[0]) for row in rows}


def _read_history_user_update_note_paths(source_vault: Path) -> set[str]:
    records_root = source_vault / "Records"
    if not records_root.is_dir():
        return set()
    paths: set[str] = set()
    for source_note in sorted(records_root.glob("*/History/*.md")):
        try:
            source_note.resolve().relative_to(source_vault.resolve())
        except ValueError as exc:
            raise ReprocessError(
                f"history user-update note escapes source vault: {source_note}"
            ) from exc
        source_path = str(source_note.relative_to(source_vault))
        front_matter, _content = _read_update_front_matter(source_note, source_path)
        if (
            front_matter.get("record_type") != "user_update"
            or front_matter.get("category") != "history"
        ):
            continue
        if front_matter.get("source_document") or front_matter.get("source_path"):
            continue
        paths.add(source_path)
    return paths


def _read_user_update_note_paths(source_vault: Path, source_db: Path) -> list[str]:
    paths = _read_db_user_update_note_paths(source_db)
    paths.update(_read_history_user_update_note_paths(source_vault))
    return sorted(paths)


def _read_update_front_matter(source_note: Path, source_path: str) -> tuple[dict[str, Any], bytes]:
    try:
        content_bytes = source_note.read_bytes()
        content = content_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReprocessError(f"cannot read user-update note: {source_path}") from exc
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise ReprocessError(f"user-update note lacks front matter: {source_path}")
    closing = normalized.find("\n---\n", 4)
    if closing < 0:
        raise ReprocessError(f"user-update note front matter is not closed: {source_path}")
    try:
        front_matter = yaml.safe_load(normalized[4:closing])
    except yaml.YAMLError as exc:
        raise ReprocessError(f"user-update note front matter is invalid: {source_path}") from exc
    if not isinstance(front_matter, dict):
        raise ReprocessError(f"user-update note front matter is not a mapping: {source_path}")
    return front_matter, content_bytes


def _validate_update_contract(
    front_matter: dict[str, Any],
    *,
    person_id: str,
    folder: str,
    source_path: str,
) -> tuple[str, str]:
    fm_person_id = str(front_matter.get("person_id") or "").strip()
    date_str = str(front_matter.get("date") or front_matter.get("observed_at") or "").strip()
    if not fm_person_id or not date_str:
        raise ReprocessError(f"user-update note identity is incomplete: {source_path}")
    if fm_person_id != person_id:
        raise ReprocessError(f"user-update note person path does not match: {source_path}")
    try:
        date.fromisoformat(date_str)
    except ValueError as exc:
        raise ReprocessError(f"user-update note date is invalid: {source_path}") from exc
    category = {
        "Medications": "medications",
        "Labs": "labs",
        "History": "history",
    }.get(folder)
    if category is None:
        raise ReprocessError(f"unsupported user-update note category: {source_path}")
    if folder == "Medications" and (
        front_matter.get("record_type") != "medication"
        or front_matter.get("category") != "medications"
    ):
        raise ReprocessError(f"medication update note contract is invalid: {source_path}")
    if folder == "Labs" and (
        front_matter.get("record_type") != "lab_panel"
        or front_matter.get("source_type") != "user_update"
    ):
        raise ReprocessError(f"lab update note contract is invalid: {source_path}")
    if folder == "History" and (
        front_matter.get("record_type") != "user_update"
        or front_matter.get("category") != "history"
    ):
        raise ReprocessError(f"history update note contract is invalid: {source_path}")
    return date_str, category


def _update_snapshot_target(
    *,
    source_vault: Path,
    source_path: str,
) -> tuple[Path, bytes]:
    relative_path = Path(source_path)
    source_note = (source_vault / relative_path).resolve()
    try:
        source_note.relative_to(source_vault.resolve())
    except ValueError as exc:
        raise ReprocessError(f"user-update note escapes source vault: {source_path}") from exc
    if not source_note.is_file():
        raise ReprocessError(f"user-update note does not exist: {source_path}")
    if len(relative_path.parts) != 4 or relative_path.parts[0] != "Records":
        raise ReprocessError(f"user-update note path is not category-native: {source_path}")
    person_id, folder = relative_path.parts[1:3]
    front_matter, content = _read_update_front_matter(source_note, source_path)
    date_str, category = _validate_update_contract(
        front_matter,
        person_id=person_id,
        folder=folder,
        source_path=source_path,
    )
    expected_name = f"{date_str}-{person_id}-{category}.md"
    if relative_path.name != expected_name:
        raise ReprocessError(f"user-update note path is not category-native: {source_path}")
    return Path(person_id) / folder / expected_name.removesuffix(".md"), content


def _validate_retained_update_snapshot(source_path: Path, processed_root: Path) -> None:
    relative_path = source_path.relative_to(processed_root)
    if len(relative_path.parts) != 3:
        raise ReprocessError(f"retained user-update path is not category-native: {relative_path}")
    person_id, folder = relative_path.parts[:2]
    front_matter, _content = _read_update_front_matter(source_path, str(relative_path))
    date_str, category = _validate_update_contract(
        front_matter,
        person_id=person_id,
        folder=folder,
        source_path=str(relative_path),
    )
    if source_path.name != f"{date_str}-{person_id}-{category}":
        raise ReprocessError(f"retained user-update path is not category-native: {relative_path}")


def authority_update_snapshot_paths(*, source_vault: Path, source_db: Path) -> set[Path]:
    """Return isolated-target paths generated from authoritative update notes."""
    return {
        _update_snapshot_target(source_vault=source_vault, source_path=source_path)[0]
        for source_path in _read_user_update_note_paths(source_vault, source_db)
    }


def _materialize_missing_update_snapshots(
    *,
    source_vault: Path,
    source_paths: list[str],
    processed_root: Path,
    source_processed_root: Path | None,
) -> int:
    snapshots = [
        _update_snapshot_target(source_vault=source_vault, source_path=source_path)
        for source_path in source_paths
    ]
    recovered = 0
    for relative_target, content in snapshots:
        target = processed_root / relative_target
        source_snapshot = (
            source_processed_root / relative_target if source_processed_root is not None else None
        )
        if source_snapshot is not None and source_snapshot.is_file():
            if not target.exists():
                recovered += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_snapshot, target)
            continue
        if target.exists() and source_snapshot is None:
            continue
        if not target.exists():
            recovered += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return recovered


def prepare_authority_update_snapshots(
    *,
    source_vault: Path,
    source_db: Path,
    processed_root: Path,
    source_processed_root: Path | None = None,
) -> int:
    """Validate and materialize authority snapshots needed by ordinary replay."""
    existing_update_sources = sorted(
        path
        for path in processed_root.rglob("*")
        if path.is_file() and is_user_update_snapshot(path)
    )
    for source_path in existing_update_sources:
        _validate_retained_update_snapshot(source_path, processed_root)
    recovered = _materialize_missing_update_snapshots(
        source_vault=source_vault,
        source_paths=_read_user_update_note_paths(source_vault, source_db),
        processed_root=processed_root,
        source_processed_root=source_processed_root,
    )
    prepared_update_sources = sorted(
        path
        for path in processed_root.rglob("*")
        if path.is_file() and is_user_update_snapshot(path)
    )
    for source_path in prepared_update_sources:
        _validate_retained_update_snapshot(source_path, processed_root)
    return recovered


def _copy_update_snapshot_notes(
    *,
    processed_root: Path,
    source_paths: list[Path],
    target_vault: Path,
) -> None:
    for source_path in source_paths:
        relative_path = source_path.relative_to(processed_root)
        target_note = (
            target_vault
            / "Records"
            / relative_path.parts[0]
            / relative_path.parts[1]
            / f"{source_path.name}.md"
        )
        target_note.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_note)


def _upsert_correction(conn: sqlite3.Connection, correction: AuthorityCorrection) -> None:
    table = "lab_results" if correction.category == "labs" else "medications"
    columns = _LAB_COLUMNS if correction.category == "labs" else _MEDICATION_COLUMNS
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        tuple(correction.values[column] for column in columns),
    )


def _correction_is_current(
    conn: sqlite3.Connection,
    correction: AuthorityCorrection,
) -> bool:
    table = "lab_results" if correction.category == "labs" else "medications"
    columns = _LAB_COLUMNS if correction.category == "labs" else _MEDICATION_COLUMNS
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?",
        (correction.values["id"],),
    ).fetchone()
    return row == tuple(correction.values[column] for column in columns)


def _copy_correction_note(
    *,
    source_vault: Path,
    target_vault: Path,
    source_path: str | None,
) -> None:
    if not source_path or not source_path.startswith("Records/"):
        return
    relative_path = Path(source_path)
    source_note = (source_vault / relative_path).resolve()
    try:
        source_note.relative_to(source_vault.resolve())
    except ValueError as exc:
        raise ReprocessError(f"correction note escapes source vault: {source_path}") from exc
    if not source_note.is_file():
        raise ReprocessError(f"correction note does not exist: {source_path}")
    target_note = target_vault / relative_path
    target_note.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_note, target_note)


def _history_correction_person_ids(source_vault: Path) -> set[str]:
    return {
        note.parent.parent.name
        for note in (source_vault / "Records").glob(f"*/History/{HISTORY_CORRECTIONS_NOTE}")
    }


def _replay_update_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
) -> tuple[int, set[str], str]:
    relative = source_path.relative_to(processed_root)
    if len(relative.parts) < 3:
        raise ReprocessError(f"invalid retained user-update path: {relative}")
    category = relative.parts[1]
    if category == "Medications":
        result = reprocess_retained_user_update_medication_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            refresh_reports=False,
        )
        return result.imported_rows, result.affected_person_ids, "medications"
    if category == "Labs":
        result = reprocess_retained_user_update_lab_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            refresh_reports=False,
        )
        return result.imported_rows, result.affected_person_ids, "labs"
    if category == "History":
        result = reprocess_retained_user_update_history_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )
        return result.imported_rows, result.affected_person_ids, "history"
    raise ReprocessError(f"unsupported retained user-update category: {relative}")


def replay_authority_overlays(
    *,
    root: Path,
    source_vault: Path,
    source_db: Path,
    target_vault: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_processed_root: Path | None = None,
    prepared_update_sources: int = 0,
) -> AuthorityReplaySummary:
    """Replay dated update notes, then active corrections, as one atomic overlay."""
    recovered_update_sources = prepared_update_sources + prepare_authority_update_snapshots(
        source_vault=source_vault,
        source_db=source_db,
        processed_root=processed_root,
        source_processed_root=source_processed_root,
    )
    update_sources = sorted(
        path
        for path in processed_root.rglob("*")
        if path.is_file() and is_user_update_snapshot(path)
    )
    for source_path in update_sources:
        _validate_retained_update_snapshot(source_path, processed_root)
    corrections = _read_active_corrections(source_db)
    history_correction_person_ids = _history_correction_person_ids(source_vault)
    affected_person_ids = (
        {path.relative_to(processed_root).parts[0] for path in update_sources}
        | {str(correction.values["person_id"]) for correction in corrections}
        | history_correction_person_ids
    )
    snapshots = {
        person_id: snapshot_person_vault_tree(target_vault, person_id)
        for person_id in affected_person_ids
    }
    lab_person_ids: set[str] = set()
    medication_person_ids: set[str] = set()
    history_person_ids: set[str] = set()
    update_rows = 0

    conn.execute("SAVEPOINT authority_overlay")
    try:
        for source_path in update_sources:
            imported_rows, person_ids, category = _replay_update_source(
                root=root,
                vault_root=target_vault,
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )
            update_rows += imported_rows
            if category == "labs":
                lab_person_ids.update(person_ids)
            elif category == "medications":
                medication_person_ids.update(person_ids)
            else:
                history_person_ids.update(person_ids)

        _copy_update_snapshot_notes(
            processed_root=processed_root,
            source_paths=update_sources,
            target_vault=target_vault,
        )

        for correction in corrections:
            correction_changed = not _correction_is_current(conn, correction)
            _upsert_correction(conn, correction)
            person_id = str(correction.values["person_id"])
            source_path = correction.values.get("source_path")
            _copy_correction_note(
                source_vault=source_vault,
                target_vault=target_vault,
                source_path=str(source_path) if source_path else None,
            )
            if correction_changed:
                log_audit_event(
                    conn,
                    event_type=AUDIT_USER_FACT_CORRECTED,
                    subject_type=("lab_result" if correction.category == "labs" else "medication"),
                    subject_id=person_id,
                    source_path=str(source_path) if source_path else None,
                    status=STATE_PROCESSED,
                    message="active user correction replayed during isolated rebuild",
                )
            if correction.category == "labs":
                lab_person_ids.add(person_id)
            else:
                medication_person_ids.add(person_id)

        for person_id in sorted(history_correction_person_ids):
            relative_note = Path("Records") / person_id / "History" / HISTORY_CORRECTIONS_NOTE
            _copy_correction_note(
                source_vault=source_vault,
                target_vault=target_vault,
                source_path=relative_note.as_posix(),
            )
            apply_history_refutations_to_notes(target_vault, person_id)
            refresh_history_category_index(target_vault, person_id)
            history_person_ids.add(person_id)

        for person_id in sorted(lab_person_ids):
            display_name = load_person_display_name(target_vault, person_id)
            write_category_index_note(
                target_vault / "Records" / person_id / "Labs",
                person_id,
                "Labs",
                display_name=display_name,
            )
            refresh_lab_dependent_reports(conn, target_vault, person_id)
        for person_id in sorted(medication_person_ids):
            display_name = load_person_display_name(target_vault, person_id)
            write_category_index_note(
                target_vault / "Records" / person_id / "Medications",
                person_id,
                "Medications",
                display_name=display_name,
            )
            write_current_medications_report(target_vault, person_id, conn)
            write_medication_conflicts_report(conn, target_vault, person_id)
            refresh_medication_dependent_reports(conn, target_vault, person_id)
            if person_id not in lab_person_ids:
                refresh_fhir_dependent_export(conn, target_vault, person_id)
        for person_id in sorted(history_person_ids):
            write_medical_history_summary_report(target_vault, person_id)
    except Exception:
        conn.execute("ROLLBACK TO authority_overlay")
        conn.execute("RELEASE authority_overlay")
        for person_id, snapshot in snapshots.items():
            restore_person_vault_tree(target_vault, person_id, snapshot)
        raise
    conn.execute("RELEASE authority_overlay")
    conn.commit()
    return AuthorityReplaySummary(
        update_sources=len(update_sources),
        recovered_update_sources=recovered_update_sources,
        update_rows=update_rows,
        correction_rows=len(corrections),
        affected_person_ids=tuple(sorted(affected_person_ids)),
    )
