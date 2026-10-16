from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from famed.doctor_visit_packet import (
    DoctorVisitPacketError,
    write_doctor_visit_packet,
    write_doctor_visit_packets_index,
)
from famed.history_sync import write_medical_history_summary_report
from famed.lab_sync import write_category_index_note
from famed.person_sync import validate_note_front_matter
from famed.report_rendering import markdown_table_cell


def _write_person_note(
    vault_root: Path,
    person_id: str,
    *,
    display_name: str = "Dummy Alice Example",
    relationships: list[dict[str, str]] | None = None,
) -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    front_matter = {
        "person_id": person_id,
        "record_type": "person",
        "display_name": display_name,
        "birth_year": 1986,
        "sex": "female",
        "relationships": relationships or [],
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    person_note.write_text(f"---\n{yaml_block}\n---\n\n# {display_name}\n", encoding="utf-8")


def _write_report(
    reports_dir: Path,
    filename: str,
    report_type: str,
    heading: str,
    body: str,
) -> None:
    front_matter = {
        "person_id": reports_dir.parent.name,
        "record_type": "report",
        "report_type": report_type,
        "generated_at": "2023-05-07T00:00:00Z",
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / filename).write_text(
        f"---\n{yaml_block}\n---\n\n# {heading}\n\n[[_index|← Reports]]\n\n{body}\n",
        encoding="utf-8",
    )


def _scaffold_reports(vault_root: Path, person_id: str = "dummy_alice") -> Path:
    reports_dir = vault_root / "Records" / person_id / "Reports"
    _write_person_note(
        vault_root,
        person_id,
        relationships=[{"relationship_type": "parent", "target_person_id": "dummy_bob"}],
    )
    _write_person_note(vault_root, "dummy_bob", display_name="Dummy Bob Example")
    _write_report(
        reports_dir,
        "medical-history-summary.md",
        "medical_history_summary",
        "Medical History Summary",
        "### 2023-01-15 — Annual Visit\n- Source: visit.pdf  Status: needs_review\n",
    )
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        "## Prescriptions\n\n| Medication | Dose |\n| --- | --- |\n| Lisinopril | 10 mg |\n",
    )
    _write_report(
        reports_dir,
        "latest-lab-results.md",
        "latest_lab_results",
        "Latest Lab Results",
        (
            "## Results\n\n### 2023-01-10\n\n| test_name | latest_value |\n"
            "| --- | --- |\n| Glucose | 99 |\n"
        ),
    )
    _write_report(
        reports_dir,
        "lab-conflicts.md",
        "lab_conflicts",
        "Lab Conflicts",
        "## Conflicts\n\n| observed_at | test_name |\n| --- | --- |\n| 2023-01-10 | Glucose |\n",
    )
    _write_report(
        reports_dir,
        "medication-conflicts.md",
        "medication_conflicts",
        "Medication Conflicts",
        "_No conflicting medication entries found._\n",
    )
    write_category_index_note(reports_dir, person_id, "Reports", display_name="Dummy Alice Example")
    return reports_dir


