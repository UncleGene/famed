from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from famed import add_family_member, import_person
from famed.person_sync import (
    PersonSyncError,
    apply_relationship_closure,
    check_relationship_age_plausibility,
    detect_person_sync_authority,
    ensure_person_inbox_scaffold,
    infer_parent_relationships_from_siblings,
    load_person_from_note,
    load_person_from_yaml,
    log_sync_conflict,
    merge_relationships,
    person_payload_from_mapping,
    reciprocal_relationship,
    render_person_note,
    sync_person_to_db,
    upsert_person,
    validate_note_front_matter,
    validate_vault_notes,
    write_person_note_idempotent,
)
from famed.setup import SCHEMA_SQL
from famed.storage_contract import RESOLUTION_QUARANTINED, RESOLUTION_VAULT_WINS


def test_load_person_from_yaml_normalizes_id_and_relationships(tmp_path: Path) -> None:
    yaml_path = tmp_path / "dummy_alice.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "person_id": "Test Person",
                "display_name": "Test Person",
                "birth_year": "1986",
                "sex": "Female",
                "relationships": [
                    {
                        "target_person_id": "Related Person",
                        "relationship_type": "spouse",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    person = load_person_from_yaml(yaml_path)

    assert person["person_id"] == "test-person"
    assert person["birth_year"] == 1986
    assert person["sex"] == "female"
    assert person["relationships"] == [
        {
            "target_person_id": "related-person",
            "relationship_type": "spouse",
        }
    ]


def test_load_person_from_note_uses_front_matter(tmp_path: Path) -> None:
    note_path = tmp_path / "dummy_alice.md"
    note_path.write_text(
        """---
person_id: dummy_alice
display_name: Dummy Alice
birth_year: 1990
sex: female
relationships:
  - relationship_type: spouse
    target_person_id: dummy_bob
---

# Dummy Alice
""",
        encoding="utf-8",
    )

    person = load_person_from_note(note_path)

    assert person["person_id"] == "dummy_alice"
    assert person["display_name"] == "Dummy Alice"
    assert person["relationships"] == [
        {
            "relationship_type": "spouse",
            "target_person_id": "dummy_bob",
        }
    ]


def test_sync_person_to_db_upserts_person_and_relationships() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    person: dict[str, Any] = {
        "person_id": "dummy_alice",
        "display_name": "Dummy Alice",
        "birth_year": 1990,
        "sex": "female",
        "relationships": [
            {
                "relationship_type": "spouse",
                "target_person_id": "dummy_bob",
            }
        ],
    }

    sync_person_to_db(conn, person)

    row = conn.execute(
        "SELECT id, display_name, birth_year, sex FROM persons WHERE id = ?",
        ("dummy_alice",),
    ).fetchone()
    assert row == ("dummy_alice", "Dummy Alice", 1990, "female")

    link = conn.execute(
        "SELECT source_person_id, target_person_id, relationship_type "
        "FROM family_links WHERE source_person_id = ?",
        ("dummy_alice",),
    ).fetchone()
    assert link == ("dummy_alice", "dummy_bob", "spouse")


def test_person_payload_allows_string_relationship_entries() -> None:
    payload = person_payload_from_mapping(
        {
            "person_id": "Test Person",
            "display_name": "Test Person",
            "birth_year": 1986,
            "sex": "female",
            "relationships": ["parent: Related Person", "child:Dummy Charlie"],
        },
        "test",
    )

    assert payload["person_id"] == "test-person"
    assert payload["relationships"] == [
        {
            "relationship_type": "parent",
            "target_person_id": "related-person",
        },
        {
            "relationship_type": "child",
            "target_person_id": "dummy-charlie",
        },
    ]


def test_person_payload_normalizes_relationship_aliases() -> None:
    payload = person_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "display_name": "Dummy Alice",
            "birth_year": 1990,
            "sex": "female",
            "relationships": ["mother:dummy_bob", "daughter:dummy_charlie", "partner:dana"],
        },
        "test",
    )

    assert payload["relationships"] == [
        {"relationship_type": "parent", "target_person_id": "dummy_bob"},
        {"relationship_type": "child", "target_person_id": "dummy_charlie"},
        {"relationship_type": "spouse", "target_person_id": "dana"},
    ]


def test_person_payload_rejects_unknown_relationship_type() -> None:
    try:
        person_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "display_name": "Dummy Alice",
                "birth_year": 1990,
                "sex": "female",
                "relationships": ["cousin:dummy_bob"],
            },
            "test",
        )
    except PersonSyncError as exc:
        assert "unsupported relationship_type" in str(exc)
    else:
        raise AssertionError("Expected PersonSyncError for unsupported relationship_type")


