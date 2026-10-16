from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from famed.clinical_interpreter import FACT_EXTRACTION_SCHEMA_VERSION, StaticFactInterpreter
from famed.history_sync import (
    HistoryReviewDocument,
    HistoryReviewSection,
    HistorySyncError,
    _ccda_immunization_mapping,
    _date_from_source_stem,
    _history_digest_chronic_label,
    _history_digest_label,
    _history_medical_area,
    _load_markdown_table_rows,
    _medication_identity_and_status,
    _render_facts_grouped,
    _suppress_unchanged_medication_repeats,
    accept_history_candidates,
    auto_accept_history_review_notes_for_source_prefix,
    backfill_history_reports,
    extract_ccda_lab_rows,
    extract_structured_history_facts_from_note,
    infer_person_id_from_processed_history_path,
    persist_history_document_review_note,
    remove_history_review_note_without_facts,
    write_medical_history_summary_report,
)
from famed.setup import SCHEMA_SQL
from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL


def _history_fact_extraction_payload(
    *,
    person_id: str = "dummy_alice",
    document_date: str = "2021-02-20",
    facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": 0.99},
        "category": "history",
        "category_confidence": 0.99,
        "facts": facts
        or [
            {
                "description": "Hypertension",
                "category": "diagnosis",
                "recorded_date": "2017-06-05",
                "confidence": 0.99,
                "evidence_snippet": "Hypertension",
            }
        ],
        "overall_confidence": 0.99,
        "evidence": [{"field": "person_id", "snippet": person_id, "confidence": 0.99}],
        "document_date": document_date,
    }


