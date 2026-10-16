#!/usr/bin/env python3
"""Add a family member directly from CLI inputs (no manual YAML required)."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from famed.person_sync import (
    PersonSyncError,
    ensure_person_inbox_scaffold,
    ensure_person_record_scaffold,
    infer_parent_relationships_from_siblings,
    load_person_from_note,
    merge_relationships,
    normalize_person_id,
    person_payload_from_mapping,
    reciprocal_relationship,
    sync_person_to_db,
    write_person_note_idempotent,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/update a family member note and sync it to data/famed.db"
    )
    parser.add_argument("--display-name", help="Human-readable name, e.g. 'Demo Person'.")
    parser.add_argument(
        "--person-id",
        help="Optional stable person id. Defaults to normalized display name.",
    )
    parser.add_argument("--birth-year", type=int, help="Birth year (required for new person).")
    parser.add_argument(
        "--sex-at-birth",
        "--sex",
        dest="sex",
        default=None,
        help="Sex at birth (required for new person).",
    )
    parser.add_argument(
        "--relationship",
        action="append",
        default=[],
        help=(
            "Repeatable directed relationship in '<relationship_type>:<target_person_id>' form, "
            "from this person to the target person."
        ),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for fields interactively.",
    )
    parser.add_argument(
        "--replace-relationships",
        action="store_true",
        help="Replace existing relationships on this person instead of merging.",
    )
    parser.add_argument(
        "--no-reciprocal",
        action="store_true",
        help="Disable automatic reciprocal relationship updates on related person notes.",
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
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def prompt_if_empty(value: str | None, label: str) -> str:
    if value and value.strip():
        return value.strip()
    return input(f"{label}: ").strip()


def prompt_optional(value: str | None, label: str) -> str | None:
    if value is not None:
        return value.strip()
    typed = input(f"{label}: ").strip()
    if not typed:
        return None
    return typed


def gather_inputs(args: argparse.Namespace) -> dict[str, Any]:
    display_name = args.display_name
    person_id = args.person_id
    birth_year = args.birth_year
    sex = args.sex
    relationships = list(args.relationship)

    if args.interactive:
        display_name = prompt_if_empty(display_name, "Display name")
        person_id = prompt_if_empty(person_id, "Person id (optional, press Enter to auto-generate)")

        if birth_year is None:
            birth_year_raw = input(
                "Birth year (required for new person; Enter to keep existing): "
            ).strip()
            if birth_year_raw:
                birth_year = int(birth_year_raw)

        sex = prompt_optional(sex, "Sex at birth (required for new person; Enter to keep existing)")
        if not relationships:
            rel_help = (
                "Relationships (optional, comma-separated as 'type:target_id', "
                "direction from this person to target)"
            )
            relationship_line = input(f"{rel_help}: ").strip()
            if relationship_line:
                relationships = [
                    item.strip() for item in relationship_line.split(",") if item.strip()
                ]

    if not display_name:
        raise PersonSyncError("display_name is required (pass --display-name or use --interactive)")

    if person_id == "":
        person_id = None

    return {
        "person_id": person_id or display_name,
        "display_name": display_name,
        "birth_year": birth_year,
        "sex": sex,
        "relationships": relationships,
    }


def main() -> int:
    args = parse_args()
    root = repo_root()

    vault_root = (root / args.vault_path).resolve()
    db_path = (root / args.db_path).resolve()
    inbox_root = (root / "data/inbox").resolve()

    try:
        raw_person = gather_inputs(args)
        candidate_person_id = normalize_person_id(raw_person["person_id"])
    except (PersonSyncError, ValueError) as exc:
        print(f"Invalid input: {exc}")
        return 1

    note_path = vault_root / "Persons" / f"{candidate_person_id}.md"
    if note_path.exists():
        try:
            existing_person = load_person_from_note(note_path)
        except (OSError, PersonSyncError) as exc:
            print(f"Failed to read existing person note: {exc}")
            return 1

        if raw_person["birth_year"] is None:
            raw_person["birth_year"] = existing_person["birth_year"]
        if raw_person["sex"] is None:
            raw_person["sex"] = existing_person["sex"]
        if not args.replace_relationships:
            raw_person["relationships"] = merge_relationships(
                existing_person["relationships"], raw_person["relationships"]
            )
    elif raw_person["birth_year"] is None:
        print("Invalid input: birth_year is required for new person")
        return 1

    if not note_path.exists() and raw_person["sex"] is None:
        print("Invalid input: sex is required for new person")
        return 1

    try:
        person = person_payload_from_mapping(raw_person, "CLI")
    except (PersonSyncError, ValueError) as exc:
        print(f"Invalid input: {exc}")
        return 1

    def load_related_person(person_id: str) -> dict[str, Any] | None:
        related_note = vault_root / "Persons" / f"{person_id}.md"
        if not related_note.exists():
            return None
        try:
            result: dict[str, Any] = load_person_from_note(related_note)
            return result
        except (OSError, PersonSyncError):
            return None

    inferred_parent_links = infer_parent_relationships_from_siblings(
        person,
        load_related_person,
    )
    if inferred_parent_links:
        person["relationships"] = merge_relationships(
            person["relationships"], inferred_parent_links
        )

    try:
        (vault_root / "Persons").mkdir(parents=True, exist_ok=True)
        ensure_person_record_scaffold(vault_root, person["person_id"])
        ensure_person_inbox_scaffold(inbox_root, person["person_id"])
        write_person_note_idempotent(note_path, person)

        with sqlite3.connect(db_path) as conn:
            sync_person_to_db(conn, person)

            if not args.no_reciprocal:
                for relationship in person["relationships"]:
                    reciprocal = reciprocal_relationship(relationship, person["person_id"])
                    if reciprocal is None:
                        continue

                    target_id = relationship["target_person_id"]
                    target_note_path = vault_root / "Persons" / f"{target_id}.md"
                    if not target_note_path.exists():
                        continue

                    target_person = load_person_from_note(target_note_path)
                    target_person["relationships"] = merge_relationships(
                        target_person["relationships"], [reciprocal]
                    )
                    ensure_person_record_scaffold(vault_root, target_person["person_id"])
                    ensure_person_inbox_scaffold(inbox_root, target_person["person_id"])
                    write_person_note_idempotent(target_note_path, target_person)
                    sync_person_to_db(conn, target_person)

            conn.commit()
    except (OSError, sqlite3.Error, PersonSyncError) as exc:
        print(f"Failed to add family member: {exc}")
        return 1

    print("Family member added successfully")
    print(f"person_id: {person['person_id']}")
    print(f"note: {note_path}")
    if not args.no_reciprocal:
        print("reciprocal links: auto-updated where related person notes already existed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
