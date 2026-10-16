from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from famed.authority_replay import authority_update_snapshot_paths, replay_authority_overlays
from famed.reprocess_sources import ReprocessError
from famed.setup import initialize_database


def _write_person(vault_root: Path, person_id: str = "dummy_person") -> None:
    person_path = vault_root / "Persons" / f"{person_id}.md"
    person_path.parent.mkdir(parents=True, exist_ok=True)
    person_path.write_text(
        "---\n"
        f"person_id: {person_id}\n"
        "record_type: person\n"
        "display_name: Dummy Person\n"
        "relationships: []\n"
        "---\n",
        encoding="utf-8",
    )


def _insert_person(db_path: Path, person_id: str = "dummy_person") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO persons (id, display_name) VALUES (?, ?)",
            (person_id, "Dummy Person"),
        )
        conn.commit()


def _write_update_sources(processed_root: Path) -> None:
    medication = (
        processed_root / "dummy_person" / "Medications" / "2099-01-01-dummy_person-medications"
    )
    medication.parent.mkdir(parents=True, exist_ok=True)
    medication.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: 2099-01-01\n"
        "---\n\n"
        "## Medications\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
        "status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Synthetic Medicine | prescription |  | 10 mg | daily | active | "
        "2099-01-01 |  | fixture |\n",
        encoding="utf-8",
    )
    lab = processed_root / "dummy_person" / "Labs" / "2099-01-01-dummy_person-labs"
    lab.parent.mkdir(parents=True, exist_ok=True)
    lab.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: lab_panel\n"
        "observed_at: 2099-01-01\n"
        "source_type: user_update\n"
        "---\n\n"
        "## Results\n\n"
        "| test_name | loinc_code | value | unit | reference_range_low | "
        "reference_range_high | observed_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| Synthetic Analyte |  | 5 | unit | 1 | 10 | 2099-01-01 |\n",
        encoding="utf-8",
    )
    history = processed_root / "dummy_person" / "History" / "2099-01-01-dummy_person-history"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: user_update\n"
        "category: history\n"
        "date: 2099-01-01\n"
        "---\n\n"
        "## History\n\n"
        "| Description | Category | Date | Details |\n"
        "| --- | --- | --- | --- |\n"
        "| Synthetic history fact | finding | 2099-01-01 | fixture |\n",
        encoding="utf-8",
    )


def _disable_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "refresh_lab_dependent_reports",
        "write_current_medications_report",
        "write_medication_conflicts_report",
        "refresh_medication_dependent_reports",
        "refresh_fhir_dependent_export",
        "write_medical_history_summary_report",
    ):
        monkeypatch.setattr(f"famed.authority_replay.{name}", lambda *_args: None)