def test_history_person_path_can_use_explicit_misfiled_source_category(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "processed"
    source = processed_root / "dummy_alice" / "Labs" / "2099-01-01-ECG 12-LEAD.pdf"

    assert infer_person_id_from_processed_history_path(source, processed_root) is None
    assert (
        infer_person_id_from_processed_history_path(
            source,
            processed_root,
            source_category="Labs",
        )
        == "dummy_alice"
    )


def _make_history_note(
    history_dir: Path,
    filename: str,
    *,
    event_date: str | None,
    title: str,
    source_file_name: str,
    source_path: str | None = None,
    verification_status: str = "needs_review",
    record_type: str = "history",
    accepted_facts: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a minimal history review note and return its path."""
    fm: dict[str, Any] = {
        "person_id": history_dir.parent.name,
        "record_type": record_type,
        "source_file_name": source_file_name,
        "source_path": source_path
        or f"data/processed/{history_dir.parent.name}/History/{source_file_name}",
        "event_date": event_date,
        "title": title,
        "verification_status": verification_status,
    }
    if accepted_facts is not None:
        fm["accepted_facts"] = accepted_facts
    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
    content = f"---\n{yaml_block}\n---\n\n# History Review\n\nSome narrative.\n"
    note_path = history_dir / filename
    note_path.write_text(content, encoding="utf-8")
    return note_path


def _scaffold(tmp_path: Path, person_id: str) -> tuple[Path, Path]:
    """Return (vault_root, history_dir) with necessary directories created."""
    vault_root = tmp_path / "vault"
    history_dir = vault_root / "Records" / person_id / "History"
    history_dir.mkdir(parents=True)
    (vault_root / "Records" / person_id / "Reports").mkdir(parents=True)
    return vault_root, history_dir


def _write_person_note(vault_root: Path, person_id: str = "dummy_alice") -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    person_note.write_text(
        (
            f"---\nperson_id: {person_id}\nrecord_type: person\n"
            "display_name: Dummy Alice\n---\n# Dummy Alice\n"
        ),
        encoding="utf-8",
    )


def test_write_medical_history_summary_report_creates_report() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "2021-01-15-visit.md",
            event_date="2021-01-15",
            title="Annual Visit",
            source_file_name="visit.pdf",
        )

        count = write_medical_history_summary_report(vault_root, "dummy_alice")

        report = vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        assert report.exists()
        assert count == 1
        content = report.read_text(encoding="utf-8")
        assert "Annual Visit" in content
        assert "2021-01-15" in content
        assert "visit.pdf" not in content
        assert "needs_review" in content


def test_write_medical_history_summary_report_sorts_dated_before_undated() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "undated-note.md",
            event_date=None,
            title="Undated Visit",
            source_file_name="u.pdf",
        )
        _make_history_note(
            history_dir,
            "2021-03-01-note.md",
            event_date="2021-03-01",
            title="Bobh Visit",
            source_file_name="m.pdf",
        )
        _make_history_note(
            history_dir,
            "2020-06-15-note.md",
            event_date="2020-06-15",
            title="June Visit",
            source_file_name="j.pdf",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        june_pos = content.index("June Visit")
        bobh_pos = content.index("Bobh Visit")
        undated_pos = content.index("Undated Visit")
        assert june_pos < bobh_pos < undated_pos


def test_write_medical_history_summary_report_null_dates_shown_as_undated() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "no-date.md",
            event_date=None,
            title="Unknown Date",
            source_file_name="x.pdf",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "## Sources Still Needing Review" in content
        assert "- undated: Unknown Date (needs_review; no accepted facts)" in content


def test_history_summary_compacts_multiple_archive_members_to_archive_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        archive_path = "data/processed/dummy_alice/HealthSummary/health-summary.zip"
        member_one = f"{archive_path}#IHE_XDM/Dummy Alice/DOC0001.XML"
        member_two = f"{archive_path}#IHE_XDM/Dummy Alice/DOC0002.XML"
        for index, member_path in enumerate((member_one, member_two), start=1):
            note_path = _make_history_note(
                history_dir,
                f"2023-02-03-doc{index:04d}-history-review.md",
                event_date="2023-02-03",
                title=f"Visit {index}",
                source_file_name=f"health-summary.zip#DOC{index:04d}.XML",
                source_path=member_path,
                verification_status="accepted",
            )
            content = note_path.read_text(encoding="utf-8")
            fm, body = content.split("---\n", 2)[1:]
            front_matter = yaml.safe_load(fm) or {}
            front_matter["accepted_facts"] = [
                {
                    "description": "GERD",
                    "category": "diagnosis",
                    "recorded_date": "2023-02-03",
                    "confidence": "high",
                }
            ]
            front_matter["verification_status"] = "accepted"
            note_path.write_text(
                f"---\n{yaml.safe_dump(front_matter, sort_keys=False).strip()}\n---\n{body}",
                encoding="utf-8",
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Reported on: 2023-02-03" in content
        assert "#DOC0001.XML" not in content
        assert "#DOC0002.XML" not in content


def test_write_medical_history_summary_report_registers_in_reports_index() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "2021-01-15-note.md",
            event_date="2021-01-15",
            title="Visit",
            source_file_name="v.pdf",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        index = vault_root / "Records" / "dummy_alice" / "Reports" / "_index.md"
        assert index.exists()
        assert "medical-history-summary" in index.read_text(encoding="utf-8")


def test_write_medical_history_summary_report_is_idempotent() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "2021-01-15-note.md",
            event_date="2021-01-15",
            title="Visit",
            source_file_name="v.pdf",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")
        write_medical_history_summary_report(vault_root, "dummy_alice")

        report = vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        content = report.read_text(encoding="utf-8")
        assert content.count("Visit") == 1


def test_write_medical_history_summary_report_returns_zero_for_no_notes() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, _ = _scaffold(tmp_path, "dummy_alice")

        count = write_medical_history_summary_report(vault_root, "dummy_alice")

        assert count == 0
        report = vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        assert not report.exists()


def test_write_medical_history_summary_report_skips_index_and_non_history_notes() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        # _index.md should be skipped
        (history_dir / "_index.md").write_text(
            "---\nperson_id: dummy_alice\nrecord_type: history_index\n---\n# History\n",
            encoding="utf-8",
        )
        # A note with a different record_type should be skipped
        _make_history_note(
            history_dir,
            "not-history.md",
            event_date="2021-01-01",
            title="Something Else",
            source_file_name="x.pdf",
            record_type="labs",
        )
        _make_history_note(
            history_dir,
            "real-history.md",
            event_date="2021-06-01",
            title="Real Visit",
            source_file_name="r.pdf",
        )

        count = write_medical_history_summary_report(vault_root, "dummy_alice")

        assert count == 1
        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "Real Visit" in content
        assert "Something Else" not in content


def test_backfill_history_reports_covers_all_persons_with_history() -> None:

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root = tmp_path / "vault"

        for person_id in ("dummy_alice", "dummy_bob"):
            history_dir = vault_root / "Records" / person_id / "History"
            history_dir.mkdir(parents=True)
            (vault_root / "Records" / person_id / "Reports").mkdir(parents=True)
            _make_history_note(
                history_dir,
                "2021-01-01-note.md",
                event_date="2021-01-01",
                title=f"{person_id.capitalize()} Visit",
                source_file_name="v.pdf",
            )

        # dummy_carol has no history notes
        (vault_root / "Records" / "dummy_carol" / "History").mkdir(parents=True)
        (vault_root / "Records" / "dummy_carol" / "Reports").mkdir(parents=True)

        result = backfill_history_reports(vault_root)

        assert set(result.keys()) == {"dummy_alice", "dummy_bob"}
        assert result["dummy_alice"] == 1
        assert result["dummy_bob"] == 1
        assert not (
            vault_root / "Records" / "dummy_carol" / "Reports" / "medical-history-summary.md"
        ).exists()


# ---------------------------------------------------------------------------
# accept_history_candidates
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    return conn


def _make_history_note_full(
    history_dir: Path,
    filename: str,
    *,
    event_date: str | None,
    title: str,
    source_file_name: str,
    source_path: str = "data/processed/dummy_alice/History/visit.pdf",
    verification_status: str = "needs_review",
) -> Path:
    fm: dict[str, Any] = {
        "person_id": "dummy_alice",
        "record_type": "history",
        "source_file_name": source_file_name,
        "source_path": source_path,
        "event_date": event_date,
        "title": title,
        "verification_status": verification_status,
    }
    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
    content = (
        f"---\n{yaml_block}\n---\n\n"
        "# History Review\n\n"
        "[[_index|← History]]\n\n"
        "## Candidate Facts\n\n"
        "Some candidate text.\n\n"
        "## Review\n\n"
        "- Verification status: needs_review\n"
    )
    note_path = history_dir / filename
    note_path.write_text(content, encoding="utf-8")
    return note_path


def test_accept_history_candidates_updates_front_matter() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Annual Visit",
            source_file_name="visit.pdf",
        )
        _write_person_note(vault_root)
        conn = _make_db()

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Type 2 diabetes",
                    "category": "diagnosis",
                    "icd_10_code": "E11",
                    "recorded_date": "2021-01-15",
                },
            ],
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )

        text = note_path.read_text(encoding="utf-8")
        fm_end = text.find("\n---", 3)
        fm = yaml.safe_load(text[3:fm_end])
        assert fm["verification_status"] == "accepted"
        assert fm["accepted_facts"] == [
            {
                "description": "Type 2 diabetes",
                "category": "diagnosis",
                "icd_10_code": "E11",
                "recorded_date": "2021-01-15",
                "value": None,
                "unit": None,
                "confidence": None,
            }
        ]
        pending_report = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "pending-review.md"
        ).read_text(encoding="utf-8")
        assert "pending_review_count: 0" in pending_report


def test_remove_history_review_note_without_facts_deletes_no_use_note() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = history_dir / "2021-01-15-visit-history-review.md"
        fm = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.xml",
            "source_path": "data/processed/dummy_alice/History/visit.xml",
            "event_date": "2021-01-15",
            "title": "Visit",
            "verification_status": "needs_review",
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        note_path.write_text(
            (
                f"---\n{yaml_block}\n---\n\n"
                "# History Review\n\n"
                "## Narrative Sections\n\n"
                "Raw source text.\n\n"
                "## Review\n\n"
                "- Verification status: needs_review\n"
            ),
            encoding="utf-8",
        )
        _write_person_note(vault_root)
        conn = _make_db()

        remove_history_review_note_without_facts(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.xml",
        )

        assert not note_path.exists()
        index_text = (history_dir / "_index.md").read_text(encoding="utf-8")
        assert "2021-01-15-visit-history-review" not in index_text
        event = conn.execute(
            "SELECT event_type, source_path FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert event == (
            "history_source_reviewed_no_facts",
            "data/processed/dummy_alice/History/visit.xml",
        )
        pending_report = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "pending-review.md"
        ).read_text(encoding="utf-8")
        assert "pending_review_count: 0" in pending_report


def test_accept_history_candidates_appends_facts_section() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Annual Visit",
            source_file_name="visit.pdf",
        )
        conn = _make_db()

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Hypertension",
                    "category": "diagnosis",
                    "icd_10_code": "I10",
                    "recorded_date": None,
                }
            ],
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )

        text = note_path.read_text(encoding="utf-8")
        assert "## Accepted Structured Facts" in text
        assert "Hypertension" in text
        assert "I10" in text


def test_accept_history_candidates_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Annual Visit",
            source_file_name="visit.pdf",
        )
        conn = _make_db()
        rows = [
            {
                "description": "Hyperlipidemia",
                "category": "diagnosis",
                "icd_10_code": None,
                "recorded_date": "2021-01-15",
            }
        ]

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=rows,
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )
        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=rows,
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )

        text = note_path.read_text(encoding="utf-8")
        assert text.count("## Accepted Structured Facts") == 1
        assert text.count("Hyperlipidemia") >= 1


def test_accept_history_candidates_regenerates_report() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Annual Visit",
            source_file_name="visit.pdf",
        )
        conn = _make_db()

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Asthma",
                    "category": "diagnosis",
                    "icd_10_code": "J45",
                    "recorded_date": "2021-01-15",
                }
            ],
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )

        report = vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        assert report.exists()
        content = report.read_text(encoding="utf-8")
        assert "Asthma" in content
        assert "J45" in content
        assert "## Medical Areas" in content
        assert "#### Asthma (ICD-10 J45)" in content
        assert "Reported on: 2021-01-15" in content


def test_accept_history_candidates_refreshes_fhir_conditions() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Annual Visit",
            source_file_name="visit.pdf",
        )
        conn = _make_db()
        conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_alice', 'Dummy Alice')")

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Type 2 diabetes",
                    "category": "diagnosis",
                    "icd_10_code": "E11",
                    "recorded_date": "2021-01-15",
                },
                {
                    "description": "Colonoscopy",
                    "category": "procedure",
                    "recorded_date": "2021-01-15",
                },
            ],
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )

        export_path = vault_root / "Records" / "dummy_alice" / "Reports" / "fhir-export.json"
        bundle = json.loads(export_path.read_text(encoding="utf-8"))
        conditions = [
            entry["resource"]
            for entry in bundle["entry"]
            if entry["resource"]["resourceType"] == "Condition"
        ]
        assert [condition["code"]["text"] for condition in conditions] == ["Type 2 diabetes"]
        assert conditions[0]["code"]["coding"][0]["code"] == "E11"


def test_accept_history_candidates_raises_on_empty_rows() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Visit",
            source_file_name="v.pdf",
        )
        conn = _make_db()
        try:
            accept_history_candidates(
                conn=conn,
                vault_root=vault_root,
                note_path=note_path,
                accepted_rows=[],
                person_id="dummy_alice",
                source_path="data/processed/dummy_alice/History/v.pdf",
            )
            raise AssertionError("Expected HistorySyncError for empty accepted_rows")
        except HistorySyncError:
            pass


def test_accept_history_candidates_raises_on_missing_description() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Visit",
            source_file_name="v.pdf",
        )
        conn = _make_db()
        try:
            accept_history_candidates(
                conn=conn,
                vault_root=vault_root,
                note_path=note_path,
                accepted_rows=[{"description": "", "category": "diagnosis"}],
                person_id="dummy_alice",
                source_path="data/processed/dummy_alice/History/v.pdf",
            )
            raise AssertionError("Expected HistorySyncError for empty description")
        except HistorySyncError:
            pass


def test_write_medical_history_summary_report_shows_accepted_facts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-10",
            "title": "Cardiology Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "CAD",
                    "category": "diagnosis",
                    "icd_10_code": "I25",
                    "recorded_date": "2021-03-10",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-cardio-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\nNarrative here.\n",
            encoding="utf-8",
        )

        count = write_medical_history_summary_report(vault_root, "dummy_alice")

        assert count == 1
        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "CAD" in content
        assert "I25" in content
        assert "## Medical Areas" in content
        assert "#### CAD (ICD-10 I25)" in content
        assert "Reported on: 2021-03-10" in content


def test_write_medical_history_summary_report_omits_empty_user_update_from_review_sources() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "user_update",
            "category": "history",
            "date": "2023-05-30",
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2023-05-30-dummy_alice-history.md").write_text(
            f"---\n{yaml_block}\n---\n\n"
            "# History Update - 2023-05-30\n\n"
            "## History\n\n"
            "| Description | Category | Date | Details |\n"
            "| --- | --- | --- | --- |\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "## Sources Still Needing Review" not in content
        assert "History Update" not in content


def test_write_medical_history_summary_report_groups_facts_by_medical_topic() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "chart.pdf",
            "source_path": "data/processed/dummy_alice/History/chart.pdf",
            "event_date": "2021-03-10",
            "title": "Annual Checkup",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Type 2 diabetes",
                    "category": "diagnosis",
                    "icd_10_code": "E11",
                    "recorded_date": "2019-04-01",
                },
                {
                    "description": "Type 2 diabetes",
                    "category": "diagnosis",
                    "icd_10_code": "E11",
                    "recorded_date": "2021-03-10",
                },
                {
                    "description": "Colonoscopy",
                    "category": "procedure",
                    "recorded_date": "2020-06-15",
                },
                {
                    "description": "Repeat colonoscopy in 5 years",
                    "category": "follow_up",
                    "recorded_date": "2020-06-15",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-checkup-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        report = vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        content = report.read_text(encoding="utf-8")
        diabetes_heading = content.index("#### Type 2 diabetes (ICD-10 E11)")
        assert (
            content.index("Reported on: 2019-04-01, 2021-03-10", diabetes_heading)
            > diabetes_heading
        )
        assert "### Gastroenterology" in content
        assert "#### Colonoscopy and Colon Polyps" in content
        assert "Reported on: 2020-06-15" in content
        assert "Repeat colonoscopy in 5 years" in content
        assert "### 2021-03-10" not in content


def test_write_medical_history_summary_report_renders_structured_digest() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "chart.pdf",
            "source_path": "data/processed/dummy_alice/History/chart.pdf",
            "event_date": "2021-03-10",
            "title": "Annual Checkup",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "High blood pressure",
                    "category": "diagnosis",
                    "recorded_date": "2019-04-01",
                },
                {
                    "description": "Hypertension",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                },
                {
                    "description": "Type 2 diabetes",
                    "category": "diagnosis",
                    "recorded_date": "2018-06-15",
                },
                {
                    "description": "Pneumonia",
                    "category": "diagnosis",
                    "recorded_date": "2021-02-01",
                },
                {
                    "description": "Colonoscopy",
                    "category": "procedure",
                    "recorded_date": "2020-06-15",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-checkup-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        digest_pos = content.index("## Digest")
        medical_areas_pos = content.index("## Medical Areas")
        assert digest_pos < medical_areas_pos
        digest_section = content[digest_pos:medical_areas_pos]
        assert "Hypertension" in digest_section
        assert "First noted: 2019-04-01. Last mentioned: 2021-03-10." in digest_section
        assert "Diabetes / prediabetes" in digest_section
        assert "First noted: 2018-06-15." in digest_section
        assert "Pneumonia" not in digest_section
        assert "Colonoscopy" not in digest_section


def test_write_medical_history_summary_report_digest_sorted_chronologically() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "chart.pdf",
            "source_path": "data/processed/dummy_alice/History/chart.pdf",
            "event_date": "2021-03-10",
            "title": "Annual Checkup",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Anxiety",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-01",
                },
                {
                    "description": "GERD",
                    "category": "diagnosis",
                    "recorded_date": "2017-01-01",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-checkup-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        digest_pos = content.index("## Digest")
        medical_areas_pos = content.index("## Medical Areas")
        digest_section = content[digest_pos:medical_areas_pos]

        # "GERD" (first noted 2020) precedes "Anxiety" (first noted 2024)
        # chronologically, even though alphabetically Anxiety < GERD.
        assert digest_section.index("GERD") < digest_section.index("Anxiety")


def test_write_medical_history_summary_report_routes_psychiatric_injury_respiratory_areas() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "chart.pdf",
            "source_path": "data/processed/dummy_alice/History/chart.pdf",
            "event_date": "2021-03-10",
            "title": "Annual Checkup",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Anxiety about health",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-01",
                },
                {
                    "description": "Attention deficit hyperactivity disorder (ADHD)",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-02",
                },
                {
                    "description": "Closed displaced fracture of shaft of right clavicle",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-03",
                },
                {
                    "description": "Fall",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-04",
                },
                {
                    "description": "Cough, unspecified type",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-05",
                },
                {
                    "description": "Age-related osteoporosis without current pathological fracture",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-06",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-checkup-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")

        assert "### Psychiatry" in content
        assert "#### Mental Health" in content
        assert "### Musculoskeletal" in content
        assert "#### Injury" in content
        assert "### Respiratory" in content
        assert "#### Respiratory Symptoms" in content

        def section(start_marker: str) -> str:
            start = content.index(start_marker)
            rest = content[start + len(start_marker) :]
            end_offset = next(
                (rest.index(m) for m in ("\n### ", "\n#### ") if m in rest),
                len(rest),
            )
            return rest[:end_offset]

        assert "Anxiety about health" in section("### Psychiatry")
        assert "Attention deficit hyperactivity disorder" in section("### Psychiatry")

        injury_body = section("#### Injury")
        assert "Closed displaced fracture of shaft of right clavicle" in injury_body
        assert "Fall" in injury_body
        # A negative/qualifier mention of "fracture" (osteoporosis without a
        # current fracture) must not be swept into the Injury bucket.
        assert "osteoporosis" not in injury_body.casefold()

        assert "Cough, unspecified type" in section("### Respiratory")


def test_write_medical_history_summary_report_digest_shows_allowlist_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "chart.pdf",
            "source_path": "data/processed/dummy_alice/History/chart.pdf",
            "event_date": "2021-03-10",
            "title": "Annual Checkup",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Pneumonia",
                    "category": "diagnosis",
                    "recorded_date": "2021-02-01",
                },
                {
                    "description": "Colonoscopy",
                    "category": "procedure",
                    "recorded_date": "2020-06-15",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-checkup-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "## Digest" in content
        assert (
            "- No active chronic background conditions identified"
            " from the current conservative allowlist." in content
        )


def test_write_medical_history_summary_report_deduplicates_same_grouped_fact_across_notes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        for filename, title, source_file_name in (
            ("2021-03-10-visit-note-history-review.md", "visitNote", "visitNote.pdf"),
            ("2021-03-10-visit-summary-history-review.md", "visitSummary", "visitSummary.pdf"),
        ):
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": source_file_name,
                "source_path": f"data/processed/dummy_alice/History/{source_file_name}",
                "event_date": "2021-03-10",
                "title": title,
                "verification_status": "accepted",
                "accepted_facts": [
                    {
                        "description": "GERD",
                        "category": "diagnosis",
                        "icd_10_code": "K21.9",
                        "recorded_date": "2021-03-10",
                    },
                ],
            }
            yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
            (history_dir / filename).write_text(
                f"---\n{yaml_block}\n---\n\n# History Review\n\n",
                encoding="utf-8",
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert content.count("GERD (ICD-10 K21.9)") == 1
        assert "Reported on: 2021-03-10" in content


def test_write_medical_history_summary_report_deduplicates_same_dated_fact_across_notes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        for filename, title, source_file_name in (
            ("2021-03-10-visit-note-history-review.md", "visitNote", "visitNote.pdf"),
            ("2021-03-10-visit-summary-history-review.md", "visitSummary", "visitSummary.pdf"),
        ):
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": source_file_name,
                "source_path": f"data/processed/dummy_alice/History/{source_file_name}",
                "event_date": "2021-03-10",
                "title": title,
                "verification_status": "accepted",
                "accepted_facts": [
                    {
                        "description": "Omeprazole 20 mg daily",
                        "category": "medication",
                        "recorded_date": "2021-03-10",
                        "value": "active",
                    },
                ],
            }
            yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
            (history_dir / filename).write_text(
                f"---\n{yaml_block}\n---\n\n# History Review\n\n",
                encoding="utf-8",
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert content.count("Omeprazole 20 mg daily — active") == 1
        assert "Reported on: 2021-03-10" in content


def test_history_summary_deduplicates_same_day_mammogram_screening_aliases() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_specs = (
            (
                "2022-11-05-mammogram-history-review.md",
                "mammogram.pdf",
                [
                    {
                        "description": (
                            "Mammogram (Bilateral Screening Tomosynthesis) "
                            "at Example Medical Imaging"
                        ),
                        "category": "screening",
                        "recorded_date": "2022-11-05",
                    },
                    {
                        "description": "Breast cancer screening by mammogram",
                        "category": "screening",
                        "recorded_date": "2022-11-05",
                    },
                    {
                        "description": (
                            "Screening Result: BI-RADS 2 (Benign) -- "
                            "No mammographic evidence of malignancy"
                        ),
                        "category": "imaging_finding",
                        "recorded_date": "2022-11-05",
                    },
                ],
            ),
            (
                "2023-06-01-summary-history-review.md",
                "summary.xml",
                [
                    {
                        "description": "Screening mammogram for breast cancer",
                        "category": "screening",
                        "recorded_date": "2022-11-05",
                    },
                ],
            ),
        )
        for filename, source_file_name, accepted_facts in note_specs:
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": source_file_name,
                "source_path": f"data/processed/dummy_alice/History/{source_file_name}",
                "event_date": filename[:10],
                "title": "Summary of Care",
                "verification_status": "accepted",
                "accepted_facts": accepted_facts,
            }
            yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
            (history_dir / filename).write_text(
                f"---\n{yaml_block}\n---\n\n# History Review\n\n",
                encoding="utf-8",
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Mammogram (Bilateral Screening Tomosynthesis)" in content
        assert "Breast cancer screening by mammogram" not in content
        assert "Screening mammogram for breast cancer" not in content
        assert "Screening Result: BI-RADS 2 (Benign)" in content


def test_history_summary_keeps_distinct_same_day_screenings() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-10",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Encounter for screening for malignant neoplasm of colon",
                    "category": "screening",
                    "recorded_date": "2021-03-10",
                },
                {
                    "description": "Encounter for screening for malignant neoplasm of prostate",
                    "category": "screening",
                    "recorded_date": "2021-03-10",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Encounter for screening for malignant neoplasm of colon" in content
        assert "Encounter for screening for malignant neoplasm of prostate" in content


def test_history_summary_deduplicates_equivalent_follow_up_facts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_specs = (
            (
                "2021-03-10-visit-note-history-review.md",
                "visitNote",
                "visitNote.pdf",
                "Colonoscopy ordered (colon cancer screening)",
            ),
            (
                "2021-03-10-visit-summary-history-review.md",
                "visitSummary",
                "visitSummary.pdf",
                "Colonoscopy ordered",
            ),
        )
        for filename, title, source_file_name, description in note_specs:
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": source_file_name,
                "source_path": f"data/processed/dummy_alice/History/{source_file_name}",
                "event_date": filename[:10],
                "title": title,
                "verification_status": "accepted",
                "accepted_facts": [
                    {
                        "description": description,
                        "category": "follow_up",
                        "recorded_date": filename[:10],
                    },
                ],
            }
            yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
            (history_dir / filename).write_text(
                f"---\n{yaml_block}\n---\n\n# History Review\n\n",
                encoding="utf-8",
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert content.count("Colonoscopy ordered") == 1
        assert "Colonoscopy ordered (colon cancer screening)" in content
        assert "Reported on: 2021-03-10" in content


def test_write_medical_history_summary_report_groups_related_knee_and_colonoscopy_items() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-10",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Right knee pain",
                    "category": "diagnosis",
                    "recorded_date": "2020-11-15",
                },
                {
                    "description": "Small popliteal cyst (MRI)",
                    "category": "imaging_finding",
                    "recorded_date": "2020-11-22",
                },
                {
                    "description": "Colonoscopy",
                    "category": "procedure",
                    "recorded_date": "2020-01-09",
                },
                {
                    "description": "Tubular adenoma, colon proximal sigmoid",
                    "category": "diagnosis",
                    "recorded_date": "2020-01-09",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "### Musculoskeletal" in content
        knee_heading = content.index("#### Right Knee")
        assert content.index("Reported on: 2020-11-15, 2020-11-22", knee_heading) > knee_heading
        assert content.index("Right knee pain", knee_heading) > knee_heading
        assert content.index("Small popliteal cyst (MRI)", knee_heading) > knee_heading
        gi_heading = content.index("#### Colonoscopy and Colon Polyps")
        assert content.index("Reported on: 2020-01-09", gi_heading) > gi_heading
        assert content.index("Colonoscopy", gi_heading) > gi_heading
        assert content.index("Tubular adenoma, colon proximal sigmoid", gi_heading) > gi_heading


def test_write_medical_history_summary_report_moves_history_entries_to_context() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-10",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Right knee pain",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                },
                {
                    "description": "History of right knee osteoarthritis",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                    "confidence": "medium",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Right Knee")
        events_pos = content.index("Events", heading)
        context_pos = content.index("Historical context", heading)
        assert events_pos < context_pos
        assert content.index("Right knee pain", events_pos) > events_pos
        assert content.index("History of right knee osteoarthritis", context_pos) > context_pos


def test_write_medical_history_summary_report_hides_negative_boilerplate_facts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-10",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "No active problems documented",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                    "confidence": "medium",
                },
                {
                    "description": "No medications documented",
                    "category": "medication",
                    "recorded_date": "2021-03-10",
                    "confidence": "medium",
                },
                {
                    "description": "No known allergies documented",
                    "category": "allergy",
                    "recorded_date": "2021-03-10",
                    "confidence": "medium",
                },
                {
                    "description": "GERD",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "GERD" in content
        assert "No active problems documented" not in content
        assert "No medications documented" not in content
        assert "No known allergies documented" not in content


def test_write_medical_history_summary_report_routes_heartburn_and_preventive_items() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-10",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Heartburn",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": (
                        "Encounter for general adult medical examination without abnormal findings"
                    ),
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Preventative health care",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Encounter for breast and pelvic examination",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Well woman exam with routine gynecological exam",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Physical exam",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Other specified postprocedural states",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Encounter for screening for malignant neoplasm of prostate",
                    "category": "screening",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
                {
                    "description": "Postnasal drip",
                    "category": "diagnosis",
                    "recorded_date": "2019-08-23",
                    "confidence": "medium",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-10-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        gi_heading = content.index("#### Upper GI / Reflux")
        assert content.index("Heartburn", gi_heading) > gi_heading
        exam_history_heading = content.index("#### Exam History")
        assert content.index("Reported on: 2019-08-23", exam_history_heading) > exam_history_heading
        assert (
            content.index("Preventative health care", exam_history_heading) > exam_history_heading
        )
        assert (
            content.index("Encounter for breast and pelvic examination", exam_history_heading)
            > exam_history_heading
        )
        assert (
            content.index("Well woman exam with routine gynecological exam", exam_history_heading)
            > exam_history_heading
        )
        assert content.index("Physical exam", exam_history_heading) > exam_history_heading
        assert (
            content.index(
                "Encounter for general adult medical examination without abnormal findings",
                exam_history_heading,
            )
            > exam_history_heading
        )
        screening_heading = content.index("#### Screening")
        assert (
            content.index(
                "Encounter for screening for malignant neoplasm of prostate", screening_heading
            )
            > screening_heading
        )
        assert (
            content.index("Other specified postprocedural states", exam_history_heading)
            > exam_history_heading
        )
        ent_heading = content.index("#### Upper Airway / Sinus")
        assert content.index("Postnasal drip", ent_heading) > ent_heading


def test_write_medical_history_summary_report_suppresses_same_day_composite_facts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2022-06-02",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Brittle nails",
                    "category": "diagnosis",
                    "recorded_date": "2022-06-02",
                    "confidence": "medium",
                },
                {
                    "description": "Mixed hyperlipidemia",
                    "category": "diagnosis",
                    "recorded_date": "2022-06-02",
                    "confidence": "medium",
                },
                {
                    "description": (
                        "Encounter for screening mammogram for malignant neoplasm of breast"
                    ),
                    "category": "screening",
                    "recorded_date": "2022-06-02",
                    "confidence": "medium",
                },
                {
                    "description": (
                        "Mixed hyperlipidemia Encounter for screening mammogram "
                        "for malignant neoplasm of breast"
                    ),
                    "category": "screening",
                    "recorded_date": "2022-06-02",
                    "confidence": "medium",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2022-06-02-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Mixed hyperlipidemia" in content
        assert "Mixed hyperlipidemia [review]" not in content
        assert "Encounter for screening mammogram for malignant neoplasm of breast" in content
        assert (
            "Mixed hyperlipidemia Encounter for screening mammogram for "
            "malignant neoplasm of breast [review]" not in content
        )


def test_repeated_short_fragment_does_not_wrongly_mark_long_fact_as_composite() -> None:
    # Reproduces a real extraction defect: a long, correct diagnosis
    # gets re-extracted as a truncated fragment on repeated source re-imports,
    # so the same short fragment ends up duplicated across many same-day
    # notes. Before the fix, `_history_fact_is_same_day_composite` counted raw
    # match occurrences rather than distinct component texts, so 2+ copies of
    # the SAME short fragment falsely satisfied the "2+ distinct components"
    # threshold and caused the long, correct fact to be wrongly suppressed —
    # leaving only the meaningless fragment behind.
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        long_fact = {
            "description": "Overweight with body mass index (BMI) of 26 to 26.9 in adult",
            "category": "diagnosis",
            "recorded_date": "2022-07-31",
        }
        fragment_fact = {
            "description": "9 in adult",
            "category": "diagnosis",
            "recorded_date": "2022-07-31",
        }
        _make_history_note(
            history_dir,
            "note1-history-review.md",
            event_date="2022-07-31",
            title="note1",
            source_file_name="note1.xml",
            verification_status="accepted",
            accepted_facts=[long_fact, fragment_fact],
        )
        for i in range(2, 5):
            _make_history_note(
                history_dir,
                f"note{i}-history-review.md",
                event_date="2022-07-31",
                title=f"note{i}",
                source_file_name=f"note{i}.xml",
                verification_status="accepted",
                accepted_facts=[fragment_fact],
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Overweight with body mass index (BMI) of 26 to 26.9 in adult" in content
        assert "#### 9 in adult" not in content


def test_write_medical_history_summary_report_needs_review_no_facts_table() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "2021-05-01-visit.md",
            event_date="2021-05-01",
            title="GP Visit",
            source_file_name="gp.pdf",
            verification_status="needs_review",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "GP Visit" in content
        assert "needs_review" in content
        # No grouped fact sections for a needs_review entry
        assert "**Diagnoses**" not in content
        assert "**Vital Signs**" not in content


def test_write_medical_history_summary_report_ignores_deleted_no_fact_notes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note(
            history_dir,
            "2021-05-01-visit.md",
            event_date="2021-05-01",
            title="GP Visit",
            source_file_name="gp.pdf",
        )
        note_path.unlink()

        write_medical_history_summary_report(vault_root, "dummy_alice")

        report = vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        assert not report.exists()


def test_accept_history_candidates_persists_value_unit_confidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Visit",
            source_file_name="visit.pdf",
        )
        conn = _make_db()

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Weight",
                    "category": "vital_sign",
                    "recorded_date": "2021-01-15",
                    "value": "200.2",
                    "unit": "lbs",
                    "confidence": "high",
                },
                {
                    "description": "GERD",
                    "category": "diagnosis",
                    "icd_10_code": "K21.9",
                    "recorded_date": "2021-01-15",
                    "confidence": "medium",
                },
            ],
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
        )

        text = note_path.read_text(encoding="utf-8")
        fm_end = text.find("\n---", 3)
        fm = yaml.safe_load(text[3:fm_end])
        facts = fm["accepted_facts"]

        # value/unit/confidence round-tripped into front matter
        weight = next(f for f in facts if f["description"] == "Weight")
        assert weight["value"] == "200.2"
        assert weight["unit"] == "lbs"
        assert weight["confidence"] == "high"


def test_auto_accept_history_review_notes_refreshes_existing_accepted_notes_when_requested() -> (
    None
):
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        note_path = history_dir / "2021-01-15-visit-history-review.md"
        note_path.write_text(
            (
                "---\n"
                "person_id: dummy_alice\n"
                "record_type: history\n"
                "source_file_name: archive.zip#DOC0001.XML\n"
                "source_path: data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML\n"
                "event_date: 2021-01-15\n"
                "title: Summary of Care\n"
                "verification_status: accepted\n"
                "accepted_facts:\n"
                "  - description: stale combined fact\n"
                "    category: screening\n"
                "    recorded_date: 2021-01-15\n"
                "---\n\n"
                "# History Review\n\n"
                "## Accepted Structured Facts\n\n"
                "- stale\n\n"
                "### Visit Diagnoses\n"
                "Mixed hyperlipidemia - Primary Encounter for screening mammogram "
                "for malignant neoplasm of breast\n"
            ),
            encoding="utf-8",
        )
        conn = _make_db()

        accepted_notes, unresolved_notes = auto_accept_history_review_notes_for_source_prefix(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_path_prefix="data/processed/dummy_alice/HealthSummary/archive.zip#",
            refresh_existing=True,
        )

        assert accepted_notes == 1
        assert unresolved_notes == 0
        text = note_path.read_text(encoding="utf-8")
        assert "stale combined fact" not in text
        assert "Mixed hyperlipidemia" in text
        assert "## Review" not in text


def test_auto_accept_history_review_notes_uses_ai_for_semantically_hard_notes_when_available() -> (
    None
):
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        note_path = history_dir / "2021-01-15-visit-history-review.md"
        note_path.write_text(
            (
                "---\n"
                "person_id: dummy_alice\n"
                "record_type: history\n"
                "source_file_name: archive.zip#DOC0001.XML\n"
                "source_path: data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML\n"
                "event_date: 2021-01-15\n"
                "title: Summary of Care\n"
                "verification_status: needs_review\n"
                "---\n\n"
                "# History Review\n\n"
                "### Visit Diagnoses\n"
                "Mixed hyperlipidemia - Primary Encounter for screening mammogram "
                "for malignant neoplasm of breast\n"
            ),
            encoding="utf-8",
        )
        conn = _make_db()
        fact_interpreter = StaticFactInterpreter(
            {
                note_path.name: _history_fact_extraction_payload(
                    document_date="2021-01-15",
                    facts=[
                        {
                            "description": "Mixed hyperlipidemia",
                            "category": "diagnosis",
                            "recorded_date": "2021-01-15",
                            "confidence": 0.99,
                            "evidence_snippet": "Mixed hyperlipidemia",
                        },
                        {
                            "description": (
                                "Encounter for screening mammogram for malignant neoplasm of breast"
                            ),
                            "category": "screening",
                            "recorded_date": "2021-01-15",
                            "confidence": 0.88,
                            "evidence_snippet": "screening mammogram",
                        },
                    ],
                )
            }
        )

        accepted_notes, unresolved_notes = auto_accept_history_review_notes_for_source_prefix(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_path_prefix="data/processed/dummy_alice/HealthSummary/archive.zip#",
            fact_interpreter=fact_interpreter,
        )

        assert accepted_notes == 1
        assert unresolved_notes == 0
        text = note_path.read_text(encoding="utf-8")
        assert "Mixed hyperlipidemia" in text
        assert "Encounter for screening mammogram for malignant neoplasm of breast" in text
        assert "confidence: medium" in text


def test_auto_accept_history_routes_semantic_immunization_through_domain_steward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        processed_root = tmp_path / "isolated-processed"
        source_path = processed_root / "dummy_alice" / "History" / "vaccine-history.pdf"
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"synthetic vaccine history")
        canonical_source = "data/processed/dummy_alice/History/vaccine-history.pdf"
        note_path = history_dir / "2099-01-15-vaccine-history-review.md"
        note_path.write_text(
            (
                "---\n"
                "person_id: dummy_alice\n"
                "record_type: history\n"
                "source_file_name: vaccine-history.pdf\n"
                f"source_path: {canonical_source}\n"
                "event_date: 2099-01-15\n"
                "title: Vaccine History\n"
                "verification_status: needs_review\n"
                "---\n\n"
                "# History Review\n\n"
                "### Allergies\n\n"
                "No known allergies documented\n\n"
                "Synthetic vaccine entry\n"
            ),
            encoding="utf-8",
        )
        fact_interpreter = StaticFactInterpreter(
            {
                note_path.name: _history_fact_extraction_payload(
                    document_date="2099-01-15",
                    facts=[
                        {
                            "description": "Synthetic vaccine",
                            "category": "immunization",
                            "recorded_date": "2099-01-15",
                            "cvx_code": "999",
                            "lot_number": "SYNTHETIC",
                            "confidence": 0.99,
                            "evidence_snippet": "Synthetic vaccine entry",
                        }
                    ],
                )
            }
        )
        captured: dict[str, Any] = {}

        def fake_persist_immunization_rows(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(
            "famed.immunization_sync.persist_immunization_rows",
            fake_persist_immunization_rows,
        )
        conn = _make_db()

        accepted_notes, unresolved_notes = auto_accept_history_review_notes_for_source_prefix(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_path_prefix=canonical_source,
            fact_interpreter=fact_interpreter,
            processed_root=processed_root,
        )

        assert accepted_notes == 1
        assert unresolved_notes == 0
        assert captured["processed_source_path"] == canonical_source
        assert captured["source_type"] == "history_extract"
        assert captured["rows"] == [
            {
                "person_id": "dummy_alice",
                "vaccine_name": "Synthetic vaccine",
                "cvx_code": "999",
                "administered_date": "2099-01-15",
                "status": "completed",
                "lot_number": "SYNTHETIC",
                "evidence_snippet": "Synthetic vaccine entry",
            }
        ]


def test_write_medical_history_summary_report_shows_value_unit_confidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-01-15",
            "title": "GI Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Blood pressure systolic",
                    "category": "vital_sign",
                    "recorded_date": "2021-01-10",
                    "value": "116",
                    "unit": "mm Hg",
                    "confidence": "high",
                },
                {
                    "description": "Blood pressure systolic",
                    "category": "vital_sign",
                    "recorded_date": "2021-01-15",
                    "value": "118",
                    "unit": "mm Hg",
                    "confidence": "medium",
                },
                {
                    "description": "Hiatal hernia",
                    "category": "diagnosis",
                    "icd_10_code": "K44.9",
                    "recorded_date": "2021-01-15",
                    "confidence": "medium",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-01-15-gi-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "## Vital Signs Snapshot" in content
        assert ("| Blood pressure systolic | 118 mm Hg | 2021-01-15 | 116 mm Hg | 2 |") in content
        assert "### Measurements" not in content
        assert "#### Vital Signs" not in content
        assert "#### Upper GI / Reflux" in content
        assert "[review]" not in content
        assert "K44.9" in content


def test_write_medical_history_summary_report_ignores_implausible_bmi_in_earlier_range() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "summary.xml",
            "source_path": "data/processed/dummy_alice/HealthSummary/summary.zip#DOC0005.XML",
            "event_date": "2023-06-03",
            "title": "Summary of Care",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "BMI",
                    "category": "vital_sign",
                    "recorded_date": "2019-08-23",
                    "value": "61.55",
                    "unit": "kg/m2",
                    "confidence": "medium",
                },
                {
                    "description": "BMI",
                    "category": "vital_sign",
                    "recorded_date": "2022-11-01",
                    "value": "26.13",
                    "unit": "kg/m2",
                    "confidence": "high",
                },
                {
                    "description": "BMI",
                    "category": "vital_sign",
                    "recorded_date": "2023-06-03",
                    "value": "26.72",
                    "unit": "kg/m2",
                    "confidence": "high",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2023-06-03-summary-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text()
        assert "| BMI | 26.72 kg/m2 | 2023-06-03 | 26.13 kg/m2 | 3 |" in content
        assert "26.13-61.55 kg/m2" not in content


def test_load_markdown_table_rows_skips_needs_review_vitals_notes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root = tmp_path / "vault"
        vitals_dir = vault_root / "Records" / "dummy_alice" / "Vitals"
        vitals_dir.mkdir(parents=True)

        (vitals_dir / "2021-01-10-reviewed-vitals-review.md").write_text(
            "\n".join(
                [
                    "---",
                    "person_id: dummy_alice",
                    "record_type: vitals",
                    "verification_status: accepted",
                    "---",
                    "",
                    "## Candidate Vital Signs",
                    "",
                    "| vital_name | value | unit |",
                    "| --- | --- | --- |",
                    "| blood pressure systolic | 118 | mm Hg |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (vitals_dir / "2021-01-11-needs-review-vitals-review.md").write_text(
            "\n".join(
                [
                    "---",
                    "person_id: dummy_alice",
                    "record_type: vitals",
                    "verification_status: needs_review",
                    "---",
                    "",
                    "## Candidate Vital Signs",
                    "",
                    "| vital_name | value | unit |",
                    "| --- | --- | --- |",
                    "| blood pressure systolic | 180 | mm Hg |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        rows = _load_markdown_table_rows(vault_root, "dummy_alice", "Vitals")

        assert rows == [
            {
                "vital_name": "blood pressure systolic",
                "value": "118",
                "unit": "mm Hg",
                "source_note": "2021-01-10-reviewed-vitals-review.md",
            }
        ]


def test_extract_structured_history_facts_from_note_parses_vitals_and_visit_diagnoses() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = history_dir / "2021-01-15-visit-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.xml\n"
            "source_path: data/processed/dummy_alice/HealthSummary/summary.zip#DOC0001.XML\n"
            "event_date: 2021-01-15\n"
            "title: Summary of Care\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Allergies\n\n"
            "No known active allergies documented as of this encounter.\n\n"
            "### Last Filed Vital Signs\n\n"
            "Pulse 76 09/13/2015 8:38 AM PDT Respiratory Rate 14 09/13/2015 8:38 AM PDT "
            "Oxygen Saturation 99% 09/13/2015 8:38 AM PDT Weight 86.6 kg (191 lb) "
            "Height 181.6 cm (5' 11.5\") Body Mass Index 26.27 documented in this encounter\n\n"
            "### Visit Diagnoses\n\n"
            "Diagnosis Start Date Pain in left wrist 9/13/2015 Encounter for "
            "screening for malignant neoplasm of colon 9/13/2015 as of 05/30/2023\n",
            encoding="utf-8",
        )

        facts = extract_structured_history_facts_from_note(note_path)

        descriptions = {str(fact["description"]) for fact in facts}
        assert "No known allergies documented" in descriptions
        assert "Pulse" in descriptions
        assert "Respiratory rate" in descriptions
        assert "O2 saturation" in descriptions
        assert "Weight" in descriptions
        assert "Height" in descriptions
        assert "BMI" in descriptions
        assert "Pain in left wrist" in descriptions
        assert "Encounter for screening for malignant neoplasm of colon" in descriptions
        assert "as of" not in descriptions
        assert all(
            fact["confidence"] == "medium"
            for fact in facts
            if fact["description"]
            in {"Pulse", "Respiratory rate", "O2 saturation", "Weight", "Height", "BMI"}
        )


def test_extract_history_facts_parses_active_problems_and_undated_visit_diagnoses() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = history_dir / "2020-08-30-visit-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.xml\n"
            "source_path: data/processed/dummy_alice/HealthSummary/summary.zip#DOC0001.XML\n"
            "event_date: 2020-08-30\n"
            "title: Summary of Care\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Active Problems\n\n"
            "Problem Noted Date Diagnosed Date Former cigarette smoker 08/20/2020 "
            "Hyperlipidemia 12/11/2017 Paroxysmal atrial fibrillation 05/14/2017 "
            "documented as of this encounter\n\n"
            "### Visit Diagnoses\n\n"
            "Diagnosis Visual field defects - Primary PCO "
            "(posterior capsular opacification), bilateral "
            "documented in this encounter\n",
            encoding="utf-8",
        )

        facts = extract_structured_history_facts_from_note(note_path)

        descriptions = {str(fact["description"]) for fact in facts}
        assert "Former cigarette smoker" in descriptions
        assert "Hyperlipidemia" in descriptions
        assert "Paroxysmal atrial fibrillation" in descriptions
        assert "Visual field defects" in descriptions
        assert "PCO (posterior capsular opacification), bilateral" in descriptions


def test_extract_history_facts_keeps_concatenated_visit_diagnoses_together() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = history_dir / "2022-06-02-visit-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.xml\n"
            "source_path: data/processed/dummy_alice/HealthSummary/summary.zip#DOC0009.XML\n"
            "event_date: 2022-06-02\n"
            "title: Summary of Care\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Visit Diagnoses\n\n"
            "Diagnosis Brittle nails - Primary Mixed hyperlipidemia "
            "Encounter for screening mammogram for malignant neoplasm of breast "
            "documented in this encounter\n",
            encoding="utf-8",
        )

        facts = extract_structured_history_facts_from_note(note_path)

        descriptions = [str(fact["description"]) for fact in facts]
        assert descriptions == ["Brittle nails"]
        categories = {str(fact["description"]): str(fact["category"]) for fact in facts}
        assert categories["Brittle nails"] == "diagnosis"


def test_extract_structured_history_facts_from_note_uses_ccda_table_rows_for_visit_diagnoses() -> (
    None
):
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        data_root = tmp_path / "data" / "processed" / "dummy_alice" / "HealthSummary"
        data_root.mkdir(parents=True)
        archive_path = data_root / "summary.zip"
        archive_member = "IHE_XDM/Dummy Alice/DOC0001.XML"
        archive_path.write_bytes(b"")
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                archive_member,
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ClinicalDocument xmlns="urn:hl7-org:v3">'
                    '  <effectiveTime value="20210220"/>'
                    "  <recordTarget><patientRole><patient>"
                    "    <name><given>Dummy Alice</given><family>Test</family></name>"
                    '    <birthTime value="19370211"/>'
                    "  </patient></patientRole></recordTarget>"
                    "  <component><structuredBody><component><section>"
                    "    <title>Visit Diagnoses</title>"
                    "    <text><table><thead><tr><th>Diagnosis</th></tr></thead><tbody>"
                    "      <tr><td><paragraph><content>Shortness of breath</content>"
                    "<content> - Primary</content></paragraph></td></tr>"
                    "      <tr><td><paragraph><content>Elevated blood pressure "
                    "reading</content></paragraph></td></tr>"
                    "      <tr><td><paragraph><content>Nausea</content></paragraph></td></tr>"
                    "    </tbody></table><footnote>documented in this encounter</footnote></text>"
                    "  </section></component></structuredBody></component>"
                    "</ClinicalDocument>"
                ),
            )

        note_path = history_dir / "2021-02-20-visit-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.zip#DOC0001.XML\n"
            "source_path: data/processed/dummy_alice/HealthSummary/"
            f"{archive_path.name}#{archive_member}\n"
            "event_date: 2021-02-20\n"
            "title: Summary of Care\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Visit Diagnoses\n\n"
            "Diagnosis Shortness of breath - Primary Elevated blood pressure reading "
            "Nausea documented in this encounter\n",
            encoding="utf-8",
        )

        facts = extract_structured_history_facts_from_note(note_path)

        descriptions = [str(fact["description"]) for fact in facts]
        assert descriptions == [
            "Shortness of breath",
            "Elevated blood pressure reading",
            "Nausea",
        ]


def test_extract_history_facts_resolves_archive_from_explicit_processed_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _, history_dir = _scaffold(tmp_path / "isolated-vault", "dummy_alice")
        processed_root = tmp_path / "isolated-processed"
        archive_path = processed_root / "dummy_alice" / "HealthSummary" / "summary.zip"
        archive_path.parent.mkdir(parents=True)
        archive_member = "IHE_XDM/Dummy Alice/DOC0001.XML"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                archive_member,
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ClinicalDocument xmlns="urn:hl7-org:v3">'
                    "  <component><structuredBody><component><section>"
                    "    <title>Visit Diagnoses</title>"
                    "    <text><table><thead><tr><th>Diagnosis</th></tr></thead><tbody>"
                    "      <tr><td>Sample Condition Alpha</td></tr>"
                    "      <tr><td>Sample Condition Beta</td></tr>"
                    "    </tbody></table></text>"
                    "  </section></component></structuredBody></component>"
                    "</ClinicalDocument>"
                ),
            )

        note_path = history_dir / "2099-01-15-summary-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.zip#DOC0001.XML\n"
            "source_path: data/processed/dummy_alice/HealthSummary/"
            f"{archive_path.name}#{archive_member}\n"
            "event_date: 2099-01-15\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Visit Diagnoses\n\n"
            "Sample Condition Alpha Sample Condition Beta\n",
            encoding="utf-8",
        )

        facts = extract_structured_history_facts_from_note(
            note_path,
            processed_root=processed_root,
        )

        assert [fact["description"] for fact in facts] == [
            "Sample Condition Alpha",
            "Sample Condition Beta",
        ]


def test_extract_history_facts_fails_closed_when_configured_archive_is_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _, history_dir = _scaffold(tmp_path / "isolated-vault", "dummy_alice")
        processed_root = tmp_path / "isolated-processed"
        processed_root.mkdir()
        note_path = history_dir / "2099-01-15-summary-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.zip#DOC0001.XML\n"
            "source_path: data/processed/dummy_alice/HealthSummary/"
            "missing.zip#DOC0001.XML\n"
            "event_date: 2099-01-15\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Visit Diagnoses\n\n"
            "Sample Condition Alpha Sample Condition Beta\n",
            encoding="utf-8",
        )

        with pytest.raises(HistorySyncError, match="configured processed root"):
            extract_structured_history_facts_from_note(
                note_path,
                processed_root=processed_root,
            )


def test_extract_history_facts_rejects_provenance_escape_from_processed_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _, history_dir = _scaffold(tmp_path / "isolated-vault", "dummy_alice")
        processed_root = tmp_path / "isolated-processed"
        processed_root.mkdir()
        note_path = history_dir / "2099-01-15-summary-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: outside.zip#DOC0001.XML\n"
            "source_path: data/processed/../outside.zip#DOC0001.XML\n"
            "event_date: 2099-01-15\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Visit Diagnoses\n\n"
            "Sample Condition Alpha\n",
            encoding="utf-8",
        )

        with pytest.raises(HistorySyncError, match="escapes configured processed root"):
            extract_structured_history_facts_from_note(
                note_path,
                processed_root=processed_root,
            )


def test_ccda_visit_diagnoses_uses_per_row_start_date_not_event_date() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        data_root = tmp_path / "data" / "processed" / "dummy_alice" / "HealthSummary"
        data_root.mkdir(parents=True)
        archive_path = data_root / "summary.zip"
        archive_member = "IHE_XDM/Dummy Alice/DOC0001.XML"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                archive_member,
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ClinicalDocument xmlns="urn:hl7-org:v3">'
                    "  <component><structuredBody><component><section>"
                    "    <title>Visit Diagnoses</title>"
                    "    <text><table>"
                    "      <thead><tr><th>Diagnosis</th><th>Start Date</th></tr></thead>"
                    "      <tbody>"
                    "        <tr><td><paragraph><content>Hypertension</content></paragraph></td>"
                    "            <td>3/15/2017</td></tr>"
                    "        <tr><td><paragraph><content>Encounter for annual physical exam"
                    "</content></paragraph></td><td>2/12/2023</td></tr>"
                    "      </tbody>"
                    "    </table></text>"
                    "  </section></component></structuredBody></component>"
                    "</ClinicalDocument>"
                ),
            )

        note_path = history_dir / "2023-06-03-doc-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            "source_file_name: summary.zip#DOC0001.XML\n"
            f"source_path: data/processed/dummy_alice/HealthSummary/"
            f"{archive_path.name}#{archive_member}\n"
            "event_date: 2023-06-03\n"
            "title: Continuity of Care\n"
            "verification_status: needs_review\n"
            "---\n\n"
            "# History Review\n\n"
            "### Visit Diagnoses\n\n"
            "Hypertension 3/15/2017 Encounter for annual physical exam 2/12/2023\n",
            encoding="utf-8",
        )

        facts = extract_structured_history_facts_from_note(note_path)

        by_desc = {str(f["description"]): f for f in facts}
        assert "Hypertension" in by_desc
        assert by_desc["Hypertension"]["recorded_date"] == "2017-03-15"
        assert by_desc["Hypertension"]["category"] == "diagnosis"
        assert "Encounter for annual physical exam" in by_desc
        assert by_desc["Encounter for annual physical exam"]["recorded_date"] == "2023-02-12"
        # Neither entry should use the document submission date
        assert all(
            str(f.get("recorded_date")) != "2023-06-03"
            for f in facts
            if f.get("category") in {"diagnosis", "screening"}
        )


def test_accept_history_candidates_excluded_low_confidence_round_trip() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        note_path = _make_history_note_full(
            history_dir,
            "2021-01-15-visit-history-review.md",
            event_date="2021-01-15",
            title="Visit",
            source_file_name="visit.pdf",
        )
        conn = _make_db()

        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Weight",
                    "category": "vital_sign",
                    "recorded_date": "2021-01-15",
                    "value": "200.2",
                    "unit": "lbs",
                    "confidence": "high",
                },
            ],
            person_id="dummy_alice",
            source_path="data/processed/dummy_alice/History/visit.pdf",
            excluded_low_confidence=3,
        )

        text = note_path.read_text(encoding="utf-8")
        fm_end = text.find("\n---", 3)
        fm = yaml.safe_load(text[3:fm_end])
        assert fm.get("excluded_low_confidence_count") == 3
        assert "low-confidence finding(s) not included" in text


# ---------------------------------------------------------------------------
# infer_person_id_from_processed_history_path
# ---------------------------------------------------------------------------


def test_infer_person_id_from_processed_history_path_valid() -> None:
    from famed.history_sync import infer_person_id_from_processed_history_path

    processed_root = Path("/repo/data/processed")
    source_path = processed_root / "dummy_alice" / "History" / "undated-visitSummary.pdf"
    assert infer_person_id_from_processed_history_path(source_path, processed_root) == "dummy_alice"


def test_infer_person_id_from_processed_history_path_wrong_category() -> None:
    from famed.history_sync import infer_person_id_from_processed_history_path

    processed_root = Path("/repo/data/processed")
    source_path = processed_root / "dummy_alice" / "Labs" / "panel.csv"
    assert infer_person_id_from_processed_history_path(source_path, processed_root) is None


def test_infer_person_id_from_processed_history_path_outside_root() -> None:
    from famed.history_sync import infer_person_id_from_processed_history_path

    processed_root = Path("/repo/data/processed")
    source_path = Path("/other/dummy_alice/History/note.xml")
    assert infer_person_id_from_processed_history_path(source_path, processed_root) is None


def test_history_note_path_strips_retained_source_date_prefix() -> None:
    from famed.history_sync import history_note_path

    note_path = history_note_path(
        Path("/repo/vault/Records/dummy_alice/History"),
        "2019-10-31",
        Path("2019-10-31-undated-191031 visitNote.pdf"),
    )

    assert note_path.name == "2019-10-31-visitnote-history-review.md"


def test_processed_history_source_path_strips_existing_date_prefix() -> None:
    from famed.history_sync import processed_history_source_path

    source_path = processed_history_source_path(
        Path("/repo/data/processed"),
        "dummy_alice",
        "2023-05-30",
        Path("1970-01-01-undated-230530-DOC0001.xml"),
    )

    assert source_path == Path("/repo/data/processed/dummy_alice/History/2023-05-30-DOC0001.xml")


def test_parse_text_history_review_document_uses_source_stem_date_when_text_date_missing() -> None:
    from famed.history_sync import parse_text_history_review_document

    document = parse_text_history_review_document(
        Path("undated-200109 Colonoscopy.pdf"),
        "Patient: Example, Dummy Alice DOB: 03/14/1968 Colonoscopy report",
    )

    assert document.document_date == "2020-01-09"


def test_parse_text_history_review_document_reads_spelled_out_dose_date() -> None:
    from famed.history_sync import parse_text_history_review_document

    document = parse_text_history_review_document(
        Path("Vaccine History.pdf"),
        "Name Dummy Alice\n"
        "Date of birth March 14, 1968\n"
        "Shingles (Zoster)\n"
        "Dose given on June 22, 2023\n"
        "Vaccine name SHINGRIX 50 MCG/0.5 ML SYRINGE\n",
    )

    assert document.document_date == "2023-06-22"
    assert document.patient_birth_year == 1968


def test_parse_text_document_date_ignores_birth_date_without_on() -> None:
    from famed.history_sync import _parse_text_document_date

    assert _parse_text_document_date("Patient was born on March 5, 1962.") is None
    assert _parse_text_document_date("Date of birth: March 5, 1962.") is None


def test_persist_history_document_review_note_deduplicates_clean_name_collision() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        conn = _make_db()
        document = HistoryReviewDocument(
            title="Visit Summary",
            document_date="2019-10-31",
            patient_given_names=("Dummy Alice",),
            patient_family_name="Example",
            patient_birth_year=1980,
            sections=(HistoryReviewSection(title="Reason", text="Follow-up."),),
        )

        first = persist_history_document_review_note(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("221031 VisitSummary.xml"),
            processed_source_path="data/processed/dummy_alice/History/2019-10-31-VisitSummary.xml",
            checksum="abc",
            source_type="ccda_xml",
            document=document,
            document_kind="C-CDA",
        )
        second = persist_history_document_review_note(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=Path("undated-191031 visitSummary.pdf"),
            processed_source_path="data/processed/dummy_alice/History/2019-10-31-visitSummary.pdf",
            checksum="def",
            source_type="history_pdf",
            document=document,
            document_kind="PDF",
        )

        assert first.name == "2019-10-31-visitsummary-history-review.md"
        assert second.name == "2019-10-31-visitsummary-pdf-history-review.md"
        index = (history_dir / "_index.md").read_text(encoding="utf-8")
        assert "[[2019-10-31-visitsummary-history-review]]" in index
        assert "[[2019-10-31-visitsummary-pdf-history-review]]" in index


# ---------------------------------------------------------------------------
# reprocess_retained_history_source
# ---------------------------------------------------------------------------


def _make_ccda_xml(
    path: Path,
    *,
    title: str = "Visit Summary",
    date: str = "20191031",
    service_start: str | None = None,
) -> None:
    """Write a minimal C-CDA XML file."""
    documentation_of = ""
    if service_start:
        documentation_of = f"""
  <documentationOf>
    <serviceEvent>
      <effectiveTime>
        <low value="{service_start}"/>
      </effectiveTime>
    </serviceEvent>
  </documentationOf>
"""
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <title>{title}</title>
  <effectiveTime value="{date}"/>
  {documentation_of}
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>Dummy Alice</given>
          <family>Test</family>
        </name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <title>Reason for Visit</title>
          <text>Annual checkup.</text>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
"""
    path.write_text(xml_content, encoding="utf-8")


def _make_mixed_ccda_xml(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20191031"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <text><table><tbody><tr><td ID="result1">Glucose</td><td>95</td></tr></tbody>
          </table></text>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="2345-7" codeSystem="2.16.840.1.113883.6.1" displayName="Glucose">
                <originalText><reference value="#result1"/></originalText>
              </code>
              <statusCode code="completed"/>
              <effectiveTime value="20191031"/>
              <value xsi:type="PQ" value="95" unit="mg/dL"/>
            </observation>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <code code="10160-0" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Medications</title>
          <text><table><tbody><tr><td ID="med1">Omeprazole 20 mg capsule</td>
          <td ID="sig1">1 capsule daily</td></tr></tbody></table></text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="EVN">
              <text><reference value="#med1"/></text>
              <statusCode code="active"/>
              <effectiveTime xsi:type="IVL_TS"><low value="20191031"/></effectiveTime>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code code="7646" codeSystem="2.16.840.1.113883.6.88"
                          displayName="Omeprazole 20 MG Delayed Release Oral Capsule">
                      <originalText><reference value="#med1"/></originalText>
                    </code>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
              <entryRelationship typeCode="COMP">
                <substanceAdministration classCode="SBADM" moodCode="EVN">
                  <text><reference value="#sig1"/></text>
                </substanceAdministration>
              </entryRelationship>
            </substanceAdministration>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <code code="8716-3" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Vital Signs</title>
          <entry>
            <organizer classCode="CLUSTER" moodCode="EVN">
              <component>
                <observation classCode="OBS" moodCode="EVN">
                  <code code="8310-5" codeSystem="2.16.840.1.113883.6.1"
                        displayName="Body temperature"/>
                  <statusCode code="completed"/>
                  <effectiveTime value="20191031"/>
                  <value xsi:type="PQ" value="98.6" unit="F"/>
                </observation>
              </component>
              <component>
                <observation classCode="OBS" moodCode="EVN">
                  <code code="8867-4" codeSystem="2.16.840.1.113883.6.1" displayName="Heart rate"/>
                  <statusCode code="completed"/>
                  <effectiveTime value="20191031"/>
                  <value xsi:type="PQ" value="72" unit="/min"/>
                </observation>
              </component>
            </organizer>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <code code="11369-6" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Immunizations</title>
          <text><table><tbody><tr><td ID="imm1">Influenza vaccine</td></tr></tbody></table></text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="EVN">
              <text><reference value="#imm1"/></text>
              <statusCode code="completed"/>
              <effectiveTime value="20191015"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code code="140" codeSystem="2.16.840.1.113883.12.292"
                          displayName="Influenza, seasonal, injectable"/>
                    <lotNumberText>FLU-2022</lotNumberText>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
            </substanceAdministration>
          </entry>
        </section>
      </component>
      <component>
        <section>
          <title>Reason for Visit</title>
          <text>Annual checkup.</text>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _make_ccda_medication_edge_case_xml(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20230316"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="10160-0" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Medications</title>
          <text>
            <list>
              <item><content ID="med1">Ferrous Sulfate (IRON PO)</content>
              <paragraph ID="sig1">Take by mouth.</paragraph></item>
              <item><content ID="med2">Magnesium 250 MG CAPS</content>
              <paragraph ID="sig2">capsule</paragraph></item>
              <item><content ID="med3">VITAMIN D PO</content>
              <paragraph ID="sig3">tablet</paragraph></item>
              <item><content ID="med4">famotidine (PEPCID) 20 mg tablet</content>
              <paragraph ID="sig4">Take 1 tablet by mouth 2 times daily.</paragraph></item>
              <item><content ID="nomeds">No known medications</content></item>
            </list>
          </text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="INT">
              <text><reference value="#sig1"/></text>
              <statusCode code="active"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code codeSystem="2.16.840.1.113883.6.88" nullFlavor="UNK">
                      <originalText><reference value="#med1"/></originalText>
                    </code>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
              <entryRelationship typeCode="COMP">
                <substanceAdministration classCode="SBADM" moodCode="EVN">
                  <text><reference value="#sig1"/></text>
                </substanceAdministration>
              </entryRelationship>
            </substanceAdministration>
          </entry>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="INT">
              <text><reference value="#sig2"/></text>
              <statusCode code="active"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code codeSystem="2.16.840.1.113883.6.88" nullFlavor="OTH">
                      <originalText><reference value="#med2"/></originalText>
                    </code>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
              <entryRelationship typeCode="COMP">
                <substanceAdministration classCode="SBADM" moodCode="EVN">
                  <text><reference value="#sig2"/></text>
                </substanceAdministration>
              </entryRelationship>
            </substanceAdministration>
          </entry>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="INT">
              <text><reference value="#sig3"/></text>
              <statusCode code="active"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code codeSystem="2.16.840.1.113883.6.88" nullFlavor="UNK">
                      <originalText><reference value="#med3"/></originalText>
                    </code>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
              <entryRelationship typeCode="COMP">
                <substanceAdministration classCode="SBADM" moodCode="EVN">
                  <text><reference value="#sig3"/></text>
                </substanceAdministration>
              </entryRelationship>
            </substanceAdministration>
          </entry>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="INT">
              <text><reference value="#sig4"/></text>
              <statusCode code="active"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code code="310273" codeSystem="2.16.840.1.113883.6.88"
                          displayName="Famotidine 20 MG Oral Tablet">
                      <originalText><reference value="#med4"/></originalText>
                    </code>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
              <entryRelationship typeCode="COMP">
                <substanceAdministration classCode="SBADM" moodCode="EVN">
                  <text><reference value="#sig4"/></text>
                </substanceAdministration>
              </entryRelationship>
            </substanceAdministration>
          </entry>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="INT" negationInd="true">
              <text><reference value="#nomeds"/></text>
              <statusCode code="active"/>
            </substanceAdministration>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _make_ccda_lab_oth_reference_range_xml(path: Path) -> None:
    """A Results section observation using Epic's non-UCUM ("OTH") encoding.

    The unit lives in a companion entryRelationship observation (SNOMED
    246514001 "units") instead of `value/@unit`, and the reference-range
    bounds are `nullFlavor="OTH"` with the numeric value nested in a
    `<translation value="...">` child instead of `low`/`high`'s own `value`
    attribute. This mirrors a real TSH result from an Epic CCDA export.
    """
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20210731"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="11579-0" codeSystem="2.16.840.1.113883.6.1" displayName="TSH"/>
              <statusCode code="completed"/>
              <effectiveTime value="20210731205856+0000"/>
              <value xsi:type="REAL" value="6.39"/>
              <interpretationCode code="H" codeSystem="2.16.840.1.113883.5.83"/>
              <entryRelationship typeCode="COMP">
                <observation classCode="OBS" moodCode="EVN">
                  <code code="246514001" codeSystem="2.16.840.1.113883.6.96"
                        codeSystemName="SNOMED CT" displayName="units"/>
                  <value xsi:type="ST">µIU/mL</value>
                </observation>
              </entryRelationship>
              <referenceRange>
                <observationRange>
                  <text>0.40 - 5.00 µIU/mL</text>
                  <value xsi:type="IVL_PQ">
                    <low nullFlavor="OTH">
                      <translation nullFlavor="OTH" value="0.40">
                        <originalText>µIU/mL</originalText>
                      </translation>
                    </low>
                    <high nullFlavor="OTH">
                      <translation nullFlavor="OTH" value="5.00">
                        <originalText>µIU/mL</originalText>
                      </translation>
                    </high>
                  </value>
                </observationRange>
              </referenceRange>
            </observation>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _make_ccda_st_quantity_xml(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20990102"/>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Average tissue thickness"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">88 µm</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Compound scan values"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">88 µm. 11 µm.</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Synthetic point-of-care measure"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">0.2 E.U./dL</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Punctuation-ended quantity"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">42 µm.</value>
            </observation>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _make_ccda_bounded_text_xml(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20990102"/>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <text>Comparator results</text>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy symbolic bound"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">&lt;0.10</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy word bound"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">above 60</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy bounded culture"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">&gt;100,000 CFU/mL Synthetic organism</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy matching culture unit"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST" unit="cfu/ml">&gt;1,000 CFU/mL Synthetic organism</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy conflicting culture unit"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST" unit="mg/dL">&gt;1,000 CFU/mL Synthetic organism</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy interval"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST" unit="/hpf">10-20</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy semiquantitative result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">1+</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy labeled ordinal result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">Small (1+)</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy descending interval"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST" unit="/hpf">20-10</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy unitless interval"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">10-20</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy non-ST interval"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="PQ" value="10-20" unit="/hpf"/>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy malformed ordinal"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">Small 1+</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy zero ordinal"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">0+</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy narrative culture"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">No growth of greater than or equal to 1000
                CFU/mL at 18 hours</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy compound reading"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">&lt;1 and &gt;2</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy mixed positive result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">&gt;1 positive</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy mixed narrative result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">&gt;1 and negative</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy slash narrative result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">&lt;1 / negative</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy staged narrative result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">at least 1 stage two</value>
            </observation>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _make_ccda_operational_metadata_xml(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20990102"/>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <text>Point-of-care results</text>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="2345-7" codeSystem="2.16.840.1.113883.6.1"
                    displayName="Dummy Glucose"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="PQ" value="90" unit="mg/dL"/>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="2345-7" codeSystem="2.16.840.1.113883.6.1"
                    displayName="Dummy Performing Location Coded Result"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="PQ" value="91" unit="mg/dL"/>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy Kit Lot Number"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="REAL" value="503039"/>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy Test Exp Date"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">12/31/2099</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy Performing Location"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">Synthetic Lab</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code nullFlavor="UNK" displayName="Dummy Internal Control Results OK"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">Passed</value>
            </observation>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _reprocess_ccda_fixture_twice(
    write_source: Callable[[Path], None],
) -> sqlite3.Connection:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vault_root = root / "vault"
        _write_person_note(vault_root, "dummy_person")
        processed_root = root / "processed"
        archive_dir = processed_root / "dummy_person" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"
        source_xml = root / "DOC0001.XML"
        write_source(source_xml)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(source_xml, arcname="IHE_XDM/Dummy Person/DOC0001.XML")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO persons (id, display_name) VALUES ('dummy_person', 'Dummy Person')"
        )

        for _ in range(2):
            result = reprocess_retained_history_archive(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                archive_path=archive_path,
                refresh_reports=False,
            )
            assert result.imported_rows == 1

    return conn


def _make_ccda_no_medications_xml(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <title>Summary of Care</title>
  <effectiveTime value="20201129"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="10160-0" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Medications</title>
          <text><content ID="nomeds">No known medications</content></text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="INT" negationInd="true">
              <text><reference value="#nomeds"/></text>
              <statusCode code="active"/>
            </substanceAdministration>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def test_parse_ccda_review_document_prefers_document_effective_time_over_service_start(
    tmp_path: Path,
) -> None:
    from famed.history_sync import parse_ccda_review_document

    source_path = tmp_path / "summary.xml"
    _make_ccda_xml(source_path, date="20230530171806-0700", service_start="19641222")

    document = parse_ccda_review_document(source_path)

    assert document.document_date == "2023-05-30"


def test_reprocess_retained_history_source_xml_creates_note_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from famed import history_sync
    from famed.setup import SCHEMA_SQL
    from famed.storage_contract import (
        AUDIT_DEPENDENT_OUTPUT_MARKED_STALE,
        AUDIT_EVENTS_SCHEMA_SQL,
        AUDIT_HISTORY_SOURCE_REPROCESSED,
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        history_dir = processed_root / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        _write_person_note(vault_root)

        source_path = history_dir / "2019-10-31-221031-visitsummary.xml"
        _make_ccda_xml(source_path, title="Visit Summary", date="20191031")
        processed_source_path = source_path.relative_to(tmp).as_posix()
        medication_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        medication_dir.mkdir(parents=True)
        stale_medication_note = medication_dir / "2019-10-31-visitsummary-medications.md"
        stale_medication_note.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: medication\n"
            f"source_path: {processed_source_path}\n"
            "---\n\n"
            "# Medication List\n\n"
            "## Medications\n\n"
            "| medication_name | medication_type | status |\n"
            "| --- | --- | --- |\n"
            "| No known medications | prescription | active |\n",
            encoding="utf-8",
        )

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, status,
                source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stale-row",
                "dummy_alice",
                "No known medications",
                "prescription",
                "active",
                "xml",
                processed_source_path,
            ),
        )
        report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("stale medication report", encoding="utf-8")
        medication_refreshes: list[str] = []
        monkeypatch.setattr(
            history_sync,
            "refresh_medication_dependent_reports",
            lambda conn, vault_root, person_id: medication_refreshes.append(person_id) or [],
        )

        result = history_sync.reprocess_retained_history_source(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 1
        assert result.affected_person_ids == {"dummy_alice"}

        # Vault note created under History/
        history_notes = [
            p
            for p in (vault_root / "Records" / "dummy_alice" / "History").glob("*.md")
            if p.name != "_index.md"
        ]
        assert len(history_notes) == 1
        note_text = history_notes[0].read_text()
        assert "verification_status: needs_review" in note_text
        assert "## Narrative Sections" in note_text
        assert "### Reason for Visit" in note_text
        assert "Annual checkup." in note_text

        # Audit events
        rows = conn.execute("SELECT event_type FROM audit_events").fetchall()
        event_types = {r[0] for r in rows}
        assert AUDIT_DEPENDENT_OUTPUT_MARKED_STALE in event_types
        assert AUDIT_HISTORY_SOURCE_REPROCESSED in event_types
        assert "medication_source_note_retired" in event_types
        assert not stale_medication_note.exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM medications WHERE source_path = ?",
            (processed_source_path,),
        ).fetchone() == (0,)
        assert not report_path.exists()
        assert medication_refreshes == ["dummy_alice"]
        pending_report = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "pending-review.md"
        ).read_text(encoding="utf-8")
        assert "pending_review_count: 1" in pending_report
        assert processed_source_path in pending_report
        assert "`fm-history-extract`" in pending_report


def test_reprocess_retained_history_source_refuses_to_clear_accepted_facts() -> None:
    from famed.history_sync import HistorySyncError, reprocess_retained_history_source
    from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        source_dir = processed_root / "dummy_alice" / "History"
        source_dir.mkdir(parents=True)
        source_path = source_dir / "2099-01-01-summary.xml"
        _make_ccda_no_medications_xml(source_path)
        processed_source_path = source_path.relative_to(tmp).as_posix()

        history_dir = vault_root / "Records" / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        note_path = history_dir / "2099-01-01-summary-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            f"source_path: {processed_source_path}\n"
            "verification_status: accepted\n"
            "accepted_facts:\n"
            "- description: Synthetic accepted finding\n"
            "  category: diagnosis\n"
            "---\n\n"
            "# History Review\n",
            encoding="utf-8",
        )
        original = note_path.read_bytes()
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        unrelated_interpreter = StaticFactInterpreter(
            {"unrelated.md": _history_fact_extraction_payload()}
        )
        with pytest.raises(HistorySyncError, match="source-bound retained decisions"):
            reprocess_retained_history_source(
                root=tmp,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
                fact_interpreter=unrelated_interpreter,
            )

        assert note_path.read_bytes() == original
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_reprocess_history_source_replays_exact_bound_decision() -> None:
    from famed.history_sync import reprocess_retained_history_source
    from famed.retained_replay import RetainedHistoryDecisionInterpreter
    from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "data" / "processed"
        source_dir = processed_root / "dummy_alice" / "History"
        source_dir.mkdir(parents=True)
        source_path = source_dir / "2099-01-01-summary.xml"
        source_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ClinicalDocument xmlns="urn:hl7-org:v3">\n'
            "  <title>Synthetic Summary</title>\n"
            '  <effectiveTime value="20990101"/>\n'
            "  <component><structuredBody><component><section>\n"
            "    <title>Clinical Notes</title><text>Synthetic narrative.</text>\n"
            "  </section></component></structuredBody></component>\n"
            "</ClinicalDocument>\n",
            encoding="utf-8",
        )
        processed_source_path = source_path.relative_to(tmp).as_posix()
        history_dir = vault_root / "Records" / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        note_path = history_dir / "2099-01-01-summary-history-review.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: history\n"
            f"source_path: {processed_source_path}\n"
            "verification_status: accepted\n"
            "accepted_facts:\n"
            "- description: Prior accepted finding\n"
            "  category: diagnosis\n"
            "extraction_mode: clinical_interpreter\n"
            "---\n\n# History Review\n",
            encoding="utf-8",
        )
        payload = _history_fact_extraction_payload(
            document_date="2099-01-01",
            facts=[
                {
                    "description": "Synthetic diagnosis",
                    "category": "diagnosis",
                    "recorded_date": "2099-01-01",
                    "confidence": 0.99,
                    "evidence_snippet": "Synthetic narrative.",
                }
            ],
        )
        interpreter = RetainedHistoryDecisionInterpreter(
            payloads={(processed_source_path, "history"): payload},
            processed_root=processed_root,
        )
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        reprocess_retained_history_source(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            fact_interpreter=interpreter,
        )

        replayed = note_path.read_text(encoding="utf-8")
        assert "verification_status: accepted" in replayed
        assert "Synthetic diagnosis" in replayed
        assert "Prior accepted finding" not in replayed


def test_reprocess_history_source_fails_closed_on_malformed_matching_note() -> None:
    from famed.history_sync import reprocess_retained_history_source
    from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        source_dir = processed_root / "dummy_alice" / "History"
        source_dir.mkdir(parents=True)
        source_path = source_dir / "2099-01-01-summary.xml"
        _make_ccda_no_medications_xml(source_path)
        processed_source_path = source_path.relative_to(tmp).as_posix()
        history_dir = vault_root / "Records" / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        note_path = history_dir / "2099-01-01-summary-history-review.md"
        note_path.write_text(
            f"---\nsource_path: {processed_source_path}\naccepted_facts: [unterminated\n---\n",
            encoding="utf-8",
        )
        original = note_path.read_bytes()
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        with pytest.raises(HistorySyncError, match="could not validate matching"):
            reprocess_retained_history_source(
                root=tmp,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )

        assert note_path.read_bytes() == original
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_reprocess_retained_history_source_preflights_protected_medication_note() -> None:
    from famed.history_sync import reprocess_retained_history_source
    from famed.medication_sync import MedicationSyncError

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        history_dir = processed_root / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        source_path = history_dir / "2099-01-01-summary.xml"
        _make_ccda_no_medications_xml(source_path)
        processed_source_path = source_path.relative_to(tmp).as_posix()

        medication_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        medication_dir.mkdir(parents=True)
        note_path = medication_dir / "2099-01-01-summary-medications.md"
        note_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: medication\n"
            "projection_role: source_evidence\n"
            f"source_path: {processed_source_path}\n"
            "---\n\n"
            "# Medication List\n\n"
            "## Notes\n\n"
            "Keep this user-authored context.\n",
            encoding="utf-8",
        )
        index_path = medication_dir / "_index.md"
        index_path.write_text("original index", encoding="utf-8")
        report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("original report", encoding="utf-8")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, status,
                source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "protected-row",
                "dummy_alice",
                "Synthetic Medication",
                "prescription",
                "active",
                "xml",
                processed_source_path,
            ),
        )
        note_before = note_path.read_bytes()
        index_before = index_path.read_bytes()
        report_before = report_path.read_bytes()
        rows_before = conn.execute("SELECT * FROM medications ORDER BY id").fetchall()

        with pytest.raises(MedicationSyncError, match="user-authored Notes content"):
            reprocess_retained_history_source(
                root=tmp,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )

        assert conn.execute("SELECT * FROM medications ORDER BY id").fetchall() == rows_before
        assert note_path.read_bytes() == note_before
        assert index_path.read_bytes() == index_before
        assert report_path.read_bytes() == report_before
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_reprocess_history_source_refreshes_fanned_out_categories() -> None:
    from famed.history_sync import reprocess_retained_history_source
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        history_dir = processed_root / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)

        source_path = history_dir / "2019-10-31-ccda.xml"
        _make_mixed_ccda_xml(source_path)

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        reprocess_retained_history_source(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert conn.execute("SELECT test_name, value, unit FROM lab_results").fetchall() == [
            ("Glucose", 95.0, "mg/dL")
        ]
        assert conn.execute(
            "SELECT medication_name, rxnorm_cui, start_date FROM medications"
        ).fetchall() == [("Omeprazole 20 mg capsule", "7646", "2019-10-31")]
        vital_note = (
            vault_root / "Records" / "dummy_alice" / "Vitals" / "2019-10-31-ccda-vitals-review.md"
        )
        assert vital_note.exists()
        vital_text = vital_note.read_text(encoding="utf-8")
        assert "temperature" in vital_text
        assert "pulse" in vital_text
        assert "source_path: processed/dummy_alice/History/2019-10-31-ccda.xml" in vital_text
        immunization_note = (
            vault_root
            / "Records"
            / "dummy_alice"
            / "Immunizations"
            / "2019-10-31-ccda-immunizations.md"
        )
        assert immunization_note.exists()
        immunization_text = immunization_note.read_text(encoding="utf-8")
        assert "record_type: immunization" in immunization_text
        assert "Influenza vaccine" in immunization_text
        assert "140" in immunization_text
        assert "2019-10-15" in immunization_text
        assert "FLU-2022" in immunization_text
        assert "source_path: processed/dummy_alice/History/2019-10-31-ccda.xml" in immunization_text


def test_extract_ccda_lab_rows_handles_oth_reference_range_and_companion_unit() -> None:
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "tsh-oth.xml"
        _make_ccda_lab_oth_reference_range_xml(source_path)

        rows = extract_ccda_lab_rows(source_path, "dummy_alice")

        tsh_rows = [row for row in rows if row["test_name"] == "TSH"]
        assert len(tsh_rows) == 1
        tsh = tsh_rows[0]
        assert tsh["value"] == 6.39
        assert tsh["unit"] == "uIU/mL"
        assert tsh["reference_range_low"] == 0.4
        assert tsh["reference_range_high"] == 5.0


def _make_ccda_lab_mixed_value_kinds_xml(path: Path) -> None:
    """Results section mixing a plain result, a qualitative value, and a rejected unit.

    Mirrors Epic exports: the companion SNOMED 246514001 "units"
    observation is a sibling of the result, qualitative analytes carry a text
    value with no numeric component, and some analytes report an equivalent but
    differently spelled unit that the LOINC/unit guard rejects.
    """
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20210731"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="2345-7" codeSystem="2.16.840.1.113883.6.1" displayName="Glucose"/>
              <statusCode code="completed"/>
              <effectiveTime value="20210731205856+0000"/>
              <value xsi:type="PQ" value="95" unit="mg/dL"/>
              <entryRelationship typeCode="COMP">
                <observation classCode="OBS" moodCode="EVN">
                  <code code="246514001" codeSystem="2.16.840.1.113883.6.96"
                        codeSystemName="SNOMED CT" displayName="units"/>
                  <value xsi:type="ST">mg/dL</value>
                </observation>
              </entryRelationship>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="5195-3" codeSystem="2.16.840.1.113883.6.1"
                    displayName="Dummy Qualitative Screen"/>
              <statusCode code="completed"/>
              <effectiveTime value="20210731205856+0000"/>
              <value xsi:type="ST">Not Detected</value>
            </observation>
          </entry>
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="17861-6" codeSystem="2.16.840.1.113883.6.1" displayName="Calcium"/>
              <statusCode code="completed"/>
              <effectiveTime value="20210731205856+0000"/>
              <value xsi:type="PQ" value="9.0" unit="MEQ/L"/>
            </observation>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def _make_ccda_lab_unit_variant_xml(path: Path) -> None:
    """Results using vendor unit spellings that are equivalent to the canonical unit.

    Includes a divalent-ion control: calcium reported in MEQ/L must NOT be
    treated as mmol/L, because mEq/L equals mmol/L only at valence 1.
    """
    observations = [
        ("2951-2", "SODIUM", "140", "MEQ/L"),
        ("2823-3", "POTASSIUM", "4.0", "MEQ/L"),
        ("17861-6", "CALCIUM", "9.0", "MEQ/L"),
        ("718-7", "HEMOGLOBIN", "13.2", "GM/DL"),
        ("786-4", "MCHC", "34", "%"),
        ("777-3", "PLATELET COUNT", "249", "10*3"),
    ]
    entries = "\n".join(
        f"""          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="{loinc}" codeSystem="2.16.840.1.113883.6.1" displayName="{name}"/>
              <statusCode code="completed"/>
              <effectiveTime value="20210731205856+0000"/>
              <value xsi:type="PQ" value="{value}" unit="{unit}"/>
            </observation>
          </entry>"""
        for loinc, name, value, unit in observations
    )
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20210731"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
{entries}
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
        encoding="utf-8",
    )