def _write_rich_reports(vault_root: Path, person_id: str = "dummy_alice") -> None:
    reports_dir = vault_root / "Records" / person_id / "Reports"
    _write_report(
        reports_dir,
        "medical-history-summary.md",
        "medical_history_summary",
        "Medical History",
        "## Digest\n\n- Hypertension. First noted: 2015-04-20.\n"
        "- Prostate cancer. First noted: 2016-09-05.\n\n"
        "## Medical Areas\n\n### Cardiovascular\n- Long body detail not for the packet\n\n"
        "## Vital Signs Snapshot\n\n| Measurement | Latest | Date | Earlier range | Readings |\n"
        "| --- | --- | --- | --- | --- |\n| Pulse | 88 /min | 2023-06-01 | 58-96 /min | 11 |\n",
    )
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        "## Prescriptions\n\n### Regular\n"
        "| Medication | Dose | Frequency | Status | Start | End | Refills through | RxNorm |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| examplepril | 10 mg | once daily | active | 2017-01-01 |  |  | 111 |\n"
        "| examplezole | 20 mg | once daily | active | 2021-02-24 |  "
        "| 2023-06-27 — renewal due | 222 |\n",
    )
    _write_report(
        reports_dir,
        "health-advisory.md",
        "health_advisory",
        "Health Advisory",
        "## Lab Observations\n\n"
        "- **Analyte In** — **Latest:** 5.0 mg/dL · **Reference Range:** 4.0–6.0 · "
        "**Trend:** ↑ · **In range:** Yes · **Previous:** 4.5 · **Date:** 2023-06-24\n"
        "- **Analyte Mild** — **Latest:** 6.5 mg/dL · **Reference Range:** 4.0–6.0 · "
        "**Trend:** ↑ · **In range:** No · **Previous:** 6.1 · **Date:** 2023-06-24\n"
        "- **Analyte Severe** — **Latest:** 134.0 ng/mL · **Reference Range:** 0.0–5.6 · "
        "**Trend:** ↑ · **In range:** No · **Previous:** 82.7 · **Date:** 2023-06-24\n\n"
        "## Family History Context\n\n> [!note] Family history observations\n\n"
        "**Dummy Bob Example** (parent) — [[dummy_bob|view record]]\n  - Asthma\n\n"
        "## Source Reports\n\n- x\n",
    )
    _write_report(
        reports_dir,
        "medication-safety-alerts.md",
        "medication_safety_alerts",
        "Medication Safety Alerts",
        "## Review Alerts\n\n| Alert | Review Priority | Reason | Clinician Action |\n"
        "| --- | --- | --- | --- |\n"
        "| Duplicate therapy | review | examplelol appears 2 times | Verify |\n",
    )
    _write_report(
        reports_dir,
        "flagged-for-discussion.md",
        "flagged_for_discussion",
        "Flagged For Discussion",
        "## Out of Range\n\n_none_\n\n## Coronary Calcium\n\n"
        "- 2099-03-10 — Observed Agatston coronary calcium score: 12.3 Agatston score. "
        "Source: [[calcium-note|calcium-note]]\n",
    )
    _write_report(
        reports_dir,
        "genetics.md",
        "genetics",
        "Genetics",
        "## Medication Genes (wallet card)\n\n"
        "| Gene | Result | Affects current medications |\n| --- | --- | --- |\n"
        "| CYP2C19 | normal metabolizer | examplezole |\n",
    )
    _write_report(
        reports_dir,
        "immunizations.md",
        "immunizations",
        "Immunizations",
        "| vaccine_name | administered_date | status |\n| --- | --- | --- |\n"
        "| Example vaccine | 2019-06-10 | completed |\n",
    )


def test_packet_is_a_curated_standalone_summary(tmp_path: Path) -> None:
    """The packet carries the facts a doctor needs, ranked, without whole bodies (ADR-031)."""
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_rich_reports(vault_root)

    packet_path = write_doctor_visit_packet(
        vault_root, "dummy_alice", generated_at=datetime(2023, 5, 7, tzinfo=UTC)
    )
    content = packet_path.read_text(encoding="utf-8")

    headings = [line for line in content.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Patient",
        "## Allergies",
        "## Problem List",
        "## Current Medications",
        "## Labs To Discuss",
        "## Coronary Calcium",
        "## Latest Vital Signs",
        "## DNA-Based Medication Results",
        "## Recent Immunizations",
        "## Record Status",
        "## Family History (Blood Relatives)",
        "## Detail Reports",
    ]
    assert "- Prostate cancer. First noted: 2016-09-05." in content
    assert "Long body detail not for the packet" not in content
    assert "111" not in content  # RxNorm column dropped
    assert content.index("Analyte Severe") < content.index("Analyte Mild")
    assert content.index("Analyte Mild") < content.index("Analyte In")
    assert "| Pulse | 88 /min | 2023-06-01 |" in content
    assert "- CYP2C19: normal metabolizer (affects examplezole)" in content
    assert "**Dummy Bob Example** (parent)" in content
    assert len(content.splitlines()) <= 120
    assert validate_note_front_matter(packet_path) == []


def test_packet_carries_calcium_and_record_status_without_generated_questions(
    tmp_path: Path,
) -> None:
    """No auto-generated questions; calcium findings and record gaps stay visible (ADR-031)."""
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_rich_reports(vault_root)

    packet_path = write_doctor_visit_packet(
        vault_root, "dummy_alice", generated_at=datetime(2023, 5, 7, tzinfo=UTC)
    )
    content = packet_path.read_text(encoding="utf-8")

    assert "Questions" not in content
    assert "what does this mean" not in content
    assert "Duplicate therapy" not in content
    calcium = content.split("## Coronary Calcium", 1)[1].split("## ", 1)[0]
    assert "Agatston coronary calcium score: 12.3 Agatston score" in calcium
    assert "Flagged for discussion" in calcium
    status = content.split("## Record Status", 1)[1].split("## ", 1)[0]
    assert "Sources disagree: [[Records/dummy_alice/Reports/lab-conflicts|Lab conflicts]]" in status
    assert "Medication conflicts" not in status
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert "question_buckets" not in front_matter