def test_authority_overlay_replays_updates_then_active_corrections_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reports(monkeypatch)
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    processed_root = tmp_path / "processed"
    _write_update_sources(processed_root)
    original_snapshots = {
        path: path.read_bytes() for path in processed_root.rglob("*") if path.is_file()
    }
    correction_note = (
        source_vault
        / "Records"
        / "dummy_person"
        / "Medications"
        / "2099-01-02-dummy-person-corrections.md"
    )
    correction_note.parent.mkdir(parents=True, exist_ok=True)
    correction_note.write_text("synthetic correction note", encoding="utf-8")
    recovered_update_note = (
        source_vault
        / "Records"
        / "dummy_person"
        / "Medications"
        / "2099-01-03-dummy_person-medications.md"
    )
    recovered_update_note.parent.mkdir(parents=True, exist_ok=True)
    recovered_content = (
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: 2099-01-03\n"
        "source: user_update\n"
        "---\n\n"
        "## Medications\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
        "status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Recovered Medicine | prescription |  | 30 mg | daily | active | "
        "2099-01-03 |  | fixture |\n"
    )
    recovered_update_note.write_bytes(recovered_content.replace("\n", "\r\n").encode("utf-8"))

    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)
    _insert_person(source_db)
    _insert_person(target_db)
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, status,
                source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "correction-row",
                "dummy_person",
                "Corrected Medicine",
                "prescription",
                "20 mg",
                "active",
                "user_correction",
                "Records/dummy_person/Medications/2099-01-02-dummy-person-corrections.md",
            ),
        )
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, status,
                source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "user-update-row",
                "dummy_person",
                "Recovered Medicine",
                "prescription",
                "30 mg",
                "active",
                "user_update",
                "Records/dummy_person/Medications/2099-01-03-dummy_person-medications.md",
            ),
        )
        conn.commit()

    with sqlite3.connect(target_db) as conn:
        first = replay_authority_overlays(
            root=tmp_path,
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            processed_root=processed_root,
            conn=conn,
        )
        counts_after_first = (
            conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0],
        )
        audits_after_first = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        second = replay_authority_overlays(
            root=tmp_path,
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            processed_root=processed_root,
            conn=conn,
        )
        counts_after_second = (
            conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0],
        )
        audits_after_second = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        correction = conn.execute(
            "SELECT dose, source_type FROM medications WHERE id = 'correction-row'"
        ).fetchone()

    assert first.update_sources == 4
    assert first.recovered_update_sources == 1
    assert first.update_rows == 4
    assert first.correction_rows == 1
    assert second.update_sources == 4
    assert second.recovered_update_sources == 0
    assert second.update_rows == 0
    assert counts_after_first == (3, 1)
    assert counts_after_second == counts_after_first
    assert audits_after_second == audits_after_first
    assert correction == ("20 mg", "user_correction")
    assert (target_vault / recovered_update_note.relative_to(source_vault)).read_bytes() == (
        recovered_update_note.read_bytes()
    )
    assert (target_vault / correction_note.relative_to(source_vault)).read_text(
        encoding="utf-8"
    ) == "synthetic correction note"
    for snapshot_path, original_bytes in original_snapshots.items():
        assert snapshot_path.read_bytes() == original_bytes
        relative = snapshot_path.relative_to(processed_root)
        target_note = (
            target_vault
            / "Records"
            / relative.parts[0]
            / relative.parts[1]
            / f"{snapshot_path.name}.md"
        )
        assert target_note.read_bytes() == original_bytes


def test_authority_overlay_rolls_back_db_and_vault_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    original_note = target_vault / "Records" / "dummy_person" / "existing.md"
    original_note.parent.mkdir(parents=True, exist_ok=True)
    original_note.write_text("before overlay", encoding="utf-8")
    processed_root = tmp_path / "processed"
    _write_update_sources(processed_root)
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)
    _insert_person(source_db)
    _insert_person(target_db)

    def fail_refresh(_conn: sqlite3.Connection, vault: Path, person_id: str) -> None:
        partial = vault / "Records" / person_id / "Reports" / "partial.md"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text("partial", encoding="utf-8")
        raise ValueError("overlay refresh failed")

    monkeypatch.setattr("famed.authority_replay.refresh_lab_dependent_reports", fail_refresh)
    with sqlite3.connect(target_db) as conn:
        with pytest.raises(ValueError, match="overlay refresh failed"):
            replay_authority_overlays(
                root=tmp_path,
                source_vault=source_vault,
                source_db=source_db,
                target_vault=target_vault,
                processed_root=processed_root,
                conn=conn,
            )
        assert conn.execute("SELECT COUNT(*) FROM medications").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (0,)

    assert original_note.read_text(encoding="utf-8") == "before overlay"
    assert not (target_vault / "Records" / "dummy_person" / "Reports" / "partial.md").exists()


