from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from famed.person_sync import validate_vault_notes
from famed.rebuild_vault import (
    RebuildError,
    rebuild_identity_layer,
    reset_existing_target_to_identity,
    validate_existing_identity_layer,
)
from famed.setup import initialize_database

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "vault-template"


def _write_person(
    vault: Path,
    person_id: str,
    display_name: str,
    relationships: list[tuple[str, str]],
) -> None:
    persons_dir = vault / "Persons"
    persons_dir.mkdir(parents=True, exist_ok=True)
    relationship_yaml = "".join(
        f"  - relationship_type: {kind}\n    target_person_id: {target}\n"
        for kind, target in relationships
    )
    (persons_dir / f"{person_id}.md").write_text(
        "---\n"
        f"person_id: {person_id}\n"
        "record_type: person\n"
        f"display_name: {display_name}\n"
        "birth_year: 1990\n"
        "sex: unknown\n"
        "relationships:\n"
        f"{relationship_yaml}"
        "---\n\n"
        f"# {display_name}\n\n"
        "## Notes\n\n"
        "- Source-vault context.\n",
        encoding="utf-8",
    )


def _source(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "source-vault"
    _write_person(vault, "dummy_alice", "Dummy Alice", [("spouse", "dummy_bob")])
    _write_person(vault, "dummy_bob", "Dummy Bob", [])
    db = tmp_path / "source.db"
    initialize_database(db)
    return vault, db


def test_identity_rebuild_is_isolated_fk_clean_and_idempotent(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    source_note = source_vault / "Persons" / "dummy_alice.md"
    source_note_before = source_note.read_bytes()
    source_db_before = source_db.read_bytes()
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    first = rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
    )
    first_note_contents = {
        path.relative_to(target_vault): path.read_bytes() for path in target_vault.rglob("*.md")
    }
    with sqlite3.connect(target_db) as conn:
        first_people = conn.execute(
            "SELECT id, display_name, birth_year, sex FROM persons ORDER BY id"
        ).fetchall()
        first_links = conn.execute(
            "SELECT source_person_id, relationship_type, target_person_id "
            "FROM family_links ORDER BY source_person_id, relationship_type, target_person_id"
        ).fetchall()
    second = rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
    )

    assert first == second
    assert first.person_count == 2
    assert first.family_link_count == 2
    assert validate_vault_notes(target_vault) == []
    assert "Source-vault context." in (target_vault / "Persons" / "dummy_alice.md").read_text(
        encoding="utf-8"
    )
    assert (target_vault / "Records" / "dummy_alice" / "Labs" / "_index.md").exists()
    assert "_No lab notes yet._" in (
        target_vault / "Records" / "dummy_alice" / "Labs" / "_index.md"
    ).read_text(encoding="utf-8")
    with sqlite3.connect(target_db) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        links = conn.execute(
            "SELECT source_person_id, relationship_type, target_person_id "
            "FROM family_links ORDER BY source_person_id"
        ).fetchall()
        people = conn.execute(
            "SELECT id, display_name, birth_year, sex FROM persons ORDER BY id"
        ).fetchall()
    assert links == [
        ("dummy_alice", "spouse", "dummy_bob"),
        ("dummy_bob", "spouse", "dummy_alice"),
    ]
    assert people == first_people
    assert links == first_links
    assert {
        path.relative_to(target_vault): path.read_bytes() for path in target_vault.rglob("*.md")
    } == first_note_contents
    assert source_note.read_bytes() == source_note_before
    assert source_db.read_bytes() == source_db_before


