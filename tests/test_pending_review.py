from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from famed.import_labs import quarantine_lab_import_error
from famed.medication_sync import quarantine_medication_import_error
from famed.pending_review import (
    PendingReviewItem,
    ReviewKind,
    derive_pending_review_snapshot,
    refresh_pending_review_reports,
    render_pending_review_report,
    validate_pending_review_item,
)
from famed.person_sync import validate_note_front_matter
from famed.setup import SCHEMA_SQL
from famed.storage_contract import (
    AUDIT_LAB_OBSERVATION_REJECTED,
    STATE_AWAITING_USER_DECISION,
    STATE_QUARANTINED,
    log_audit_event,
)


def _scaffold(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    conn = sqlite3.connect(db_dir / "famed.db")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('person_one')")
    vault_root = tmp_path / "famed-vault"
    person_note = vault_root / "Persons" / "person_one.md"
    person_note.parent.mkdir(parents=True)
    person_note.write_text(
        "---\nperson_id: person_one\ndisplay_name: Example Person\nrecord_type: person\n---\n",
        encoding="utf-8",
    )
    return conn, vault_root


def _insert_medication(
    conn: sqlite3.Connection,
    *,
    row_id: str,
    dose: str,
    source_path: str,
    source_type: str = "manual",
    medication_name: str = "Synthetic Medication",
) -> None:
    conn.execute(
        """
        INSERT INTO medications (
            id, person_id, medication_name, medication_type, dose, frequency,
            status, source_type, source_path
        ) VALUES (?, 'person_one', ?, 'prescription', ?, 'daily',
                  'active', ?, ?)
        """,
        (row_id, medication_name, dose, source_type, source_path),
    )


def test_contract_rejects_parent_traversal_source() -> None:
    item = PendingReviewItem(
        item_id="a" * 20,
        kind="inbox_source",
        person_id=None,
        subject="Synthetic source",
        reason="Pending ingestion.",
        source_paths=("../outside.pdf",),
        resolution_skill="fm-ingest-inbox",
        resolution_action="Process it.",
    )

    with pytest.raises(ValueError, match="repository-relative"):
        validate_pending_review_item(item)


def test_contract_rejects_unknown_review_kind() -> None:
    item = PendingReviewItem(
        item_id="a" * 20,
        kind=cast(ReviewKind, "unknown"),
        person_id=None,
        subject="Synthetic source",
        reason="Pending ingestion.",
        source_paths=("data/inbox/sample.pdf",),
        resolution_skill="fm-ingest-inbox",
        resolution_action="Process it.",
    )

    with pytest.raises(ValueError, match="unsupported pending review kind"):
        validate_pending_review_item(item)


def test_helper_refuses_missing_database_without_creating_one(
    tmp_path: Path,
) -> None:
    (tmp_path / "famed-vault" / "Persons").mkdir(parents=True)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "review_pending.py"),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "database does not exist" in result.stderr
    assert not (tmp_path / "data" / "famed.db").exists()


def test_helper_exposes_display_name_for_human_summary(tmp_path: Path) -> None:
    conn, _vault_root = _scaffold(tmp_path)
    conn.commit()
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "review_pending.py"),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["people"]["person_one"] == {
        "display_name": "Example Person",
        "items": [],
    }


def test_snapshot_uses_current_db_and_on_disk_state(tmp_path: Path) -> None:
    conn, vault_root = _scaffold(tmp_path)
    _insert_medication(
        conn,
        row_id="med-a",
        dose="10 mg",
        source_path="data/processed/person_one/Medications/source-a.txt",
    )
    _insert_medication(
        conn,
        row_id="med-b",
        dose="20 mg",
        source_path="data/processed/person_one/Medications/source-b.txt",
    )
    quarantine = tmp_path / "data" / "quarantine" / "person_one" / "Labs" / "sample.csv"
    quarantine.parent.mkdir(parents=True)
    quarantine.write_text("synthetic", encoding="utf-8")
    hidden = tmp_path / "data" / "quarantine" / "person_one" / ".DS_Store"
    hidden.write_text("metadata", encoding="utf-8")
    unresolved = tmp_path / "data" / "inbox" / "shared" / "sample.pdf"
    unresolved.parent.mkdir(parents=True)
    unresolved.write_text("synthetic", encoding="utf-8")
    log_audit_event(
        conn,
        event_type="lab_import_quarantined",
        subject_type="lab_source",
        subject_id="person_one",
        source_path="data/quarantine/person_one/Labs/sample.csv",
        status=STATE_QUARANTINED,
        message="Synthetic validation failure.",
    )
    log_audit_event(
        conn,
        event_type="lab_import_quarantined",
        subject_type="lab_source",
        subject_id="person_one",
        source_path="data/quarantine/person_one/Labs/removed.csv",
        status=STATE_QUARANTINED,
        message="Historical event for removed file.",
    )

    snapshot = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)

    person_items = snapshot.items_for_person("person_one")
    assert {item.kind for item in person_items} == {
        "medication_conflict",
        "quarantined_source",
    }
    assert len(person_items) == 2
    assert person_items[1].reason == "Synthetic validation failure."
    assert person_items[1].resolution_skill == "fm-ingest-inbox"
    assert [item.source_paths[0] for item in snapshot.unresolved_items] == [
        "data/inbox/shared/sample.pdf"
    ]
    assert all(".DS_Store" not in source for item in snapshot.items for source in item.source_paths)
    assert all(
        "removed.csv" not in source for item in snapshot.items for source in item.source_paths
    )


