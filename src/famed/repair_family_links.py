#!/usr/bin/env python3
"""Repair family links in vault notes and SQLite from current relationship rules."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from famed.person_sync import (
    PersonSyncError,
    apply_relationship_closure,
    check_relationship_age_plausibility,
    load_person_from_note,
    sync_person_to_db,
    write_person_note_idempotent,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill inferred family links (including sibling-derived parents) and resync DB"
        )
    )
    parser.add_argument("--vault-path", default="famed-vault", help="Path to Obsidian vault")
    parser.add_argument("--db-path", default="data/famed.db", help="Path to SQLite DB")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    args = parse_args()
    root = repo_root()
    vault_root = (root / args.vault_path).resolve()
    db_path = (root / args.db_path).resolve()

    persons_dir = vault_root / "Persons"
    note_paths = sorted(path for path in persons_dir.glob("*.md") if not path.name.startswith("_"))
    if not note_paths:
        print(f"No person notes found in: {persons_dir}")
        return 0

    persons_by_id: dict[str, dict[str, Any]] = {}
    note_by_id: dict[str, Path] = {}

    try:
        for note_path in note_paths:
            person = load_person_from_note(note_path)
            persons_by_id[person["person_id"]] = person
            note_by_id[person["person_id"]] = note_path
    except (OSError, PersonSyncError) as exc:
        print(f"Failed to load person notes: {exc}")
        return 1

    changed_ids = apply_relationship_closure(persons_by_id)

    for warning in check_relationship_age_plausibility(persons_by_id):
        print(f"[relationship warning] {warning}")

    try:
        for person_id in sorted(changed_ids):
            write_person_note_idempotent(note_by_id[person_id], persons_by_id[person_id])
    except OSError as exc:
        print(f"Failed to write updated notes: {exc}")
        return 1

    try:
        with sqlite3.connect(db_path) as conn:
            for person in persons_by_id.values():
                sync_person_to_db(conn, person)
            conn.commit()
    except (sqlite3.Error, PersonSyncError) as exc:
        print(f"Failed to sync repaired relationships to DB: {exc}")
        return 1

    print(f"Repaired family links for {len(changed_ids)} person note(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