def test_extract_ccda_lab_rows_accepts_reviewed_unit_equivalences() -> None:
    """Reviewed vendor unit spellings resolve to the canonical unit (ADR-035)."""
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "units.xml"
        _make_ccda_lab_unit_variant_xml(source_path)

        rows = {row["test_name"]: row for row in extract_ccda_lab_rows(source_path, "dummy_alice")}

        assert rows["Sodium"]["unit"] == "mmol/L"
        assert rows["Sodium"]["value"] == 140.0
        assert rows["Potassium"]["unit"] == "mmol/L"
        assert rows["Hemoglobin"]["unit"] == "g/dL"
        assert rows["MCHC"]["unit"] == "g/dL"
        assert rows["Platelet count"]["unit"] == "K/uL"


def test_extract_ccda_lab_rows_does_not_convert_divalent_ion_milliequivalents() -> None:
    """mEq/L equals mmol/L only at valence 1; calcium must not inherit the rule."""
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "units.xml"
        _make_ccda_lab_unit_variant_xml(source_path)

        rejected: list[dict[str, Any]] = []
        rows = {
            row["test_name"]: row
            for row in extract_ccda_lab_rows(source_path, "dummy_alice", rejected=rejected)
        }

        # Sodium is monovalent and is in the reviewed table; calcium is divalent,
        # so it must never inherit the equivalence. It stays out of canonical data
        # and is surfaced for review rather than silently converted.
        assert rows["Sodium"]["unit"] == "mmol/L"
        assert "Calcium" not in rows
        calcium_rejections = [
            item for item in rejected if str(item["test_name"]).casefold() == "calcium"
        ]
        assert len(calcium_rejections) == 1
        assert calcium_rejections[0]["unit"] == "MEQ/L"