def test_snapshot_classifies_root_inbox_file_without_parent_category(tmp_path: Path) -> None:
    conn, vault_root = _scaffold(tmp_path)
    source = tmp_path / "data" / "inbox" / "root-source.pdf"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic", encoding="utf-8")

    snapshot = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)

    assert len(snapshot.unresolved_items) == 1
    assert snapshot.unresolved_items[0].subject == "inbox: root-source.pdf"
    assert snapshot.unresolved_items[0].source_paths == ("data/inbox/root-source.pdf",)


def test_snapshot_includes_current_history_review_notes(tmp_path: Path) -> None:
    conn, vault_root = _scaffold(tmp_path)
    history_dir = vault_root / "Records" / "person_one" / "History"
    history_dir.mkdir(parents=True)
    source_path = "data/processed/person_one/History/synthetic-report.pdf"
    (history_dir / "2099-01-01-synthetic-history-review.md").write_text(
        "---\n"
        "person_id: person_one\n"
        "record_type: history\n"
        "source_file_name: synthetic-report.pdf\n"
        f"source_path: {source_path}\n"
        "verification_status: needs_review\n"
        "accepted_facts: []\n"
        "---\n\n"
        "# History Review\n",
        encoding="utf-8",
    )
    (history_dir / "2099-01-02-unresolved-history-review.md").write_text(
        "---\n"
        "person_id: person_one\n"
        "record_type: history\n"
        "source_file_name: unresolved-report.pdf\n"
        "source_path: data/processed/person_one/History/unresolved-report.pdf\n"
        "verification_status: needs_review\n"
        "accepted_facts:\n"
        "- description: Synthetic accepted finding\n"
        "  category: diagnosis\n"
        "---\n\n"
        "# History Review\n",
        encoding="utf-8",
    )

    snapshot = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)

    items = snapshot.items_for_person("person_one")
    assert len(items) == 2
    assert items[0].kind == "history_source"
    assert items[0].source_paths == (source_path,)
    assert items[0].resolution_skill == "fm-history-extract"
    assert items[0].detail_report == ("Records/person_one/Reports/medical-history-summary.md")
    assert items[0].reason == (
        "Retained history source has not produced accepted structured facts."
    )
    assert items[1].reason == "Structured history facts remain marked for review."


def test_snapshot_surfaces_history_note_missing_source_provenance(tmp_path: Path) -> None:
    conn, vault_root = _scaffold(tmp_path)
    history_dir = vault_root / "Records" / "person_one" / "History"
    history_dir.mkdir(parents=True)
    note_path = history_dir / "2099-01-01-missing-source-history-review.md"
    note_path.write_text(
        "---\n"
        "person_id: person_one\n"
        "record_type: history\n"
        "verification_status: needs_review\n"
        "---\n\n"
        "# History Review\n",
        encoding="utf-8",
    )

    snapshot = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)

    item = snapshot.items_for_person("person_one")[0]
    assert item.reason == "History review note lacks retained source provenance."
    assert item.source_paths == (
        "famed-vault/Records/person_one/History/2099-01-01-missing-source-history-review.md",
    )
    assert item.resolution_action == (
        "Reconcile retained provenance before source-first history extraction."
    )


def test_user_authority_removes_medication_conflict_but_keeps_audit(tmp_path: Path) -> None:
    conn, vault_root = _scaffold(tmp_path)
    _insert_medication(conn, row_id="med-a", dose="10 mg", source_path="source-a")
    _insert_medication(conn, row_id="med-b", dose="20 mg", source_path="source-b")
    before = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)
    assert len(before.items_for_person("person_one")) == 1

    _insert_medication(
        conn,
        row_id="med-correction",
        dose="10 mg",
        source_path="Records/person_one/Medications/correction.md",
        source_type="user_correction",
    )
    log_audit_event(
        conn,
        event_type="medication_conflict_detected",
        subject_type="medication_conflict",
        subject_id="historical-conflict",
        status=STATE_AWAITING_USER_DECISION,
        message="Historical pending event remains append-only.",
    )

    after = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)
    assert after.items_for_person("person_one") == ()
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE subject_id = 'historical-conflict'"
    ).fetchone() == (1,)


