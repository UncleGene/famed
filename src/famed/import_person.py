#!/usr/bin/env python3
"""Import person profiles from inbox YAML files into vault notes and SQLite."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from famed.lab_sync import write_category_index_note
from famed.person_sync import (
    PERSON_RECORD_SUBDIRS,
    PersonSyncError,
    apply_relationship_closure,
    check_relationship_age_plausibility,
    ensure_person_inbox_scaffold,
    ensure_person_record_scaffold,
    load_person_from_note,
    load_person_from_yaml,
    merge_relationships,
    sync_person_to_db,
    write_person_note_idempotent,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import person profiles from data/inbox/persons into famed-vault and data/famed.db"
        )
    )
    parser.add_argument(
        "--inbox-dir",
        default="data/inbox/persons",
        help="Folder containing person YAML files (default: data/inbox/persons)",
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
        "--person-file",
        help="Single person YAML file path to import; defaults to all *.yaml/*.yml in inbox.",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def collect_yaml_files(inbox_dir: Path, single_file: str | None) -> list[Path]:
    if single_file:
        file_path = Path(single_file)
        if not file_path.is_absolute():
            file_path = (repo_root() / file_path).resolve()
        return [file_path]

    return sorted(inbox_dir.glob("*.yaml")) + sorted(inbox_dir.glob("*.yml"))


def load_known_person_notes(vault_root: Path) -> dict[str, dict[str, Any]]:
    persons_dir = vault_root / "Persons"
    persons_by_id: dict[str, dict[str, Any]] = {}
    for note_path in sorted(
        path for path in persons_dir.glob("*.md") if not path.name.startswith("_")
    ):
        person = load_person_from_note(note_path)
        persons_by_id[person["person_id"]] = person
    return persons_by_id


def main() -> int:
    args = parse_args()
    root = repo_root()

    inbox_dir = (root / args.inbox_dir).resolve()
    vault_root = (root / args.vault_path).resolve()
    db_path = (root / args.db_path).resolve()
    inbox_root = (root / "data/inbox").resolve()

    inbox_dir.mkdir(parents=True, exist_ok=True)
    (vault_root / "Persons").mkdir(parents=True, exist_ok=True)

    yaml_files = collect_yaml_files(inbox_dir, args.person_file)
    if not yaml_files:
        print(f"No person YAML files found in: {inbox_dir}")
        return 0

    imported_person_ids: set[str] = set()
    errors: list[str] = []

    with sqlite3.connect(db_path) as conn:
        persons_by_id = load_known_person_notes(vault_root)

        for yaml_file in yaml_files:
            try:
                person = load_person_from_yaml(yaml_file)
                existing_person = persons_by_id.get(person["person_id"])
                if existing_person is not None:
                    person["relationships"] = merge_relationships(
                        existing_person["relationships"], person["relationships"]
                    )
                persons_by_id[person["person_id"]] = person
                imported_person_ids.add(person["person_id"])
                ensure_person_record_scaffold(vault_root, person["person_id"])
                ensure_person_inbox_scaffold(inbox_root, person["person_id"])

                note_path = vault_root / "Persons" / f"{person['person_id']}.md"
                write_person_note_idempotent(note_path, person)

                # Ensure every category hub index exists and is current.
                person_records_dir = vault_root / "Records" / person["person_id"]
                for subdir in PERSON_RECORD_SUBDIRS:
                    category_dir = person_records_dir / subdir
                    write_category_index_note(
                        category_dir,
                        person["person_id"],
                        subdir,
                        display_name=person["display_name"],
                    )
            except (OSError, sqlite3.Error, PersonSyncError) as exc:
                errors.append(f"{yaml_file.name}: {exc}")

        changed_person_ids = apply_relationship_closure(persons_by_id)
        person_ids_to_sync = imported_person_ids | changed_person_ids
        for person_id in sorted(person_ids_to_sync):
            try:
                person = persons_by_id[person_id]
                note_path = vault_root / "Persons" / f"{person_id}.md"
                write_person_note_idempotent(note_path, person)
                sync_person_to_db(conn, person)
            except (OSError, sqlite3.Error, PersonSyncError) as exc:
                errors.append(f"{person_id}: {exc}")

        conn.commit()

    for warning in check_relationship_age_plausibility(persons_by_id):
        print(f"[relationship warning] {warning}")

    print(f"Imported {len(imported_person_ids)} person file(s).")
    if errors:
        print("Encountered errors:")
        for error in errors:
            print(f"- {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
