from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from famed.clinical_interpreter import (
    FACT_EXTRACTION_SCHEMA_VERSION,
    CandidateMedicationFact,
    InterpreterEvidence,
    InterpreterFactExtraction,
)
from famed.medication_sync import (
    MedicationSyncError,
    build_medication_conflicts_from_db,
    build_supplement_extraction,
    canonicalize_candidate_medication_facts,
    infer_person_id_from_processed_medication_path,
    medication_name_key,
    medication_row_id,
    parse_medication_csv,
    parse_medication_markdown_table,
    parse_supplement_text,
    persist_medication_rows,
    quarantine_medication_import_error,
    record_medication_conflict_resolution_events,
    render_medication_conflicts_report,
    render_medication_note,
    replace_medications_for_source_path,
    reprocess_retained_supplement_source,
    retire_empty_medication_source_notes,
    upsert_medication,
    write_current_medications_report,
    write_medication_conflicts_report,
    write_medication_note,
)
from famed.reprocess_sources import ReprocessError
from famed.setup import SCHEMA_SQL
from famed.storage_contract import ensure_audit_events_table

_EVIDENCE = (InterpreterEvidence(field="medication_name", snippet="Sample", confidence=0.95),)


def _make_extraction(
    facts: list[CandidateMedicationFact],
    person_id: str = "dummy_alice",
) -> InterpreterFactExtraction:
    return InterpreterFactExtraction(
        schema_version=FACT_EXTRACTION_SCHEMA_VERSION,
        person_id=person_id,
        person_confidence=0.95,
        category="medications",
        category_confidence=0.95,
        facts=tuple(facts),
        evidence=_EVIDENCE,
        overall_confidence=0.95,
        document_date=None,
    )


def _make_fact(
    name: str = "Lisinopril",
    medication_type: str = "prescription",
    dose: str | None = "10 mg",
    status: str | None = "active",
) -> CandidateMedicationFact:
    return CandidateMedicationFact(
        name=name,
        dose=dose,
        frequency="daily",
        status=status,
        medication_type=medication_type,  # type: ignore[arg-type]
        confidence=0.95,
    )


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "famed.db")
    conn.executescript(SCHEMA_SQL)
    return conn


def _scaffold_person(vault_root: Path, person_id: str = "dummy_alice") -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    person_note.write_text(
        f"---\nperson_id: {person_id}\ndisplay_name: Dummy Alice\n---\n", encoding="utf-8"
    )


def test_retire_empty_medication_source_notes_preserves_user_notes(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    ensure_audit_events_table(conn)
    vault_root = tmp_path / "vault"
    _scaffold_person(vault_root)
    medication_dir = vault_root / "Records" / "dummy_alice" / "Medications"
    medication_dir.mkdir(parents=True)
    source_path = (
        "data/processed/dummy_alice/HealthSummary/summary.zip#IHE_XDM/Dummy Alice/DOC0001.XML"
    )
    note_path = medication_dir / "2099-01-01-doc0001-medications.md"
    content = render_medication_note(
        [
            {
                "person_id": "dummy_alice",
                "medication_name": "Synthetic Medication",
                "medication_type": "prescription",
                "status": "active",
            }
        ],
        source_file_name="summary.zip#DOC0001.XML",
        source_path=source_path,
    )
    note_path.write_text(content + "\nKeep this user-authored context.\n", encoding="utf-8")
    original = note_path.read_bytes()

    with pytest.raises(
        MedicationSyncError,
        match="cannot retire empty medication source note with user-authored Notes content",
    ):
        retire_empty_medication_source_notes(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_paths=(source_path,),
        )

    assert note_path.read_bytes() == original
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'medication_source_note_retired'"
    ).fetchone() == (0,)


def test_retire_empty_medication_source_notes_ignores_non_source_projection_role(
    tmp_path: Path,
) -> None:
    conn = _db(tmp_path)
    ensure_audit_events_table(conn)
    vault_root = tmp_path / "vault"
    _scaffold_person(vault_root)
    medication_dir = vault_root / "Records" / "dummy_alice" / "Medications"
    medication_dir.mkdir(parents=True)
    source_path = "data/processed/dummy_alice/History/summary.xml"
    note_path = medication_dir / "2099-01-01-user-update.md"
    note_path.write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: medication\n"
        "projection_role: user_update\n"
        f"source_path: {source_path}\n"
        "---\n\n"
        "# Medication Update\n",
        encoding="utf-8",
    )
    original = note_path.read_bytes()

    retired = retire_empty_medication_source_notes(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_paths=(source_path,),
    )

    assert retired == []
    assert note_path.read_bytes() == original
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'medication_source_note_retired'"
    ).fetchone() == (0,)


# ---------------------------------------------------------------------------
# canonicalize_candidate_medication_facts
# ---------------------------------------------------------------------------


class TestCanonicalizeCandidateMedicationFacts:
    def test_propagates_prescription_type(self) -> None:
        fact = _make_fact(medication_type="prescription")
        rows = canonicalize_candidate_medication_facts(_make_extraction([fact]), "src")
        assert rows[0]["medication_type"] == "prescription"

    def test_propagates_supplement_type(self) -> None:
        fact = _make_fact(name="Vitamin D", medication_type="supplement")
        rows = canonicalize_candidate_medication_facts(_make_extraction([fact]), "src")
        assert rows[0]["medication_type"] == "supplement"

    def test_propagates_otc_type(self) -> None:
        fact = _make_fact(name="Ibuprofen", medication_type="otc")
        rows = canonicalize_candidate_medication_facts(_make_extraction([fact]), "src")
        assert rows[0]["medication_type"] == "otc"

    def test_all_valid_types_accepted(self) -> None:
        for med_type in ("prescription", "otc", "supplement", "herbal", "vaccine"):
            fact = _make_fact(medication_type=med_type)
            rows = canonicalize_candidate_medication_facts(_make_extraction([fact]), "src")
            assert rows[0]["medication_type"] == med_type

    def test_includes_person_id_and_name(self) -> None:
        fact = _make_fact(name="Metformin")
        rows = canonicalize_candidate_medication_facts(
            _make_extraction([fact], person_id="dummy_bob"), "src"
        )
        assert rows[0]["person_id"] == "dummy_bob"
        assert rows[0]["medication_name"] == "Metformin"

    def test_raises_on_empty_facts(self) -> None:
        with pytest.raises(MedicationSyncError, match="no medication facts found"):
            canonicalize_candidate_medication_facts(_make_extraction([]), "src")

    def test_raises_on_missing_name(self) -> None:
        # CandidateMedicationFact enforces name at construction; test blank after normalization
        fact = CandidateMedicationFact(name="   ", medication_type="prescription", confidence=1.0)
        with pytest.raises(MedicationSyncError, match="medication name is required"):
            canonicalize_candidate_medication_facts(_make_extraction([fact]), "src")


# ---------------------------------------------------------------------------
# upsert_medication
# ---------------------------------------------------------------------------


