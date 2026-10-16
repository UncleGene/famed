"""Identity-first cold rebuild into an isolated vault and SQLite database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from famed.lab_sync import write_category_index_note
from famed.person_sync import (
    PERSON_RECORD_SUBDIRS,
    PersonSyncError,
    apply_relationship_closure,
    assert_unique_display_name,
    ensure_person_record_scaffold,
    load_person_from_note,
    replace_relationships,
    upsert_person,
    validate_vault_notes,
    write_person_note_idempotent,
)
from famed.setup import copy_template_tree, initialize_database

EXPECTED_TARGET_TABLES = frozenset(
    {
        "audit_events",
        "dna_imports",
        "family_links",
        "genotypes",
        "immunizations",
        "ingestion_log",
        "lab_results",
        "dedup_links",
        "medications",
        "persons",
        "sync_conflicts",
    }
)
NON_IDENTITY_TARGET_TABLES = tuple(sorted(EXPECTED_TARGET_TABLES - {"persons", "family_links"}))


class RebuildError(ValueError):
    """Raised when an isolated rebuild cannot proceed safely."""


@dataclass(frozen=True)
class IdentityRebuildResult:
    """Verified result of rebuilding the person and family-graph layer."""

    person_count: int
    family_link_count: int


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    return first == second or first in second.parents or second in first.parents


def _validate_isolated_paths(
    *,
    source_vault: Path,
    source_db: Path,
    target_vault: Path,
    target_db: Path,
) -> None:
    if not source_vault.is_dir():
        raise RebuildError(f"source vault does not exist: {source_vault}")
    if not source_db.is_file():
        raise RebuildError(f"source database does not exist: {source_db}")
    if _paths_overlap(source_vault, target_vault):
        raise RebuildError("source and target vault paths must not overlap")
    if source_db.resolve() == target_db.resolve():
        raise RebuildError("source and target database paths must differ")
    if source_vault.resolve() in target_db.resolve().parents:
        raise RebuildError("target database cannot be inside the source vault")
    if target_vault.resolve() in source_db.resolve().parents:
        raise RebuildError("source database cannot be inside the target vault")
    if target_vault.resolve() in target_db.resolve().parents:
        raise RebuildError("target database must be outside the target vault")


def _load_source_people(source_vault: Path) -> dict[str, dict[str, Any]]:
    persons_dir = source_vault / "Persons"
    note_paths = sorted(path for path in persons_dir.glob("*.md") if not path.name.startswith("_"))
    if not note_paths:
        raise RebuildError(f"source vault has no person notes: {persons_dir}")

    persons: dict[str, dict[str, Any]] = {}
    display_names: dict[str, str] = {}
    for note_path in note_paths:
        try:
            person = load_person_from_note(note_path)
        except (OSError, PersonSyncError, yaml.YAMLError) as exc:
            raise RebuildError(f"invalid person note {note_path}: {exc}") from exc
        person_id = person["person_id"]
        if note_path.stem != person_id:
            raise RebuildError(
                f"person note filename {note_path.stem!r} does not match person_id {person_id!r}"
            )
        persons[person_id] = person
        display_key = str(person["display_name"]).casefold()
        if display_key in display_names:
            raise RebuildError(
                "display_name must be unique across source people "
                f"(conflicts: {display_names[display_key]!r}, {person_id!r})"
            )
        display_names[display_key] = person_id

    apply_relationship_closure(persons)
    known_ids = set(persons)
    dangling = sorted(
        (person_id, relationship["relationship_type"], relationship["target_person_id"])
        for person_id, person in persons.items()
        for relationship in person["relationships"]
        if relationship["target_person_id"] not in known_ids
    )
    if dangling:
        details = ", ".join(f"{source} {kind} -> {target}" for source, kind, target in dangling)
        raise RebuildError(f"family graph references unknown people: {details}")
    return persons


def _reject_unexpected_target_people(target_vault: Path, source_person_ids: set[str]) -> None:
    target_persons_dir = target_vault / "Persons"
    if not target_persons_dir.exists():
        return
    target_ids = {
        path.stem for path in target_persons_dir.glob("*.md") if not path.name.startswith("_")
    }
    unexpected = sorted(target_ids - source_person_ids)
    if unexpected:
        raise RebuildError(
            "target vault contains person notes outside the rebuild source: "
            + ", ".join(unexpected)
        )


def _reject_non_identity_target_vault_content(
    target_vault: Path,
    template_root: Path,
    source_person_ids: set[str],
) -> None:
    if not target_vault.exists():
        return
    allowed = _identity_vault_paths(template_root, source_person_ids)
    unexpected = sorted(
        path.relative_to(target_vault)
        for path in target_vault.rglob("*")
        if path.is_file() and path.relative_to(target_vault) not in allowed
    )
    if unexpected:
        raise RebuildError(
            "target vault contains non-identity artifacts: "
            + ", ".join(str(path) for path in unexpected)
        )


def _identity_vault_paths(template_root: Path, person_ids: set[str]) -> set[Path]:
    allowed = {
        path.relative_to(template_root) for path in template_root.rglob("*") if path.is_file()
    }
    allowed.update(Path("Persons") / f"{person_id}.md" for person_id in person_ids)
    allowed.update(
        Path("Records") / person_id / category / "_index.md"
        for person_id in person_ids
        for category in PERSON_RECORD_SUBDIRS
    )
    return allowed


def _reject_unexpected_target_db_people(target_db: Path, source_person_ids: set[str]) -> None:
    if not target_db.exists():
        return
    try:
        with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'persons'"
            ).fetchone()
            if table is None:
                return
            target_ids = {str(row[0]) for row in conn.execute("SELECT id FROM persons")}
    except sqlite3.Error as exc:
        raise RebuildError(f"cannot inspect existing target database {target_db}: {exc}") from exc
    unexpected = sorted(target_ids - source_person_ids)
    if unexpected:
        raise RebuildError(
            "target database contains people outside the rebuild source: " + ", ".join(unexpected)
        )


def _reject_non_identity_target_db_rows(target_db: Path) -> None:
    if not target_db.exists():
        return
    try:
        with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
            table_names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if table_names != EXPECTED_TARGET_TABLES:
                missing = sorted(EXPECTED_TARGET_TABLES - table_names)
                unexpected = sorted(table_names - EXPECTED_TARGET_TABLES)
                details = []
                if missing:
                    details.append("missing: " + ", ".join(missing))
                if unexpected:
                    details.append("unexpected: " + ", ".join(unexpected))
                raise RebuildError(
                    "target database schema is not recognized (" + "; ".join(details) + ")"
                )
            populated = [
                str(row[0])
                for row in conn.execute(
                    "SELECT 'audit_events' WHERE EXISTS (SELECT 1 FROM audit_events) "
                    "UNION ALL SELECT 'dna_imports' WHERE EXISTS (SELECT 1 FROM dna_imports) "
                    "UNION ALL SELECT 'genotypes' WHERE EXISTS (SELECT 1 FROM genotypes) "
                    "UNION ALL SELECT 'immunizations' WHERE EXISTS (SELECT 1 FROM immunizations) "
                    "UNION ALL SELECT 'ingestion_log' WHERE EXISTS (SELECT 1 FROM ingestion_log) "
                    "UNION ALL SELECT 'lab_results' WHERE EXISTS (SELECT 1 FROM lab_results) "
                    "UNION ALL SELECT 'medications' WHERE EXISTS (SELECT 1 FROM medications) "
                    "UNION ALL SELECT 'dedup_links' "
                    "WHERE EXISTS (SELECT 1 FROM dedup_links) "
                    "UNION ALL SELECT 'sync_conflicts' WHERE EXISTS (SELECT 1 FROM sync_conflicts)"
                )
            ]
    except sqlite3.Error as exc:
        raise RebuildError(f"cannot inspect existing target database {target_db}: {exc}") from exc
    if populated:
        raise RebuildError("target database contains non-identity rows in: " + ", ".join(populated))


def _relationship_keys(persons: dict[str, dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (person_id, relationship["relationship_type"], relationship["target_person_id"])
        for person_id, person in persons.items()
        for relationship in person["relationships"]
    }


def validate_existing_identity_layer(
    *,
    source_vault: Path,
    source_db: Path,
    target_vault: Path,
    target_db: Path,
) -> IdentityRebuildResult:
    """Validate an existing target identity layer before resuming medical replay."""
    _validate_isolated_paths(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
    )
    persons = _load_source_people(source_vault)
    source_ids = set(persons)
    target_ids = {
        path.stem
        for path in (target_vault / "Persons").glob("*.md")
        if not path.name.startswith("_")
    }
    if target_ids != source_ids:
        raise RebuildError("existing target person notes do not match the rebuild source")
    expected_relationships = _relationship_keys(persons)
    try:
        with sqlite3.connect(f"file:{target_db}?mode=ro", uri=True) as conn:
            actual_ids = {str(row[0]) for row in conn.execute("SELECT id FROM persons")}
            actual_relationships = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    "SELECT source_person_id, relationship_type, target_person_id FROM family_links"
                )
            }
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise RebuildError(f"cannot validate existing target identity: {exc}") from exc
    if actual_ids != source_ids or actual_relationships != expected_relationships:
        raise RebuildError("existing target database identity does not match the rebuild source")
    if violations:
        raise RebuildError(f"target foreign-key validation failed: {violations}")
    return IdentityRebuildResult(len(actual_ids), len(actual_relationships))


def reset_existing_target_to_identity(
    *,
    target_vault: Path,
    target_db: Path,
    template_root: Path,
    person_ids: set[str],
) -> None:
    """Remove derived medical state after identity validation for a clean replay."""
    allowed = _identity_vault_paths(template_root, person_ids)
    for path in target_vault.rglob("*"):
        if path.is_file() and path.relative_to(target_vault) not in allowed:
            path.unlink()
    copy_template_tree(template_root, target_vault, overwrite=True)
    with sqlite3.connect(target_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for table in NON_IDENTITY_TARGET_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def rebuild_identity_layer(
    *,
    source_vault: Path,
    source_db: Path,
    target_vault: Path,
    target_db: Path,
    template_root: Path,
) -> IdentityRebuildResult:
    """Rebuild person notes, category hubs, and reciprocal family links.

    The complete source family graph is parsed and validated before the target
    vault or database is created. Source paths are read-only inputs.
    """
    source_vault = source_vault.resolve()
    source_db = source_db.resolve()
    target_vault = target_vault.resolve()
    target_db = target_db.resolve()
    template_root = template_root.resolve()

    _validate_isolated_paths(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
    )
    if not template_root.is_dir():
        raise RebuildError(f"vault template does not exist: {template_root}")

    persons = _load_source_people(source_vault)
    source_person_ids = set(persons)
    _reject_unexpected_target_people(target_vault, source_person_ids)
    _reject_non_identity_target_vault_content(target_vault, template_root, source_person_ids)
    _reject_unexpected_target_db_people(target_db, source_person_ids)
    _reject_non_identity_target_db_rows(target_db)

    copy_template_tree(template_root, target_vault)
    initialize_database(target_db)

    target_persons_dir = target_vault / "Persons"
    target_persons_dir.mkdir(parents=True, exist_ok=True)
    for person_id, person in sorted(persons.items()):
        source_note = source_vault / "Persons" / f"{person_id}.md"
        target_note = target_persons_dir / f"{person_id}.md"
        target_note.write_text(source_note.read_text(encoding="utf-8"), encoding="utf-8")
        write_person_note_idempotent(target_note, person)
        ensure_person_record_scaffold(target_vault, person_id)
        for category in PERSON_RECORD_SUBDIRS:
            write_category_index_note(
                target_vault / "Records" / person_id / category,
                person_id,
                category,
                note_label="lab" if category == "Labs" else None,
                display_name=person["display_name"],
            )

    vault_issues = validate_vault_notes(target_vault)
    if vault_issues:
        details = "; ".join(f"{issue.path}: {issue.message}" for issue in vault_issues)
        raise RebuildError(f"target vault validation failed: {details}")

    expected_relationships = _relationship_keys(persons)
    with sqlite3.connect(target_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for person in persons.values():
            assert_unique_display_name(conn, person)
            upsert_person(conn, person)
        for person in persons.values():
            replace_relationships(conn, person)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RebuildError(f"target foreign-key validation failed: {violations}")
        person_count = int(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0])
        family_link_count = int(conn.execute("SELECT COUNT(*) FROM family_links").fetchone()[0])
        actual_relationships = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                "SELECT source_person_id, relationship_type, target_person_id FROM family_links"
            )
        }
        if person_count != len(persons):
            raise RebuildError(
                f"target person count mismatch: expected {len(persons)}, found {person_count}"
            )
        if actual_relationships != expected_relationships:
            raise RebuildError(
                "target family graph mismatch: "
                f"expected {len(expected_relationships)} links, found {len(actual_relationships)}"
            )
        conn.commit()

    return IdentityRebuildResult(
        person_count=person_count,
        family_link_count=family_link_count,
    )