@pytest.mark.parametrize("target_snapshot_exists", [True, False])
def test_authority_overlay_preserves_retained_snapshot_over_vault_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_snapshot_exists: bool,
) -> None:
    _disable_reports(monkeypatch)
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    relative_note = Path("Records/dummy_person/Medications/2099-01-01-dummy_person-medications.md")
    source_note = source_vault / relative_note
    source_note.parent.mkdir(parents=True, exist_ok=True)
    source_note.write_text(
        "---\nperson_id: dummy_person\nrecord_type: medication\n"
        "category: medications\ndate: 2099-01-01\n---\n\n"
        "## Medications\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
        "status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Lower Vault Medicine | prescription |  |  |  | active | 2099-01-01 |  |  |\n",
        encoding="utf-8",
    )
    processed_root = tmp_path / "processed"
    retained = processed_root / "dummy_person/Medications/2099-01-01-dummy_person-medications"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_text(
        source_note.read_text(encoding="utf-8").replace(
            "Lower Vault Medicine", "Higher Retained Medicine"
        ),
        encoding="utf-8",
    )
    retained_bytes = retained.read_bytes()
    source_processed_root = tmp_path / "source-processed"
    source_retained = (
        source_processed_root / "dummy_person/Medications/2099-01-01-dummy_person-medications"
    )
    source_retained.parent.mkdir(parents=True)
    source_retained.write_bytes(retained_bytes)
    if not target_snapshot_exists:
        retained.unlink()
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)
    _insert_person(source_db)
    _insert_person(target_db)
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            "INSERT INTO medications "
            "(id, person_id, medication_name, source_type, source_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("row", "dummy_person", "Lower Vault Medicine", "user_update", str(relative_note)),
        )
        conn.commit()
    with sqlite3.connect(target_db) as conn:
        summary = replay_authority_overlays(
            root=tmp_path,
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            processed_root=processed_root,
            source_processed_root=source_processed_root,
            conn=conn,
        )
        medication_name = conn.execute("SELECT medication_name FROM medications").fetchone()[0]

    assert summary.recovered_update_sources == (0 if target_snapshot_exists else 1)
    assert medication_name == "Higher Retained Medicine"
    assert retained.read_bytes() == retained_bytes
    assert (target_vault / relative_note).read_bytes() == retained_bytes


def test_authority_overlay_refreshes_target_only_recovered_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reports(monkeypatch)
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    relative_note = Path("Records/dummy_person/Medications/2099-01-01-dummy_person-medications.md")
    source_note = source_vault / relative_note
    source_note.parent.mkdir(parents=True, exist_ok=True)
    source_note.write_text(
        "---\nperson_id: dummy_person\nrecord_type: medication\n"
        "category: medications\ndate: 2099-01-01\n---\n\n"
        "## Medications\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
        "status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Current Vault Medicine | prescription |  |  |  | active | 2099-01-01 |  | "
        "current evidence |\n",
        encoding="utf-8",
    )
    source_processed_root = tmp_path / "source-processed"
    source_processed_root.mkdir()
    processed_root = tmp_path / "target-processed"
    retained = processed_root / "dummy_person/Medications/2099-01-01-dummy_person-medications"
    retained.parent.mkdir(parents=True)
    retained.write_text(
        source_note.read_text(encoding="utf-8").replace(
            "Current Vault Medicine", "Stale Recovered Medicine"
        ),
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)
    _insert_person(source_db)
    _insert_person(target_db)
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            "INSERT INTO medications "
            "(id, person_id, medication_name, source_type, source_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("row", "dummy_person", "Current Vault Medicine", "user_update", str(relative_note)),
        )
        conn.commit()

    with sqlite3.connect(target_db) as conn:
        replay_authority_overlays(
            root=tmp_path,
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            processed_root=processed_root,
            source_processed_root=source_processed_root,
            conn=conn,
        )

    assert retained.read_bytes() == source_note.read_bytes()
    assert (target_vault / relative_note).read_bytes() == source_note.read_bytes()