def test_extract_ccda_lab_rows_skips_companion_units_observation() -> None:
    """The SNOMED 246514001 companion carries a unit, not a result of its own."""
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "mixed.xml"
        _make_ccda_lab_mixed_value_kinds_xml(source_path)

        rows = extract_ccda_lab_rows(source_path, "dummy_alice")

        assert [
            row["test_name"] for row in rows if str(row["test_name"]).casefold() == "units"
        ] == []


def test_extract_ccda_lab_rows_keeps_qualitative_value_as_text() -> None:
    """A non-numeric result belongs in value_text, not discarded (REQ-008)."""
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "mixed.xml"
        _make_ccda_lab_mixed_value_kinds_xml(source_path)

        rows = extract_ccda_lab_rows(source_path, "dummy_alice")

        qualitative = [row for row in rows if row["test_name"] == "Dummy Qualitative Screen"]
        assert len(qualitative) == 1
        assert qualitative[0]["value"] is None
        assert qualitative[0]["value_text"] == "Not Detected"


def test_extract_ccda_lab_rows_surfaces_rejected_observations() -> None:
    """Rejected rows must be reportable, never silently discarded (invariant 5)."""
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "mixed.xml"
        _make_ccda_lab_mixed_value_kinds_xml(source_path)

        rejected: list[dict[str, Any]] = []
        rows = extract_ccda_lab_rows(source_path, "dummy_alice", rejected=rejected)

        assert [row["test_name"] for row in rows] == ["Glucose", "Dummy Qualitative Screen"]
        assert len(rejected) == 1
        assert str(rejected[0]["test_name"]).casefold() == "calcium"
        assert "MEQ/L" in rejected[0]["reason"]


