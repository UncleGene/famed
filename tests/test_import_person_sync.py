from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from famed import import_person
from famed.setup import SCHEMA_SQL


def test_import_person_merges_existing_note_relationships_for_same_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    persons_dir.mkdir(parents=True)
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    (persons_dir / "dummy_alice.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: person\n"
        "display_name: Dummy Alice\n"
        "birth_year: 1990\n"
        "sex: female\n"
        "relationships:\n"
        "  - relationship_type: spouse\n"
        "    target_person_id: dummy_bob\n"
        "---\n\n"
        "# Dummy Alice\n\n"
        "## Notes\n\n"
        "- Existing note.\n",
        encoding="utf-8",
    )

    inbox_dir = data_dir / "inbox" / "persons"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "dummy_alice.yaml").write_text(
        yaml.safe_dump(
            {
                "person_id": "dummy_alice",
                "display_name": "Dummy Alice",
                "birth_year": 1990,
                "sex": "female",
                "relationships": ["parent:dummy_carol"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(import_person, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_person"])

    assert import_person.main() == 0

    note_content = (persons_dir / "dummy_alice.md").read_text(encoding="utf-8")
    assert "target_person_id: dummy_bob" in note_content
    assert "target_person_id: dummy_carol" in note_content
    assert "Existing note." in note_content

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source_person_id, relationship_type, target_person_id "
            "FROM family_links ORDER BY relationship_type, target_person_id"
        ).fetchall()

    assert rows == [
        ("dummy_alice", "parent", "dummy_carol"),
        ("dummy_alice", "spouse", "dummy_bob"),
    ]