def test_authority_overlay_recovers_history_update_without_database_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_reports(monkeypatch)
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    relative_note = Path("Records/dummy_person/History/2099-01-01-dummy_person-history.md")
    source_note = source_vault / relative_note
    source_note.parent.mkdir(parents=True, exist_ok=True)
    source_note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: user_update\n"
        "category: history\n"
        "date: 2099-01-01\n"
        "---\n\n"
        "## History\n\n"
        "| Description | Category | Date | Details |\n"
        "| --- | --- | --- | --- |\n"
        "| Synthetic history fact | finding | 2099-01-01 | fixture |\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)
    _insert_person(source_db)
    _insert_person(target_db)
    processed_root = tmp_path / "processed"

    assert authority_update_snapshot_paths(
        source_vault=source_vault,
        source_db=source_db,
    ) == {Path("dummy_person/History/2099-01-01-dummy_person-history")}

    with sqlite3.connect(target_db) as conn:
        summary = replay_authority_overlays(
            root=tmp_path,
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            processed_root=processed_root,
            conn=conn,
        )

    assert summary.recovered_update_sources == 1
    assert summary.update_rows == 1
    assert (target_vault / relative_note).read_bytes() == source_note.read_bytes()


def test_authority_overlay_excludes_source_derived_history_from_user_authority(
    tmp_path: Path,
) -> None:
    source_vault = tmp_path / "source-vault"
    _write_person(source_vault)
    relative_note = Path("Records/dummy_person/History/2099-01-01-dummy_person-history.md")
    source_note = source_vault / relative_note
    source_note.parent.mkdir(parents=True, exist_ok=True)
    source_note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: user_update\n"
        "category: history\n"
        "date: 2099-01-01\n"
        "source_document: data/processed/dummy_person/HealthSummary/synthetic.zip\n"
        "---\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    initialize_database(source_db)
    _insert_person(source_db)

    assert (
        authority_update_snapshot_paths(
            source_vault=source_vault,
            source_db=source_db,
        )
        == set()
    )


def test_authority_snapshot_recovery_rejects_person_path_mismatch(tmp_path: Path) -> None:
    source_vault = tmp_path / "source-vault"
    note = source_vault / "Records" / "dummy_person" / "Medications" / "update.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\n"
        "person_id: different_dummy\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: 2099-01-01\n"
        "---\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    initialize_database(source_db)
    _insert_person(source_db)
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, status,
                source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "user-update-row",
                "dummy_person",
                "Synthetic Medicine",
                "prescription",
                "active",
                "user_update",
                "Records/dummy_person/Medications/update.md",
            ),
        )
        conn.commit()

    with pytest.raises(ReprocessError, match="person path does not match"):
        authority_update_snapshot_paths(source_vault=source_vault, source_db=source_db)


@pytest.mark.parametrize(
    ("folder", "front_matter", "table"),
    [
        (
            "Medications",
            "record_type: lab_panel\ncategory: medications\n",
            "medications",
        ),
        (
            "Labs",
            "record_type: lab_panel\nsource_type: manual\n",
            "lab_results",
        ),
    ],
)
def test_authority_snapshot_recovery_rejects_wrong_category_contract(
    tmp_path: Path,
    folder: str,
    front_matter: str,
    table: str,
) -> None:
    source_vault = tmp_path / "source-vault"
    suffix = "medications" if folder == "Medications" else "labs"
    relative_note = (
        Path("Records") / "dummy_person" / folder / f"2099-01-01-dummy_person-{suffix}.md"
    )
    note = source_vault / relative_note
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        f"---\nperson_id: dummy_person\n{front_matter}date: 2099-01-01\n---\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    initialize_database(source_db)
    _insert_person(source_db)
    with sqlite3.connect(source_db) as conn:
        if table == "medications":
            conn.execute(
                "INSERT INTO medications "
                "(id, person_id, medication_name, source_type, source_path) "
                "VALUES (?, ?, ?, ?, ?)",
                ("row", "dummy_person", "Synthetic Medicine", "user_update", str(relative_note)),
            )
        else:
            conn.execute(
                "INSERT INTO lab_results "
                "(id, person_id, test_name, observed_at, source_type, source_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "row",
                    "dummy_person",
                    "Synthetic Analyte",
                    "2099-01-01",
                    "user_update",
                    str(relative_note),
                ),
            )
        conn.commit()

    with pytest.raises(ReprocessError, match="update note contract is invalid"):
        authority_update_snapshot_paths(source_vault=source_vault, source_db=source_db)