def test_extract_ccda_lab_rows_splits_single_st_quantity_and_rejects_compound_text() -> None:
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "st-quantity.xml"
        _make_ccda_st_quantity_xml(source_path)

        rows = extract_ccda_lab_rows(source_path, "dummy_person")

        assert [(row["test_name"], row["value"], row["unit"]) for row in rows] == [
            ("Average tissue thickness", 88.0, "µm"),
            ("Synthetic point-of-care measure", 0.2, "E.U./dL"),
        ]


def test_extract_ccda_lab_rows_excludes_operational_metadata_before_validation() -> None:
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "operational-metadata.xml"
        _make_ccda_operational_metadata_xml(source_path)
        rejected: list[dict[str, Any]] = []

        rows = extract_ccda_lab_rows(source_path, "dummy_person", rejected=rejected)

        assert [(row["loinc_code"], row["value"]) for row in rows] == [
            ("2345-7", 90.0),
            ("2345-7", 91.0),
        ]
        assert rejected == []


def test_reprocess_operational_metadata_keeps_only_clinical_results_idempotently() -> None:
    from datetime import UTC, datetime

    from famed.fhir_export import build_fhir_bundle, validate_fhir_bundle

    conn = _reprocess_ccda_fixture_twice(_make_ccda_operational_metadata_xml)
    rows = conn.execute(
        "SELECT test_name, value, value_text FROM lab_results ORDER BY value"
    ).fetchall()
    assert rows == [
        ("Dummy Glucose", 90.0, "90"),
        ("Dummy Performing Location Coded Result", 91.0, "91"),
    ]
    rejected_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'lab_observation_rejected'
        """
    ).fetchone()[0]
    assert rejected_count == 0

    bundle, observation_count, *_ = build_fhir_bundle(
        conn,
        "dummy_person",
        generated_at=datetime(2099, 1, 3, tzinfo=UTC),
    )
    observations = [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == "Observation"
    ]
    assert observation_count == 2
    assert sorted(
        (observation["code"]["text"], observation["valueQuantity"]["value"])
        for observation in observations
    ) == [
        ("Dummy Glucose", 90.0),
        ("Dummy Performing Location Coded Result", 91.0),
    ]
    assert validate_fhir_bundle(bundle) == []


def test_extract_ccda_lab_rows_keeps_bounded_results_as_text() -> None:
    """Bounded results remain text, not guessed numeric trend points."""
    from famed.history_sync import extract_ccda_lab_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "comparators.xml"
        _make_ccda_bounded_text_xml(source_path)
        rejected: list[dict[str, Any]] = []

        rows = extract_ccda_lab_rows(source_path, "dummy_person", rejected=rejected)

        assert [
            (row["test_name"], row["value"], row["value_text"], row["unit"]) for row in rows
        ] == [
            ("Dummy symbolic bound", None, "<0.10", ""),
            ("Dummy word bound", None, "above 60", ""),
            (
                "Dummy bounded culture",
                None,
                ">100,000 CFU/mL Synthetic organism",
                "CFU/mL",
            ),
            (
                "Dummy matching culture unit",
                None,
                ">1,000 CFU/mL Synthetic organism",
                "CFU/mL",
            ),
            ("Dummy interval", None, "10-20", "/hpf"),
            ("Dummy semiquantitative result", None, "1+", ""),
            ("Dummy labeled ordinal result", None, "Small (1+)", ""),
        ]
        assert {row["test_name"] for row in rejected} == {
            "Dummy conflicting culture unit",
            "Dummy descending interval",
            "Dummy unitless interval",
            "Dummy non-ST interval",
            "Dummy malformed ordinal",
            "Dummy zero ordinal",
            "Dummy narrative culture",
            "Dummy compound reading",
            "Dummy mixed positive result",
            "Dummy mixed narrative result",
            "Dummy slash narrative result",
            "Dummy staged narrative result",
        }


def test_reprocess_bounded_text_persists_idempotently_and_exports_value_string() -> None:
    from datetime import UTC, datetime

    from famed.fhir_export import build_fhir_bundle, validate_fhir_bundle

    conn = _reprocess_ccda_fixture_twice(_make_ccda_bounded_text_xml)
    rows = conn.execute(
        """
        SELECT test_name, value, value_text, unit
        FROM lab_results
        ORDER BY test_name
        """
    ).fetchall()
    assert rows == [
        (
            "Dummy bounded culture",
            None,
            ">100,000 CFU/mL Synthetic organism",
            "CFU/mL",
        ),
        ("Dummy interval", None, "10-20", "/hpf"),
        ("Dummy labeled ordinal result", None, "Small (1+)", ""),
        (
            "Dummy matching culture unit",
            None,
            ">1,000 CFU/mL Synthetic organism",
            "CFU/mL",
        ),
        ("Dummy semiquantitative result", None, "1+", ""),
        ("Dummy symbolic bound", None, "<0.10", ""),
        ("Dummy word bound", None, "above 60", ""),
    ]
    rejected_events = conn.execute(
        """
        SELECT status, message
        FROM audit_events
        WHERE event_type = 'lab_observation_rejected'
        """
    ).fetchall()
    assert len(rejected_events) == 2
    assert all(status == "awaiting_user_decision" for status, _ in rejected_events)
    assert all("Dummy conflicting culture unit" in message for _, message in rejected_events)
    source_paths = conn.execute(
        """
        SELECT DISTINCT source_path
        FROM lab_results
        WHERE test_name IN (
            'Dummy interval',
            'Dummy labeled ordinal result',
            'Dummy semiquantitative result'
        )
        """
    ).fetchall()
    assert len(source_paths) == 1
    assert ".zip#" in source_paths[0][0]

    bundle, observation_count, *_ = build_fhir_bundle(
        conn,
        "dummy_person",
        generated_at=datetime(2099, 1, 3, tzinfo=UTC),
    )
    observations = [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == "Observation"
    ]
    assert observation_count == 7
    assert sorted(observation["valueString"] for observation in observations) == sorted(
        [
            "<0.10",
            "1+",
            "10-20",
            ">1,000 CFU/mL Synthetic organism",
            ">100,000 CFU/mL Synthetic organism",
            "Small (1+)",
            "above 60",
        ]
    )
    assert validate_fhir_bundle(bundle) == []


def test_extract_ccda_medication_rows_prefers_medication_name_over_sig_and_skips_no_meds() -> None:
    from famed.history_sync import extract_ccda_medication_rows

    with tempfile.TemporaryDirectory() as td:
        source_path = Path(td) / "edge-medications.xml"
        _make_ccda_medication_edge_case_xml(source_path)

        rows = extract_ccda_medication_rows(source_path, "dummy_alice")

        assert [row["medication_name"] for row in rows] == [
            "Ferrous Sulfate (IRON PO)",
            "Magnesium 250 MG CAPS",
            "VITAMIN D PO",
            "famotidine (PEPCID) 20 mg tablet",
        ]
        assert [row["frequency"] for row in rows] == [
            None,
            None,
            None,
            "Take 1 tablet by mouth 2 times daily.",
        ]


def test_reprocess_retained_history_archive_removes_stale_empty_medication_member_rows() -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        archive_dir = processed_root / "dummy_alice" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"

        member_relpath = "IHE_XDM/Dummy Alice/DOC0013.XML"
        temp_xml = tmp / "DOC0013.XML"
        _make_ccda_no_medications_xml(temp_xml)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(temp_xml, arcname=member_relpath)

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        stale_source_path = (
            "processed/dummy_alice/HealthSummary/summary.zip#IHE_XDM/Dummy Alice/DOC0013.XML"
        )
        medication_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        medication_dir.mkdir(parents=True)
        stale_note = medication_dir / "2020-11-29-doc0013-medications.md"
        stale_note.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: medication\n"
            f"source_path: {stale_source_path!r}\n"
            "---\n\n"
            "# Medication List\n\n"
            "## Medications\n\n"
            "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
            "status | start_date | end_date | evidence |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| No known medications | prescription | unresolved |  |  | active |  |  | "
            "No known medications |\n",
            encoding="utf-8",
        )
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, rxnorm_cui, medication_name, medication_type, dose,
                frequency, start_date, end_date, status, source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stale-row",
                "dummy_alice",
                None,
                "No known medications",
                "prescription",
                None,
                None,
                None,
                None,
                "active",
                "zip_xml",
                stale_source_path,
            ),
        )

        for _ in range(2):
            result = reprocess_retained_history_archive(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                archive_path=archive_path,
            )
            assert result.imported_rows == 1

        assert conn.execute(
            "SELECT COUNT(*) FROM medications WHERE source_path = ?", (stale_source_path,)
        ).fetchone() == (0,)
        assert not stale_note.exists()
        assert stale_note.stem not in (medication_dir / "_index.md").read_text(encoding="utf-8")
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE event_type = 'medication_source_note_retired'
              AND source_path = ?
            """,
            (stale_source_path,),
        ).fetchone() == (1,)
        report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        # With no remaining active medications the report is not written; if it
        # exists (e.g. a pre-existing copy), "No known medications" must not appear.
        assert not report_path.exists() or "No known medications" not in report_path.read_text(
            encoding="utf-8"
        )


