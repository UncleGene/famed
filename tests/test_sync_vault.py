from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from famed import sync_vault
from famed.person_sync import render_person_note, upsert_person, validate_vault_notes
from famed.setup import SCHEMA_SQL
from famed.sync_vault import detect_person_divergence, repair_navigation


def _make_person(person_id: str = "dummy_alice") -> dict[str, object]:
    return {
        "person_id": person_id,
        "display_name": "Dummy Alice",
        "birth_year": 1990,
        "sex": "female",
        "relationships": [],
    }


def test_vault_templates_use_space_indented_yaml() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    template_paths = [
        repo_root / "vault-template" / "Persons" / "_template.md",
        repo_root / "vault-template" / "Records" / "_person_id" / "Labs" / "_template.md",
    ]

    for template_path in template_paths:
        content = template_path.read_text(encoding="utf-8")
        front_matter = content.split("---\n", 2)[1]
        assert "\t" not in front_matter


def test_person_index_has_expected_columns() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    content = (repo_root / "vault-template" / "Persons" / "_index.md").read_text(encoding="utf-8")

    assert 'file.link AS "Person"' in content
    assert 'link(person_id) AS "Person"' in content
    assert 'GROUP BY person_id + "|" + test_name AS key' in content


def test_person_template_carries_current_identity_fields() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    content = (repo_root / "vault-template" / "Persons" / "_template.md").read_text(
        encoding="utf-8"
    )
    front_matter = content.split("---\n", 2)[1]
    parsed = yaml.safe_load(front_matter)

    assert parsed["legal_name"] == ""
    assert parsed["birth_date"] == ""
    assert parsed["identifiers"] == []


def _db_with_person(person_id: str = "dummy_alice") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    upsert_person(conn, _make_person(person_id))
    conn.commit()
    return conn


def _write_lab_panel_note(
    labs_dir: Path,
    name: str,
    extra_front_matter: str = "",
    source_type: str = "csv",
) -> None:
    source_metadata = f"{extra_front_matter}\n" if extra_front_matter else ""
    (labs_dir / name).write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        f"source_type: {source_type}\n"
        f"{source_metadata}"
        "---\n\n"
        "# Lab Panel\n\n"
        "## Results\n\n"
        "| test_name | loinc_code | value | unit | reference_range_low | "
        "reference_range_high | observed_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| Hemoglobin A1c | 4548-4 | 5.8 | % | 4.0 | 5.6 | 2023-04-20 |\n",
        encoding="utf-8",
    )