@pytest.mark.parametrize(
    ("folder", "filename", "front_matter", "error"),
    [
        (
            "Medications",
            "2099-01-01-dummy_person-medications",
            "record_type: lab_panel\ncategory: medications\nsource_type: user_update\n",
            "medication update note contract is invalid",
        ),
        (
            "Labs",
            "2099-01-01-dummy_person-labs",
            "record_type: medication\nsource_type: user_update\n",
            "lab update note contract is invalid",
        ),
        (
            "History",
            "2099-01-01-dummy_person-history",
            "record_type: user_update\ncategory: medications\n",
            "history update note contract is invalid",
        ),
        (
            "History",
            "noncanonical-update",
            "record_type: user_update\ncategory: history\n",
            "retained user-update path is not category-native",
        ),
    ],
)
def test_authority_overlay_rejects_invalid_existing_snapshot_before_mutation(
    tmp_path: Path,
    folder: str,
    filename: str,
    front_matter: str,
    error: str,
) -> None:
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    processed_root = tmp_path / "processed"
    snapshot = processed_root / "dummy_person" / folder / filename
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        f"---\nperson_id: dummy_person\n{front_matter}date: 2099-01-01\n---\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)
    _insert_person(source_db)
    _insert_person(target_db)

    with sqlite3.connect(target_db) as conn:
        with pytest.raises(ReprocessError, match=error):
            replay_authority_overlays(
                root=tmp_path,
                source_vault=source_vault,
                source_db=source_db,
                target_vault=target_vault,
                processed_root=processed_root,
                conn=conn,
            )
        assert conn.execute("SELECT COUNT(*) FROM medications").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (0,)
    assert not (target_vault / "Records" / "dummy_person" / folder / f"{filename}.md").exists()


def test_authority_overlay_replays_history_refutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    from famed.history_sync import write_history_refutations

    _disable_reports(monkeypatch)
    source_vault = tmp_path / "source-vault"
    target_vault = tmp_path / "target-vault"
    _write_person(source_vault)
    _write_person(target_vault)
    write_history_refutations(
        source_vault,
        "dummy_person",
        [{"description": "Synthetic condition", "recorded_date": "2099-02-01", "reason": "x"}],
    )
    rebuilt_note = target_vault / "Records/dummy_person/History/visit-history-review.md"
    rebuilt_note.parent.mkdir(parents=True, exist_ok=True)
    rebuilt_note.write_text(
        "---\nperson_id: dummy_person\nrecord_type: history\nevent_date: '2099-02-01'\n"
        "verification_status: accepted\naccepted_facts:\n"
        "- description: Synthetic condition\n  category: diagnosis\n"
        "- description: Synthetic finding\n  category: finding\n---\n\n# History Review\n",
        encoding="utf-8",
    )
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    initialize_database(source_db)
    initialize_database(target_db)

    with sqlite3.connect(target_db) as conn:
        replay_authority_overlays(
            root=tmp_path,
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            processed_root=tmp_path / "processed",
            conn=conn,
        )

    corrections = "Records/dummy_person/History/history-corrections.md"
    assert (target_vault / corrections).read_bytes() == (source_vault / corrections).read_bytes()
    text = rebuilt_note.read_text(encoding="utf-8")
    fm = yaml.safe_load(text[3 : text.find("\n---", 3)])
    assert [fact["description"] for fact in fm["accepted_facts"]] == ["Synthetic finding"]
    assert [fact["description"] for fact in fm["refuted_facts"]] == ["Synthetic condition"]
    history_index = target_vault / "Records/dummy_person/History/_index.md"
    assert "[[history-corrections]]" in history_index.read_text(encoding="utf-8")