def test_packet_states_missing_inputs_instead_of_failing(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    _write_person_note(vault_root, "dummy_alice", relationships=[])
    reports_dir.mkdir(parents=True)

    packet_path = write_doctor_visit_packet(
        vault_root, "dummy_alice", generated_at=datetime(2023, 5, 7, 1, tzinfo=UTC)
    )
    content = packet_path.read_text(encoding="utf-8")

    assert "_Medical history summary not available yet._" in content
    assert "_Current medications report not available yet._" in content
    assert "No allergy information recorded" in content
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["missing_reports"] == [
        "medical_history_summary",
        "current_medications",
        "health_advisory",
    ]


def test_write_doctor_visit_packet_collision_suffix(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    generated_at = datetime(2023, 5, 7, 2, 0, 0, tzinfo=UTC)

    first = write_doctor_visit_packet(vault_root, "dummy_alice", generated_at=generated_at)
    second = write_doctor_visit_packet(vault_root, "dummy_alice", generated_at=generated_at)

    assert first.name == "20230507-020000-doctor-visit-packet.md"
    assert second.name == "20230507-020000-doctor-visit-packet-2.md"


def test_write_doctor_visit_packets_index_and_reports_index_include_nested_links(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)

    packet_path = write_doctor_visit_packet(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 7, 3, 0, 0, tzinfo=UTC),
    )

    top_index = reports_dir / "_index.md"
    nested_index = reports_dir / "doctor-visit-packets" / "_index.md"
    top_content = top_index.read_text(encoding="utf-8")
    nested_content = nested_index.read_text(encoding="utf-8")
    assert "[[doctor-visit-packets/_index|Doctor visit packets]]" in top_content
    assert f"[[{packet_path.stem}]]" in nested_content


def test_top_level_reports_index_keeps_nested_packet_link_after_report_regeneration(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    person_id = "dummy_alice"
    _write_person_note(vault_root, person_id, relationships=[])
    reports_dir = vault_root / "Records" / person_id / "Reports"
    history_dir = vault_root / "Records" / person_id / "History"
    history_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    history_front_matter = {
        "person_id": person_id,
        "record_type": "history",
        "source_file_name": "visit.pdf",
        "source_path": f"data/processed/{person_id}/History/visit.pdf",
        "event_date": "2023-01-15",
        "title": "Annual Visit",
        "verification_status": "accepted",
    }
    yaml_block = yaml.safe_dump(history_front_matter, sort_keys=False, allow_unicode=False).strip()
    (history_dir / "2023-01-15-visit.md").write_text(
        f"---\n{yaml_block}\n---\n\n# History Review\n\nNarrative.\n",
        encoding="utf-8",
    )
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        "## Prescriptions\n\n_None recorded._\n",
    )
    _write_report(
        reports_dir,
        "latest-lab-results.md",
        "latest_lab_results",
        "Latest Lab Results",
        "## Results\n\n_No labs recorded._\n",
    )
    write_category_index_note(reports_dir, person_id, "Reports", display_name="Dummy Alice Example")

    write_doctor_visit_packet(
        vault_root,
        person_id,
        generated_at=datetime(2023, 5, 7, 4, 0, 0, tzinfo=UTC),
    )
    write_medical_history_summary_report(vault_root, person_id)

    top_index = reports_dir / "_index.md"
    top_content = top_index.read_text(encoding="utf-8")
    assert "[[medical-history-summary|Medical history summary]]" in top_content
    assert "[[doctor-visit-packets/_index|Doctor visit packets]]" in top_content


def test_markdown_table_cell_escapes_pipes_and_newlines() -> None:
    assert markdown_table_cell("A|B\nC") == r"A\|B C"


def test_doctor_visit_packet_module_does_not_import_ingestion_or_ai_paths() -> None:
    module_text = Path("src/famed/doctor_visit_packet.py").read_text(encoding="utf-8")
    forbidden = (
        "extract_pdf_text",
        "clinical_interpreter",
        "inbox_orchestrator",
    )
    for token in forbidden:
        assert token not in module_text


def test_packet_output_dir_allowlist_rejects_noncanonical_path(tmp_path: Path) -> None:
    from famed.doctor_visit_packet import _validate_allowed_packet_dir

    vault_root = tmp_path / "vault"
    with pytest.raises(DoctorVisitPacketError, match="doctor-visit packet output must stay under"):
        _validate_allowed_packet_dir(
            vault_root,
            "dummy_alice",
            vault_root / "Records" / "dummy_bob" / "Reports",
        )


def test_write_doctor_visit_packets_index_empty_state(tmp_path: Path) -> None:
    packet_dir = tmp_path / "vault" / "Records" / "dummy_alice" / "Reports" / "doctor-visit-packets"
    packet_dir.mkdir(parents=True)
    index_path = write_doctor_visit_packets_index(
        packet_dir, "dummy_alice", display_name="Dummy Alice"
    )
    content = index_path.read_text(encoding="utf-8")
    assert "_No doctor-visit packets yet._" in content