def test_reprocess_retained_history_archive_preflight_is_all_or_nothing() -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.medication_sync import MedicationSyncError

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        archive_dir = processed_root / "dummy_alice" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"
        member_names = (
            "IHE_XDM/Dummy Alice/DOC0001.XML",
            "IHE_XDM/Dummy Alice/DOC0002.XML",
        )
        member_xml = tmp / "no-medications.xml"
        _make_ccda_no_medications_xml(member_xml)
        with zipfile.ZipFile(archive_path, "w") as archive:
            for member_name in member_names:
                archive.write(member_xml, arcname=member_name)

        source_paths = tuple(
            f"processed/dummy_alice/HealthSummary/summary.zip#{member_name}"
            for member_name in member_names
        )
        medication_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        medication_dir.mkdir(parents=True)
        note_paths: list[Path] = []
        for index, source_path in enumerate(source_paths, start=1):
            note_path = medication_dir / f"2099-01-0{index}-doc000{index}-medications.md"
            note_path.write_text(
                "---\n"
                "person_id: dummy_alice\n"
                "record_type: medication\n"
                "projection_role: source_evidence\n"
                f"source_path: {source_path!r}\n"
                "---\n\n"
                "# Medication List\n\n"
                "## Notes\n" + ("\nKeep this later member.\n" if index == 2 else ""),
                encoding="utf-8",
            )
            note_paths.append(note_path)
        index_path = medication_dir / "_index.md"
        index_path.write_text("original index", encoding="utf-8")
        report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        report_path.parent.mkdir(parents=True)
        report_path.write_text("original dependent output", encoding="utf-8")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        for index, source_path in enumerate(source_paths, start=1):
            conn.execute(
                """
                INSERT INTO medications (
                    id, person_id, medication_name, medication_type, status,
                    source_type, source_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"protected-row-{index}",
                    "dummy_alice",
                    f"Synthetic Medication {index}",
                    "prescription",
                    "active",
                    "zip_xml",
                    source_path,
                ),
            )
        note_bytes_before = [path.read_bytes() for path in note_paths]
        index_before = index_path.read_bytes()
        report_before = report_path.read_bytes()
        rows_before = conn.execute("SELECT * FROM medications ORDER BY id").fetchall()

        with pytest.raises(MedicationSyncError, match="user-authored Notes content"):
            reprocess_retained_history_archive(
                root=tmp,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                archive_path=archive_path,
            )

        assert conn.execute("SELECT * FROM medications ORDER BY id").fetchall() == rows_before
        assert [path.read_bytes() for path in note_paths] == note_bytes_before
        assert index_path.read_bytes() == index_before
        assert report_path.read_bytes() == report_before
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)


def test_reprocess_archive_medications_converge_from_sibling_member_state() -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.medication_sync import medication_row_id
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        archive_dir = processed_root / "dummy_alice" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"

        first_member = "IHE_XDM/Dummy Alice/DOC0001.XML"
        second_member = "IHE_XDM/Dummy Alice/DOC0002.XML"
        first_xml = tmp / "DOC0001.XML"
        second_xml = tmp / "DOC0002.XML"
        _make_mixed_ccda_xml(first_xml)
        _make_mixed_ccda_xml(second_xml)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(first_xml, arcname=first_member)
            archive.write(second_xml, arcname=second_member)

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        reprocess_retained_history_archive(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive_path,
        )

        first_source_path = (
            "processed/dummy_alice/HealthSummary/summary.zip#IHE_XDM/Dummy Alice/DOC0001.XML"
        )
        second_source_path = (
            "processed/dummy_alice/HealthSummary/summary.zip#IHE_XDM/Dummy Alice/DOC0002.XML"
        )
        conn.execute(
            "UPDATE medications SET id = ?, source_path = ? WHERE source_path = ?",
            (
                medication_row_id("dummy_alice", second_source_path, 0),
                second_source_path,
                first_source_path,
            ),
        )

        reprocess_retained_history_archive(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive_path,
        )
        reprocess_retained_history_archive(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive_path,
        )

        rows = conn.execute(
            "SELECT medication_name, source_path FROM medications ORDER BY source_path"
        ).fetchall()
        assert rows == [("Omeprazole 20 mg capsule", first_source_path)]


def test_reprocess_retained_history_archive_reapplies_deterministic_history_acceptance() -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        archive_dir = processed_root / "dummy_alice" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"

        member_relpath = "IHE_XDM/Dummy Alice/DOC0001.XML"
        temp_xml = tmp / "DOC0001.XML"
        temp_xml.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <title>Summary of Care</title>
  <effectiveTime value="20210220"/>
  <recordTarget>
    <patientRole>
      <patient>
        <name><given>Dummy Alice</given><family>Test</family></name>
        <birthTime value="19370211"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <title>Active Problems</title>
          <text>Problem Noted Date Diagnosed Date Hypertension 06/05/2017
          Gastroesophageal reflux disease 03/02/2019</text>
        </section>
      </component>
      <component>
        <section>
          <title>Visit Diagnoses</title>
          <text>Diagnosis Right leg pain - Primary History of cerebrovascular accident
          Arteriosclerosis of coronary artery documented in this encounter</text>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
""",
            encoding="utf-8",
        )
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(temp_xml, arcname=member_relpath)

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        result = reprocess_retained_history_archive(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive_path,
        )

        assert result.imported_rows == 1
        note_text = (
            vault_root
            / "Records"
            / "dummy_alice"
            / "History"
            / "2021-02-20-doc0001-history-review.md"
        ).read_text(encoding="utf-8")
        assert "verification_status: accepted" in note_text
        assert "accepted_facts:" in note_text
        report_text = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Hypertension" in report_text


@pytest.mark.parametrize(
    ("legacy_provenance", "with_decision"), [(False, False), (True, False), (True, True)]
)
def test_reprocess_history_archive_requires_replay_for_interpreted_facts(
    legacy_provenance: bool, with_decision: bool
) -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.retained_replay import RetainedHistoryDecisionInterpreter

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "data" / "processed"
        archive_dir = processed_root / "dummy_alice" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"
        member_name = "IHE_XDM/Dummy Alice/DOC0001.XML"
        source_xml = tmp / "DOC0001.XML"
        source_xml.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ClinicalDocument xmlns="urn:hl7-org:v3">\n'
            "  <title>Synthetic Summary</title>\n"
            '  <effectiveTime value="20201129"/>\n'
            "  <component><structuredBody><component><section>\n"
            "    <title>Clinical Notes</title><text>Synthetic narrative.</text>\n"
            "  </section></component></structuredBody></component>\n"
            "</ClinicalDocument>\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(source_xml, arcname=member_name)

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        reprocess_retained_history_archive(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive_path,
        )
        note_path = next(
            path
            for path in (vault_root / "Records" / "dummy_alice" / "History").glob("*.md")
            if path.name != "_index.md"
        )
        note_text = note_path.read_text(encoding="utf-8")
        front_matter_end = note_text.find("\n---", 3)
        front_matter = yaml.safe_load(note_text[3:front_matter_end])
        archive_member_path = front_matter["source_path"]
        legacy_path = processed_root / "dummy_alice" / "History" / "DOC0001.XML"
        if legacy_provenance:
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_bytes(source_xml.read_bytes())
            legacy_source_path = legacy_path.relative_to(tmp).as_posix()
            note_path.write_text(
                note_text.replace(archive_member_path, legacy_source_path), encoding="utf-8"
            )
            front_matter["source_path"] = legacy_source_path
        accept_history_candidates(
            conn=conn,
            vault_root=vault_root,
            note_path=note_path,
            accepted_rows=[
                {
                    "description": "Synthetic diagnosis",
                    "category": "diagnosis",
                    "recorded_date": "2020-11-29",
                    "confidence": "high",
                }
            ],
            person_id="dummy_alice",
            source_path=front_matter["source_path"],
            extraction_mode="clinical_interpreter",
        )
        original = note_path.read_bytes()
        assert b"extraction_mode: clinical_interpreter" in original
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()

        if with_decision:
            payload = _history_fact_extraction_payload(
                document_date="2020-11-29",
                facts=[
                    {
                        "description": "Synthetic diagnosis",
                        "category": "diagnosis",
                        "recorded_date": "2020-11-29",
                        "confidence": 0.99,
                        "evidence_snippet": "Synthetic narrative.",
                    }
                ],
            )
            interpreter = RetainedHistoryDecisionInterpreter(
                payloads={(archive_member_path, "history"): payload},
                processed_root=processed_root,
            )
            reprocess_retained_history_archive(
                root=tmp,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                archive_path=archive_path,
                fact_interpreter=interpreter,
            )
            replayed = note_path.read_text(encoding="utf-8")
            assert "verification_status: accepted" in replayed
            assert "Synthetic diagnosis" in replayed
            assert archive_member_path in replayed
            assert not legacy_path.exists()
            return

        with pytest.raises(HistorySyncError, match="source-bound retained decisions"):
            reprocess_retained_history_archive(
                root=tmp,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                archive_path=archive_path,
            )

        assert note_path.read_bytes() == original
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == audit_count
        if legacy_provenance:
            assert legacy_path.read_bytes() == source_xml.read_bytes()


def test_reprocess_retained_history_archive_refreshes_lab_and_medication_reports_once() -> None:
    from famed import history_sync
    from famed.history_sync import reprocess_retained_history_archive
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        archive_dir = processed_root / "dummy_alice" / "HealthSummary"
        archive_dir.mkdir(parents=True)
        archive_path = archive_dir / "summary.zip"

        first_xml = tmp / "DOC0001.XML"
        second_xml = tmp / "DOC0002.XML"
        _make_mixed_ccda_xml(first_xml)
        _make_mixed_ccda_xml(second_xml)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(first_xml, arcname="IHE_XDM/Dummy Alice/DOC0001.XML")
            archive.write(second_xml, arcname="IHE_XDM/Dummy Alice/DOC0002.XML")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        lab_refreshes: list[str] = []
        medication_refreshes: list[str] = []

        def fake_refresh_lab_reports(
            conn: sqlite3.Connection,
            vault_root: Path,
            person_id: str,
            *,
            ref_db_path: Path | None = None,
            findings_ledger_path: Path | None = None,
        ) -> list[Path]:
            del conn, vault_root, ref_db_path, findings_ledger_path
            lab_refreshes.append(person_id)
            return []

        def fake_refresh_medication_reports(
            conn: sqlite3.Connection,
            vault_root: Path,
            person_id: str,
            *,
            ref_db_path: Path | None = None,
            findings_ledger_path: Path | None = None,
        ) -> list[Path]:
            del conn, vault_root, ref_db_path, findings_ledger_path
            medication_refreshes.append(person_id)
            return []

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            history_sync,
            "refresh_lab_dependent_reports",
            fake_refresh_lab_reports,
        )
        monkeypatch.setattr(
            history_sync,
            "refresh_medication_dependent_reports",
            fake_refresh_medication_reports,
        )
        try:
            reprocess_retained_history_archive(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                archive_path=archive_path,
            )
        finally:
            monkeypatch.undo()

        assert medication_refreshes == ["dummy_alice"]
        assert lab_refreshes == ["dummy_alice"]