def test_existing_identity_validation_allows_medical_resume(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
    )
    medical_note = target_vault / "Records" / "dummy_alice" / "Labs" / "panel.md"
    medical_note.write_text("synthetic medical artifact", encoding="utf-8")
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "INSERT INTO lab_results "
            "(id, person_id, test_name, observed_at, source_type) VALUES (?, ?, ?, ?, ?)",
            ("synthetic", "dummy_alice", "Synthetic Test", "2099-01-01", "fixture"),
        )
        conn.commit()

    result = validate_existing_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
    )

    assert result.person_count == 2
    assert result.family_link_count == 2

    reset_existing_target_to_identity(
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
        person_ids={"dummy_alice", "dummy_bob"},
    )

    assert not medical_note.exists()
    with sqlite3.connect(target_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM persons").fetchone() == (2,)


def test_resume_refreshes_static_vault_templates(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    template_root = tmp_path / "vault-template"
    person_template = template_root / "Persons" / "_template.md"
    person_template.parent.mkdir(parents=True)
    person_template.write_text("old template\n", encoding="utf-8")
    rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=template_root,
    )
    person_template.write_text("current template\n", encoding="utf-8")

    reset_existing_target_to_identity(
        target_vault=target_vault,
        target_db=target_db,
        template_root=template_root,
        person_ids={"dummy_alice", "dummy_bob"},
    )

    assert (target_vault / "Persons" / "_template.md").read_text(
        encoding="utf-8"
    ) == "current template\n"


def test_resume_preflight_failure_preserves_existing_medical_target(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
    )
    medical_note = target_vault / "Records" / "dummy_alice" / "Labs" / "panel.md"
    medical_note.write_text("synthetic medical artifact", encoding="utf-8")
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "INSERT INTO lab_results "
            "(id, person_id, test_name, observed_at, source_type) VALUES (?, ?, ?, ?, ?)",
            ("synthetic", "dummy_alice", "Synthetic Test", "2099-01-01", "fixture"),
        )
        conn.commit()

    result = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_ROOT.parent / "scripts" / "rebuild_vault.py"),
            "--source-vault",
            str(source_vault),
            "--source-db",
            str(source_db),
            "--target-vault",
            str(target_vault),
            "--target-db",
            str(target_db),
            "--resume-medical",
            "--source-processed",
            str(tmp_path / "missing-processed"),
            "--target-processed",
            str(tmp_path / "target-processed"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "source processed root does not exist" in result.stderr
    assert medical_note.read_text(encoding="utf-8") == "synthetic medical artifact"
    with sqlite3.connect(target_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (1,)


def test_cli_requires_authority_replay_before_dna(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_ROOT.parent / "scripts" / "rebuild_vault.py"),
            "--source-vault",
            str(tmp_path / "source-vault"),
            "--source-db",
            str(tmp_path / "source.db"),
            "--target-vault",
            str(tmp_path / "target-vault"),
            "--target-db",
            str(tmp_path / "target.db"),
            "--replay-dna",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--replay-dna requires --replay-authority" in result.stderr


def test_cli_requires_dna_replay_before_comparison(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_ROOT.parent / "scripts" / "rebuild_vault.py"),
            "--source-vault",
            str(tmp_path / "source-vault"),
            "--source-db",
            str(tmp_path / "source.db"),
            "--target-vault",
            str(tmp_path / "target-vault"),
            "--target-db",
            str(tmp_path / "target.db"),
            "--compare",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--compare requires --replay-dna" in result.stderr


@pytest.mark.parametrize("decision_flag", ["--retained-decisions", "--history-decisions"])
def test_cli_requires_medical_replay_for_retained_decisions(
    tmp_path: Path,
    decision_flag: str,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_ROOT.parent / "scripts" / "rebuild_vault.py"),
            "--source-vault",
            str(tmp_path / "source-vault"),
            "--source-db",
            str(tmp_path / "source.db"),
            "--target-vault",
            str(tmp_path / "target-vault"),
            "--target-db",
            str(tmp_path / "target.db"),
            decision_flag,
            str(tmp_path / "history-decisions.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--retained-decisions requires --replay-medical or --resume-medical" in result.stderr


@pytest.mark.parametrize("script_name", ["rebuild_vault.py", "reprocess_source.py"])
def test_retained_decision_cli_keeps_history_decisions_alias(script_name: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_ROOT.parent / "scripts" / script_name),
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--retained-decisions" in result.stdout
    assert "--history-decisions" in result.stdout


def test_fresh_cli_replay_prepares_authority_snapshot_before_medical_sources(
    tmp_path: Path,
) -> None:
    source_vault, source_db = _source(tmp_path)
    update_note = (
        source_vault
        / "Records"
        / "dummy_alice"
        / "Medications"
        / "2099-01-01-dummy_alice-medications.md"
    )
    update_note.parent.mkdir(parents=True)
    update_note.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: 2099-01-01\n"
        "---\n\n"
        "## Medications\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
        "status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Examplemed | prescription |  | 25 mg | daily | active | 2099-01-02 "
        "| 2099-01-04 | fixture |\n"
        "| Examplemed | prescription |  | 50 mg | daily | active | 2099-01-05 "
        "|  | fixture |\n",
        encoding="utf-8",
    )
    source_processed = tmp_path / "source-processed"
    narrative = (
        source_processed / "dummy_alice" / "Medications" / "2099-01-01 Synthetic Medication Updates"
    )
    narrative.parent.mkdir(parents=True)
    narrative.write_text(
        "Synthetic Medication Updates\n\nCurrent:\n\n"
        "Examplemed 25 mg from 2099-01-02; 50 mg from 2099-01-05 (daily)\n",
        encoding="utf-8",
    )
    with sqlite3.connect(source_db) as conn:
        conn.executemany(
            "INSERT INTO persons (id, display_name) VALUES (?, ?)",
            (("dummy_alice", "Dummy Alice"), ("dummy_bob", "Dummy Bob")),
        )
        conn.executemany(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency,
                start_date, end_date, status, source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "timeline-1",
                    "dummy_alice",
                    "Examplemed",
                    "prescription",
                    "25 mg",
                    "daily",
                    "2099-01-02",
                    "2099-01-04",
                    "active",
                    "user_update",
                    "Records/dummy_alice/Medications/2099-01-01-dummy_alice-medications.md",
                ),
                (
                    "timeline-2",
                    "dummy_alice",
                    "Examplemed",
                    "prescription",
                    "50 mg",
                    "daily",
                    "2099-01-05",
                    None,
                    "active",
                    "user_update",
                    "Records/dummy_alice/Medications/2099-01-01-dummy_alice-medications.md",
                ),
            ),
        )
        conn.commit()
    source_db_before = source_db.read_bytes()
    update_note_before = update_note.read_bytes()
    narrative_before = narrative.read_bytes()

    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    target_processed = tmp_path / "target-processed"
    result = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_ROOT.parent / "scripts" / "rebuild_vault.py"),
            "--source-vault",
            str(source_vault),
            "--source-db",
            str(source_db),
            "--target-vault",
            str(target_vault),
            "--target-db",
            str(target_db),
            "--replay-medical",
            "--replay-authority",
            "--source-processed",
            str(source_processed),
            "--target-processed",
            str(target_processed),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "0 failed" in result.stdout
    assert "1 recovered from vault authority" in result.stdout
    assert "authority-backed medication narrative deferred" in result.stdout
    assert source_db.read_bytes() == source_db_before
    assert update_note.read_bytes() == update_note_before
    assert narrative.read_bytes() == narrative_before
    with sqlite3.connect(target_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM medications WHERE source_type = 'user_update'"
        ).fetchone() == (2,)


