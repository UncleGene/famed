#!/usr/bin/env python3
"""Sync person and lab front matter from vault notes into SQLite."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from famed.dependent_reports import refresh_lab_dependent_reports
from famed.lab_sync import (
    LabSyncError,
    backfill_lab_units_in_db,
    load_labs_from_note,
    purge_non_result_lab_rows_in_db,
    repair_lab_loinc_codes,
    repair_lab_units,
    replace_lab_results_for_source_path,
    write_category_index_note,
    write_flagged_for_discussion_report,
    write_lab_results_report,
)
from famed.medication_sync import repair_medication_rxnorm_cuis
from famed.person_sync import (
    PERSON_RECORD_SUBDIRS,
    PersonSyncError,
    apply_relationship_closure,
    check_relationship_age_plausibility,
    ensure_person_inbox_scaffold,
    ensure_person_record_scaffold,
    load_person_from_note,
    log_sync_conflict,
    sync_person_to_db,
    validate_note_front_matter,
    validate_vault_notes,
    write_person_note_idempotent,
)
from famed.reference_cache import load_loinc_index, load_rxnorm_index
from famed.storage_contract import (
    AUDIT_LAB_SOURCE_PATH_MIGRATED,
    AUTHORITY_AMBIGUOUS,
    AUTHORITY_DB_WINS,
    RESOLUTION_DB_WINS,
    RESOLUTION_QUARANTINED,
    RESOLUTION_VAULT_WINS,
    STATE_PROCESSED,
    AuthorityDecision,
    Resolution,
    log_audit_event,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan famed-vault/Persons and sync front matter into data/famed.db"
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
        "--non-interactive",
        action="store_true",
        help=(
            "Suppress user prompts. Conflicts are quarantined rather than resolved interactively."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate generated vault note front matter without syncing the database.",
    )
    parser.add_argument(
        "--repair-navigation",
        action="store_true",
        help=("Regenerate person hub links and category indexes without syncing the database."),
    )
    parser.add_argument(
        "--repair-loinc",
        action="store_true",
        help=(
            "Fill blank loinc_code cells in all lab panel notes using the local LOINC "
            "reference cache, then exit. Non-blank codes are never overwritten."
        ),
    )
    parser.add_argument(
        "--repair-rxnorm",
        action="store_true",
        help=(
            "Fill blank rxnorm_cui cells in all medication notes and DB rows using the "
            "local RxNorm reference cache, then exit. Non-blank CUIs are never overwritten."
        ),
    )
    parser.add_argument(
        "--repair-trends",
        action="store_true",
        help=(
            "Regenerate lab-results.md for every person who has lab data in the DB, "
            "then exit. Use after adding lab data to backfill the report without a full re-import."
        ),
    )
    parser.add_argument(
        "--repair-units",
        action="store_true",
        help=(
            "Fill empty unit cells for reviewed LOINCs (ADR-035) and drop non-result "
            "narrative rows in lab panel notes and DB rows, regenerate affected lab "
            "reports, then exit. Empty units on non-reviewed LOINCs are left untouched."
        ),
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_mtime(path: Path) -> str:
    """Return the file's mtime as an ISO-8601 UTC string (seconds precision)."""
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def detect_person_divergence(
    conn: sqlite3.Connection,
    person_id: str,
    note_path: Path,
) -> AuthorityDecision | None:
    """Check whether the DB record has diverged from the vault note since the last sync.

    Returns one of:
    - ``None``           — no divergence; proceed with vault-wins sync
    - ``"db_wins"``      — DB was modified after last sync, vault note unchanged; skip overwrite
    - ``"ambiguous"``    — both vault note and DB modified since last sync; needs resolution
    """
    row = conn.execute(
        "SELECT updated_at, vault_synced_at FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()

    if row is None:
        return None  # new record, no divergence possible

    updated_at, vault_synced_at = row

    if not vault_synced_at:
        return None  # first sync for this record

    note_mtime = _iso_mtime(note_path)
    vault_changed = note_mtime > vault_synced_at
    db_changed = updated_at is not None and updated_at > vault_synced_at

    if db_changed and vault_changed:
        return AUTHORITY_AMBIGUOUS
    if db_changed:
        return AUTHORITY_DB_WINS
    return None


def _resolve_person_conflict(
    conn: sqlite3.Connection,
    person_id: str,
    divergence: AuthorityDecision,
    note_path: Path,
    non_interactive: bool,
) -> Resolution:
    """Log divergence to sync_conflicts and return the chosen resolution label."""
    if divergence == AUTHORITY_DB_WINS:
        log_sync_conflict(
            conn,
            table_name="persons",
            record_id=person_id,
            field_name=None,
            vault_value=str(note_path),
            db_value=None,
            resolution=RESOLUTION_DB_WINS,
        )
        return RESOLUTION_DB_WINS

    # Ambiguous: both sides changed.
    if non_interactive:
        log_sync_conflict(
            conn,
            table_name="persons",
            record_id=person_id,
            field_name=None,
            vault_value=str(note_path),
            db_value=None,
            resolution=RESOLUTION_QUARANTINED,
        )
        return RESOLUTION_QUARANTINED

    row = conn.execute(
        "SELECT updated_at, vault_synced_at FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()
    db_updated_at = row[0] if row else "unknown"
    note_mtime = _iso_mtime(note_path)

    print(
        f"\nConflict for person '{person_id}':\n"
        f"  vault note last modified : {note_mtime}\n"
        f"  DB record last updated   : {db_updated_at}\n"
        f"  last vault sync          : {row[1] if row else 'never'}\n"
        "Choose resolution:\n"
        "  [v] vault wins — overwrite DB with vault note values\n"
        "  [d] DB wins    — keep DB values, skip this person\n"
        "  [q] quarantine — skip now, review manually\n"
    )
    while True:
        choice = input("Resolution [v/d/q]: ").strip().lower()
        if choice in ("v", "d", "q"):
            break
        print("Please enter v, d, or q.")

    resolution_map: dict[str, Resolution] = {
        "v": RESOLUTION_VAULT_WINS,
        "d": RESOLUTION_DB_WINS,
        "q": RESOLUTION_QUARANTINED,
    }
    resolution = resolution_map[choice]
    log_sync_conflict(
        conn,
        table_name="persons",
        record_id=person_id,
        field_name=None,
        vault_value=str(note_path),
        db_value=None,
        resolution=resolution,
    )
    return resolution


def repair_navigation(vault_root: Path) -> tuple[int, int]:
    """Regenerate person hub links and person-scoped category indexes."""
    persons_dir = vault_root / "Persons"
    records_dir = vault_root / "Records"
    if not persons_dir.exists():
        raise PersonSyncError(f"Persons directory not found: {persons_dir}")
    records_dir.mkdir(parents=True, exist_ok=True)

    repaired_persons = 0
    regenerated_indexes = 0
    note_paths = sorted(path for path in persons_dir.glob("*.md") if not path.name.startswith("_"))

    for note_path in note_paths:
        violations = validate_note_front_matter(note_path)
        if violations:
            joined = "; ".join(violations)
            raise PersonSyncError(f"{note_path.name}: {joined}")

        person = load_person_from_note(note_path)
        write_person_note_idempotent(note_path, person)
        ensure_person_record_scaffold(vault_root, person["person_id"])

        person_records_dir = records_dir / person["person_id"]
        for subdir in PERSON_RECORD_SUBDIRS:
            write_category_index_note(
                person_records_dir / subdir,
                person["person_id"],
                subdir,
                display_name=person["display_name"],
            )
            regenerated_indexes += 1

        repaired_persons += 1

    return repaired_persons, regenerated_indexes


def lab_db_source_path(vault_root: Path, note_path: Path, labs: list[dict[str, Any]]) -> str:
    """Return the source key to use when rebuilding lab DB rows from a vault note."""
    note_source_path = str(note_path.relative_to(vault_root))
    retained_source_paths = {
        str(lab.get("source_path") or "").strip()
        for lab in labs
        if str(lab.get("source_path") or "").strip()
    }
    if not retained_source_paths:
        return note_source_path
    if len(retained_source_paths) > 1:
        raise LabSyncError(
            f"lab note has conflicting source_path values: {sorted(retained_source_paths)}"
        )
    return retained_source_paths.pop()


def main() -> int:
    args = parse_args()
    root = repo_root()

    vault_root = (root / args.vault_path).resolve()
    db_path = (root / args.db_path).resolve()
    inbox_root = (root / "data/inbox").resolve()

    if args.repair_navigation:
        try:
            repaired_persons, regenerated_indexes = repair_navigation(vault_root)
        except (OSError, PersonSyncError) as exc:
            print(f"Navigation repair failed: {exc}")
            return 1

        print(
            "Navigation repair completed: "
            f"{repaired_persons} person note(s), "
            f"{regenerated_indexes} category index note(s)."
        )
        return 0

    if args.repair_loinc:
        reference_root = root / "data" / "reference"
        loinc_index = load_loinc_index(reference_root)
        if not loinc_index:
            print(
                "LOINC reference cache not found. "
                "Run: python scripts/cache.py refresh --dataset loinc"
            )
            return 1
        notes_updated, cells_filled = repair_lab_loinc_codes(vault_root, loinc_index)
        print(
            f"LOINC repair completed: {cells_filled} cell(s) filled across {notes_updated} note(s)."
        )
        return 0

    if args.repair_trends:
        db_path = root / "data" / "famed.db"
        with sqlite3.connect(db_path) as conn:
            persons = [
                row[0]
                for row in conn.execute("SELECT DISTINCT person_id FROM lab_results").fetchall()
            ]
            updated = 0
            for person_id in sorted(persons):
                try:
                    _, count = write_lab_results_report(conn, vault_root, person_id)
                    write_flagged_for_discussion_report(conn, vault_root, person_id)
                    updated += 1
                except Exception as exc:
                    print(f"  {person_id}: skipped — {exc}", file=sys.stderr)
        print(f"Trends repair completed: lab-results.md regenerated for {updated} person(s).")
        return 0

    if args.repair_units:
        db_path = root / "data" / "famed.db"
        ref_db_path = root / "data" / "reference" / "famed-reference.db"
        with sqlite3.connect(db_path) as conn:
            units_filled = backfill_lab_units_in_db(conn)
            junk_deleted = purge_non_result_lab_rows_in_db(conn)
            conn.commit()
            notes_changed = repair_lab_units(vault_root)
            persons = [
                row[0]
                for row in conn.execute("SELECT DISTINCT person_id FROM lab_results").fetchall()
            ]
            reports_refreshed = 0
            for person_id in sorted(persons):
                try:
                    refresh_lab_dependent_reports(
                        conn,
                        vault_root,
                        person_id,
                        ref_db_path=ref_db_path,
                    )
                    reports_refreshed += 1
                except Exception as exc:
                    print(f"  {person_id}: skipped — {exc}", file=sys.stderr)
        print(
            "Unit repair completed: "
            f"{units_filled} DB unit(s) filled, {junk_deleted} non-result row(s) purged, "
            f"{notes_changed} panel note(s) updated, "
            f"reports refreshed for {reports_refreshed} person(s)."
        )
        return 0

    if args.repair_rxnorm:
        reference_root = root / "data" / "reference"
        rxnorm_index = load_rxnorm_index(reference_root)
        if not rxnorm_index:
            print(
                "RxNorm reference cache not found. "
                "Run: python scripts/cache.py refresh --dataset rxnorm"
            )
            return 1
        db_path = root / "data" / "famed.db"
        with sqlite3.connect(db_path) as conn:
            notes_updated, cells_filled = repair_medication_rxnorm_cuis(
                vault_root, rxnorm_index, conn
            )
        print(
            "RxNorm repair completed: "
            f"{cells_filled} cell(s) filled across {notes_updated} note(s)."
        )
        return 0

    if args.validate_only:
        issues = validate_vault_notes(vault_root)
        if not issues:
            print("Vault note validation passed.")
            return 0

        print("Vault note validation failed:")
        for issue in issues:
            print(f"- {issue.path}: {issue.message}")
        return 1

    persons_dir = vault_root / "Persons"
    if not persons_dir.exists():
        print(f"Persons directory not found: {persons_dir}")
        return 1

    records_dir = vault_root / "Records"
    if not records_dir.exists():
        print(f"Records directory not found: {records_dir}")
        return 1

    note_paths = sorted(path for path in persons_dir.glob("*.md") if not path.name.startswith("_"))
    if not note_paths:
        print(f"No person notes found in: {persons_dir}")
        return 0

    synced_persons = 0
    skipped_db_wins = 0
    quarantined = 0
    synced_labs = 0
    lab_person_ids_to_refresh: set[str] = set()
    errors: list[str] = []
    synced_persons_by_id: dict[str, dict[str, Any]] = {}

    with sqlite3.connect(db_path) as conn:
        for note_path in note_paths:
            try:
                violations = validate_note_front_matter(note_path)
                if violations:
                    joined = "; ".join(violations)
                    errors.append(f"{note_path.name}: {joined}")
                    continue

                person = load_person_from_note(note_path)
                divergence = detect_person_divergence(conn, person["person_id"], note_path)

                if divergence is not None:
                    resolution = _resolve_person_conflict(
                        conn,
                        person["person_id"],
                        divergence,
                        note_path,
                        non_interactive=args.non_interactive,
                    )
                    if resolution == RESOLUTION_VAULT_WINS:
                        # User chose vault; fall through to normal sync below.
                        pass
                    elif resolution == RESOLUTION_DB_WINS:
                        skipped_db_wins += 1
                        continue
                    else:  # quarantined
                        quarantined += 1
                        continue

                ensure_person_record_scaffold(vault_root, person["person_id"])
                ensure_person_inbox_scaffold(inbox_root, person["person_id"])
                person_records_dir = vault_root / "Records" / person["person_id"]
                for subdir in PERSON_RECORD_SUBDIRS:
                    write_category_index_note(
                        person_records_dir / subdir,
                        person["person_id"],
                        subdir,
                        display_name=person["display_name"],
                    )
                sync_person_to_db(conn, person)
                synced_persons_by_id[person["person_id"]] = person
                synced_persons += 1
            except (OSError, sqlite3.Error, PersonSyncError) as exc:
                errors.append(f"{note_path.name}: {exc}")

        changed_person_ids = apply_relationship_closure(synced_persons_by_id)
        for person_id in sorted(changed_person_ids):
            try:
                person = synced_persons_by_id[person_id]
                note_path = vault_root / "Persons" / f"{person_id}.md"
                write_person_note_idempotent(note_path, person)
                sync_person_to_db(conn, person)
            except (OSError, sqlite3.Error, PersonSyncError) as exc:
                errors.append(f"{person_id}: {exc}")

        for warning in check_relationship_age_plausibility(synced_persons_by_id):
            print(f"[relationship warning] {warning}")

        lab_note_paths = sorted(
            path for path in records_dir.glob("*/Labs/*.md") if not path.name.startswith("_")
        )
        for note_path in lab_note_paths:
            try:
                violations = validate_note_front_matter(note_path)
                if violations:
                    joined = "; ".join(violations)
                    errors.append(f"{note_path.relative_to(vault_root)}: {joined}")
                    continue

                labs = load_labs_from_note(note_path)
                source_path = lab_db_source_path(vault_root, note_path, labs)
                note_source_path = str(note_path.relative_to(vault_root))
                if source_path != note_source_path:
                    cursor = conn.execute(
                        "DELETE FROM lab_results WHERE source_path = ?",
                        (note_source_path,),
                    )
                    if cursor.rowcount:
                        person_id = str(labs[0].get("person_id") or "") if labs else None
                        log_audit_event(
                            conn,
                            event_type=AUDIT_LAB_SOURCE_PATH_MIGRATED,
                            subject_type="lab_panel",
                            subject_id=person_id,
                            source_path=source_path,
                            status=STATE_PROCESSED,
                            message=f"source key migrated from {note_source_path}",
                        )
                replace_lab_results_for_source_path(conn, source_path, labs)
                synced_labs += len(labs)
                if labs:
                    lab_person_ids_to_refresh.add(str(labs[0]["person_id"]))
            except (OSError, sqlite3.Error, PersonSyncError, LabSyncError) as exc:
                errors.append(f"{note_path.relative_to(vault_root)}: {exc}")

        for person_id in sorted(lab_person_ids_to_refresh):
            try:
                refresh_lab_dependent_reports(conn, vault_root, person_id)
            except (OSError, sqlite3.Error, LabSyncError) as exc:
                errors.append(f"{person_id}: failed to refresh lab-dependent reports: {exc}")

        conn.commit()

    parts = [f"Synced {synced_persons} person note(s) and {synced_labs} lab note(s)."]
    if skipped_db_wins:
        parts.append(f"{skipped_db_wins} person(s) skipped (DB wins).")
    if quarantined:
        parts.append(f"{quarantined} person(s) quarantined (conflict).")
    print(" ".join(parts))

    if errors:
        print("Encountered errors:")
        for error in errors:
            print(f"- {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