def test_reprocess_history_source_removes_stale_immunization_placeholder() -> None:
    from famed.history_sync import reprocess_retained_history_source
    from famed.setup import SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        history_dir = processed_root / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)

        source_path = history_dir / "2019-10-31-ccda.xml"
        _make_mixed_ccda_xml(source_path)

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        reprocess_retained_history_source(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )
        immunization_note = (
            vault_root
            / "Records"
            / "dummy_alice"
            / "Immunizations"
            / "2019-10-31-ccda-immunizations.md"
        )
        assert immunization_note.exists()
        placeholder_path = (
            vault_root
            / "Records"
            / "dummy_alice"
            / "Immunizations"
            / "2019-10-31-ccda-placeholder.md"
        )
        placeholder_path.parent.mkdir(parents=True, exist_ok=True)
        placeholder_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: category_placeholder\n"
            "category: Immunizations\n"
            "review_status: needs_review\n"
            "source_type: xml\n"
            "source_path: processed/dummy_alice/History/2019-10-31-ccda.xml\n"
            "source_file_name: 2019-10-31-ccda.xml\n"
            "---\n",
            encoding="utf-8",
        )

        _make_ccda_xml(source_path, title="Visit Summary", date="20191031")
        reprocess_retained_history_source(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert not placeholder_path.exists()
        assert not immunization_note.exists()
        assert conn.execute("SELECT COUNT(*) FROM immunizations").fetchone() == (0,)
        assert not (
            vault_root / "Records" / "dummy_alice" / "Reports" / "immunizations.md"
        ).exists()


def test_ccda_immunization_mapping_strips_embedded_date_lists() -> None:
    section = ET.fromstring(
        """
        <section xmlns="urn:hl7-org:v3">
          <text>
            <table>
              <tbody>
                <tr ID="imm1">
                  <td>Moderna SARS-CoV-2 Vaccination (Given 1/6/2019, 4/13/2018, 3/17/2018)</td>
                </tr>
              </tbody>
            </table>
          </text>
          <entry>
            <substanceAdministration>
              <text><reference value="#imm1"/></text>
              <effectiveTime value="20190106"/>
              <statusCode code="completed"/>
              <consumable>
                <manufacturedProduct>
                  <manufacturedMaterial>
                    <code code="207" codeSystem="2.16.840.1.113883.12.292"
                          displayName="COVID-19, mRNA, LNP-S, PF, 100 mcg/0.5 mL dose
                          or 50 mcg/0.25 mL dose"/>
                    <lotNumberText>059H21A</lotNumberText>
                  </manufacturedMaterial>
                </manufacturedProduct>
              </consumable>
            </substanceAdministration>
          </entry>
        </section>
        """
    )

    administration = section.find(
        "./hl7:entry/hl7:substanceAdministration", {"hl7": "urn:hl7-org:v3"}
    )
    assert administration is not None

    mapping = _ccda_immunization_mapping(
        administration,
        section,
        person_id="dummy_alice",
        document_date="2023-05-30",
    )

    assert mapping is not None
    assert mapping["vaccine_name"] == "Moderna SARS-CoV-2 Vaccination"


def test_reprocess_retained_history_source_rejects_wrong_category() -> None:
    from famed.history_sync import HistorySyncError, reprocess_retained_history_source
    from famed.setup import SCHEMA_SQL
    from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        processed_root = tmp / "processed"
        labs_dir = processed_root / "dummy_alice" / "Labs"
        labs_dir.mkdir(parents=True)
        source_path = labs_dir / "panel.pdf"
        source_path.write_bytes(b"%PDF-1.4 fake")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        with pytest.raises(HistorySyncError, match="must be under data/processed"):
            reprocess_retained_history_source(
                root=tmp,
                vault_root=tmp / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )


def test_reprocess_retained_history_source_rejects_unsupported_suffix() -> None:
    from famed.history_sync import HistorySyncError, reprocess_retained_history_source
    from famed.setup import SCHEMA_SQL
    from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        processed_root = tmp / "processed"
        history_dir = processed_root / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        source_path = history_dir / "note.txt"
        source_path.write_text("some text")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        with pytest.raises(HistorySyncError, match="must be a .pdf"):
            reprocess_retained_history_source(
                root=tmp,
                vault_root=tmp / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )


def test_reprocess_retained_history_source_tif_ocr_creates_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from famed import ocr as ocr_module
    from famed.history_sync import reprocess_retained_history_source
    from famed.storage_contract import (
        AUDIT_DEPENDENT_OUTPUT_MARKED_STALE,
        AUDIT_HISTORY_SOURCE_REPROCESSED,
    )

    monkeypatch.setattr(ocr_module, "is_ocr_available", lambda: True)
    monkeypatch.setattr(
        ocr_module,
        "ocr_image_file",
        lambda _path: (
            "Patient: Doe, Jane  DOB: 01/15/1980  Date: 01/09/2020\n"
            "PATHOLOGY REPORT\n"
            "Specimen: Gastric biopsy\n"
            "Diagnosis: Chronic gastritis"
        ),
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vault_root = tmp / "vault"
        processed_root = tmp / "processed"
        history_dir = processed_root / "dummy_alice" / "History"
        history_dir.mkdir(parents=True)
        (vault_root / "Persons").mkdir(parents=True)

        source_path = history_dir / "2020-01-09-pathology.tif"
        source_path.write_bytes(b"tif fixture")

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

        result = reprocess_retained_history_source(
            root=tmp,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 1
        assert result.affected_person_ids == {"dummy_alice"}

        history_notes = [
            p
            for p in (vault_root / "Records" / "dummy_alice" / "History").glob("*.md")
            if p.name != "_index.md"
        ]
        assert len(history_notes) == 1
        note_text = history_notes[0].read_text()
        assert "verification_status: needs_review" in note_text
        assert "record_type: history" in note_text
        assert "## Narrative Sections" in note_text
        assert "Chronic gastritis" in note_text

        rows = conn.execute("SELECT event_type FROM audit_events").fetchall()
        event_types = {r[0] for r in rows}
        assert AUDIT_DEPENDENT_OUTPUT_MARKED_STALE in event_types
        assert AUDIT_HISTORY_SOURCE_REPROCESSED in event_types


def test_write_medical_history_summary_report_hides_encounter_metadata() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2022-06-15",
            "title": "Annual GYN Exam with Dr. Smith",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Annual GYN Exam with Dr. Smith at Example Clinic OBGYN",
                    "category": "other",
                    "recorded_date": "2022-06-15",
                    "confidence": "high",
                },
                {
                    "description": "Mixed hyperlipidemia",
                    "category": "diagnosis",
                    "recorded_date": "2022-06-15",
                    "confidence": "high",
                },
                {
                    "description": "Establish Care with Dr. Jones",
                    "category": "other",
                    "recorded_date": "2022-06-01",
                    "confidence": "high",
                },
                {
                    "description": "Leena Chacko, MD NPI: 1639206139 PCP - General Family Medicine",
                    "category": "other",
                    "recorded_date": "2022-06-01",
                    "confidence": "high",
                },
                {
                    "description": "External films for comparison only — no results in chart",
                    "category": "other",
                    "recorded_date": "2021-01-15",
                    "confidence": "high",
                },
                {
                    "description": "Office Visit with Dr. Smith at Example Clinic",
                    "category": "other",
                    "recorded_date": "2022-03-10",
                    "confidence": "high",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2022-06-15-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Mixed hyperlipidemia" in content
        assert "Annual GYN Exam with Dr. Smith" not in content
        assert "Establish Care with Dr. Jones" not in content
        assert "Leena Chacko, MD NPI" not in content
        assert "External films for comparison" not in content
        assert "Office Visit with Dr. Smith" not in content


def test_write_medical_history_summary_report_suppresses_subsumed_same_day_facts() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2022-10-02",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Preventative health care",
                    "category": "screening",
                    "recorded_date": "2022-10-02",
                    "confidence": "high",
                },
                {
                    "description": "Preventative health care (GYN annual exam)",
                    "category": "screening",
                    "recorded_date": "2022-10-02",
                    "confidence": "high",
                },
                {
                    "description": "Colonoscopy routine screening",
                    "category": "screening",
                    "recorded_date": "2022-10-02",
                    "confidence": "high",
                },
                {
                    "description": "Colonoscopy routine screening — colon cancer surveillance",
                    "category": "screening",
                    "recorded_date": "2022-10-02",
                    "confidence": "high",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2022-10-02-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Preventative health care (GYN annual exam)" in content
        assert content.count("Preventative health care (") == 1
        assert "Colonoscopy routine screening — colon cancer surveillance" in content
        assert content.count("Colonoscopy routine screening —") == 1


def test_write_medical_history_summary_report_shows_review_reason_marker() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-03-15",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Colon cancer screening",
                    "category": "screening",
                    "recorded_date": "2021-03-15",
                    "confidence": "medium",
                    "review_reason": "date inferred from context",
                },
                {
                    "description": "Mixed hyperlipidemia",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-15",
                    "confidence": "medium",
                },
                {
                    "description": "Hypertension",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-15",
                    "confidence": "high",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-03-15-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "(needs source check: date inferred from context)" in content
        assert "[review]" not in content
        assert "Hypertension" in content
        assert "Hypertension [review]" not in content


def test_write_medical_history_summary_report_flags_questionable_diagnosis_support() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2023-02-12",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "High blood pressure",
                    "category": "diagnosis",
                    "recorded_date": "2023-02-12",
                    "confidence": "medium",
                },
                {
                    "description": "Coronary artery disease",
                    "category": "diagnosis",
                    "recorded_date": "2023-02-12",
                    "confidence": "medium",
                },
                {
                    "description": "CAC score 51.9 Agatston",
                    "category": "imaging_finding",
                    "recorded_date": "2022-03-10",
                    "confidence": "high",
                },
                {
                    "description": "Symptoms precipitated by cold air",
                    "category": "other",
                    "recorded_date": "2023-02-12",
                    "confidence": "high",
                },
                {
                    "description": "Alcohol dependence in remission",
                    "category": "diagnosis",
                    "recorded_date": "2023-02-12",
                    "confidence": "medium",
                },
                {
                    "description": "AUDIT-C score: 0; low risk alcohol use",
                    "category": "social_history",
                    "recorded_date": "2023-02-12",
                    "confidence": "high",
                },
                {
                    "description": "Never smoker",
                    "category": "social_history",
                    "recorded_date": "2023-02-12",
                    "confidence": "high",
                },
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2023-02-12-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "## Diagnosis Support Review" in content
        assert (
            "#### High blood pressure "
            "[[#Diagnosis Support Review|support review: source asserted only]]"
        ) in content
        assert (
            "Coronary artery disease "
            "[[#Diagnosis Support Review|support review: partial objective support]]"
        ) in content
        assert (
            "Alcohol dependence in remission "
            "[[#Diagnosis Support Review|support review: conflicting local evidence]]"
        ) in content
        assert "[review]" not in content
        assert "| high blood pressure | source asserted only |" in content
        assert "| coronary artery disease | partial objective support |" in content
        assert "| alcohol dependence in remission | conflicting local evidence |" in content
        assert "Never smoker [support review:" not in content
        assert "| never smoker |" not in content


def test_write_medical_history_summary_report_omits_supported_hypertension_review() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_person_note(vault_root)
        medications_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        medications_dir.mkdir(parents=True)
        (medications_dir / "2023-02-12-medications.md").write_text(
            "\n".join(
                [
                    "---",
                    "person_id: dummy_alice",
                    "record_type: medication_list",
                    "---",
                    "",
                    "## Medications",
                    "",
                    "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
                    "status | start_date | end_date | evidence |",
                    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
                    "| Losartan | prescription |  | 50 mg | daily | active | "
                    "2021-01-01 |  | medication list |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2023-02-12",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Hypertension",
                    "category": "diagnosis",
                    "recorded_date": "2023-02-12",
                    "confidence": "high",
                }
            ],
        }
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2023-02-12-visit-history-review.md").write_text(
            f"---\n{yaml_block}\n---\n\n# History Review\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Hypertension" in content
        assert "## Diagnosis Support Review" not in content
        assert "[support review:" not in content


def test_reprocess_source_script_reprocesses_all_healthsummary_archives(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    reprocess_source_module = importlib.import_module("scripts.reprocess_source")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)

    vault_root = tmp_path / "famed-vault"
    for person_id, display_name in (("dummy_alice", "Dummy Alice"), ("dummy_bob", "Dummy Bob")):
        person_note = vault_root / "Persons" / f"{person_id}.md"
        person_note.parent.mkdir(parents=True, exist_ok=True)
        person_note.write_text(
            (
                "---\n"
                f"person_id: {person_id}\n"
                "record_type: person\n"
                f"display_name: {display_name}\n"
                "birth_year: 1980\n"
                "---\n"
                f"# {display_name}\n"
            ),
            encoding="utf-8",
        )

        archive_dir = data_dir / "processed" / person_id / "HealthSummary"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / "summary.zip"
        xml_path = tmp_path / f"{person_id}-summary.xml"
        _make_mixed_ccda_xml(xml_path)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(xml_path, arcname=f"IHE_XDM/{display_name}/DOC0001.XML")

    monkeypatch.setattr(reprocess_source_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["reprocess_source", "--all-healthsummary-archives"],
    )

    reprocess_source_module.main()

    for person_id in ("dummy_alice", "dummy_bob"):
        trends_path = vault_root / "Records" / person_id / "Reports" / "lab-results.md"
        advisory_path = vault_root / "Records" / person_id / "Reports" / "health-advisory.md"
        assert trends_path.exists()
        assert advisory_path.exists()
        assert "Glucose" in trends_path.read_text(encoding="utf-8")


def test_history_summary_suppresses_procedure_order_when_later_result_exists() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        def _write(filename: str, event_date: str, facts: list[dict[str, Any]]) -> None:
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": f"{filename}.pdf",
                "source_path": f"data/processed/dummy_alice/History/{filename}.pdf",
                "event_date": event_date,
                "title": filename,
                "verification_status": "accepted",
                "accepted_facts": facts,
            }
            (history_dir / f"{filename}-history-review.md").write_text(
                f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()}\n---\n\n",
                encoding="utf-8",
            )

        _write(
            "2019-10-31-visit",
            "2019-10-31",
            [
                {
                    "description": "Colonoscopy ordered (colon cancer screening)",
                    "category": "follow_up",
                    "recorded_date": "2019-10-31",
                },
                {
                    "description": "Suprep Bowel Prep Kit — prescribed for colonoscopy prep",
                    "category": "medication",
                    "recorded_date": "2019-10-31",
                },
            ],
        )
        _write(
            "2020-01-09-colonoscopy",
            "2020-01-09",
            [
                {
                    "description": "Polypectomy — adenoma removed during colonoscopy",
                    "category": "procedure",
                    "recorded_date": "2020-01-09",
                },
            ],
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Polypectomy" in content
        assert "Colonoscopy ordered" not in content
        assert "Suprep Bowel Prep Kit" not in content


def test_history_summary_keeps_procedure_order_without_later_result() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-06-01",
            "title": "Visit",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "MRI spine ordered",
                    "category": "follow_up",
                    "recorded_date": "2021-06-01",
                },
            ],
        }
        (history_dir / "2021-06-01-visit-history-review.md").write_text(
            f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()}\n---\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "MRI spine ordered" in content


def test_history_summary_suppresses_empty_body_chronic_issue_sections() -> None:
    # Same diagnosis on 3+ distinct visit dates → all land in one issue group (same
    # description → same issue heading) with empty body → Part B suppresses the section.
    # The Digest still shows the condition with first/last dates.
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        for i, date in enumerate(("2017-01-15", "2018-06-10", "2019-11-30")):
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": f"visit{i}.pdf",
                "source_path": f"data/processed/dummy_alice/History/visit{i}.pdf",
                "event_date": date,
                "title": f"Visit {i}",
                "verification_status": "accepted",
                "accepted_facts": [
                    {"description": "Hypertension", "category": "diagnosis", "recorded_date": date},
                ],
            }
            (history_dir / f"{date}-visit{i}-history-review.md").write_text(
                f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()}\n---\n\n",
                encoding="utf-8",
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "## Digest" in content
        assert "Hypertension" in content
        assert "#### Hypertension" not in content


def test_history_summary_keeps_chronic_section_with_meaningful_detail() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        def _write(filename: str, event_date: str, facts: list[dict[str, Any]]) -> None:
            fm: dict[str, Any] = {
                "person_id": "dummy_alice",
                "record_type": "history",
                "source_file_name": f"{filename}.pdf",
                "source_path": f"data/processed/dummy_alice/History/{filename}.pdf",
                "event_date": event_date,
                "title": filename,
                "verification_status": "accepted",
                "accepted_facts": facts,
            }
            (history_dir / f"{filename}-history-review.md").write_text(
                f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()}\n---\n\n",
                encoding="utf-8",
            )

        _write(
            "2017-01-15-visit",
            "2017-01-15",
            [
                {
                    "description": "Hypertension",
                    "category": "diagnosis",
                    "recorded_date": "2017-01-15",
                }
            ],
        )
        _write(
            "2019-05-10-visit",
            "2019-05-10",
            [
                {
                    "description": "Hypertension",
                    "category": "diagnosis",
                    "recorded_date": "2019-05-10",
                    "value": "Stage 2",
                }
            ],
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "#### Hypertension" in content
        assert "Stage 2" in content


def test_history_summary_rolls_up_repeated_chronic_mentions_in_multi_condition_issue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        _make_history_note(
            history_dir,
            "2018-01-10-visit-history-review.md",
            event_date="2018-01-10",
            title="2018-01-10-visit",
            source_file_name="2018-01-10-visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "High cholesterol",
                    "category": "diagnosis",
                    "recorded_date": "2018-01-10",
                }
            ],
        )
        _make_history_note(
            history_dir,
            "2019-03-15-visit-history-review.md",
            event_date="2019-03-15",
            title="2019-03-15-visit",
            source_file_name="2019-03-15-visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "Hyperlipidemia",
                    "category": "diagnosis",
                    "recorded_date": "2019-03-15",
                }
            ],
        )
        _make_history_note(
            history_dir,
            "2020-07-01-visit-history-review.md",
            event_date="2020-07-01",
            title="2020-07-01-visit",
            source_file_name="2020-07-01-visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "LDL Cholesterol",
                    "category": "diagnosis",
                    "recorded_date": "2020-07-01",
                    "value": "190 mg/dL",
                }
            ],
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Lipids and Vascular Risk")
        body = content[heading:]
        assert (
            "Hyperlipidemia / high cholesterol. First noted: 2018-01-10."
            " Last mentioned: 2019-03-15." in body
        )
        # The bare repeated mentions no longer each get their own dated bullet.
        assert "- 2018-01-10" not in body
        assert "- 2019-03-15" not in body
        # The concrete value-bearing reading stays individually dated.
        assert "- 2020-07-01" in body
        assert "190 mg/dL" in body


def test_history_summary_rolls_up_repeated_urinary_incontinence_mentions() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        for note_date in ("2022-07-29", "2022-07-30", "2022-08-05", "2022-08-12"):
            _make_history_note(
                history_dir,
                f"{note_date}-visit-history-review.md",
                event_date=note_date,
                title=f"{note_date}-visit",
                source_file_name=f"{note_date}-visit.pdf",
                verification_status="accepted",
                accepted_facts=[
                    {
                        "description": "Urinary incontinence, mixed",
                        "category": "diagnosis",
                        "recorded_date": note_date,
                    }
                ],
            )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Genitourinary")
        body = content[heading:]
        assert "Urinary incontinence. First noted: 2022-07-29. Last mentioned: 2022-08-12." in body
        # The bare repeated mentions no longer each get their own dated bullet.
        assert "- 2022-07-29" not in body
        assert "- 2022-08-12" not in body


def test_history_digest_chronic_label_does_not_match_fecal_incontinence() -> None:
    """The "Urinary incontinence" allowlist entry must not catch unrelated GI
    diagnoses that also contain the bare word "incontinence"."""
    assert (
        _history_digest_chronic_label(
            {"category": "diagnosis", "description": "Fecal incontinence"}
        )
        is None
    )
    assert (
        _history_digest_chronic_label(
            {"category": "diagnosis", "description": "Bowel incontinence"}
        )
        is None
    )
    assert (
        _history_digest_chronic_label(
            {"category": "diagnosis", "description": "Urinary incontinence, mixed"}
        )
        == "Urinary incontinence"
    )
    assert (
        _history_digest_chronic_label(
            {"category": "diagnosis", "description": "Stress incontinence of urine"}
        )
        == "Urinary incontinence"
    )


def test_history_medical_area_does_not_route_st_or_respiratory_depression_to_psychiatry() -> None:
    """Bare "depression" would also match unrelated cardiac/respiratory phrases;
    these specific known collisions must not be routed to Psychiatry."""
    st_depression = {"fact": {"category": "diagnosis", "description": "ST depression on EKG"}}
    assert _history_medical_area(st_depression, {}) != ("Psychiatry", "Mental Health")

    respiratory_depression = {
        "fact": {"category": "diagnosis", "description": "Respiratory depression"}
    }
    assert _history_medical_area(respiratory_depression, {}) != ("Psychiatry", "Mental Health")

    real_depression = {"fact": {"category": "diagnosis", "description": "Depression"}}
    assert _history_medical_area(real_depression, {}) == ("Psychiatry", "Mental Health")