class TestUpsertMedication:
    def test_inserts_row_with_medication_type(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        row: dict[str, Any] = {
            "person_id": "dummy_alice",
            "medication_name": "Lisinopril",
            "medication_type": "prescription",
            "rxnorm_cui": None,
            "dose": "10 mg",
            "frequency": "daily",
            "start_date": None,
            "end_date": None,
            "status": "active",
            "source_path": "data/processed/dummy_alice/Medications/2023-01-01-meds.txt",
            "row_index": 0,
        }
        upsert_medication(conn, row)
        stored = conn.execute(
            "SELECT medication_name, medication_type, source_type "
            "FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone()
        assert stored == ("Lisinopril", "prescription", "manual")

    def test_updates_medication_type_on_conflict(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_path = "data/processed/dummy_alice/Medications/2023-01-01-meds.txt"
        row: dict[str, Any] = {
            "person_id": "dummy_alice",
            "medication_name": "Vitamin C",
            "medication_type": "prescription",
            "rxnorm_cui": None,
            "dose": "500 mg",
            "frequency": "daily",
            "start_date": None,
            "end_date": None,
            "status": "active",
            "source_path": source_path,
            "row_index": 0,
        }
        upsert_medication(conn, row)
        row["medication_type"] = "supplement"
        upsert_medication(conn, row)
        stored = conn.execute(
            "SELECT medication_type FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone()
        assert stored == ("supplement",)

    def test_raises_without_source_path(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        row: dict[str, Any] = {
            "person_id": "dummy_alice",
            "medication_name": "Aspirin",
            "medication_type": "otc",
            "rxnorm_cui": None,
            "dose": None,
            "frequency": None,
            "start_date": None,
            "end_date": None,
            "status": "active",
        }
        with pytest.raises(MedicationSyncError, match="source_path is required"):
            upsert_medication(conn, row)


# ---------------------------------------------------------------------------
# replace_medications_for_source_path
# ---------------------------------------------------------------------------


class TestReplaceMedicationsForSourcePath:
    def test_replaces_and_preserves_type(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_path = "data/processed/dummy_alice/Medications/2023-01-01-meds.txt"
        meds = [
            {
                "person_id": "dummy_alice",
                "medication_name": "Lisinopril",
                "medication_type": "prescription",
                "rxnorm_cui": None,
                "dose": "10 mg",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
            },
            {
                "person_id": "dummy_alice",
                "medication_name": "Vitamin D",
                "medication_type": "supplement",
                "rxnorm_cui": None,
                "dose": "2000 IU",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
            },
        ]
        inserted, skipped = replace_medications_for_source_path(conn, source_path, meds)
        assert (inserted, skipped) == (2, 0)
        rows = conn.execute(
            "SELECT medication_name, medication_type FROM medications ORDER BY medication_name"
        ).fetchall()
        assert ("Lisinopril", "prescription") in rows
        assert ("Vitamin D", "supplement") in rows


class TestMedicationQuarantine:
    def test_quarantines_under_medication_lane(self, tmp_path: Path) -> None:
        root = tmp_path
        quarantine_root = root / "data" / "quarantine"
        source_file = root / "data" / "inbox" / "dummy_alice" / "medications" / "list.csv"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("medication_name\nOmeprazole\n", encoding="utf-8")

        quarantine_path = quarantine_medication_import_error(
            None,
            root,
            quarantine_root,
            "dummy_alice",
            source_file,
            "medication_csv",
            "validation failed: example",
        )

        assert quarantine_path == "data/quarantine/dummy_alice/Medications/undated-list.csv"
        assert not source_file.exists()
        assert (quarantine_root / "dummy_alice" / "Medications" / "undated-list.csv").exists()


# ---------------------------------------------------------------------------
# render_medication_note
# ---------------------------------------------------------------------------


class TestRenderMedicationNote:
    def test_medication_type_in_table_not_front_matter(self) -> None:
        meds = [
            {
                "person_id": "dummy_alice",
                "medication_name": "Vitamin C",
                "medication_type": "supplement",
                "rxnorm_cui": None,
                "dose": "500 mg",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
                "route": None,
                "evidence_snippet": None,
            }
        ]
        note = render_medication_note(
            meds,
            source_file_name="supplements.txt",
            source_path="data/processed/dummy_alice/Medications/2023-01-01-supplements.txt",
        )
        fm = yaml.safe_load(note.split("---")[1])
        # Per-row fields must not appear in front matter
        assert "medication_type" not in fm
        assert "medication_name" not in fm
        assert "dose" not in fm
        # medication_type must appear in the table body
        assert "| supplement |" in note

    def test_table_includes_medication_type_column(self) -> None:
        meds = [
            {
                "person_id": "dummy_alice",
                "medication_name": "Aspirin",
                "medication_type": "otc",
                "rxnorm_cui": None,
                "dose": "81 mg",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
                "route": "oral",
                "evidence_snippet": None,
            }
        ]
        note = render_medication_note(
            meds,
            source_file_name="meds.txt",
            source_path="data/processed/dummy_alice/Medications/2023-01-01-meds.txt",
        )
        assert "| medication_type |" in note
        assert "| otc |" in note

    def test_front_matter_contains_only_provenance_fields(self) -> None:
        meds = [
            {
                "person_id": "dummy_alice",
                "medication_name": "Lisinopril",
                "rxnorm_cui": None,
                "dose": "10 mg",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
                "route": None,
                "evidence_snippet": None,
            }
        ]
        note = render_medication_note(
            meds,
            source_file_name="meds.txt",
            source_path="data/processed/dummy_alice/Medications/undated-meds.txt",
        )
        fm = yaml.safe_load(note.split("---")[1])
        assert set(fm.keys()) == {
            "person_id",
            "record_type",
            "source_file_name",
            "source_path",
            "projection_role",
        }
        assert fm["projection_role"] == "source_evidence"
        # medication_type defaults to prescription and appears in table body
        assert "| prescription |" in note

    def test_unresolved_rxnorm_is_visible_in_table(self) -> None:
        meds = [
            {
                "person_id": "dummy_alice",
                "medication_name": "Lisinopril",
                "rxnorm_cui": None,
                "dose": "10 mg",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
                "route": None,
                "evidence_snippet": None,
            }
        ]
        note = render_medication_note(
            meds,
            source_file_name="meds.txt",
            source_path="data/processed/dummy_alice/Medications/undated-meds.txt",
        )
        assert "| unresolved |" in note

    def test_same_source_regeneration_preserves_notes_and_replaces_rows(
        self, tmp_path: Path
    ) -> None:
        note_path = tmp_path / "medications.md"
        first = [
            {
                "person_id": "dummy_alice",
                "medication_name": "OldDrug",
                "status": "active",
            }
        ]
        second = [
            {
                "person_id": "dummy_alice",
                "medication_name": "NewDrug",
                "status": "active",
            }
        ]
        write_medication_note(
            note_path,
            first,
            source_file_name="meds.txt",
            source_path="data/processed/dummy_alice/Medications/meds.txt",
        )
        note_path.write_text(
            note_path.read_text(encoding="utf-8") + "\nCaregiver note.\n",
            encoding="utf-8",
        )

        write_medication_note(
            note_path,
            second,
            source_file_name="meds.txt",
            source_path="data/processed/dummy_alice/Medications/meds.txt",
        )

        content = note_path.read_text(encoding="utf-8")
        assert "OldDrug" not in content
        assert "NewDrug" in content
        assert content.count("Caregiver note.") == 1


# ---------------------------------------------------------------------------
# write_current_medications_report
# ---------------------------------------------------------------------------


class TestWriteCurrentMedicationsReport:
    def _insert_meds(self, conn: sqlite3.Connection, meds: list[dict[str, Any]]) -> None:
        for i, m in enumerate(meds):
            conn.execute(
                """
                INSERT INTO medications
                    (
                        id,
                        person_id,
                        medication_name,
                        medication_type,
                        rxnorm_cui,
                        dose,
                        frequency,
                        start_date,
                        end_date,
                        status,
                        source_type,
                        source_path
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    medication_row_id(m["person_id"], m.get("source_path", "sp"), i),
                    m["person_id"],
                    m["medication_name"],
                    m.get("medication_type", "prescription"),
                    m.get("rxnorm_cui"),
                    m.get("dose"),
                    m.get("frequency"),
                    m.get("start_date"),
                    m.get("end_date"),
                    m.get("status", "active"),
                    m.get("source_type", "manual"),
                    m.get("source_path", "data/processed/dummy_alice/Medications/meds.txt"),
                ),
            )

    def test_report_groups_medication_types_and_schedules(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Lisinopril",
                    "medication_type": "prescription",
                    "frequency": "daily",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Amlodipine",
                    "medication_type": "prescription",
                    "frequency": "daily",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Albuterol",
                    "medication_type": "prescription",
                    "frequency": "as needed",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Antacid",
                    "medication_type": "otc",
                    "frequency": "PRN for heartburn",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Vitamin D",
                    "medication_type": "supplement",
                    "frequency": "once daily",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Turmeric",
                    "medication_type": "supplement",
                    "frequency": "daily as needed",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Magnesium",
                    "medication_type": "supplement",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Herbal Blend",
                    "medication_type": "herbal",
                    "frequency": "weekly",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Example Vaccine",
                    "medication_type": "vaccine",
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "# Current Medications" in content
        # Allergy status leads the list so a prescriber sees it first.
        assert content.index("## Allergies") < content.index("## Prescriptions")
        assert "## Over-the-counter" not in content
        assert content.index("## Prescriptions") < content.index("## Supplements")
        assert content.index("## Supplements") < content.index("## Herbals")
        assert content.index("## Herbals") < content.index("## Vaccines")
        assert content.index("## Vaccines") < content.index("## As needed")
        prescriptions = content.split("## Prescriptions", 1)[1].split("## Supplements", 1)[0]
        assert prescriptions.index("### Regular") < prescriptions.index("Lisinopril")
        assert prescriptions.index("Amlodipine") < prescriptions.index("Lisinopril")
        assert "### As needed" not in prescriptions
        supplements = content.split("## Supplements", 1)[1].split("## Herbals", 1)[0]
        assert supplements.index("### Regular") < supplements.index("Vitamin D")
        assert supplements.index("### Schedule not recorded") < supplements.index("Magnesium")
        assert "| Herbal Blend |  | weekly |" in content
        assert "| Example Vaccine |  |  |" in content
        as_needed = content.split("## As needed", 1)[1]
        assert "| Medication | Type | Dose | Frequency |" in as_needed
        assert as_needed.index("Albuterol") < as_needed.index("Antacid")
        assert as_needed.index("Antacid") < as_needed.index("Turmeric")
        assert "| Albuterol | Prescription |  | as needed |" in as_needed
        assert "| Antacid | OTC |  | as needed |" in as_needed
        assert "| Turmeric | Supplement |  | daily as needed |" in as_needed

    def test_report_no_supplement_section_when_none(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Metformin",
                    "medication_type": "prescription",
                }
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "# Current Medications" in content
        assert "Metformin" in content
        assert "## Supplements" not in content

    def test_report_prescriptions_empty_when_no_prescriptions(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Vitamin C",
                    "medication_type": "supplement",
                }
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "# Current Medications" in content
        assert "Vitamin C" in content
        assert "## Prescriptions" not in content

    def test_report_excludes_discontinued(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "OldDrug",
                    "medication_type": "prescription",
                    "status": "discontinued",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "NewDrug",
                    "medication_type": "prescription",
                    "status": "active",
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "OldDrug" not in content
        assert "NewDrug" in content

    def test_report_excludes_non_current_statuses(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "AbortedDrug",
                    "medication_type": "prescription",
                    "status": "aborted",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "CompletedDrug",
                    "medication_type": "prescription",
                    "status": "completed",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "ActiveDrug",
                    "medication_type": "prescription",
                    "status": "active",
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "AbortedDrug" not in content
        assert "CompletedDrug" not in content
        assert "ActiveDrug" in content

    def test_report_written_under_reports_dir(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(conn, [{"person_id": "dummy_alice", "medication_name": "TestDrug"}])
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        assert (
            report_path
            == vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        )
        assert report_path.exists()

    def test_report_front_matter(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(conn, [{"person_id": "dummy_alice", "medication_name": "TestDrug"}])
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["person_id"] == "dummy_alice"
        assert fm["record_type"] == "report"
        assert fm["report_type"] == "current_medications"
        assert "generated_at" in fm

    def test_reports_index_updated(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(conn, [{"person_id": "dummy_alice", "medication_name": "TestDrug"}])
        conn.commit()
        write_current_medications_report(vault_root, "dummy_alice", conn)
        index_path = vault_root / "Records" / "dummy_alice" / "Reports" / "_index.md"
        assert index_path.exists()
        index_content = index_path.read_text(encoding="utf-8")
        assert "current-medications" in index_content

    def test_report_omits_evidence_link_without_source_notes(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(conn, [{"person_id": "dummy_alice", "medication_name": "TestDrug"}])
        conn.commit()

        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)

        assert report_path is not None
        assert "Medication source evidence" not in report_path.read_text(encoding="utf-8")

    def test_report_has_no_misleading_backlink(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(conn, [{"person_id": "dummy_alice", "medication_name": "TestDrug"}])
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "← Medications" not in content

    def test_report_marks_unresolved_rxnorm_explicitly(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        conn.execute(
            """
            INSERT INTO medications
                (id, person_id, medication_name, medication_type, rxnorm_cui, status, source_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                medication_row_id(
                    "dummy_alice", "data/processed/dummy_alice/Medications/meds.txt", 0
                ),
                "dummy_alice",
                "Vitamin D3",
                "supplement",
                None,
                "active",
                "data/processed/dummy_alice/Medications/meds.txt",
            ),
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "Vitamin D3" in content
        assert "| unresolved |" in content

    def test_report_prefers_user_entered_brand_over_later_imported_generic_alias(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "25 mg from 2023-05-28; 50 mg from 2023-06-02; 100 mg from 2023-06-09",
                    "frequency": "daily",
                    "start_date": "2023-05-28",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-05-30-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "sertraline (ZOLOFT) 50 mg tablet",
                    "medication_type": "prescription",
                    "rxnorm_cui": "312941",
                    "frequency": (
                        "Start 1/2 tablet by mouth daily for 5 to 7 days, then increase to a "
                        "full tablet by mouth daily for 1 week, then increase to 2 tablets by "
                        "mouth daily thereafter."
                    ),
                    "start_date": "2023-05-19",
                    "source_type": "manual",
                    "source_path": (
                        "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert content.count("Zoloft") == 1
        assert "sertraline (ZOLOFT) 50 mg tablet" not in content
        assert "| 82728 |" in content
        assert "| 312941 |" not in content

    def test_report_prefers_corrected_generic_over_legacy_brand_alias(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "famotidine (PEPCID) 20 MG tablet",
                    "medication_type": "prescription",
                    "rxnorm_cui": "310273",
                    "dose": "unspecified",
                    "frequency": "as needed",
                    "source_type": "user_correction",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Pepcid 20 MG Tablet",
                    "medication_type": "prescription",
                    "rxnorm_cui": "104094",
                    "dose": "1.0",
                    "frequency": "1 tablet at bedtime as needed Orally Once a day",
                    "source_type": "manual",
                },
            ],
        )
        conn.commit()

        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)

        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert content.count("Pepcid") == 1
        assert "| famotidine (Pepcid) | Prescription | unspecified | as needed |" in content
        assert "| Pepcid | 1.0 |" not in content

    def test_report_prefers_user_correction_over_undated_source_conflict(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "dabigatran (PRADAXA) 150 mg capsule",
                    "medication_type": "prescription",
                    "rxnorm_cui": "1037045",
                    "dose": "150 mg",
                    "frequency": "twice daily",
                    "start_date": "2016-04-17",
                    "source_type": "user_correction",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "dabigatran etexilate (Pradaxa) 150 MG capsule",
                    "medication_type": "prescription",
                    "rxnorm_cui": "1037045",
                    "dose": "150 mg",
                    "frequency": "Take 150 mg by mouth once daily.",
                    "source_type": "zip_xml",
                },
            ],
        )
        conn.commit()

        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)

        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert content.count("Pradaxa") == 1
        assert "| dabigatran (Pradaxa) | 150 mg | twice daily | active |" in content
        assert "once daily" not in content

    def test_detailed_prn_aliases_keep_distinct_intervals(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "ExampleDrug",
                    "medication_type": "prescription",
                    "dose": "10 mg",
                    "frequency": "Take 1 tablet every 4 hours as needed",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "example generic (ExampleDrug)",
                    "medication_type": "prescription",
                    "dose": "10 mg",
                    "frequency": "Take 1 tablet every 8 hours as needed",
                },
            ],
        )
        conn.commit()

        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)

        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        as_needed = content.split("## As needed", 1)[1]
        assert "take 1 tablet every 4 hours as needed" in as_needed
        assert "take 1 tablet every 8 hours as needed" in as_needed

    def test_report_collapses_imported_alias_with_same_effective_signature(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "nitroglycerin (Nitrostat)",
                    "medication_type": "prescription",
                    "rxnorm_cui": "4917",
                    "dose": "0.4 mg",
                    "frequency": "PRN for chest pain",
                    "source_type": "source_import",
                    "source_path": (
                        "Records/dummy_alice/Medications/2022-06-10-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "nitroGLYCERIN (NITROSTAT) 0.4 mg sublingual tablet",
                    "medication_type": "prescription",
                    "rxnorm_cui": "198039",
                    "frequency": (
                        "Place 1 tablet (0.4 mg total) under the tongue every 5 (five) minutes "
                        "as needed for Chest pain"
                    ),
                    "source_type": "manual",
                    "source_path": (
                        "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert content.count("Nitrostat") == 1
        assert "sublingual tablet" not in content
        assert "| 4917 |" in content
        assert "| 198039 |" not in content

    def test_report_keeps_user_entered_dose_periods_when_hiding_imported_alias(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        _today = date.today()
        _start_25 = (_today - timedelta(days=32)).isoformat()
        _end_25 = (_today - timedelta(days=22)).isoformat()
        _start_50 = (_today - timedelta(days=21)).isoformat()
        _end_50 = (_today + timedelta(days=29)).isoformat()
        _start_100 = (_today + timedelta(days=30)).isoformat()
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "25 mg",
                    "frequency": "daily",
                    "start_date": _start_25,
                    "end_date": _end_25,
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-05-30-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "50 mg",
                    "frequency": "daily",
                    "start_date": _start_50,
                    "end_date": _end_50,
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-05-30-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "100 mg",
                    "frequency": "daily",
                    "start_date": _start_100,
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-05-30-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "sertraline (ZOLOFT) 50 mg tablet",
                    "medication_type": "prescription",
                    "rxnorm_cui": "312941",
                    "frequency": (
                        "Start 1/2 tablet by mouth daily for 5 to 7 days, then increase to a "
                        "full tablet by mouth daily for 1 week, then increase to 2 tablets by "
                        "mouth daily thereafter."
                    ),
                    "start_date": "2023-05-19",
                    "source_type": "manual",
                    "source_path": (
                        "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        # row_25 ended in the past — filtered from active list
        # row_50 is the current active dose (started in past, ends in future)
        # row_100 starts in the future — filtered from active list
        today = date.today()
        end_50 = (today + timedelta(days=29)).isoformat()
        start_50 = (today - timedelta(days=21)).isoformat()
        row_50 = f"| Zoloft | 50 mg | once daily | active | {start_50} | {end_50} |  | 82728 |"
        assert "| Zoloft | 25 mg |" not in content
        assert row_50 in content
        assert "| Zoloft | 100 mg |" not in content
        assert "sertraline (ZOLOFT) 50 mg tablet" not in content
        assert "| 312941 |" not in content

    def test_report_hides_past_dose_periods_shows_only_current_dose(self, tmp_path: Path) -> None:
        """Past-ended dose escalation steps must not appear in the active medications list."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "25 mg",
                    "frequency": "daily",
                    "start_date": "2022-01-01",
                    "end_date": "2022-02-01",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2022-02-01-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "50 mg",
                    "frequency": "daily",
                    "start_date": "2022-02-01",
                    "end_date": "2022-03-01",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2022-02-01-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "rxnorm_cui": "82728",
                    "dose": "100 mg",
                    "frequency": "daily",
                    "start_date": "2022-03-01",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2022-02-01-dummy_alice-medications.md"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "| Zoloft | 25 mg |" not in content
        assert "| Zoloft | 50 mg |" not in content
        assert "| Zoloft | 100 mg | once daily | active | 2022-03-01 |  |  | 82728 |" in content

    def test_user_correction_not_taken_excluded_from_report(self, tmp_path: Path) -> None:
        """Medications marked not-taken via user_correction must not appear in the active list."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "rosuvastatin (Crestor)",
                    "medication_type": "prescription",
                    "rxnorm_cui": "859747",
                    "dose": "10 mg",
                    "status": "active",
                    "start_date": "2023-02-13",
                    "source_type": "manual",
                    "source_path": (
                        "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "rosuvastatin (Crestor)",
                    "medication_type": "prescription",
                    "rxnorm_cui": "859747",
                    "dose": "10 mg",
                    "status": "not-taken",
                    "start_date": "2023-02-13",
                    "source_type": "user_correction",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-06-08-dummy_alice-corrections.md"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is None

    def test_imported_not_taken_does_not_suppress_active(self, tmp_path: Path) -> None:
        """A not-taken row from an imported source must not suppress an active user row."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "metformin",
                    "medication_type": "prescription",
                    "rxnorm_cui": "6809",
                    "dose": "500 mg",
                    "status": "active",
                    "start_date": "2020-01-01",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2020-01-01-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "metformin",
                    "medication_type": "prescription",
                    "rxnorm_cui": "6809",
                    "dose": "500 mg",
                    "status": "not-taken",
                    "start_date": "2019-12-01",
                    "source_type": "manual",
                    "source_path": (
                        "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "metformin" in content.lower()

    def test_user_discontinued_suppresses_older_active_row_from_other_note(
        self, tmp_path: Path
    ) -> None:
        """A user-reported discontinuation must suppress an older open-ended active row
        for the same medication recorded under a different dated update note."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "dose": "100 mg",
                    "status": "active",
                    "start_date": "2023-06-09",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-05-30-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "medication_type": "prescription",
                    "dose": "100 mg",
                    "status": "discontinued",
                    "start_date": "2023-06-09",
                    "end_date": "2023-06-21",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2023-06-23-dummy_alice-medications.md"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is None

    def test_imported_discontinued_does_not_suppress_active_from_other_source(
        self, tmp_path: Path
    ) -> None:
        """A discontinued row from an imported source must not suppress an active user row."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "metformin",
                    "medication_type": "prescription",
                    "dose": "500 mg",
                    "status": "active",
                    "start_date": "2020-01-01",
                    "source_type": "user_update",
                    "source_path": (
                        "Records/dummy_alice/Medications/2020-01-01-dummy_alice-medications.md"
                    ),
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "metformin",
                    "medication_type": "prescription",
                    "dose": "500 mg",
                    "status": "discontinued",
                    "start_date": "2019-12-01",
                    "source_type": "manual",
                    "source_path": (
                        "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
                    ),
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "metformin" in content.lower()

    def test_same_note_discontinued_row_does_not_suppress_sibling_active_row(
        self, tmp_path: Path
    ) -> None:
        """A titration note that discontinues an old dose while starting a new one in
        the same session must not suppress its own new active dose."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        same_note = "Records/dummy_alice/Medications/2017-06-02-dummy_alice-medications.md"
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Venlafaxine",
                    "medication_type": "prescription",
                    "dose": "37.5 mg",
                    "status": "discontinued",
                    "start_date": "2017-01-01",
                    "end_date": "2017-06-01",
                    "source_type": "user_update",
                    "source_path": same_note,
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Venlafaxine",
                    "medication_type": "prescription",
                    "dose": "75 mg",
                    "status": "active",
                    "start_date": "2017-06-02",
                    "source_type": "user_update",
                    "source_path": same_note,
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "venlafaxine" in content.lower()
        assert "75 mg" in content

    def test_backfills_earliest_historical_start_date(self, tmp_path: Path) -> None:
        """Active medication with a newer re-prescription date shows the original first date."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Omeprazole",
                    "medication_type": "prescription",
                    "rxnorm_cui": "7646",
                    "dose": "20 mg",
                    "status": "completed",
                    "start_date": "2019-11-22",
                    "end_date": "2020-05-01",
                    "source_type": "manual",
                    "source_path": "data/processed/dummy_alice/HealthSummary/old.zip#DOC0001.XML",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Omeprazole",
                    "medication_type": "prescription",
                    "rxnorm_cui": "7646",
                    "dose": "20 mg",
                    "status": "active",
                    "start_date": "2023-05-07",
                    "source_type": "manual",
                    "source_path": "data/processed/dummy_alice/HealthSummary/new.zip#DOC0001.XML",
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "| Omeprazole | 20 mg |" in content
        # must show the earliest known start (2022), not the latest re-prescription (2026)
        assert "2019-11-22" in content
        assert "2023-05-07" not in content

    def test_same_note_same_dose_contradiction_suppresses_active_row(self, tmp_path: Path) -> None:
        """A user_update 'not-taken' row suppresses an active row for the same dose
        recorded under the same note.

        Both rows cannot be true: one says a dose is active, the other says that exact
        dose was never started. The same-source_path exception exists for titrations and
        must not shield a contradiction.
        """
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        note = "Records/dummy_alice/Medications/2022-07-01-dummy_alice-medications.md"
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Levothyroxine",
                    "dose": "25 mcg",
                    "status": "active",
                    "source_type": "source_import",
                    "source_path": note,
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Levothyroxine",
                    "dose": "25 mcg",
                    "status": "not-taken",
                    "source_type": "user_update",
                    "source_path": note,
                },
                # control: an unrelated active row keeps the report non-empty, so the
                # assertion below proves suppression rather than an empty report
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Lisinopril",
                    "dose": "10 mg",
                    "status": "active",
                    "source_type": "user_update",
                    "source_path": note,
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "Lisinopril" in content
        assert "Levothyroxine" not in content

    def test_same_note_different_dose_titration_is_not_suppressed(self, tmp_path: Path) -> None:
        """A titration note that discontinues an old dose while starting a new one keeps
        the new dose visible. This is the case the same-source_path exception protects."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        note = "Records/dummy_alice/Medications/2023-06-23-dummy_alice-medications.md"
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Venlafaxine",
                    "dose": "37.5 mg",
                    "status": "discontinued",
                    "source_type": "user_update",
                    "source_path": note,
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Venlafaxine",
                    "dose": "75 mg",
                    "status": "active",
                    "source_type": "user_update",
                    "source_path": note,
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "| Venlafaxine | 75 mg |" in content
        assert "37.5 mg" not in content

    def test_cross_note_discontinuation_still_suppresses_any_dose(self, tmp_path: Path) -> None:
        """A discontinuation in a *different* note suppresses regardless of dose, so the
        dose comparison does not weaken the pre-existing cross-note behavior."""
        vault_root = tmp_path / "famed-vault"
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        self._insert_meds(
            conn,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "sertraline (Zoloft) 50 mg tablet",
                    "dose": "50 mg",
                    "status": "active",
                    "source_type": "zip_xml",
                    "source_path": "data/processed/dummy_alice/HealthSummary/a.zip#DOC0001.XML",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zoloft",
                    "dose": "100 mg",
                    "status": "discontinued",
                    "source_type": "user_update",
                    "source_path": "Records/dummy_alice/Medications/2023-06-09-meds.md",
                },
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Lisinopril",
                    "dose": "10 mg",
                    "status": "active",
                    "source_type": "user_update",
                    "source_path": "Records/dummy_alice/Medications/2023-06-09-meds.md",
                },
            ],
        )
        conn.commit()
        report_path = write_current_medications_report(vault_root, "dummy_alice", conn)
        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "Lisinopril" in content
        assert "sertraline" not in content


# ---------------------------------------------------------------------------
# persist_medication_rows — report refresh hook
# ---------------------------------------------------------------------------


class TestPersistMedicationRowsReportRefresh:
    def test_current_medications_report_generated_after_persist(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        _scaffold_person(vault_root, "dummy_alice")
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        conn.commit()

        source_file = tmp_path / "supplements.txt"
        source_file.write_text("Vitamin D 2000 IU\n", encoding="utf-8")

        rows = [
            {
                "person_id": "dummy_alice",
                "medication_name": "Vitamin D",
                "medication_type": "supplement",
                "rxnorm_cui": None,
                "dose": "2000 IU",
                "frequency": "daily",
                "start_date": None,
                "end_date": None,
                "status": "active",
                "route": None,
                "evidence_snippet": None,
            }
        ]
        persist_medication_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=source_file,
            processed_source_path="data/processed/dummy_alice/Medications/undated-supplements.txt",
            checksum="abc123",
            source_type="ai_extracted",
            rows=rows,
            document_date=None,
        )
        conn.commit()

        report_path = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "# Current Medications" in content
        assert "Vitamin D" in content
        persisted_source_type = conn.execute(
            "SELECT source_type FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone()
        assert persisted_source_type == ("ai_extracted",)
        source_note = next(
            (vault_root / "Records" / "dummy_alice" / "Medications").glob("*-medications.md")
        )
        source_front_matter = yaml.safe_load(
            source_note.read_text(encoding="utf-8").split("---")[1]
        )
        assert source_front_matter["projection_role"] == "source_evidence"
        medication_index = source_note.parent / "_index.md"
        index_content = medication_index.read_text(encoding="utf-8")
        assert "— source evidence" in index_content
        assert "Canonical current view" in index_content
        assert (
            "[[Records/dummy_alice/Medications/_index|Medication source evidence]]"
            in report_path.read_text(encoding="utf-8")
        )

        reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
        assert not (reports_dir / "pgx-coverage.md").exists()
        assert not (reports_dir / "pgx-wallet-card.md").exists()

    def test_unattended_persist_records_and_renders_pending_conflict(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "famed-vault"
        _scaffold_person(vault_root, "dummy_alice")
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        for source_name, dose in (("source-a.txt", "10 mg"), ("source-b.txt", "20 mg")):
            source_file = tmp_path / source_name
            source_file.write_text(f"Examplemed {dose}\n", encoding="utf-8")
            persist_medication_rows(
                conn=conn,
                vault_root=vault_root,
                person_id="dummy_alice",
                source_file=source_file,
                processed_source_path=(f"data/processed/dummy_alice/Medications/{source_name}"),
                checksum=f"checksum-{source_name}",
                source_type="ai_extracted",
                rows=[_med_row(name="Examplemed", dose=dose)],
                document_date=None,
            )

        report = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "medication-conflicts.md"
        ).read_text(encoding="utf-8")
        assert "verification_status: needs_review" in report
        assert "pending_review_count: 1" in report
        event = conn.execute(
            """
            SELECT status
            FROM audit_events
            WHERE event_type = 'medication_conflict_detected'
            """
        ).fetchone()
        assert event == ("awaiting_user_decision",)


# ---------------------------------------------------------------------------
# parse_supplement_text
# ---------------------------------------------------------------------------


class TestParseSupplementText:
    def test_empty_content_yields_no_facts(self) -> None:
        assert parse_supplement_text("") == []

    def test_only_blank_lines_yields_no_facts(self) -> None:
        assert parse_supplement_text("\n\n   \n") == []

    def test_skips_title_line_without_dose(self) -> None:
        facts = parse_supplement_text("Supplement List\n\nD3 2000 IU\n")
        assert len(facts) == 1
        assert facts[0].name == "D3"
        assert facts[0].dose == "2000 IU"

    def test_extracts_name_and_dose_from_simple_line(self) -> None:
        facts = parse_supplement_text("B12 1000 mcg\n")
        assert facts[0].name == "B12"
        assert facts[0].dose == "1000 mcg"
        assert facts[0].medication_type == "supplement"
        assert facts[0].status == "active"

    def test_handles_multi_word_name(self) -> None:
        facts = parse_supplement_text("Magnesium Glycinate 400 mg\n")
        assert facts[0].name == "Magnesium Glycinate"
        assert facts[0].dose == "400 mg"

    def test_strips_trailing_parenthetical_from_dose(self) -> None:
        facts = parse_supplement_text("Turmeric 2600 mg (periodically)\n")
        assert facts[0].name == "Turmeric"
        assert facts[0].dose == "2600 mg"
        assert "(periodically)" in facts[0].evidence_snippet  # type: ignore[operator]

    def test_preserves_parenthetical_in_name(self) -> None:
        facts = parse_supplement_text("Example Sterol Complex (phytosterols) 900 mg\n")
        assert facts[0].name == "Example Sterol Complex (phytosterols)"
        assert facts[0].dose == "900 mg"

    def test_paused_section_marks_discontinued(self) -> None:
        facts = parse_supplement_text(
            "Current:\n\nD3 2000 IU\n\nPaused 04/27/26:\n\nL-Carnitine 250 mg\n"
        )
        assert len(facts) == 2
        assert facts[0].name == "D3"
        assert facts[0].status == "active"
        assert facts[1].name == "L-Carnitine"
        assert facts[1].status == "discontinued"

    def test_typo_curent_section_treated_as_active(self) -> None:
        facts = parse_supplement_text("Curent:\n\nD3 2000 IU\n")
        assert facts[0].status == "active"

    def test_section_after_paused_resets_to_active(self) -> None:
        facts = parse_supplement_text("Paused:\n\nL-Carnitine 250 mg\n\nCurrent:\n\nD3 2000 IU\n")
        assert facts[0].status == "discontinued"
        assert facts[1].status == "active"

    def test_lines_without_dose_pattern_skipped(self) -> None:
        facts = parse_supplement_text(
            "Free-text note that is not a supplement.\nAnother random line.\n"
        )
        assert facts == []

    def test_no_dose_line_inside_section_becomes_supplement_with_null_dose(self) -> None:
        facts = parse_supplement_text("Current:\n\nSaw Palmetto\n")
        assert len(facts) == 1
        assert facts[0].name == "Saw Palmetto"
        assert facts[0].dose is None
        assert facts[0].status == "active"
        assert facts[0].evidence_snippet == "Saw Palmetto"

    def test_no_dose_line_outside_section_is_skipped_as_metadata(self) -> None:
        facts = parse_supplement_text("Saw Palmetto\n")
        assert facts == []

    def test_prose_inside_section_is_not_parsed_as_supplement_name(self) -> None:
        facts = parse_supplement_text("Current:\n\nTake this list with meals.\n")
        assert facts == []

    def test_recognizes_iu_unit(self) -> None:
        facts = parse_supplement_text("Vitamin K 100 IU\n")
        assert facts[0].dose == "100 IU"

    def test_recognizes_mcg_unit(self) -> None:
        facts = parse_supplement_text("MK-7 K-2 100 mcg\n")
        assert facts[0].name == "MK-7 K-2"
        assert facts[0].dose == "100 mcg"

    def test_evidence_snippet_preserves_raw_line(self) -> None:
        facts = parse_supplement_text("  D3 2000 IU  \n")
        assert facts[0].evidence_snippet == "D3 2000 IU"

    def test_multi_dose_narrative_line_is_skipped_not_garbled(self) -> None:
        # A titration schedule naming several doses on several dates describes
        # several distinct facts; collapsing it into one dose string would
        # silently misrepresent the source, so it must be skipped rather than
        # recorded as a single wrong fact.
        facts = parse_supplement_text(
            "Current:\n\nZoloft 25 mg from 2023-05-28; 50 mg from 2023-06-02; "
            "100 mg from 2023-06-09 (daily)\n"
        )
        assert facts == []

    def test_single_dose_line_with_one_mention_still_parses(self) -> None:
        facts = parse_supplement_text("Current:\n\nZoloft 50 mg (daily)\n")
        assert len(facts) == 1
        assert facts[0].name == "Zoloft"
        assert facts[0].dose == "50 mg"

    def test_full_real_world_file(self) -> None:
        content = (
            "Supplement List\n\n"
            "Curent:\n\n"
            "Example Sterol Complex (phytosterols) 900 mg\n"
            "Pantethine 600 mg\n"
            "Saw Palmetto 500 mg\n"
            "Turmeric 2600 mg (periodically)\n"
            "D3 2000 IU\n"
            "CoQ10 100 mg\n"
            "B12 1000 mcg\n"
            "Magnesium Glycinate 400 mg\n\n"
            "Paused 04/27/26:\n\n"
            "L-Carnitine 250 mg\n"
            "MK-7 K-2 100 mcg\n"
        )
        facts = parse_supplement_text(content)
        assert len(facts) == 10
        active = [f for f in facts if f.status == "active"]
        discontinued = [f for f in facts if f.status == "discontinued"]
        assert len(active) == 8
        assert len(discontinued) == 2
        assert all(f.medication_type == "supplement" for f in facts)


class TestParseMedicationCsv:
    def test_parses_required_and_optional_fields(self, tmp_path: Path) -> None:
        source_file = tmp_path / "medications.csv"
        source_file.write_text(
            "medication_name,dose,frequency,start_date,medication_type,rxnorm_cui,status\n"
            "Omeprazole,20 mg,daily,2023-04-01,prescription,12345,active\n",
            encoding="utf-8",
        )

        rows = parse_medication_csv(source_file, "dummy_alice")

        assert rows == [
            {
                "person_id": "dummy_alice",
                "medication_name": "Omeprazole",
                "medication_type": "prescription",
                "rxnorm_cui": "12345",
                "dose": "20 mg",
                "frequency": "daily",
                "start_date": "2023-04-01",
                "end_date": None,
                "status": "active",
                "route": None,
                "evidence_snippet": None,
            }
        ]

    def test_accepts_alias_name_column(self, tmp_path: Path) -> None:
        source_file = tmp_path / "medications.csv"
        source_file.write_text("medication,dose\nLisinopril,10 mg\n", encoding="utf-8")

        rows = parse_medication_csv(source_file, "dummy_alice")

        assert rows[0]["medication_name"] == "Lisinopril"

    def test_accepts_vendor_drug_name_column(self, tmp_path: Path) -> None:
        """Vendor portal CSV with 'Drug Name' header maps to medication_name."""
        source_file = tmp_path / "medications.csv"
        source_file.write_text("Drug Name,Dosage\nAtorvastatin,20 mg\n", encoding="utf-8")

        rows = parse_medication_csv(source_file, "dummy_alice")

        assert rows[0]["medication_name"] == "Atorvastatin"
        assert rows[0]["dose"] == "20 mg"

    def test_accepts_vendor_product_name_column(self, tmp_path: Path) -> None:
        """Vendor portal CSV with 'Product Name' header maps to medication_name."""
        source_file = tmp_path / "medications.csv"
        source_file.write_text("Product Name,Strength\nMetformin,500 mg\n", encoding="utf-8")

        rows = parse_medication_csv(source_file, "dummy_alice")

        assert rows[0]["medication_name"] == "Metformin"
        assert rows[0]["dose"] == "500 mg"

    def test_accepts_vendor_sig_column_as_frequency(self, tmp_path: Path) -> None:
        """Vendor portal CSV with 'Sig' header maps to frequency."""
        source_file = tmp_path / "medications.csv"
        source_file.write_text("medication_name,Sig\nOmeprazole,Once daily\n", encoding="utf-8")

        rows = parse_medication_csv(source_file, "dummy_alice")

        assert rows[0]["frequency"] == "Once daily"

    def test_rejects_mismatched_person_id_column(self, tmp_path: Path) -> None:
        source_file = tmp_path / "medications.csv"
        source_file.write_text(
            "person_id,medication_name\ndummy_bob,Omeprazole\n", encoding="utf-8"
        )

        with pytest.raises(MedicationSyncError, match="person_id column must match"):
            parse_medication_csv(source_file, "dummy_alice")

    def test_rejects_invalid_start_date(self, tmp_path: Path) -> None:
        source_file = tmp_path / "medications.csv"
        source_file.write_text(
            "medication_name,start_date\nOmeprazole,April 1/2026\n",
            encoding="utf-8",
        )

        with pytest.raises(MedicationSyncError, match="start_date must be an ISO date"):
            parse_medication_csv(source_file, "dummy_alice")


# ---------------------------------------------------------------------------
# parse_medication_markdown_table
# ---------------------------------------------------------------------------


class TestParseMedicationMarkdownTable:
    _TABLE_HEADER = (
        "| medication_name | medication_type | rxnorm_cui | dose | frequency "
        "| status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )

    def test_returns_none_for_freeform_prose(self) -> None:
        assert parse_medication_markdown_table("Current:\n\nD3 2000 IU\n") is None

    def test_parses_rows_with_empty_cells(self) -> None:
        content = (
            "# Medication List\n\n## Medications\n\n"
            + self._TABLE_HEADER
            + "| Venlafaxine | prescription |  | 37.5 mg |  | active | 2023-06-18 "
            "| 2023-06-24 |  |\n"
            "| Venlafaxine | prescription |  | 75 mg |  | active | 2023-06-25 |  |  |\n"
            "| Zoloft | prescription | 82728 | 100 mg |  | discontinued | 2023-06-09 "
            "| 2023-06-21 |  |\n"
        )
        facts = parse_medication_markdown_table(content)
        assert facts is not None
        assert len(facts) == 3
        assert facts[0].name == "Venlafaxine"
        assert facts[0].dose == "37.5 mg"
        assert facts[0].status == "active"
        assert facts[0].start_date == "2023-06-18"
        assert facts[0].end_date == "2023-06-24"
        assert facts[1].name == "Venlafaxine"
        assert facts[1].dose == "75 mg"
        assert facts[1].end_date is None
        assert facts[2].name == "Zoloft"
        assert facts[2].dose == "100 mg"
        assert facts[2].status == "discontinued"

    def test_stops_at_first_non_table_line_after_rows(self) -> None:
        content = (
            self._TABLE_HEADER + "| D3 | supplement |  | 2000 IU |  | active |  |  |  |\n\n"
            "## Notes\n\nSome trailing prose.\n"
        )
        facts = parse_medication_markdown_table(content)
        assert facts is not None
        assert len(facts) == 1
        assert facts[0].name == "D3"

    def test_raises_on_missing_alignment_row(self) -> None:
        content = (
            self._TABLE_HEADER.splitlines()[0] + "\n| D3 | supplement |  |  |  |  |  |  |  |\n"
        )
        with pytest.raises(MedicationSyncError, match="alignment row"):
            parse_medication_markdown_table(content)


# ---------------------------------------------------------------------------
# build_supplement_extraction
# ---------------------------------------------------------------------------


class TestBuildSupplementExtraction:
    def test_returns_none_for_unparseable_file(self, tmp_path: Path) -> None:
        source = tmp_path / "supplements.txt"
        source.write_text("Free-form text only.\n", encoding="utf-8")
        assert build_supplement_extraction("dummy_alice", source) is None

    def test_builds_valid_extraction_for_parseable_file(self, tmp_path: Path) -> None:
        source = tmp_path / "supplements.txt"
        source.write_text("D3 2000 IU\nB12 1000 mcg\n", encoding="utf-8")
        extraction = build_supplement_extraction("dummy_alice", source)
        assert extraction is not None
        assert extraction.person_id == "dummy_alice"
        assert extraction.category == "medications"
        assert extraction.person_confidence == 1.0
        assert extraction.category_confidence == 1.0
        assert extraction.overall_confidence == 1.0
        assert len(extraction.facts) == 2
        assert len(extraction.evidence) >= 1
        assert extraction.evidence[0].field == "source_filename"
        assert extraction.evidence[0].snippet == "supplements.txt"


# ---------------------------------------------------------------------------
# medication_name_key
# ---------------------------------------------------------------------------


class TestMedicationNameKey:
    def test_lowercases_and_collapses_whitespace(self) -> None:
        assert medication_name_key("  Lisinopril  ") == "lisinopril"
        assert medication_name_key("Vitamin\tD") == "vitamin d"
        assert medication_name_key("Magnesium  Glycinate") == "magnesium glycinate"

    def test_none_or_empty_returns_empty_string(self) -> None:
        assert medication_name_key(None) == ""
        assert medication_name_key("") == ""
        assert medication_name_key("   ") == ""


# ---------------------------------------------------------------------------
# Cross-source dedup in replace_medications_for_source_path
# ---------------------------------------------------------------------------


def _med_row(
    *,
    name: str,
    dose: str | None = "10 mg",
    frequency: str | None = "daily",
    medication_type: str = "prescription",
    status: str = "active",
    start_date: str | None = None,
    end_date: str | None = None,
    person_id: str = "dummy_alice",
) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "medication_name": name,
        "medication_type": medication_type,
        "rxnorm_cui": None,
        "dose": dose,
        "frequency": frequency,
        "start_date": start_date,
        "end_date": end_date,
        "status": status,
        "route": None,
        "evidence_snippet": None,
    }


class TestCrossSourceDedup:
    def test_redundant_same_dose_skipped(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril")],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril")],
        )
        assert (inserted, skipped) == (0, 1)
        rows = conn.execute(
            "SELECT source_path FROM medications WHERE medication_name = 'Lisinopril'"
        ).fetchall()
        assert len(rows) == 1

    def test_redundant_dose_embedded_in_name_and_sig_skipped(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="atorvastatin (Lipitor)", dose="80 mg", frequency="once daily")],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [
                _med_row(
                    name="atorvastatin (LIPITOR) 80 mg tablet",
                    dose=None,
                    frequency="Take 1 tablet (80 mg total) by mouth daily",
                )
            ],
        )

        assert (inserted, skipped) == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM medications").fetchone() == (1,)

    def test_conflicting_dose_keeps_both_rows(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", dose="10 mg")],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", dose="20 mg")],
        )
        assert (inserted, skipped) == (1, 0)
        doses = {
            row[0]
            for row in conn.execute(
                "SELECT dose FROM medications WHERE medication_name = 'Lisinopril'"
            )
        }
        assert doses == {"10 mg", "20 mg"}

    def test_redundant_row_is_skipped_after_an_earlier_conflicting_match(
        self, tmp_path: Path
    ) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", dose="10 mg")],
        )
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", dose="20 mg")],
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-c.txt",
            [_med_row(name="Lisinopril", dose="20 mg")],
        )

        assert (inserted, skipped) == (0, 1)
        assert conn.execute(
            "SELECT COUNT(*) FROM medications WHERE medication_name = 'Lisinopril'"
        ).fetchone() == (2,)

    def test_conflicting_frequency_keeps_both_rows(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", frequency="daily")],
        )
        inserted, _ = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", frequency="twice daily")],
        )
        assert inserted == 1

    def test_dedup_is_case_insensitive_on_name(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril")],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="LISINOPRIL")],
        )
        assert (inserted, skipped) == (0, 1)

    def test_dedup_collapses_existing_name_whitespace(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Magnesium  Glycinate")],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="magnesium glycinate")],
        )
        assert (inserted, skipped) == (0, 1)

    def test_dedup_respects_medication_type(self, tmp_path: Path) -> None:
        # Same name across different types should not be deduped (e.g. someone
        # both takes a prescription and a supplement form).
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Vitamin D", medication_type="prescription")],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Vitamin D", medication_type="supplement")],
        )
        assert (inserted, skipped) == (1, 0)

    @pytest.mark.parametrize("historical_status", ["discontinued", "aborted", "completed"])
    def test_historical_rows_do_not_gate_a_new_active_import(
        self, tmp_path: Path, historical_status: str
    ) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", status=historical_status)],
        )
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", status="active")],
        )
        assert (inserted, skipped) == (1, 0)

    @pytest.mark.parametrize("historical_status", ["discontinued", "aborted", "completed"])
    def test_incoming_historical_rows_are_not_deduplicated_as_active(
        self, tmp_path: Path, historical_status: str
    ) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", status="active")],
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", status=historical_status)],
        )

        assert (inserted, skipped) == (1, 0)

    def test_exact_historical_period_from_another_source_is_redundant(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        historical = _med_row(
            name="Lisinopril",
            status="aborted",
            start_date="2098-01-01",
            end_date="2098-02-01",
        )
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [historical],
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [historical],
        )

        assert (inserted, skipped) == (0, 1)

    def test_same_active_signature_with_a_distinct_period_is_retained(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", start_date="2098-01-01")],
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", start_date="2098-02-01")],
        )

        assert (inserted, skipped) == (1, 0)

    def test_explicit_duration_distinguishes_otherwise_equivalent_periods(
        self, tmp_path: Path
    ) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Examplemed", frequency="one by mouth daily")],
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [
                _med_row(
                    name="Examplemed",
                    frequency="one by mouth daily; Duration: 90 days",
                )
            ],
        )

        assert (inserted, skipped) == (1, 0)

    def test_dedup_does_not_apply_within_same_source(self, tmp_path: Path) -> None:
        # Within one source, replace_medications_for_source_path deletes first then
        # inserts; cross-source check sees no other source and inserts both.
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril"), _med_row(name="Vitamin D")],
        )
        assert (inserted, skipped) == (2, 0)

    def test_suppressed_fact_survives_owner_reimport_that_drops_it(self, tmp_path: Path) -> None:
        """A cross-source-redundant row must not be lost when its owner later drops it.

        Source A claims the fact, so source B's equivalent row is skipped. When
        source A is reprocessed and no longer yields that fact, the fact must
        remain canonically present under source B rather than disappearing from
        the database while still rendering in both evidence notes (ADR-006
        reversible linkage; ADR-038 canonical DB mismatches are bugs).
        """
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_a = "data/processed/dummy_alice/Medications/source-a.txt"
        source_b = "data/processed/dummy_alice/Medications/source-b.txt"
        shared = _med_row(name="Lisinopril")
        unrelated = _med_row(name="Vitamin D")

        replace_medications_for_source_path(conn, source_a, [dict(shared), dict(unrelated)])
        inserted, skipped = replace_medications_for_source_path(conn, source_b, [dict(shared)])
        assert (inserted, skipped) == (0, 1)

        # Source A is reprocessed and no longer states the shared therapy.
        replace_medications_for_source_path(conn, source_a, [dict(unrelated)])

        rows = conn.execute(
            "SELECT source_path FROM medications WHERE medication_name = 'Lisinopril'"
        ).fetchall()
        assert [row[0] for row in rows] == [source_b]

    def test_restore_does_not_override_user_authored_authority(self, tmp_path: Path) -> None:
        """A restored source row must never outrank a user correction (ADR-004)."""
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_a = "data/processed/dummy_alice/Medications/source-a.txt"
        source_b = "data/processed/dummy_alice/Medications/source-b.txt"

        replace_medications_for_source_path(conn, source_a, [_med_row(name="Lisinopril")])
        replace_medications_for_source_path(conn, source_b, [_med_row(name="Lisinopril")])

        # The user corrects the therapy, then source A stops stating it.
        upsert_medication(
            conn,
            {
                **_med_row(name="Lisinopril", dose="5 mg"),
                "source_path": "Records/dummy_alice/Medications/2023-01-01-corrections.md",
                "row_index": 0,
                "source_type": "user_correction",
            },
        )
        conn.execute(
            "DELETE FROM medications WHERE source_path = ? AND source_type != 'user_correction'",
            (source_a,),
        )
        replace_medications_for_source_path(conn, source_a, [_med_row(name="Vitamin D")])

        doses = {
            row[0]
            for row in conn.execute(
                "SELECT dose FROM medications WHERE medication_name = 'Lisinopril'"
            )
        }
        assert doses == {"5 mg"}, "the user-corrected value must remain the only one"
        # The link survives so the fact is still recoverable if the correction is revoked.
        assert conn.execute("SELECT COUNT(*) FROM dedup_links").fetchone() == (1,)

    def test_suppression_records_reversible_link_with_owner_provenance(
        self, tmp_path: Path
    ) -> None:
        """ADR-006: a dedup decision must be reversible and carry its provenance."""
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_a = "data/processed/dummy_alice/Medications/source-a.txt"
        source_b = "data/processed/dummy_alice/Medications/source-b.txt"

        replace_medications_for_source_path(conn, source_a, [_med_row(name="Lisinopril")])
        replace_medications_for_source_path(conn, source_b, [_med_row(name="Lisinopril")])

        links = conn.execute(
            "SELECT subject_type, person_id, suppressed_source_path, owner_source_path, payload "
            "FROM dedup_links"
        ).fetchall()
        assert len(links) == 1
        subject_type, person_id, suppressed, owner, payload = links[0]
        assert (subject_type, person_id, suppressed, owner) == (
            "medication",
            "dummy_alice",
            source_b,
            source_a,
        )
        assert json.loads(payload)["medication_name"] == "Lisinopril"

    def test_suppressed_fact_is_not_duplicated_while_owner_keeps_it(self, tmp_path: Path) -> None:
        """Re-materialisation must not fire while the owning source still states the fact."""
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_a = "data/processed/dummy_alice/Medications/source-a.txt"
        source_b = "data/processed/dummy_alice/Medications/source-b.txt"
        shared = _med_row(name="Lisinopril")

        replace_medications_for_source_path(conn, source_a, [dict(shared)])
        replace_medications_for_source_path(conn, source_b, [dict(shared)])
        # Reprocessing either source repeatedly keeps exactly one canonical row.
        replace_medications_for_source_path(conn, source_a, [dict(shared)])
        replace_medications_for_source_path(conn, source_b, [dict(shared)])

        rows = conn.execute(
            "SELECT source_path FROM medications WHERE medication_name = 'Lisinopril'"
        ).fetchall()
        assert [row[0] for row in rows] == [source_a]