def test_merge_relationships_deduplicates_items() -> None:
    existing = [
        {"relationship_type": "spouse", "target_person_id": "alex"},
        {"relationship_type": "child", "target_person_id": "casey"},
    ]
    incoming = [
        {"relationship_type": "child", "target_person_id": "casey"},
        {"relationship_type": "child", "target_person_id": "jordan"},
    ]

    merged = merge_relationships(existing, incoming)
    assert merged == [
        {"relationship_type": "spouse", "target_person_id": "alex"},
        {"relationship_type": "child", "target_person_id": "casey"},
        {"relationship_type": "child", "target_person_id": "jordan"},
    ]


def test_reciprocal_relationship_for_parent_child_and_spouse() -> None:
    assert reciprocal_relationship(
        {"relationship_type": "parent", "target_person_id": "sam"}, "casey"
    ) == {
        "relationship_type": "child",
        "target_person_id": "casey",
    }
    assert reciprocal_relationship(
        {"relationship_type": "child", "target_person_id": "casey"}, "sam"
    ) == {
        "relationship_type": "parent",
        "target_person_id": "sam",
    }
    assert reciprocal_relationship(
        {"relationship_type": "spouse", "target_person_id": "alex"}, "sam"
    ) == {
        "relationship_type": "spouse",
        "target_person_id": "sam",
    }


def test_person_payload_requires_birth_year() -> None:
    try:
        person_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "display_name": "Dummy Alice",
                "relationships": [],
            },
            "test",
        )
    except PersonSyncError as exc:
        assert "birth_year is required" in str(exc)
    else:
        raise AssertionError("Expected PersonSyncError for missing birth_year")


def test_person_payload_requires_sex() -> None:
    try:
        person_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "display_name": "Dummy Alice",
                "birth_year": 1990,
                "relationships": [],
            },
            "test",
        )
    except PersonSyncError as exc:
        assert "sex is required" in str(exc)
    else:
        raise AssertionError("Expected PersonSyncError for missing sex")


def test_person_payload_accepts_sex_field() -> None:
    payload = person_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "display_name": "Dummy Alice",
            "birth_year": 1990,
            "sex": "female",
            "relationships": [],
        },
        "test",
    )

    assert payload["sex"] == "female"


def test_person_payload_backfills_from_legacy_sex_at_birth() -> None:
    """Notes written before the sex/sex_at_birth consolidation are still read correctly."""
    payload = person_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "display_name": "Dummy Alice",
            "birth_year": 1990,
            "sex_at_birth": "female",
            "relationships": [],
        },
        "test",
    )

    assert payload["sex"] == "female"


def test_sync_person_to_db_rejects_duplicate_display_name() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    sync_person_to_db(
        conn,
        {
            "person_id": "sam",
            "display_name": "Sam Doe",
            "birth_year": 1988,
            "sex": "",
            "relationships": [],
        },
    )

    try:
        sync_person_to_db(
            conn,
            {
                "person_id": "sam-duplicate",
                "display_name": "Sam Doe",
                "birth_year": 1990,
                "sex": "",
                "relationships": [],
            },
        )
    except PersonSyncError as exc:
        assert "display_name must be unique" in str(exc)
    else:
        raise AssertionError("Expected PersonSyncError for duplicate display_name")


def test_infer_parent_relationships_from_sibling() -> None:
    person: dict[str, Any] = {
        "person_id": "dummy_carol",
        "display_name": "Dummy Carol",
        "birth_year": 2001,
        "sex": "f",
        "relationships": [{"relationship_type": "sibling", "target_person_id": "dummy_bob"}],
    }
    sibling: dict[str, Any] = {
        "person_id": "dummy_bob",
        "display_name": "Dummy Bob",
        "birth_year": 2006,
        "sex": "m",
        "relationships": [
            {"relationship_type": "parent", "target_person_id": "dummy_alice"},
            {"relationship_type": "parent", "target_person_id": "diana"},
            {"relationship_type": "sibling", "target_person_id": "dummy_carol"},
        ],
    }

    inferred = infer_parent_relationships_from_siblings(
        person, lambda pid: sibling if pid == "dummy_bob" else None
    )
    assert inferred == [
        {"relationship_type": "parent", "target_person_id": "dummy_alice"},
        {"relationship_type": "parent", "target_person_id": "diana"},
    ]