def test_history_medical_area_does_not_route_fallopian_to_injury() -> None:
    """Bare "fall" would also match inside "fallopian"; require it as a
    standalone word."""
    fallopian = {
        "fact": {"category": "diagnosis", "description": "History of fallopian tube ligation"}
    }
    assert _history_medical_area(fallopian, {}) != ("Musculoskeletal", "Injury")

    real_fall = {"fact": {"category": "diagnosis", "description": "Fall"}}
    assert _history_medical_area(real_fall, {}) == ("Musculoskeletal", "Injury")


def test_history_summary_keeps_single_chronic_mention_individually_dated() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "2019-04-01-visit-history-review.md",
            event_date="2019-04-01",
            title="Visit",
            source_file_name="visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "High cholesterol",
                    "category": "diagnosis",
                    "recorded_date": "2019-04-01",
                },
            ],
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Lipids and Vascular Risk")
        body = content[heading:]
        assert "- 2019-04-01" in body
        assert "First noted:" not in body


def test_history_summary_suppresses_context_redundant_with_same_issue_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        _make_history_note(
            history_dir,
            "2018-06-01-visit-history-review.md",
            event_date="2018-06-01",
            title="2018-06-01-visit",
            source_file_name="2018-06-01-visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "History of GERD",
                    "category": "diagnosis",
                    "recorded_date": "2018-06-01",
                }
            ],
        )
        _make_history_note(
            history_dir,
            "2019-01-01-visit-history-review.md",
            event_date="2019-01-01",
            title="2019-01-01-visit",
            source_file_name="2019-01-01-visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "GERD",
                    "category": "diagnosis",
                    "recorded_date": "2019-01-01",
                    "value": "controlled with PPI",
                }
            ],
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Upper GI / Reflux")
        body = content[heading:]
        assert "controlled with PPI" in body
        assert "Historical context" not in body


def test_history_summary_keeps_context_when_sole_evidence_for_issue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _make_history_note(
            history_dir,
            "2017-09-01-visit-history-review.md",
            event_date="2017-09-01",
            title="Visit",
            source_file_name="visit.pdf",
            verification_status="accepted",
            accepted_facts=[
                {
                    "description": "History of GERD",
                    "category": "diagnosis",
                    "recorded_date": "2017-09-01",
                }
            ],
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Upper GI / Reflux")
        body = content[heading:]
        assert "Historical context" in body
        assert "History of GERD" in body


def _write_two_medication_visits(
    history_dir: Path, first_description: str, second_description: str
) -> None:
    _make_history_note(
        history_dir,
        "2019-10-31-visit-history-review.md",
        event_date="2019-10-31",
        title="2019-10-31-visit",
        source_file_name="2019-10-31-visit.pdf",
        verification_status="accepted",
        accepted_facts=[
            {
                "description": first_description,
                "category": "medication",
                "recorded_date": "2019-10-31",
            },
        ],
    )
    _make_history_note(
        history_dir,
        "2020-02-17-visit-history-review.md",
        event_date="2020-02-17",
        title="2020-02-17-visit",
        source_file_name="2020-02-17-visit.pdf",
        verification_status="accepted",
        accepted_facts=[
            {
                "description": second_description,
                "category": "medication",
                "recorded_date": "2020-02-17",
            },
        ],
    )


def test_history_summary_suppresses_unchanged_medication_repeat() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_two_medication_visits(
            history_dir, "Omeprazole 20 mg daily — active", "Omeprazole 20 mg daily — continued"
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Medication Changes")
        events_start = content.index("Events", heading)
        next_heading = content.find("####", events_start)
        events_body = content[events_start : next_heading if next_heading != -1 else len(content)]
        assert "2019-10-31" in events_body
        assert "Omeprazole 20 mg daily — active" in events_body
        # The unchanged "continued" restatement on the later date is suppressed
        # from Events (the "Reported on" line above Events still lists both
        # source dates for provenance).
        assert "2020-02-17" not in events_body
        assert "continued" not in events_body


def test_history_summary_keeps_medication_dose_change() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_two_medication_visits(
            history_dir, "Omeprazole 20 mg daily — active", "Omeprazole 40 mg daily — active"
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Medication Changes")
        body = content[heading:]
        # A real dose change is a different identity string, so both dates render.
        assert "Omeprazole 20 mg daily — active" in body
        assert "Omeprazole 40 mg daily — active" in body


def test_history_summary_keeps_medication_status_change() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
        _write_two_medication_visits(
            history_dir,
            "Pepcid (famotidine) 20 mg — PRN",
            "Pepcid (famotidine) 20 mg — discontinued",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        heading = content.index("#### Medication Changes")
        body = content[heading:]
        # A real status change (PRN -> discontinued) is not a no-op; both render.
        assert "Pepcid (famotidine) 20 mg — PRN" in body
        assert "Pepcid (famotidine) 20 mg — discontinued" in body


def test_medication_identity_for_missing_description_is_unique_per_fact() -> None:
    fact_a: dict[str, Any] = {"category": "medication", "recorded_date": "2019-10-31"}
    fact_b: dict[str, Any] = {"category": "medication", "recorded_date": "2020-02-17"}
    identity_a, status_a = _medication_identity_and_status(fact_a)
    identity_b, status_b = _medication_identity_and_status(fact_b)
    # Two distinct medication facts that both happen to be missing a
    # description must never collapse into one shared placeholder identity.
    assert identity_a != identity_b
    assert status_a is None
    assert status_b is None


def test_suppress_unchanged_medication_repeats_keeps_facts_missing_description() -> None:
    fact_a: dict[str, Any] = {"category": "medication", "recorded_date": "2019-10-31"}
    fact_b: dict[str, Any] = {"category": "medication", "recorded_date": "2020-02-17"}
    items = [
        {"fact": fact_a, "entry": {}},
        {"fact": fact_b, "entry": {}},
    ]
    result = _suppress_unchanged_medication_repeats(items)
    assert result == items


def test_history_summary_collapses_same_day_condition_aliases_to_preferred_label() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        fm: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "summary.xml",
            "source_path": "data/processed/dummy_alice/HealthSummary/summary.zip#DOC0001.XML",
            "event_date": "2021-03-10",
            "title": "Summary of Care",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "PAF",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                },
                {
                    "description": "Paroxysmal atrial fibrillation",
                    "category": "diagnosis",
                    "recorded_date": "2021-03-10",
                },
            ],
        }
        (history_dir / "2021-03-10-summary-history-review.md").write_text(
            f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()}\n---\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Paroxysmal atrial fibrillation" in content
        assert "PAF" not in content
        assert "#### PAF" not in content


def test_history_summary_prefers_natural_language_condition_name_over_coded_label() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")

        first_note: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "summary.xml",
            "source_path": "data/processed/dummy_alice/HealthSummary/summary.zip#DOC0001.XML",
            "event_date": "2021-01-10",
            "title": "Summary of Care",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Malignant neoplasm of prostate",
                    "category": "diagnosis",
                    "recorded_date": "2021-01-10",
                }
            ],
        }
        second_note: dict[str, Any] = {
            "person_id": "dummy_alice",
            "record_type": "history",
            "source_file_name": "visit.pdf",
            "source_path": "data/processed/dummy_alice/History/visit.pdf",
            "event_date": "2021-06-15",
            "title": "Urology Follow-up",
            "verification_status": "accepted",
            "accepted_facts": [
                {
                    "description": "Prostate cancer",
                    "category": "diagnosis",
                    "recorded_date": "2021-06-15",
                }
            ],
        }
        first_yaml = yaml.safe_dump(first_note, sort_keys=False, allow_unicode=False).strip()
        second_yaml = yaml.safe_dump(second_note, sort_keys=False, allow_unicode=False).strip()
        (history_dir / "2021-01-10-summary-history-review.md").write_text(
            f"---\n{first_yaml}\n---\n\n",
            encoding="utf-8",
        )
        (history_dir / "2021-06-15-visit-history-review.md").write_text(
            f"---\n{second_yaml}\n---\n\n",
            encoding="utf-8",
        )

        write_medical_history_summary_report(vault_root, "dummy_alice")

        content = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
        ).read_text(encoding="utf-8")
        assert "Prostate cancer" in content
        assert "Malignant neoplasm of prostate" not in content
        # Two dated body events plus one Digest line (ADR-029).
        assert "- Prostate cancer. First noted: 2021-01-10." in content
        assert content.count("Prostate cancer") == 3


# ---------------------------------------------------------------------------
# Non-vital fact value rendering + ISO source-stem date parsing
# (coronary-calcium note defect: values dropped at render; undated- misname).
# ---------------------------------------------------------------------------


def test_render_facts_grouped_includes_value_and_unit_for_non_vital_fact() -> None:
    facts = [
        {
            "description": "Observed Agatston coronary calcium score",
            "category": "imaging_finding",
            "recorded_date": "2022-03-10",
            "value": "51.9",
            "unit": "Agatston score",
            "confidence": "high",
        },
        {
            "description": "Coronary calcium score percentile for age/sex/race cohort",
            "category": "imaging_finding",
            "recorded_date": "2022-03-10",
            "value": "57",
            "unit": "percentile",
            "confidence": "high",
        },
    ]
    rendered = _render_facts_grouped(facts, note_date="2022-03-10")
    assert "Observed Agatston coronary calcium score: 51.9 Agatston score" in rendered
    assert "Coronary calcium score percentile for age/sex/race cohort: 57 percentile" in rendered


def test_render_facts_grouped_omits_value_suffix_when_no_value() -> None:
    facts = [
        {
            "description": "CT coronary artery calcium scoring performed",
            "category": "procedure",
            "recorded_date": "2022-03-10",
            "value": None,
            "unit": None,
            "confidence": "high",
        },
    ]
    rendered = _render_facts_grouped(facts, note_date="2022-03-10")
    assert "CT coronary artery calcium scoring performed" in rendered
    assert "CT coronary artery calcium scoring performed:" not in rendered


def test_date_from_source_stem_parses_iso_date() -> None:
    assert _date_from_source_stem("2022-03-10-Coronary Artery Calcium Scoring") == "2022-03-10"


def test_date_from_source_stem_still_parses_compact_yymmdd() -> None:
    assert _date_from_source_stem("report-220310-labs") == "2022-03-10"


def test_date_from_source_stem_rejects_invalid_iso_date() -> None:
    assert _date_from_source_stem("2025-13-40-bad") is None


def test_date_from_source_stem_returns_none_without_date() -> None:
    assert _date_from_source_stem("Coronary Artery Calcium Scoring") is None


def test_ccda_extraction_captures_source_interpretation_flag(tmp_path: Path) -> None:
    """The C-CDA states an abnormality flag per observation. Discarding it lost
    the lab's judgement on individual results, including critically-abnormal
    ones, and left a panel-level verdict as the only signal.
    """
    xml = """<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <component><structuredBody><component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
    <entry><organizer><component><observation classCode="OBS" moodCode="EVN">
      <code code="2345-7" codeSystem="2.16.840.1.113883.6.1" displayName="Glucose"/>
      <effectiveTime value="20230420080000"/>
      <value xsi:type="PQ" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
             value="95" unit="mg/dL"/>
      <interpretationCode code="AA"/>
      <referenceRange><observationRange><value xsi:type="IVL_PQ"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
        <low value="70"/><high value="100"/></value></observationRange></referenceRange>
    </observation></component></organizer></entry>
  </section></component></structuredBody></component>
</ClinicalDocument>
"""
    source = tmp_path / "doc.xml"
    source.write_text(xml, encoding="utf-8")

    rows = extract_ccda_lab_rows(source, "dummy_alice")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Glucose"
    assert rows[0]["interpretation_code"] == "AA"


def _write_household_person_note(vault_root: Path, person_id: str, birth_year: int) -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    person_note.write_text(
        (
            f"---\nperson_id: {person_id}\nrecord_type: person\n"
            f"display_name: {person_id}\nbirth_year: {birth_year}\nsex: female\n"
            f"---\n# {person_id}\n"
        ),
        encoding="utf-8",
    )
    (vault_root / "Records" / person_id / "Reports").mkdir(parents=True, exist_ok=True)


def _link_parent_and_child(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    conn.executemany(
        "INSERT INTO family_links (id, source_person_id, target_person_id, relationship_type) "
        "VALUES (?, ?, ?, ?)",
        [
            (f"{child_id}-{parent_id}", child_id, parent_id, "parent"),
            (f"{parent_id}-{child_id}", parent_id, child_id, "child"),
        ],
    )


def test_accept_history_candidates_refreshes_subject_and_blood_relative_reports(
    tmp_path: Path,
) -> None:
    """Accepted history reaches every report that reads it (CORE principle 7, ADR-010)."""
    vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
    _write_household_person_note(vault_root, "dummy_alice", birth_year=1960)
    _write_household_person_note(vault_root, "dummy_bob", birth_year=1990)
    conn = _make_db()
    _link_parent_and_child(conn, parent_id="dummy_alice", child_id="dummy_bob")
    note_path = _make_history_note_full(
        history_dir,
        "2021-01-15-visit-history-review.md",
        event_date="2021-01-15",
        title="Annual Visit",
        source_file_name="visit.pdf",
    )

    accept_history_candidates(
        conn=conn,
        vault_root=vault_root,
        note_path=note_path,
        accepted_rows=[
            {"description": "Asthma", "category": "diagnosis"},
            {
                "description": "Screening colonoscopy, normal",
                "category": "procedure",
                "recorded_date": "2021-01-15",
            },
        ],
        person_id="dummy_alice",
        source_path="data/processed/dummy_alice/History/visit.pdf",
    )

    own_advisory = vault_root / "Records" / "dummy_alice" / "Reports" / "health-advisory.md"
    child_advisory = vault_root / "Records" / "dummy_bob" / "Reports" / "health-advisory.md"
    assert "last done 2021-01-15" in own_advisory.read_text(encoding="utf-8")
    child_text = child_advisory.read_text(encoding="utf-8")
    assert "## Family History Context" in child_text
    assert "Asthma" in child_text


def test_accept_history_candidates_leaves_non_blood_relatives_untouched(
    tmp_path: Path,
) -> None:
    """A spouse's reports do not read the subject's history (ADR-028)."""
    vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
    _write_household_person_note(vault_root, "dummy_alice", birth_year=1960)
    _write_household_person_note(vault_root, "dummy_carol", birth_year=1960)
    conn = _make_db()
    conn.executemany(
        "INSERT INTO family_links (id, source_person_id, target_person_id, relationship_type) "
        "VALUES (?, ?, ?, ?)",
        [
            ("dummy_alice-dummy_carol", "dummy_alice", "dummy_carol", "spouse"),
            ("dummy_carol-dummy_alice", "dummy_carol", "dummy_alice", "spouse"),
        ],
    )
    note_path = _make_history_note_full(
        history_dir,
        "2021-01-15-visit-history-review.md",
        event_date="2021-01-15",
        title="Annual Visit",
        source_file_name="visit.pdf",
    )

    accept_history_candidates(
        conn=conn,
        vault_root=vault_root,
        note_path=note_path,
        accepted_rows=[{"description": "Type 2 diabetes", "category": "diagnosis"}],
        person_id="dummy_alice",
        source_path="data/processed/dummy_alice/History/visit.pdf",
    )

    spouse_reports = vault_root / "Records" / "dummy_carol" / "Reports"
    assert not (spouse_reports / "health-advisory.md").exists()


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("History of prostate cancer", "Prostate cancer"),
        ("Malignant neoplasm of prostate", "Prostate cancer"),
        ("Breast carcinoma", "Breast cancer"),
        ("Non-Hodgkin lymphoma", "Lymphoma"),
        ("Cerebrovascular accident (CVA)", "Stroke / cerebrovascular disease"),
        ("History of stroke", "Stroke / cerebrovascular disease"),
        (
            "Cerebral infarction without residual deficits",
            "Stroke / cerebrovascular disease",
        ),
        ("Transient ischemic attack (TIA)", "Stroke / cerebrovascular disease"),
    ],
)
def test_history_digest_includes_cancer_and_cerebrovascular_history(
    description: str, expected: str
) -> None:
    """A cancer or stroke is background a reader must see first (ADR-029)."""
    fact = {"category": "diagnosis", "description": description}

    assert _history_digest_label(fact) == expected
    assert _history_digest_chronic_label(fact) is None


@pytest.mark.parametrize(
    "description",
    [
        "Screen for colon cancer",
        "Colonoscopy biopsy showed adenoma and hyperplastic polyps; no cancer",
        "Chronic gastritis; negative for H. pylori, dysplasia, and malignancy",
        "Family history of malignant neoplasm of prostate in father",
        "Family history of stroke",
        "Rule out stroke",
    ],
)
def test_history_digest_excludes_negated_screening_and_family_mentions(
    description: str,
) -> None:
    """Screening, negation, and relatives' history never become the person's Digest."""
    fact = {"category": "diagnosis", "description": description}

    assert _history_digest_label(fact) is None


def _write_accepted_history_note(
    history_dir: Path, event_date: str, facts: list[dict[str, Any]]
) -> None:
    fm: dict[str, Any] = {
        "person_id": "dummy_alice",
        "record_type": "history",
        "source_file_name": f"{event_date}.pdf",
        "source_path": f"data/processed/dummy_alice/History/{event_date}.pdf",
        "event_date": event_date,
        "title": "Visit",
        "verification_status": "accepted",
        "accepted_facts": facts,
    }
    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=False).strip()
    (history_dir / f"{event_date}-visit-history-review.md").write_text(
        f"---\n{yaml_block}\n---\n\n", encoding="utf-8"
    )


def test_identical_history_events_on_three_or_more_dates_roll_up(tmp_path: Path) -> None:
    """A fact restated at every visit renders once with its span and count."""
    vault_root, history_dir = _scaffold(tmp_path, "dummy_alice")
    dates = ["2017-05-14", "2018-06-08", "2019-03-01", "2020-06-12"]
    for event_date in dates:
        _write_accepted_history_note(
            history_dir,
            event_date,
            [{"description": "Mass of urinary bladder", "category": "diagnosis"}],
        )
    _write_accepted_history_note(
        history_dir,
        "2021-01-10",
        [{"description": "Gross hematuria", "category": "diagnosis"}],
    )
    _write_accepted_history_note(
        history_dir,
        "2021-02-11",
        [{"description": "Gross hematuria", "category": "diagnosis"}],
    )

    write_medical_history_summary_report(vault_root, "dummy_alice")
    content = (
        vault_root / "Records" / "dummy_alice" / "Reports" / "medical-history-summary.md"
    ).read_text(encoding="utf-8")

    assert content.count("Mass of urinary bladder") == 1
    assert (
        "- Mass of urinary bladder. First noted: 2017-05-14. "
        "Last mentioned: 2020-06-12. (4 mentions)" in content
    )
    # Two mentions stay individually dated.
    assert content.count("Gross hematuria") == 2


def test_history_digest_chronic_label_matches_osteoporosis_diagnosis_not_screening() -> None:
    assert (
        _history_digest_chronic_label(
            {
                "category": "diagnosis",
                "description": "Age-related osteoporosis without current pathological fracture",
            }
        )
        == "Osteoporosis"
    )
    assert (
        _history_digest_chronic_label(
            {"category": "screening", "description": "Screening for osteoporosis"}
        )
        is None
    )