class TestUserCorrectionGuard:
    def test_preserves_user_correction_on_same_source_reimport(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_path = "data/processed/dummy_alice/Medications/source-a.txt"
        row_id = medication_row_id("dummy_alice", source_path, 0)
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency,
                status, source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                "dummy_alice",
                "Lisinopril",
                "prescription",
                "20 mg",
                "daily",
                "active",
                "user_correction",
                source_path,
            ),
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            source_path,
            [_med_row(name="Lisinopril", dose="10 mg")],
        )

        assert (inserted, skipped) == (0, 1)
        stored = conn.execute(
            "SELECT source_type, dose FROM medications WHERE id = ?", (row_id,)
        ).fetchone()
        assert stored == ("user_correction", "20 mg")

    def test_logs_skipped_user_correction_conflict(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency,
                status, source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "med-corrected-id",
                "dummy_alice",
                "Lisinopril",
                "prescription",
                "20 mg",
                "daily",
                "active",
                "user_correction",
                "data/processed/dummy_alice/Medications/original.txt",
            ),
        )

        inserted, skipped = replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", dose="10 mg")],
        )

        assert (inserted, skipped) == (0, 1)
        event = conn.execute(
            "SELECT event_type, subject_type, subject_id, source_path, status FROM audit_events"
        ).fetchone()
        assert event == (
            "skipped_user_correction",
            "medication",
            "dummy_alice",
            "data/processed/dummy_alice/Medications/source-a.txt",
            "processed",
        )