def test_apply_relationship_closure_adds_known_reciprocals_and_inferred_parents() -> None:
    persons_by_id: dict[str, dict[str, Any]] = {
        "dummy_alice": {
            "person_id": "dummy_alice",
            "display_name": "Dummy Alice",
            "birth_year": 1980,
            "sex": "female",
            "relationships": [],
        },
        "dummy_bob": {
            "person_id": "dummy_bob",
            "display_name": "Dummy Bob",
            "birth_year": 2010,
            "sex": "male",
            "relationships": [
                {"relationship_type": "parent", "target_person_id": "dummy_alice"},
                {"relationship_type": "sibling", "target_person_id": "dummy_carol"},
            ],
        },
        "dummy_carol": {
            "person_id": "dummy_carol",
            "display_name": "Dummy Carol",
            "birth_year": 2012,
            "sex": "female",
            "relationships": [],
        },
    }

    changed = apply_relationship_closure(persons_by_id)

    assert changed == {"dummy_alice", "dummy_carol"}
    assert {"relationship_type": "child", "target_person_id": "dummy_bob"} in persons_by_id[
        "dummy_alice"
    ]["relationships"]
    assert {"relationship_type": "sibling", "target_person_id": "dummy_bob"} in persons_by_id[
        "dummy_carol"
    ]["relationships"]
    assert {"relationship_type": "parent", "target_person_id": "dummy_alice"} in persons_by_id[
        "dummy_carol"
    ]["relationships"]


def test_ensure_person_inbox_scaffold_creates_person_root_only(tmp_path: Path) -> None:
    inbox_root = tmp_path / "data" / "inbox"

    ensure_person_inbox_scaffold(inbox_root, "dummy_alice")

    assert (inbox_root / "dummy_alice").is_dir()
    assert not (inbox_root / "dummy_alice" / "labs").exists()