def test_identity_rebuild_rejects_dangling_relationship_before_writes(tmp_path: Path) -> None:
    source_vault = tmp_path / "source-vault"
    _write_person(source_vault, "dummy_alice", "Dummy Alice", [("parent", "dummy_missing")])
    source_db = tmp_path / "source.db"
    initialize_database(source_db)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"

    with pytest.raises(RebuildError, match="unknown people"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            target_db=target_db,
            template_root=TEMPLATE_ROOT,
        )

    assert not target_vault.exists()
    assert not target_db.exists()


def test_identity_rebuild_rejects_overlapping_vaults(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    with pytest.raises(RebuildError, match="must not overlap"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=source_vault / "rebuild",
            target_db=tmp_path / "target.db",
            template_root=TEMPLATE_ROOT,
        )


def test_identity_rebuild_rejects_unexpected_existing_target_person(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_vault = tmp_path / "target-vault"
    _write_person(target_vault, "dummy_extra", "Dummy Extra", [])

    with pytest.raises(RebuildError, match="outside the rebuild source"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            target_db=tmp_path / "target.db",
            template_root=TEMPLATE_ROOT,
        )


def test_identity_rebuild_rejects_unexpected_existing_target_db_person(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_db = tmp_path / "target.db"
    initialize_database(target_db)
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "INSERT INTO persons (id, display_name) VALUES (?, ?)",
            ("dummy_extra", "Dummy Extra"),
        )
        conn.commit()

    with pytest.raises(RebuildError, match="outside the rebuild source"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=tmp_path / "target-vault",
            target_db=target_db,
            template_root=TEMPLATE_ROOT,
        )

    assert not (tmp_path / "target-vault").exists()


def test_identity_rebuild_rejects_dirty_same_person_target_vault(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
    )
    stale_note = target_vault / "Records" / "dummy_alice" / "Labs" / "stale.md"
    stale_note.write_text("stale medical content", encoding="utf-8")

    with pytest.raises(RebuildError, match="non-identity artifacts"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            target_db=target_db,
            template_root=TEMPLATE_ROOT,
        )

    assert stale_note.read_text(encoding="utf-8") == "stale medical content"


def test_identity_rebuild_rejects_dirty_same_person_target_db(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"
    rebuild_identity_layer(
        source_vault=source_vault,
        source_db=source_db,
        target_vault=target_vault,
        target_db=target_db,
        template_root=TEMPLATE_ROOT,
    )
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "INSERT INTO lab_results "
            "(id, person_id, test_name, observed_at, source_type) VALUES (?, ?, ?, ?, ?)",
            ("stale", "dummy_alice", "Synthetic Test", "2099-01-01", "manual"),
        )
        conn.commit()

    with pytest.raises(RebuildError, match="non-identity rows in: lab_results"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            target_db=target_db,
            template_root=TEMPLATE_ROOT,
        )


def test_identity_rebuild_wraps_malformed_yaml_before_writes(tmp_path: Path) -> None:
    source_vault = tmp_path / "source-vault"
    persons_dir = source_vault / "Persons"
    persons_dir.mkdir(parents=True)
    (persons_dir / "dummy_alice.md").write_text(
        "---\nperson_id: [unterminated\n---\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    initialize_database(source_db)
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"

    with pytest.raises(RebuildError, match="invalid person note"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            target_db=target_db,
            template_root=TEMPLATE_ROOT,
        )

    assert not target_vault.exists()
    assert not target_db.exists()


def test_identity_rebuild_rejects_unrecognized_quoted_target_table(tmp_path: Path) -> None:
    source_vault, source_db = _source(tmp_path)
    target_db = tmp_path / "target.db"
    initialize_database(target_db)
    with sqlite3.connect(target_db) as conn:
        conn.execute('CREATE TABLE "odd""table" (value TEXT)')
        conn.execute('INSERT INTO "odd""table" (value) VALUES (?)', ("stale",))
        conn.commit()

    with pytest.raises(RebuildError, match="schema is not recognized.*unexpected: odd.*table"):
        rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=tmp_path / "target-vault",
            target_db=target_db,
            template_root=TEMPLATE_ROOT,
        )

    assert not (tmp_path / "target-vault").exists()