# ---------------------------------------------------------------------------
# medication-conflicts.md report
# ---------------------------------------------------------------------------


class TestMedicationConflictsReport:
    def _setup_conflict(self, tmp_path: Path) -> sqlite3.Connection:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", dose="10 mg")],
        )
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", dose="20 mg")],
        )
        return conn

    def test_build_returns_conflict_rows(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)
        conflicts = build_medication_conflicts_from_db(conn, "dummy_alice")
        assert len(conflicts) == 2
        assert {row["dose"] for row in conflicts} == {"10 mg", "20 mg"}
        assert all(row["medication_name"] == "Lisinopril" for row in conflicts)

    def test_build_returns_empty_when_no_conflicts(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril")],
        )
        assert build_medication_conflicts_from_db(conn, "dummy_alice") == []

    def test_build_excludes_discontinued(self, tmp_path: Path) -> None:
        # A discontinued historical row with a different dose should not produce
        # a conflict against an active row.
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Lisinopril", dose="5 mg", status="discontinued")],
        )
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Lisinopril", dose="10 mg", status="active")],
        )
        assert build_medication_conflicts_from_db(conn, "dummy_alice") == []

    def test_build_excludes_aborted_imported_rows(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-a.txt",
            [_med_row(name="Zoloft", dose="25 mg", status="active")],
        )
        replace_medications_for_source_path(
            conn,
            "data/processed/dummy_alice/Medications/source-b.txt",
            [_med_row(name="Zoloft", dose="50 mg", status="aborted")],
        )

        assert build_medication_conflicts_from_db(conn, "dummy_alice") == []

    def test_build_does_not_conflict_user_authored_dose_periods(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        source_path = "Records/dummy_alice/Medications/2023-05-30-dummy_alice-medications.md"
        replace_medications_for_source_path(
            conn,
            source_path,
            [
                _med_row(name="Zoloft", dose="25 mg"),
                _med_row(name="Zoloft", dose="50 mg"),
                _med_row(name="Zoloft", dose="100 mg"),
            ],
        )

        assert build_medication_conflicts_from_db(conn, "dummy_alice") == []

    def test_build_ignores_equivalent_dose_embedded_in_name_and_sig(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        rows = [
            (
                medication_row_id("dummy_alice", "source-a", 0),
                "dummy_alice",
                "atorvastatin (Lipitor)",
                "prescription",
                "80 mg",
                "once daily",
                "active",
                "source-a",
            ),
            (
                medication_row_id("dummy_alice", "source-b", 0),
                "dummy_alice",
                "atorvastatin (LIPITOR) 80 mg tablet",
                "prescription",
                None,
                "Take 1 tablet (80 mg total) by mouth daily",
                "active",
                "source-b",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency, status,
                source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        assert build_medication_conflicts_from_db(conn, "dummy_alice") == []

    def test_build_deduplicates_repeated_archive_member_conflict_rows(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        rows = [
            (
                medication_row_id("dummy_alice", "source-a", 0),
                "dummy_alice",
                "Lisinopril",
                "prescription",
                "10 mg",
                "daily",
                "active",
                "source-a",
            ),
            (
                medication_row_id("dummy_alice", "archive.zip#DOC0001.XML", 0),
                "dummy_alice",
                "Lisinopril 20 mg tablet",
                "prescription",
                None,
                "Take 1 tablet (20 mg) by mouth daily",
                "active",
                "archive.zip#DOC0001.XML",
            ),
            (
                medication_row_id("dummy_alice", "archive.zip#DOC0002.XML", 0),
                "dummy_alice",
                "Lisinopril 20 mg tablet",
                "prescription",
                None,
                "Take 1 tablet (20 mg) by mouth daily",
                "active",
                "archive.zip#DOC0002.XML",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency, status,
                source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        conflicts = build_medication_conflicts_from_db(conn, "dummy_alice")
        assert len(conflicts) == 2
        assert {row["source_path"] for row in conflicts} == {"source-a", "archive.zip"}

    def test_render_no_conflicts_message(self) -> None:
        rendered = render_medication_conflicts_report("dummy_alice", [])
        assert "_No conflicting medication entries found._" in rendered

    def test_render_includes_each_source_path(self) -> None:
        conflict_rows = [
            {
                "medication_name": "Lisinopril",
                "medication_type": "prescription",
                "dose": "10 mg",
                "frequency": "daily",
                "source_path": "data/processed/dummy_alice/Medications/source-a.txt",
            },
            {
                "medication_name": "Lisinopril",
                "medication_type": "prescription",
                "dose": "20 mg",
                "frequency": "daily",
                "source_path": "data/processed/dummy_alice/Medications/source-b.txt",
            },
        ]
        rendered = render_medication_conflicts_report("dummy_alice", conflict_rows)
        assert "data/processed/dummy_alice/Medications/source-a.txt" in rendered
        assert "data/processed/dummy_alice/Medications/source-b.txt" in rendered
        assert "10 mg" in rendered
        assert "20 mg" in rendered
        assert "REQ-" not in rendered
        assert "ADR-" not in rendered

    def test_write_creates_report_under_reports_dir(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)
        vault_root = tmp_path / "famed-vault"
        report_path, count = write_medication_conflicts_report(conn, vault_root, "dummy_alice")
        assert (
            report_path
            == vault_root / "Records" / "dummy_alice" / "Reports" / "medication-conflicts.md"
        )
        assert report_path.exists()
        assert count == 2
        index_path = vault_root / "Records" / "dummy_alice" / "Reports" / "_index.md"
        assert "medication-conflicts" in index_path.read_text(encoding="utf-8")

    def test_write_records_one_idempotent_pending_review_event(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)
        vault_root = tmp_path / "famed-vault"

        write_medication_conflicts_report(conn, vault_root, "dummy_alice")
        write_medication_conflicts_report(conn, vault_root, "dummy_alice")

        rows = conn.execute(
            """
            SELECT event_type, subject_type, status, message
            FROM audit_events
            WHERE event_type = 'medication_conflict_detected'
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][:3] == (
            "medication_conflict_detected",
            "medication_conflict",
            "awaiting_user_decision",
        )
        payload = json.loads(rows[0][3])
        assert payload["schema_version"] == "famed.medication-conflict-review.v1"
        assert payload["conflict_axis"] == "dose"
        assert {candidate["value"] for candidate in payload["candidates"]} == {
            "10 mg",
            "20 mg",
        }

    def test_write_preserves_each_candidate_source_path(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)
        third_source = "data/processed/dummy_alice/Medications/source-c.txt"
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency,
                status, source_type, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                medication_row_id("dummy_alice", third_source, 0),
                "dummy_alice",
                "Lisinopril",
                "prescription",
                "10 mg",
                "daily",
                "active",
                "manual",
                third_source,
            ),
        )

        report_path, count = write_medication_conflicts_report(
            conn, tmp_path / "famed-vault", "dummy_alice"
        )

        assert count == 3
        report = report_path.read_text(encoding="utf-8")
        assert "source-a.txt" in report
        assert "source-b.txt" in report
        assert "source-c.txt" in report
        payload = json.loads(
            conn.execute(
                """
                SELECT message
                FROM audit_events
                WHERE event_type = 'medication_conflict_detected'
                """
            ).fetchone()[0]
        )
        assert len(payload["candidates"]) == 3

    def test_write_omits_resolved_conflict_rows(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)
        vault_root = tmp_path / "famed-vault"
        write_medication_conflicts_report(conn, vault_root, "dummy_alice")
        record_medication_conflict_resolution_events(
            conn,
            person_id="dummy_alice",
            medication_name="Lisinopril",
            medication_type="prescription",
            resolved_dose="10 mg",
            resolved_frequency="daily",
            actor="caregiver",
            reason="Resolved during review.",
        )

        report_path, count = write_medication_conflicts_report(conn, vault_root, "dummy_alice")

        report = report_path.read_text(encoding="utf-8")
        assert count == 0
        assert "verification_status: accepted" in report
        assert "pending_review_count: 0" in report
        assert "_No conflicting medication entries found._" in report
        assert "Lisinopril" not in report

    def test_render_marks_current_conflicts_as_pending_review(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)

        rendered = render_medication_conflicts_report(
            "dummy_alice",
            build_medication_conflicts_from_db(conn, "dummy_alice"),
        )

        assert "verification_status: needs_review" in rendered
        assert "pending_review_count: 1" in rendered
        assert "> [!warning] Human Resolver review required" in rendered
        assert "| Lisinopril | prescription | dose | pending |" in rendered

    def test_user_authority_resolves_source_contradiction(self, tmp_path: Path) -> None:
        conn = self._setup_conflict(tmp_path)
        conn.execute(
            """
            UPDATE medications
            SET source_type = 'user_update'
            WHERE source_path = 'data/processed/dummy_alice/Medications/source-a.txt'
            """
        )

        assert build_medication_conflicts_from_db(conn, "dummy_alice") == []


class TestMedicationConflictAxisSemantics:
    """Per-axis contradiction detection (ADR-042).

    Each case is a way the previous whole-signature detector either hid a real
    contradiction or manufactured one from a source that said nothing.
    """

    def _conn(self, tmp_path: Path) -> sqlite3.Connection:
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        return conn

    def _insert(
        self,
        conn: sqlite3.Connection,
        source: str,
        *,
        name: str = "Anticoagulant",
        dose: str | None = None,
        frequency: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str = "active",
    ) -> None:
        conn.execute(
            """
            INSERT INTO medications (
                id, person_id, medication_name, medication_type, dose, frequency,
                status, source_path, start_date, end_date
            ) VALUES (?, ?, ?, 'prescription', ?, ?, ?, ?, ?, ?)
            """,
            (
                medication_row_id("dummy_alice", source, 0),
                "dummy_alice",
                name,
                dose,
                frequency,
                status,
                source,
                start_date,
                end_date,
            ),
        )

    def test_undated_row_does_not_suppress_a_real_contradiction(self, tmp_path: Path) -> None:
        # The defect that let two active anticoagulant rows report as no conflict:
        # a missing start_date was read as "documented as current", so the dated
        # row was dismissed as history. Absence of a date is not evidence of
        # recency (ADR-041, ADR-042).
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", dose="150 mg", frequency="once daily")
        self._insert(
            conn, "source-b", dose="150 mg", frequency="twice daily", start_date="2016-04-17"
        )

        conflicts = build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03")

        assert {row["conflict_axis"] for row in conflicts} == {"frequency"}
        assert {row["frequency"] for row in conflicts} == {"once daily", "twice daily"}

    def test_source_silent_on_an_axis_does_not_disagree_about_it(self, tmp_path: Path) -> None:
        # Identical doses; one source records a schedule and the other records
        # none. Silence is not a competing value.
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", name="Vitamin D3", dose="2000 IU", frequency="once daily")
        self._insert(conn, "source-b", name="Vitamin D3", dose="2000 IU", frequency=None)

        assert build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03") == []

    def test_placeholder_dose_does_not_disagree_with_a_stated_dose(self, tmp_path: Path) -> None:
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", dose="20 mg", frequency="as needed")
        self._insert(conn, "source-b", dose="unspecified", frequency="as needed")

        assert build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03") == []

    def test_axes_are_reported_independently(self, tmp_path: Path) -> None:
        # Doses agree, schedules do not: the conflict names frequency alone.
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", dose="20 mg", frequency="once daily")
        self._insert(conn, "source-b", dose="20 mg", frequency="twice daily")

        conflicts = build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03")

        assert conflicts
        assert {row["conflict_axis"] for row in conflicts} == {"frequency"}

    def test_therapy_that_already_stopped_cannot_contradict_current_therapy(
        self, tmp_path: Path
    ) -> None:
        # A hand-entered note stating "through 05/06/26" parses to a real
        # end_date; the row must leave contradiction detection once that passes.
        conn = self._conn(tmp_path)
        self._insert(
            conn,
            "source-a",
            name="Omeprazole",
            frequency="2-3x/week at 4PM",
            end_date="2023-05-06",
        )
        self._insert(
            conn, "source-b", name="Omeprazole", frequency="daily, morning", start_date="2023-05-07"
        )

        assert build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03") == []
        # Before it lapses, the same pair is a live disagreement.
        assert build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-05-05")

    def test_lapsed_authorization_window_still_counts_as_current(self, tmp_path: Path) -> None:
        # ADR-041: a lapsed authorized_through is a refill signal, not a stop.
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", dose="20 mg", frequency="once daily")
        self._insert(conn, "source-b", dose="40 mg", frequency="once daily")
        conn.execute("UPDATE medications SET authorized_through = '2023-06-27'")

        conflicts = build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03")

        assert {row["conflict_axis"] for row in conflicts} == {"dose"}

    def test_prn_qualifier_is_not_read_as_a_scheduled_dose(self, tmp_path: Path) -> None:
        # "by mouth daily as needed" is patient-directed dosing. Matching the
        # "daily" and dropping the qualifier would invert its meaning and
        # manufacture a conflict against a genuinely as-needed source.
        conn = self._conn(tmp_path)
        self._insert(
            conn,
            "source-a",
            name="Omeprazole",
            dose="20 mg",
            frequency="Take 1 capsule (20 mg total) by mouth daily as needed",
        )
        self._insert(conn, "source-b", name="Omeprazole", dose="20 mg", frequency="as needed")

        assert build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03") == []

    def test_dated_dose_change_sequence_is_still_suppressed(self, tmp_path: Path) -> None:
        # A titration recorded across dated rows is history, not disagreement.
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", name="Zoloft", dose="25 mg", start_date="2023-05-28")
        self._insert(conn, "source-b", name="Zoloft", dose="50 mg", start_date="2023-06-02")

        assert build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03") == []

    def test_report_names_the_disagreeing_axis(self, tmp_path: Path) -> None:
        conn = self._conn(tmp_path)
        self._insert(conn, "source-a", dose="150 mg", frequency="once daily")
        self._insert(
            conn, "source-b", dose="150 mg", frequency="twice daily", start_date="2016-04-17"
        )
        conflicts = build_medication_conflicts_from_db(conn, "dummy_alice", as_of="2023-08-03")

        rendered = render_medication_conflicts_report("dummy_alice", conflicts)

        assert "disagrees_on" in rendered
        assert "| frequency |" in rendered
        assert "_No conflicting medication entries found._" not in rendered


# ---------------------------------------------------------------------------
# infer_person_id_from_processed_medication_path
# ---------------------------------------------------------------------------


class TestInferPersonIdFromProcessedMedicationPath:
    def test_returns_person_id_for_valid_path(self, tmp_path: Path) -> None:
        processed_root = tmp_path / "processed"
        source = processed_root / "dummy_alice" / "Medications" / "undated-supplements"
        assert (
            infer_person_id_from_processed_medication_path(source, processed_root) == "dummy_alice"
        )

    def test_returns_none_for_wrong_category(self, tmp_path: Path) -> None:
        processed_root = tmp_path / "processed"
        source = processed_root / "dummy_alice" / "Labs" / "panel.csv"
        assert infer_person_id_from_processed_medication_path(source, processed_root) is None

    def test_returns_none_for_path_outside_processed(self, tmp_path: Path) -> None:
        processed_root = tmp_path / "processed"
        source = tmp_path / "inbox" / "dummy_alice" / "Medications" / "file.txt"
        assert infer_person_id_from_processed_medication_path(source, processed_root) is None


# ---------------------------------------------------------------------------
# persist_medication_rows — retained_source flag
# ---------------------------------------------------------------------------


class TestPersistMedicationRowsRetainedSource:
    def _setup(self, tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
        vault_root = tmp_path / "famed-vault"
        _scaffold_person(vault_root, "dummy_alice")
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        conn.commit()
        return vault_root, conn

    def _rows(self) -> list[dict[str, Any]]:
        return [
            {
                "person_id": "dummy_alice",
                "medication_name": "Vitamin D",
                "medication_type": "supplement",
                "rxnorm_cui": None,
                "dose": "2000 IU",
                "frequency": None,
                "start_date": None,
                "end_date": None,
                "status": "active",
                "route": None,
                "evidence_snippet": None,
            }
        ]

    def test_retained_source_emits_reprocessed_audit_event(self, tmp_path: Path) -> None:

        vault_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        source_file = tmp_path / "processed" / "dummy_alice" / "Medications" / "undated-supplements"
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text("Vitamin D 2000 IU\n", encoding="utf-8")

        persist_medication_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=source_file,
            processed_source_path="data/processed/dummy_alice/Medications/undated-supplements",
            checksum="abc",
            source_type="supplement_list",
            rows=self._rows(),
            document_date=None,
            retained_source=True,
        )
        conn.commit()

        events = conn.execute(
            "SELECT event_type FROM audit_events WHERE event_type = 'medication_source_reprocessed'"
        ).fetchall()
        assert len(events) == 1

    def test_retained_source_emits_stale_markers_for_both_reports(self, tmp_path: Path) -> None:

        vault_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        source_file = tmp_path / "processed" / "dummy_alice" / "Medications" / "undated-supplements"
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text("Vitamin D 2000 IU\n", encoding="utf-8")

        persist_medication_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=source_file,
            processed_source_path="data/processed/dummy_alice/Medications/undated-supplements",
            checksum="abc",
            source_type="supplement_list",
            rows=self._rows(),
            document_date=None,
            retained_source=True,
        )
        conn.commit()

        stale = conn.execute(
            "SELECT message FROM audit_events WHERE event_type = 'dependent_output_marked_stale'"
        ).fetchall()
        output_paths = {row[0] for row in stale}
        assert any("current-medications" in p for p in output_paths)
        assert any("medication-conflicts" in p for p in output_paths)

    def test_non_retained_source_uses_import_accepted_event(self, tmp_path: Path) -> None:

        vault_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        source_file = tmp_path / "supplements.txt"
        source_file.write_text("Vitamin D 2000 IU\n", encoding="utf-8")

        persist_medication_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=source_file,
            processed_source_path="data/processed/dummy_alice/Medications/undated-supplements.txt",
            checksum="abc",
            source_type="supplement_list",
            rows=self._rows(),
            document_date=None,
            retained_source=False,
        )
        conn.commit()

        accepted = conn.execute(
            "SELECT event_type FROM audit_events WHERE event_type = 'medication_import_accepted'"
        ).fetchall()
        assert len(accepted) == 1
        reprocessed = conn.execute(
            "SELECT event_type FROM audit_events WHERE event_type = 'medication_source_reprocessed'"
        ).fetchall()
        assert len(reprocessed) == 0


# ---------------------------------------------------------------------------
# reprocess_retained_supplement_source
# ---------------------------------------------------------------------------


class TestReprocessRetainedSupplementSource:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path, sqlite3.Connection]:
        root = tmp_path
        vault_root = tmp_path / "famed-vault"
        processed_root = tmp_path / "data" / "processed"
        _scaffold_person(vault_root, "dummy_alice")
        conn = _db(tmp_path)
        conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
        conn.commit()
        return root, vault_root, processed_root, conn

    def _make_supplement_file(self, processed_root: Path, content: str) -> Path:
        source = processed_root / "dummy_alice" / "Medications" / "undated-dummy_alice-supplements"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")
        return source

    def test_happy_path_updates_db_and_reports(self, tmp_path: Path) -> None:

        root, vault_root, processed_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        content = "Current:\n\nVitamin D 2000 IU\nCoQ10 100 mg\n"
        source = self._make_supplement_file(processed_root, content)

        result = reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        conn.commit()

        assert result.imported_rows == 2
        assert result.affected_person_ids == {"dummy_alice"}
        rows = conn.execute(
            "SELECT medication_name, status FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert "Vitamin D" in names
        assert "CoQ10" in names
        report = vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        assert report.exists()

    def test_status_flip_propagates_to_db(self, tmp_path: Path) -> None:

        root, vault_root, processed_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        source = self._make_supplement_file(
            processed_root, "Current:\n\nVitamin D 2000 IU\nMK-7 K-2 100 mcg\n"
        )

        reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        conn.commit()

        row = conn.execute(
            "SELECT status FROM medications WHERE person_i"
            "d='dummy_alice' AND medication_name='MK-7 K-2'"
        ).fetchone()
        assert row is not None
        assert row[0] == "active"

        source.write_text(
            "Current:\n\nVitamin D 2000 IU\n\nPaused 04/27/26:\n\nMK-7 K-2 100 mcg\n",
            encoding="utf-8",
        )
        reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        conn.commit()

        row = conn.execute(
            "SELECT status FROM medications WHERE person_i"
            "d='dummy_alice' AND medication_name='MK-7 K-2'"
        ).fetchone()
        assert row is not None
        assert row[0] == "discontinued"

    def test_idempotent_on_rerun(self, tmp_path: Path) -> None:

        root, vault_root, processed_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        source = self._make_supplement_file(processed_root, "Current:\n\nVitamin D 2000 IU\n")

        reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT medication_name FROM medications WHERE person_id='dummy_alice'"
        ).fetchall()
        assert len(rows) == 1

    def test_rejects_path_outside_processed_medications(self, tmp_path: Path) -> None:

        root, vault_root, processed_root, conn = self._setup(tmp_path)
        bad_path = tmp_path / "inbox" / "dummy_alice" / "Medications" / "supplements"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("Vitamin D 2000 IU\n", encoding="utf-8")

        with pytest.raises(ReprocessError, match="data/processed"):
            reprocess_retained_supplement_source(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=bad_path,
            )

    def test_rejects_empty_parse_result(self, tmp_path: Path) -> None:

        root, vault_root, processed_root, conn = self._setup(tmp_path)
        source = self._make_supplement_file(processed_root, "Supplement List\n\n")

        with pytest.raises(ReprocessError, match="no parseable supplement lines"):
            reprocess_retained_supplement_source(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source,
            )

    def test_parses_markdown_table_with_empty_cells(self, tmp_path: Path) -> None:
        root, vault_root, processed_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        content = (
            "# Medication List\n\n## Medications\n\n"
            "| medication_name | medication_type | rxnorm_cui | dose | frequency "
            "| status | start_date | end_date | evidence |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| Venlafaxine | prescription |  | 37.5 mg |  | active | 2023-06-18 "
            "| 2023-06-24 |  |\n"
            "| Venlafaxine | prescription |  | 75 mg |  | active | 2023-06-25 |  |  |\n"
            "| Zoloft | prescription | 82728 | 100 mg |  | discontinued | 2023-06-09 "
            "| 2023-06-21 |  |\n"
        )
        source = self._make_supplement_file(processed_root, content)

        result = reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        conn.commit()

        assert result.imported_rows == 3
        rows = conn.execute(
            "SELECT medication_name, dose, status FROM medications "
            "WHERE person_id='dummy_alice' ORDER BY medication_name, dose"
        ).fetchall()
        assert rows == [
            ("Venlafaxine", "37.5 mg", "active"),
            ("Venlafaxine", "75 mg", "active"),
            ("Zoloft", "100 mg", "discontinued"),
        ]
        # No raw table syntax ("|") leaked into any field.
        assert all("|" not in (row[0] or "") for row in rows)
        assert all("|" not in (row[1] or "") for row in rows)

    def test_rejects_multi_dose_narrative_as_unparseable(self, tmp_path: Path) -> None:
        root, vault_root, processed_root, conn = self._setup(tmp_path)
        source = self._make_supplement_file(
            processed_root,
            "Current:\n\nZoloft 25 mg from 2023-05-28; 50 mg from 2023-06-02; "
            "100 mg from 2023-06-09 (daily)\n",
        )

        with pytest.raises(ReprocessError, match="no parseable supplement lines"):
            reprocess_retained_supplement_source(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source,
            )

    def test_emits_reprocessed_and_stale_audit_events(self, tmp_path: Path) -> None:

        root, vault_root, processed_root, conn = self._setup(tmp_path)
        ensure_audit_events_table(conn)
        source = self._make_supplement_file(processed_root, "Current:\n\nVitamin D 2000 IU\n")

        reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source,
        )
        conn.commit()

        reprocessed = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'medication_source_reprocessed'"
        ).fetchone()[0]
        assert reprocessed == 1

        stale = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'dependent_output_marked_stale'"
        ).fetchone()[0]
        assert stale == 2


class TestFillRxnormCuis:
    def _make_rows(self, names: list[str], cui_preset: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "medication_name": name,
                "rxnorm_cui": cui_preset,
                "medication_type": "prescription",
                "dose": None,
                "frequency": None,
                "status": "active",
            }
            for name in names
        ]

    def test_fills_blank_cuis_from_index(self) -> None:
        from famed.medication_sync import fill_rxnorm_cuis

        rows = self._make_rows(["Metformin", "Lisinopril"])
        index = {"metformin": "5521", "lisinopril": "3498"}
        fill_rxnorm_cuis(rows, index)
        assert rows[0]["rxnorm_cui"] == "5521"
        assert rows[1]["rxnorm_cui"] == "3498"

    def test_does_not_overwrite_existing_cui(self) -> None:
        from famed.medication_sync import fill_rxnorm_cuis

        rows = self._make_rows(["Metformin"], cui_preset="EXISTING")
        index = {"metformin": "5521"}
        fill_rxnorm_cuis(rows, index)
        assert rows[0]["rxnorm_cui"] == "EXISTING"

    def test_leaves_null_when_name_not_in_index(self) -> None:
        from famed.medication_sync import fill_rxnorm_cuis

        rows = self._make_rows(["Unobtainium"])
        index = {"metformin": "5521"}
        fill_rxnorm_cuis(rows, index)
        assert rows[0]["rxnorm_cui"] is None

    def test_noop_on_empty_index(self) -> None:
        from famed.medication_sync import fill_rxnorm_cuis

        rows = self._make_rows(["Metformin"])
        fill_rxnorm_cuis(rows, {})
        assert rows[0]["rxnorm_cui"] is None

    def test_normalizes_name_for_lookup(self) -> None:
        from famed.medication_sync import fill_rxnorm_cuis

        rows = self._make_rows(["METFORMIN HCL"])
        index = {"metformin hcl": "5521"}
        fill_rxnorm_cuis(rows, index)
        assert rows[0]["rxnorm_cui"] == "5521"


class TestRepairMedicationRxnormCuis:
    def _make_medication_note(self, medication_name: str, rxnorm_cui: str = "") -> str:
        front_matter = "---\nrecord_type: medication\nperson_id: dummy_alice\n---\n\n"
        header = "# Medication List\n\n[[_index|← Medications]]\n\n## Medications\n\n"
        table = (
            "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
            "status | start_date | end_date | evidence |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            f"| {medication_name} | prescription | {rxnorm_cui} |  |  | active |  |  |  |\n"
        )
        return front_matter + header + table

    def _setup_vault_note(self, vault_root: Path, medication_name: str) -> Path:
        meds_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        meds_dir.mkdir(parents=True)
        note_path = meds_dir / "2021-01-01-test-medications.md"
        note_path.write_text(self._make_medication_note(medication_name), encoding="utf-8")
        return note_path

    def test_fills_blank_cells_in_vault_note(self, tmp_path: Path) -> None:
        from famed.medication_sync import repair_medication_rxnorm_cuis

        vault_root = tmp_path / "vault"
        note_path = self._setup_vault_note(vault_root, "Metformin")
        rxnorm_index = {"metformin": "5521"}

        notes_updated, cells_filled = repair_medication_rxnorm_cuis(vault_root, rxnorm_index)

        assert notes_updated == 1
        assert cells_filled == 1
        content = note_path.read_text(encoding="utf-8")
        assert "| Metformin | prescription | 5521 |" in content

    def test_does_not_overwrite_existing_cui_in_note(self, tmp_path: Path) -> None:
        from famed.medication_sync import repair_medication_rxnorm_cuis

        vault_root = tmp_path / "vault"
        meds_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        meds_dir.mkdir(parents=True)
        note_path = meds_dir / "test-medications.md"
        note_path.write_text(
            self._make_medication_note("Metformin", rxnorm_cui="EXISTING"), encoding="utf-8"
        )
        rxnorm_index = {"metformin": "5521"}

        notes_updated, cells_filled = repair_medication_rxnorm_cuis(vault_root, rxnorm_index)

        assert notes_updated == 0
        assert cells_filled == 0
        content = note_path.read_text(encoding="utf-8")
        assert "EXISTING" in content

    def test_no_match_leaves_note_unchanged(self, tmp_path: Path) -> None:
        from famed.medication_sync import repair_medication_rxnorm_cuis

        vault_root = tmp_path / "vault"
        self._setup_vault_note(vault_root, "Unobtainium")
        rxnorm_index = {"metformin": "5521"}

        notes_updated, cells_filled = repair_medication_rxnorm_cuis(vault_root, rxnorm_index)

        assert notes_updated == 0
        assert cells_filled == 0

    def test_noop_on_empty_index(self, tmp_path: Path) -> None:
        from famed.medication_sync import repair_medication_rxnorm_cuis

        vault_root = tmp_path / "vault"
        self._setup_vault_note(vault_root, "Metformin")

        notes_updated, cells_filled = repair_medication_rxnorm_cuis(vault_root, {})

        assert notes_updated == 0
        assert cells_filled == 0

    def test_updates_db_rows_when_conn_provided(self, tmp_path: Path) -> None:
        from famed.medication_sync import repair_medication_rxnorm_cuis
        from famed.setup import SCHEMA_SQL

        vault_root = tmp_path / "vault"
        self._setup_vault_note(vault_root, "Metformin")

        db_path = tmp_path / "famed.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO medications (id, person_id, medication_name, medication_type, "
            "rxnorm_cui, dose, frequency, start_date, end_date, status, source_type, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "dummy_alice|test.md|0",
                "dummy_alice",
                "Metformin",
                "prescription",
                None,
                None,
                None,
                None,
                None,
                "active",
                "ai_extracted",
                "test.md",
            ),
        )
        conn.commit()

        rxnorm_index = {"metformin": "5521"}
        repair_medication_rxnorm_cuis(vault_root, rxnorm_index, conn)
        conn.commit()

        row = conn.execute(
            "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone()
        assert row is not None
        assert row[0] == "5521"
        conn.close()

    def test_db_does_not_overwrite_existing_cui(self, tmp_path: Path) -> None:
        from famed.medication_sync import repair_medication_rxnorm_cuis
        from famed.setup import SCHEMA_SQL

        vault_root = tmp_path / "vault"
        meds_dir = vault_root / "Records" / "dummy_alice" / "Medications"
        meds_dir.mkdir(parents=True)
        (meds_dir / "test-medications.md").write_text(
            self._make_medication_note("Metformin", rxnorm_cui="EXISTING"), encoding="utf-8"
        )

        db_path = tmp_path / "famed.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO medications (id, person_id, medication_name, medication_type, "
            "rxnorm_cui, dose, frequency, start_date, end_date, status, source_type, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "dummy_alice|test.md|0",
                "dummy_alice",
                "Metformin",
                "prescription",
                "EXISTING",
                None,
                None,
                None,
                None,
                "active",
                "ai_extracted",
                "test.md",
            ),
        )
        conn.commit()

        rxnorm_index = {"metformin": "5521"}
        repair_medication_rxnorm_cuis(vault_root, rxnorm_index, conn)
        conn.commit()

        row = conn.execute(
            "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone()
        assert row is not None
        assert row[0] == "EXISTING"
        conn.close()