def test_import_person_main_creates_reports_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_dir = data_dir / "inbox" / "persons"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "dummy_alice.yaml").write_text(
        yaml.safe_dump(
            {
                "person_id": "dummy_alice",
                "display_name": "Dummy Alice",
                "birth_year": 1990,
                "sex": "female",
            },
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(import_person, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_person"])

    assert import_person.main() == 0

    reports_index = vault_root / "Records" / "dummy_alice" / "Reports" / "_index.md"
    assert reports_index.exists()
    assert "record_type: reports_index" in reports_index.read_text(encoding="utf-8")


def test_import_person_main_closes_reciprocal_relationships_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox_dir = data_dir / "inbox" / "persons"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "dummy_alice.yaml").write_text(
        yaml.safe_dump(
            {
                "person_id": "dummy_alice",
                "display_name": "Dummy Alice",
                "birth_year": 1980,
                "sex": "female",
                "relationships": ["parent:dummy_bob"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (inbox_dir / "dummy_bob.yaml").write_text(
        yaml.safe_dump(
            {
                "person_id": "dummy_bob",
                "display_name": "Dummy Bob",
                "birth_year": 2010,
                "sex": "male",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(import_person, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_person"])
    assert import_person.main() == 0
    monkeypatch.setattr(sys, "argv", ["import_person"])
    assert import_person.main() == 0

    dummy_bob_note = vault_root / "Persons" / "dummy_bob.md"
    dummy_bob_content = dummy_bob_note.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source_person_id, relationship_type, target_person_id "
            "FROM family_links ORDER BY source_person_id, relationship_type, target_person_id"
        ).fetchall()

    assert "relationship_type: child" in dummy_bob_content
    assert "target_person_id: dummy_alice" in dummy_bob_content
    assert rows == [
        ("dummy_alice", "parent", "dummy_bob"),
        ("dummy_bob", "child", "dummy_alice"),
    ]


def test_add_family_member_preserves_target_notes_when_adding_reciprocal(
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

    dummy_bob_note = persons_dir / "dummy_bob.md"
    dummy_bob_note.write_text(
        render_person_note({**_make_person("dummy_bob"), "display_name": "Dummy Bob"}),
        encoding="utf-8",
    )
    dummy_bob_note.write_text(
        dummy_bob_note.read_text(encoding="utf-8") + "\n- Keep this target note.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(add_family_member, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "add_family_member",
            "--person-id",
            "dummy_alice",
            "--display-name",
            "Dummy Alice",
            "--birth-year",
            "1980",
            "--sex",
            "female",
            "--relationship",
            "parent:dummy_bob",
        ],
    )

    assert add_family_member.main() == 0

    dummy_bob_content = dummy_bob_note.read_text(encoding="utf-8")
    assert "Keep this target note." in dummy_bob_content
    assert "relationship_type: child" in dummy_bob_content
    assert "target_person_id: dummy_alice" in dummy_bob_content


# ---------------------------------------------------------------------------
# Milestone 1.1 — authority chain and divergence detection
# ---------------------------------------------------------------------------


def _make_person(person_id: str = "dummy_alice") -> dict[str, Any]:
    return {
        "person_id": person_id,
        "display_name": "Dummy Alice",
        "birth_year": 1990,
        "sex": "female",
        "relationships": [],
    }


def test_schema_has_vault_synced_at_in_persons() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(persons)")}
    assert "vault_synced_at" in columns


def test_schema_has_sync_conflicts_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sync_conflicts" in tables


def test_upsert_person_sets_vault_synced_at() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    upsert_person(conn, _make_person())

    row = conn.execute(
        "SELECT vault_synced_at, updated_at FROM persons WHERE id = 'dummy_alice'"
    ).fetchone()
    vault_synced_at, _ = row
    assert vault_synced_at is not None, "vault_synced_at should be set after upsert"


def test_upsert_person_does_not_change_updated_at_on_re_sync() -> None:
    """A vault re-sync must not advance updated_at so divergence checks remain meaningful."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    # First sync — establishes the record.
    upsert_person(conn, _make_person())
    first_updated_at = conn.execute(
        "SELECT updated_at FROM persons WHERE id = 'dummy_alice'"
    ).fetchone()[0]

    # Simulate a small time gap by advancing vault_synced_at via a raw update.
    conn.execute(
        "UPDATE persons SET vault_synced_at = datetime('now', '+1 second') WHERE id = 'dummy_alice'"
    )

    # Second vault sync — should not change updated_at.
    upsert_person(conn, _make_person())
    second_updated_at = conn.execute(
        "SELECT updated_at FROM persons WHERE id = 'dummy_alice'"
    ).fetchone()[0]

    assert first_updated_at == second_updated_at, (
        "updated_at must not change during a vault sync; "
        f"was {first_updated_at!r}, now {second_updated_at!r}"
    )


def test_log_sync_conflict_inserts_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    log_sync_conflict(
        conn,
        table_name="persons",
        record_id="dummy_alice",
        field_name="display_name",
        vault_value="Dummy Alice",
        db_value="ALICE",
        resolution=RESOLUTION_VAULT_WINS,
    )
    conn.commit()

    row = conn.execute("SELECT table_name, record_id, resolution FROM sync_conflicts").fetchone()
    audit_row = conn.execute(
        "SELECT event_type, subject_type, subject_id, resolution, message FROM audit_events"
    ).fetchone()

    assert row == ("persons", "dummy_alice", RESOLUTION_VAULT_WINS)
    assert audit_row == (
        "sync_resolution_recorded",
        "persons",
        "dummy_alice",
        RESOLUTION_VAULT_WINS,
        "field: display_name",
    )


def test_log_sync_conflict_upserts_on_repeated_call() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    for resolution in (RESOLUTION_QUARANTINED, RESOLUTION_VAULT_WINS):
        log_sync_conflict(
            conn,
            table_name="persons",
            record_id="dummy_alice",
            field_name="display_name",
            vault_value="Dummy Alice",
            db_value="ALICE",
            resolution=resolution,
        )
    conn.commit()

    rows = conn.execute("SELECT resolution FROM sync_conflicts").fetchall()
    audit_rows = conn.execute("SELECT resolution FROM audit_events ORDER BY rowid").fetchall()

    assert len(rows) == 1
    assert rows[0][0] == RESOLUTION_VAULT_WINS
    assert audit_rows == [(RESOLUTION_QUARANTINED,), (RESOLUTION_VAULT_WINS,)]


def test_log_sync_conflict_rejects_invalid_resolution() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    try:
        log_sync_conflict(
            conn,
            table_name="persons",
            record_id="dummy_alice",
            field_name="display_name",
            vault_value="Dummy Alice",
            db_value="ALICE",
            resolution="accepted",
        )
    except ValueError as exc:
        assert "invalid resolution" in str(exc)
    else:
        raise AssertionError("Expected invalid sync conflict resolution to be rejected")


# ---------------------------------------------------------------------------
# Milestone 1.1 — divergence detection
# ---------------------------------------------------------------------------


def test_detect_authority_returns_new_for_unknown_person() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    result = detect_person_sync_authority(conn, "nobody", "display_name", "Nobody")
    assert result == "new"


def test_detect_authority_vault_wins_when_db_unchanged_since_sync() -> None:
    """When updated_at <= vault_synced_at the last write was a vault sync → vault wins."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    upsert_person(conn, _make_person())
    # Advance vault_synced_at past updated_at to simulate a subsequent re-sync.
    conn.execute(
        "UPDATE persons SET vault_synced_at = datetime"
        "('now', '+5 seconds') WHERE id = 'dummy_alice'"
    )

    result = detect_person_sync_authority(
        conn, "dummy_alice", "display_name", "Dummy Alice Different"
    )
    assert result == "vault_wins"


def test_detect_authority_ambiguous_when_both_modified() -> None:
    """When updated_at > vault_synced_at and values differ, neither side is safe to prefer."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    upsert_person(conn, _make_person())
    # Simulate an out-of-band DB modification after the last vault sync.
    conn.execute(
        "UPDATE persons SET display_name = 'Dummy Alice Modified', "
        "updated_at = datetime('now', '+5 seconds') "
        "WHERE id = 'dummy_alice'"
    )

    result = detect_person_sync_authority(
        conn, "dummy_alice", "display_name", "Dummy Alice Vault Version"
    )
    assert result == "ambiguous"


def test_detect_authority_vault_wins_when_values_agree_despite_later_updated_at() -> None:
    """When updated_at > vault_synced_at but the value is identical, there is no real conflict."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    upsert_person(conn, _make_person())
    conn.execute(
        "UPDATE persons SET updated_at = datetime('now', '+5 seconds') WHERE id = 'dummy_alice'"
    )

    # Incoming value matches what is already in the DB.
    result = detect_person_sync_authority(conn, "dummy_alice", "display_name", "Dummy Alice")
    assert result == "vault_wins"


def test_sync_person_to_db_logs_conflict_for_ambiguous_field() -> None:
    """sync_person_to_db must record a quarantined conflict when a field is ambiguous."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    upsert_person(conn, _make_person())
    # Simulate an out-of-band DB modification after the last vault sync.
    conn.execute(
        "UPDATE persons SET display_name = 'Dummy Alice DB Edit', "
        "updated_at = datetime('now', '+5 seconds') "
        "WHERE id = 'dummy_alice'"
    )

    incoming: dict[str, Any] = {**_make_person(), "display_name": "Dummy Alice Vault Edit"}
    sync_person_to_db(conn, incoming)
    conn.commit()

    row = conn.execute(
        "SELECT field_name, vault_value, db_value, resolution FROM sync_conflicts "
        "WHERE record_id = 'dummy_alice'"
    ).fetchone()
    audit_row = conn.execute(
        "SELECT event_type, subject_type, subject_id, resolution, message FROM audit_events"
    ).fetchone()

    assert row is not None, "Expected a sync_conflict row to be logged"
    field_name, vault_value, db_value, resolution = row
    assert field_name == "display_name"
    assert vault_value == "Dummy Alice Vault Edit"
    assert db_value == "Dummy Alice DB Edit"
    assert resolution == RESOLUTION_QUARANTINED
    assert audit_row == (
        "sync_resolution_recorded",
        "persons",
        "dummy_alice",
        RESOLUTION_QUARANTINED,
        "field: display_name",
    )


def test_sync_person_to_db_no_conflict_logged_for_clean_resync() -> None:
    """A plain re-sync of unchanged data must not create any sync_conflict rows."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    sync_person_to_db(conn, _make_person())
    # Second sync with identical data.
    sync_person_to_db(conn, _make_person())
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM sync_conflicts").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Milestone 1.2 — Note Schema and Navigability Contract
# ---------------------------------------------------------------------------


def test_render_person_note_includes_records_hub_links() -> None:
    """render_person_note must include a ## Records section with hub wiki links."""
    content = render_person_note(_make_person())

    assert "## Records" in content
    for subdir in ("Labs", "History", "Medications", "Genes", "Insights", "Reports"):
        assert f"[[Records/dummy_alice/{subdir}/_index|{subdir}]]" in content


def test_render_person_note_puts_front_matter_before_records() -> None:
    """Front matter block must come before the ## Records section."""
    content = render_person_note(_make_person())

    fm_end = content.index("---\n\n")
    records_start = content.index("## Records")
    assert fm_end < records_start


def test_write_person_note_idempotent_creates_fresh_note(tmp_path: Path) -> None:
    """write_person_note_idempotent writes a new note when none exists."""
    note_path = tmp_path / "dummy_alice.md"

    write_person_note_idempotent(note_path, _make_person())

    assert note_path.exists()
    content = note_path.read_text(encoding="utf-8")
    assert "person_id: dummy_alice" in content
    assert "## Records" in content
    assert "## Notes" in content


def test_write_person_note_idempotent_preserves_notes_section(tmp_path: Path) -> None:
    """Re-running write_person_note_idempotent must keep user-written Notes content."""
    note_path = tmp_path / "dummy_alice.md"

    # First write — creates the note
    write_person_note_idempotent(note_path, _make_person())

    # Simulate user editing the Notes section
    original = note_path.read_text(encoding="utf-8")
    user_addition = "\n- Custom user note added after import."
    note_path.write_text(original + user_addition, encoding="utf-8")

    # Second write (e.g. after YAML change) — must preserve user note
    updated_person = {**_make_person(), "display_name": "Dummy Alice Updated"}
    write_person_note_idempotent(note_path, updated_person)

    final = note_path.read_text(encoding="utf-8")
    assert "Custom user note added after import." in final
    assert "Dummy Alice Updated" in final


def test_write_person_note_idempotent_updates_records_links_on_re_import(
    tmp_path: Path,
) -> None:
    """## Records section must reflect the current person_id after re-import."""
    note_path = tmp_path / "dummy_alice.md"

    write_person_note_idempotent(note_path, _make_person())

    # Re-import should still contain the Records links
    write_person_note_idempotent(note_path, _make_person())

    content = note_path.read_text(encoding="utf-8")
    assert "[[Records/dummy_alice/Labs/_index|Labs]]" in content


def test_validate_note_front_matter_passes_valid_note(tmp_path: Path) -> None:
    """A well-formed note with compact front matter reports no violations."""
    note_path = tmp_path / "dummy_alice.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_type: manual\n"
        "---\n\n"
        "# Dummy Alice\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_accepts_history_accepted_facts(tmp_path: Path) -> None:
    """accept_history_candidates writes accepted_facts and excluded_low_confidence_count."""
    note_path = tmp_path / "history.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: history\n"
        "verification_status: accepted\n"
        "extraction_mode: clinical_interpreter\n"
        "excluded_low_confidence_count: 2\n"
        "accepted_facts:\n"
        "  - description: Appendectomy\n"
        "    category: procedure\n"
        "    confidence: high\n"
        "---\n\n"
        "# History\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_accepts_medication_user_update_fields(
    tmp_path: Path,
) -> None:
    """user_update_sync writes category/date/source on medication notes."""
    note_path = tmp_path / "medications.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: '2023-06-08'\n"
        "source: user_update\n"
        "---\n\n"
        "# Medication List\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_requires_lab_panel_fields(tmp_path: Path) -> None:
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\nperson_id: dummy_alice\nrecord_type: lab_panel\n---\n\n# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("lab_panel" in v and "observed_at" in v for v in violations)
    assert any("lab_panel" in v and "source_type" in v for v in violations)


def test_validate_note_front_matter_requires_lab_result_fields(tmp_path: Path) -> None:
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        (
            "---\nperson_id: dummy_alice\nrecord_type: lab_"
            "result\nobserved_at: 2023-04-20\n---\n\n# Lab\n"
        ),
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("lab_result" in v and "test_name" in v for v in violations)
    assert any("lab_result" in v and "source_type" in v for v in violations)


def test_validate_note_front_matter_requires_report_fields(tmp_path: Path) -> None:
    note_path = tmp_path / "report.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: report\n"
        "report_type: latest_lab_results\n"
        "---\n\n"
        "# Report\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("report" in v and "generated_at" in v for v in violations)


def test_validate_note_front_matter_allows_report_review_state(tmp_path: Path) -> None:
    note_path = tmp_path / "report.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: report\n"
        "report_type: medication_conflicts\n"
        "generated_at: 2023-08-03\n"
        "verification_status: needs_review\n"
        "pending_review_count: 1\n"
        "---\n\n"
        "# Medication Conflicts\n",
        encoding="utf-8",
    )

    assert validate_note_front_matter(note_path) == []


def test_validate_note_front_matter_requires_index_person_id(tmp_path: Path) -> None:
    note_path = tmp_path / "_index.md"
    note_path.write_text(
        "---\nrecord_type: labs_index\n---\n\n# Labs\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("labs_index" in v and "person_id" in v for v in violations)


def test_validate_note_front_matter_requires_record_type(tmp_path: Path) -> None:
    """A generated note missing record_type must be flagged."""
    note_path = tmp_path / "dummy_alice.md"
    note_path.write_text(
        "---\nperson_id: dummy_alice\n---\n\n# Dummy Alice\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("record_type" in v for v in violations)


def test_validate_note_front_matter_catches_json_blob(tmp_path: Path) -> None:
    """A field containing an embedded JSON object is flagged as a violation."""
    note_path = tmp_path / "dummy_alice.md"
    note_path.write_text(
        '---\nperson_id: dummy_alice\npayload: \'{"key": "value"}\'\n---\n\n# Dummy Alice\n',
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("embedded JSON blob" in v for v in violations)


def test_validate_note_front_matter_requires_person_id(tmp_path: Path) -> None:
    """A note missing person_id must be flagged."""
    note_path = tmp_path / "orphan.md"
    note_path.write_text(
        "---\nrecord_type: lab_panel\n---\n\n# Orphan\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("person_id" in v for v in violations)


def test_validate_note_front_matter_rejects_absolute_source_path(tmp_path: Path) -> None:
    """Source references in front matter must be portable relative paths."""
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "source_path: /tmp/source.pdf\n"
        "---\n\n"
        "# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("source_path" in v and "relative" in v for v in violations)


def test_validate_note_front_matter_accepts_retained_processed_source_path(
    tmp_path: Path,
) -> None:
    """Generated source references point to retained processed sources."""
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_type: csv\n"
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-panel.csv\n"
        "---\n\n"
        "# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_accepts_vault_note_source_path_fallback(
    tmp_path: Path,
) -> None:
    """Legacy or manual records may fall back to a vault note source key."""
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_type: manual\n"
        "source_path: Records/dummy_alice/Labs/2023-04-20-panel.md\n"
        "---\n\n"
        "# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_rejects_non_retained_data_source_path(
    tmp_path: Path,
) -> None:
    """Persisted note source_path values must not point back to inbox/quarantine."""
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_type: csv\n"
        "source_path: data/inbox/dummy_alice/panel.csv\n"
        "---\n\n"
        "# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any(
        "source_path" in violation and "data/processed" in violation for violation in violations
    )


def test_validate_note_front_matter_rejects_unknown_known_record_field(
    tmp_path: Path,
) -> None:
    """Known record types must use their explicit compact metadata contract."""
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "unexpected: value\n"
        "---\n\n"
        "# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("unexpected" in v and "not allowed" in v for v in violations)


def test_validate_note_front_matter_rejects_structured_metadata(
    tmp_path: Path,
) -> None:
    """Bulky structured front matter belongs in Markdown body sections."""
    note_path = tmp_path / "lab.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_details:\n"
        "  provider: Quest\n"
        "---\n\n"
        "# Lab\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert any("source_details" in v and "not allowed" in v for v in violations)
    assert any("source_details" in v and "too structured" in v for v in violations)


def test_validate_note_front_matter_allows_unknown_future_scalar_metadata(
    tmp_path: Path,
) -> None:
    """Future category schemas can be introduced later without blocking scalar metadata."""
    note_path = tmp_path / "future.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: future_category\n"
        "status: needs_review\n"
        "---\n\n"
        "# Future\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_accepts_category_placeholder(tmp_path: Path) -> None:
    """Placeholder notes keep unsupported category captures compact and source-linked."""
    note_path = tmp_path / "placeholder.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: category_placeholder\n"
        "category: History\n"
        "review_status: needs_review\n"
        "source_type: pdf\n"
        "source_path: data/processed/dummy_alice/History/undated-visit-summary.pdf\n"
        "source_file_name: undated-visit-summary.pdf\n"
        "---\n\n"
        "# Placeholder\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_note_front_matter_keeps_legacy_person_profiles_readable(
    tmp_path: Path,
) -> None:
    """Person profiles without record_type remain valid until re-rendered."""
    persons_dir = tmp_path / "Persons"
    persons_dir.mkdir()
    note_path = persons_dir / "dummy_alice.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "display_name: Dummy Alice\n"
        "birth_year: 1990\n"
        "sex: female\n"
        "relationships: []\n"
        "---\n\n"
        "# Dummy Alice\n",
        encoding="utf-8",
    )

    violations = validate_note_front_matter(note_path)

    assert violations == []


def test_validate_vault_notes_scans_generated_notes_and_skips_templates(
    tmp_path: Path,
) -> None:
    """Vault-wide validation should cover generated notes without requiring future schemas."""
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    template_dir = vault_root / "Records" / "_person_id" / "Labs"

    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    template_dir.mkdir(parents=True)

    (persons_dir / "_index.md").write_text("# Persons\n", encoding="utf-8")
    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )
    (labs_dir / "2023-04-20-panel.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_type: csv\n"
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-panel.csv\n"
        "---\n\n"
        "# Lab Panel\n",
        encoding="utf-8",
    )
    (labs_dir / "_index.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: labs_index\n---\n\n"
        "# Labs\n\n"
        "[[dummy_alice|Person]]\n\n"
        "- [[2023-04-20-panel]]\n",
        encoding="utf-8",
    )
    (reports_dir / "_index.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: reports_index\n---\n\n# Reports\n",
        encoding="utf-8",
    )
    (template_dir / "_template.md").write_text("# Template\n", encoding="utf-8")

    issues = validate_vault_notes(vault_root)

    assert issues == []


def test_validate_vault_notes_reports_note_paths(tmp_path: Path) -> None:
    """Vault-wide validation should return path context for every violation."""
    vault_root = tmp_path / "famed-vault"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)
    (labs_dir / "bad.md").write_text(
        "---\nperson_id: dummy_alice\npayload: '{}'\n---\n\n# Bad\n",
        encoding="utf-8",
    )

    issues = validate_vault_notes(vault_root)

    assert len(issues) == 3
    assert {issue.path for issue in issues} == {
        Path("Records/dummy_alice/Labs/bad.md"),
        Path("Records/dummy_alice/Labs/_index.md"),
    }
    assert any("record_type" in issue.message for issue in issues)
    assert any("embedded JSON blob" in issue.message for issue in issues)
    assert any("category index" in issue.message for issue in issues)


def test_validate_vault_notes_reports_unlinked_record_note(tmp_path: Path) -> None:
    """Record notes must be linked from their category index."""
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)

    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )
    (labs_dir / "2023-04-20-panel.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: lab_panel\n---\n\n# Lab Panel\n",
        encoding="utf-8",
    )
    (labs_dir / "_index.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: labs_index\n---\n\n# Labs\n",
        encoding="utf-8",
    )

    issues = validate_vault_notes(vault_root)

    assert any(
        issue.path == Path("Records/dummy_alice/Labs/2023-04-20-panel.md")
        and "does not link" in issue.message
        for issue in issues
    )


def test_validate_vault_notes_reports_category_index_missing_from_person_hub(
    tmp_path: Path,
) -> None:
    """Category indexes must be reachable from the person profile."""
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)

    (persons_dir / "dummy_alice.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: person\n"
        "display_name: Dummy Alice\n"
        "birth_year: 1990\n"
        "sex: female\n"
        "---\n\n"
        "# Dummy Alice\n",
        encoding="utf-8",
    )
    (labs_dir / "_index.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: labs_index\n---\n\n# Labs\n",
        encoding="utf-8",
    )

    issues = validate_vault_notes(vault_root)

    assert any(
        issue.path == Path("Records/dummy_alice/Labs/_index.md")
        and "person hub does not link" in issue.message
        for issue in issues
    )


def _rel(rel_type: str, target: str) -> dict[str, str]:
    return {"relationship_type": rel_type, "target_person_id": target}


def _make_age_person(
    person_id: str,
    birth_year: int,
    relationships: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "display_name": person_id.capitalize(),
        "birth_year": birth_year,
        "sex": "m",
        "relationships": relationships or [],
    }


def test_check_age_plausibility_no_warnings_for_valid_parent_child() -> None:
    parent = _make_age_person("dad", 1941, [_rel("child", "son")])
    son = _make_age_person("son", 1968, [_rel("parent", "dad")])
    persons = {"dad": parent, "son": son}

    assert check_relationship_age_plausibility(persons) == []


def test_check_age_plausibility_valid_parent_younger_than_target_no_warning() -> None:
    older = _make_age_person("elder", 1941, [_rel("child", "younger")])
    younger = _make_age_person("younger", 1968, [_rel("parent", "elder")])
    persons = {"elder": older, "younger": younger}

    warnings = check_relationship_age_plausibility(persons)
    # younger (1968) claims elder (1941) as parent — gap is 27 years ≥ 12 → valid
    assert warnings == []


def test_check_age_plausibility_warns_when_parent_is_actually_younger() -> None:
    # elder (1941) claims younger (1968) as their parent — impossible
    older = _make_age_person("elder", 1941, [_rel("parent", "younger")])
    younger = _make_age_person("younger", 1968)
    persons = {"elder": older, "younger": younger}

    warnings = check_relationship_age_plausibility(persons)
    assert len(warnings) == 1
    assert "Elder" in warnings[0]
    assert "Younger" in warnings[0]
    assert "parent" in warnings[0]


def test_check_age_plausibility_no_warning_when_birth_year_missing() -> None:
    no_year: dict[str, Any] = {
        "person_id": "unknown",
        "display_name": "Unknown",
        "sex": "m",
        "relationships": [_rel("parent", "child")],
    }
    child = _make_age_person("child", 1980)
    persons: dict[str, Any] = {"unknown": no_year, "child": child}

    assert check_relationship_age_plausibility(persons) == []


def test_check_age_plausibility_warns_for_insufficient_gap() -> None:
    # child (1963) claims parent (1958) as parent — gap is only 5 years
    parent = _make_age_person("parent", 1958, [_rel("child", "child")])
    child = _make_age_person("child", 1963, [_rel("parent", "parent")])
    persons = {"parent": parent, "child": child}

    warnings = check_relationship_age_plausibility(persons)
    assert len(warnings) == 1
    assert "5 year" in warnings[0]


def test_check_age_plausibility_grandparent_uses_larger_gap() -> None:
    # gc (1980) claims gp (1960) as grandparent — gap is 20 years, below the 24-year minimum
    gp = _make_age_person("gp", 1960, [])
    gc = _make_age_person("gc", 1980, [_rel("grandparent", "gp")])
    persons = {"gp": gp, "gc": gc}

    warnings = check_relationship_age_plausibility(persons)
    assert len(warnings) == 1
    assert "grandparent" in warnings[0]


def test_check_age_plausibility_no_duplicate_warnings_from_reciprocals() -> None:
    # Both directions present (parent and child reciprocals)
    parent = _make_age_person("parent", 1940, [_rel("child", "child")])
    child = _make_age_person("child", 1980, [_rel("parent", "parent")])
    persons = {"parent": parent, "child": child}

    # child→parent is valid (gap 40 ≥ 12); parent→child is "child" type (not checked)
    warnings = check_relationship_age_plausibility(persons)
    assert warnings == []