def test_no_divergence_on_first_sync(tmp_path: Path) -> None:
    """A brand-new DB record (no vault_synced_at) reports no divergence."""
    note = tmp_path / "dummy_alice.md"
    note.write_text("# Dummy Alice", encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    result = detect_person_divergence(conn, "dummy_alice", note)
    assert result is None


def test_no_divergence_when_only_vault_changed(tmp_path: Path) -> None:
    """After first sync, only the vault note changing → vault wins (no divergence)."""
    note = tmp_path / "dummy_alice.md"
    note.write_text("# Dummy Alice", encoding="utf-8")

    conn = _db_with_person()
    # Simulate vault_synced_at set in the past; updated_at equals vault_synced_at.
    conn.execute("UPDATE persons SET vault_synced_at = updated_at WHERE id = 'dummy_alice'")
    conn.commit()

    result = detect_person_divergence(conn, "dummy_alice", note)
    assert result is None


def test_db_wins_when_only_db_modified_after_sync(tmp_path: Path) -> None:
    """DB row updated_at > vault_synced_at while note mtime <= vault_synced_at → db_wins."""
    note = tmp_path / "dummy_alice.md"
    note.write_text("# Dummy Alice", encoding="utf-8")

    conn = _db_with_person()
    # vault_synced_at is in the future relative to now so the note mtime (real filesystem time)
    # is < vault_synced_at.  updated_at is even further in the future to simulate a manual DB
    # edit occurring after the last vault sync.
    conn.execute(
        """
        UPDATE persons
        SET vault_synced_at = datetime('now', '+1 hour'),
            updated_at = datetime('now', '+2 hours')
        WHERE id = 'dummy_alice'
        """
    )
    conn.commit()

    result = detect_person_divergence(conn, "dummy_alice", note)
    assert result == "db_wins"


def test_ambiguous_when_both_sides_modified(tmp_path: Path) -> None:
    """Both DB updated_at and note mtime are newer than vault_synced_at → ambiguous."""
    note = tmp_path / "dummy_alice.md"
    note.write_text("# Dummy Alice", encoding="utf-8")

    conn = _db_with_person()
    # vault_synced_at is set to a distant past so both DB updated_at and file mtime are newer.
    conn.execute(
        "UPDATE persons SET vault_synced_at = '1997-01-01 00:00:00', "
        "updated_at = '1997-01-02 00:00:00' WHERE id = 'dummy_alice'"
    )
    conn.commit()

    result = detect_person_divergence(conn, "dummy_alice", note)
    assert result == "ambiguous"


def test_sync_vault_main_creates_reports_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    records_dir = vault_root / "Records"
    persons_dir.mkdir(parents=True)
    records_dir.mkdir(parents=True)

    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    note_path = persons_dir / "dummy_alice.md"
    note_path.write_text(render_person_note(_make_person()), encoding="utf-8")

    monkeypatch.setattr(sync_vault, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_vault"])

    assert sync_vault.main() == 0

    reports_index = vault_root / "Records" / "dummy_alice" / "Reports" / "_index.md"
    assert reports_index.exists()
    assert "record_type: reports_index" in reports_index.read_text(encoding="utf-8")


def test_sync_vault_closes_reciprocal_person_relationships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    records_dir = vault_root / "Records"
    persons_dir.mkdir(parents=True)
    records_dir.mkdir(parents=True)

    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    dummy_alice = {
        **_make_person("dummy_alice"),
        "relationships": [{"relationship_type": "parent", "target_person_id": "dummy_bob"}],
    }
    dummy_bob = {**_make_person("dummy_bob"), "display_name": "Dummy Bob"}
    (persons_dir / "dummy_alice.md").write_text(render_person_note(dummy_alice), encoding="utf-8")
    (persons_dir / "dummy_bob.md").write_text(render_person_note(dummy_bob), encoding="utf-8")

    monkeypatch.setattr(sync_vault, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_vault"])

    assert sync_vault.main() == 0

    dummy_bob_content = (persons_dir / "dummy_bob.md").read_text(encoding="utf-8")
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


def test_sync_vault_uses_lab_note_processed_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)

    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO lab_results (
                id,
                person_id,
                test_name,
                value,
                unit,
                observed_at,
                source_type,
                source_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-note-path-row",
                "dummy_alice",
                "Hemoglobin A1c",
                5.7,
                "%",
                "2023-04-20",
                "csv",
                "Records/dummy_alice/Labs/2023-04-20-quest-report-panel.md",
            ),
        )
        conn.commit()

    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )
    _write_lab_panel_note(
        labs_dir,
        "2023-04-20-quest-report-panel.md",
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
    )

    monkeypatch.setattr(sync_vault, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_vault"])

    assert sync_vault.main() == 0

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT source_path FROM lab_results").fetchall()
        audit_row = conn.execute(
            """
            SELECT event_type, subject_type, subject_id, source_path, status, message
            FROM audit_events
            """
        ).fetchone()

    assert rows == [("data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",)]
    assert audit_row == (
        "lab_source_path_migrated",
        "lab_panel",
        "dummy_alice",
        "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
        "processed",
        "source key migrated from Records/dummy_alice/Labs/2023-04-20-quest-report-panel.md",
    )
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    flagged = (reports_dir / "flagged-for-discussion.md").read_text(encoding="utf-8")
    assert "Hemoglobin A1c" in flagged
    assert "2023-04-20" in flagged

    monkeypatch.setattr(sys, "argv", ["sync_vault"])
    assert sync_vault.main() == 0

    with sqlite3.connect(db_path) as conn:
        audit_rows = conn.execute(
            "SELECT event_type FROM audit_events WHERE event_type = ?",
            ("lab_source_path_migrated",),
        ).fetchall()

    assert audit_rows == [("lab_source_path_migrated",)]


def test_sync_vault_falls_back_to_lab_note_path_for_legacy_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)

    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )
    _write_lab_panel_note(labs_dir, "2023-04-20-manual-panel.md", source_type="manual")

    monkeypatch.setattr(sync_vault, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_vault"])

    assert sync_vault.main() == 0

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT source_path FROM lab_results").fetchone()

    assert row == ("Records/dummy_alice/Labs/2023-04-20-manual-panel.md",)


def test_sync_vault_validate_only_reports_vault_note_violations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "famed-vault"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)
    (labs_dir / "bad.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "source_path: /tmp/source.pdf\n"
        "---\n\n"
        "# Bad\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_vault, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_vault", "--validate-only"])

    assert sync_vault.main() == 1

    captured = capsys.readouterr()
    assert "Vault note validation failed:" in captured.out
    assert "Records/dummy_alice/Labs/bad.md" in captured.out
    assert "source_path" in captured.out