def test_reports_are_idempotent_and_link_detail_without_inlining(tmp_path: Path) -> None:
    conn, vault_root = _scaffold(tmp_path)
    _insert_medication(
        conn,
        row_id="med-a",
        dose="10 mg",
        source_path="source-a",
        medication_name="Synthetic Medication 10 mg",
    )
    _insert_medication(
        conn,
        row_id="med-b",
        dose="20 mg",
        source_path="source-b",
        medication_name="Synthetic Medication 20 mg",
    )

    snapshot = refresh_pending_review_reports(conn, root=tmp_path, vault_root=vault_root)
    report_path = vault_root / "Records" / "person_one" / "Reports" / "pending-review.md"
    first = report_path.read_bytes()
    refresh_pending_review_reports(conn, root=tmp_path, vault_root=vault_root)

    report = report_path.read_text(encoding="utf-8")
    assert report_path.read_bytes() == first
    assert "pending_review_count: 1" in report
    assert "[[Records/person_one/Reports/medication-conflicts.md|" in report
    assert "10 mg" not in report
    assert "20 mg" not in report
    body = report.split("---", 2)[2]
    assert "# Pending Review — Example Person" in body
    assert "| person |" not in body
    assert "pending-review" in (report_path.parent / "_index.md").read_text(encoding="utf-8")
    assert validate_note_front_matter(report_path) == []
    assert len(snapshot.items_for_person("person_one")) == 1


@pytest.mark.parametrize(
    "quarantine_import_error",
    [quarantine_lab_import_error, quarantine_medication_import_error],
    ids=["lab", "medication"],
)
def test_person_scoped_quarantine_immediately_refreshes_report(
    tmp_path: Path,
    quarantine_import_error,
) -> None:
    conn, vault_root = _scaffold(tmp_path)
    source = tmp_path / "data" / "inbox" / "person_one" / "sample.csv"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic", encoding="utf-8")

    quarantine_path = quarantine_import_error(
        conn,
        tmp_path,
        tmp_path / "data" / "quarantine",
        "person_one",
        source,
        "csv",
        "Synthetic validation failure.",
    )

    report_path = vault_root / "Records" / "person_one" / "Reports" / "pending-review.md"
    report = report_path.read_text(encoding="utf-8")
    assert "pending_review_count: 1" in report
    assert "Synthetic validation failure." in report

    retained_source = tmp_path / quarantine_path
    processed_source = tmp_path / "data" / "processed" / "person_one" / retained_source.name
    processed_source.parent.mkdir(parents=True)
    retained_source.replace(processed_source)
    refresh_pending_review_reports(conn, root=tmp_path, vault_root=vault_root)

    refreshed = report_path.read_text(encoding="utf-8")
    assert "pending_review_count: 0" in refreshed
    assert "Synthetic validation failure." not in refreshed


def test_empty_report_is_explicitly_bounded_to_supported_adapters() -> None:
    report = render_pending_review_report("person_one", (), generated_on="2099-01-01")

    assert "verification_status: accepted" in report
    assert "pending_review_count: 0" in report
    assert "No supported current review items" in report


def test_empty_report_states_when_absence_was_established() -> None:
    """An empty queue is a dated observation, not a standing all-clear (ADR-043)."""
    report = render_pending_review_report("person_one", (), generated_on="2099-01-01")

    assert "No supported current review items as of 2099-01-01" in report
    assert "added since then are not counted" in report
    assert "`fm-review-pending`" in report


@pytest.mark.parametrize(
    "source_path",
    [
        "data/processed/person_one/History/source.xml",
        "data/processed/person_one/Labs/report.zip#DOC0001.XML",
        "data/processed/unknown_person/HealthSummary/report.zip#DOC0001.XML",
    ],
)
def test_unstructured_rejected_labs_are_reported_as_coverage_gap(
    tmp_path: Path, source_path: str
) -> None:
    conn, vault_root = _scaffold(tmp_path)
    log_audit_event(
        conn,
        event_type=AUDIT_LAB_OBSERVATION_REJECTED,
        subject_type="lab_result",
        subject_id="person_one",
        source_path=source_path,
        status=STATE_AWAITING_USER_DECISION,
        message="Two rejected observations were historically aggregated.",
    )

    snapshot = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault_root)

    assert snapshot.items == ()
    assert snapshot.unsupported_event_counts == (("lab_observation_rejected", 1),)