def test_repair_navigation_backfills_person_links_and_category_indexes(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)

    (persons_dir / "dummy_alice.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "display_name: Dummy Alice\n"
        "birth_year: 1990\n"
        "sex: female\n"
        "relationships: []\n"
        "---\n\n"
        "# Dummy Alice\n\n"
        "## Notes\n\n"
        "- Existing note.\n",
        encoding="utf-8",
    )
    (labs_dir / "2023-04-20-panel.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: lab_panel\n"
        "observed_at: 2023-04-20\n"
        "source_type: manual\n"
        "---\n\n"
        "# Lab Panel\n",
        encoding="utf-8",
    )

    repaired_persons, regenerated_indexes = repair_navigation(vault_root)

    assert repaired_persons == 1
    assert regenerated_indexes == 8
    person_content = (persons_dir / "dummy_alice.md").read_text(encoding="utf-8")
    assert "record_type: person" in person_content
    assert "[[Records/dummy_alice/Labs/_index|Labs]]" in person_content
    assert "[[Records/dummy_alice/Vitals/_index|Vitals]]" in person_content
    assert "Existing note." in person_content

    labs_index = labs_dir / "_index.md"
    assert labs_index.exists()
    assert "[[2023-04-20-panel]]" in labs_index.read_text(encoding="utf-8")
    assert validate_vault_notes(vault_root) == []


def test_repair_navigation_regenerates_category_indexes_idempotently(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    persons_dir.mkdir(parents=True)
    labs_dir.mkdir(parents=True)

    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )
    _write_lab_panel_note(labs_dir, "2023-04-20-panel.md", source_type="manual")
    labs_index = labs_dir / "_index.md"
    labs_index.write_text(
        "---\nperson_id: dummy_alice\nrecord_type: labs_index\n---\n\n"
        "# Labs\n\n"
        "- [[stale-panel]]\n"
        "- [[2023-04-20-panel]]\n"
        "- [[2023-04-20-panel]]\n",
        encoding="utf-8",
    )

    first_result = repair_navigation(vault_root)
    first_index_content = labs_index.read_text(encoding="utf-8")

    second_result = repair_navigation(vault_root)
    second_index_content = labs_index.read_text(encoding="utf-8")

    assert first_result == second_result == (1, 8)
    assert second_index_content == first_index_content
    assert "stale-panel" not in second_index_content
    assert second_index_content.count("[[2023-04-20-panel]]") == 1
    assert validate_vault_notes(vault_root) == []


def test_validate_vault_notes_accepts_superseded_medication_evidence(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    medications_dir = vault_root / "Records" / "dummy_alice" / "Medications"
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    persons_dir.mkdir(parents=True)
    medications_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )
    (medications_dir / "_index.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: medications_index\n---\n\n"
        "# Medications\n\n"
        "- [[2023-01-01-medications]] - superseded by "
        "[[Records/dummy_alice/Reports/current-medications]]\n",
        encoding="utf-8",
    )
    (reports_dir / "_index.md").write_text(
        "---\nperson_id: dummy_alice\nrecord_type: reports_index\n---\n\n"
        "# Reports\n\n"
        "- [[current-medications]]\n",
        encoding="utf-8",
    )
    (reports_dir / "current-medications.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: report\n"
        "report_type: current_medications\n"
        "generated_at: '2023-01-02'\n"
        "---\n\n"
        "# Current Medications\n",
        encoding="utf-8",
    )
    (medications_dir / "2023-01-01-medications.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: '2023-01-01'\n"
        "projection_role: superseded_evidence\n"
        "superseded_by: Records/dummy_alice/Reports/current-medications\n"
        "---\n\n"
        "# Medication List\n\n"
        "[[_index|<- Medications]]\n",
        encoding="utf-8",
    )

    assert validate_vault_notes(vault_root) == []


def test_sync_vault_repair_navigation_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "famed-vault"
    persons_dir = vault_root / "Persons"
    persons_dir.mkdir(parents=True)
    (persons_dir / "dummy_alice.md").write_text(
        render_person_note(_make_person()), encoding="utf-8"
    )

    monkeypatch.setattr(sync_vault, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_vault", "--repair-navigation"])

    assert sync_vault.main() == 0

    captured = capsys.readouterr()
    assert "Navigation repair completed:" in captured.out
    assert "1 person note(s)" in captured.out
