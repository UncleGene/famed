from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from famed.findings_ledger import record_finding
from famed.generate_medication_safety_alerts import main as generate_alerts_main
from famed.lab_sync import write_category_index_note
from famed.medication_safety_alerts import write_medication_safety_alerts
from famed.person_sync import validate_note_front_matter
from famed.setup import SCHEMA_SQL


def _user_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_medication(
    conn: sqlite3.Connection,
    person_id: str,
    medication_name: str,
    *,
    status: str = "active",
    source_path: str = "Records/dummy_alice/Medications/2023-01-01-meds.md",
) -> None:
    conn.execute(
        """
        INSERT INTO medications
            (id, person_id, rxnorm_cui, medication_name, medication_type,
             dose, frequency, status, source_type, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{person_id}-{medication_name}",
            person_id,
            "32968",
            medication_name,
            "prescription",
            "75 mg",
            "daily",
            status,
            "manual",
            source_path,
        ),
    )


def _write_person_note(vault_root: Path, person_id: str = "dummy_alice") -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    front_matter = {
        "person_id": person_id,
        "record_type": "person",
        "display_name": "Dummy Alice Example",
        "birth_year": 1986,
        "sex": "female",
        "relationships": [],
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    person_note.write_text(f"---\n{yaml_block}\n---\n\n# Dummy Alice Example\n", encoding="utf-8")


def _write_report(
    reports_dir: Path,
    filename: str,
    report_type: str,
    heading: str,
    body: str,
    *,
    extra_front_matter: dict[str, str] | None = None,
) -> None:
    front_matter = {
        "person_id": reports_dir.parent.name,
        "record_type": "report",
        "report_type": report_type,
        "generated_at": "2023-05-21T00:00:00Z",
    }
    front_matter.update(extra_front_matter or {})
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / filename).write_text(
        f"---\n{yaml_block}\n---\n\n# {heading}\n\n[[_index|<- Reports]]\n\n{body}\n",
        encoding="utf-8",
    )


def _scaffold_reports(vault_root: Path) -> Path:
    _write_person_note(vault_root)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        "## Prescriptions\n\n| Medication | Dose |\n| --- | --- |\n| Clopidogrel | 75 mg |\n",
    )
    _write_report(
        reports_dir,
        "medication-conflicts.md",
        "medication_conflicts",
        "Medication Conflicts",
        "## Conflicts\n\n| Medication | Issue |\n| --- | --- |\n| Clopidogrel | duplicate dose |\n",
    )
    _write_report(
        reports_dir,
        "genetics.md",
        "genetics",
        "Genetics",
        "## Medication Genes (wallet card)\n\n_No medication gene could be interpreted._\n",
    )
    _write_report(
        reports_dir,
        "latest-lab-results.md",
        "latest_lab_results",
        "Latest Lab Results",
        "## Results\n\n| test_name | latest_value |\n| --- | --- |\n| Creatinine | 0.8 |\n",
    )
    _write_report(
        reports_dir,
        "lab-conflicts.md",
        "lab_conflicts",
        "Lab Conflicts",
        "_No conflicting lab entries found._\n",
    )
    _write_report(
        reports_dir,
        "medical-history-summary.md",
        "medical_history_summary",
        "Medical History Summary",
        "## Diagnoses / Findings\n\n- 2023-01-10: Hypertension. Status: needs_review\n",
    )
    write_category_index_note(
        reports_dir, "dummy_alice", "Reports", display_name="Dummy Alice Example"
    )
    return reports_dir


def test_write_medication_safety_alerts_surfaces_conflicts_findings_and_boundaries(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)
    ledger = tmp_path / "ledger" / "findings.jsonl"
    record_finding(
        ledger,
        finding_id="pgx-ready",
        person_id="dummy_alice",
        domain="pgx_coverage",
        statement=(
            "Clopidogrel has local CPIC coverage for CYP2C19; phenotype remains not interpreted."
        ),
        evidence_tier="E1",
        confidence=0.85,
        evidence_check="verified",
        source_paths=["Records/dummy_alice/Reports/pgx-wallet-card.md"],
    )
    record_finding(
        ledger,
        finding_id="other-domain",
        person_id="dummy_alice",
        domain="carrier_screening",
        statement="Carrier finding should not appear.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/carrier-screening.md"],
    )

    report_path, finding_count = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        findings_ledger_path=ledger,
        generated_at=datetime(2023, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert finding_count == 1
    assert report_path == reports_dir / "medication-safety-alerts.md"
    content = report_path.read_text(encoding="utf-8")
    assert "# Medication Safety Alerts - Dummy Alice Example" in content
    assert "Contact a healthcare professional" in content
    assert "not medical advice" in content
    assert "do not recommend specific dose changes" in content
    assert "do not replace clinical pharmacogenetic assessment" in content
    assert "Medication conflict rows are present." in content
    assert "Clopidogrel has local CPIC coverage for CYP2C19" in content
    assert "Carrier finding should not appear." not in content
    assert "| Alert | Review Priority | Reason | Clinician Action |" in content
    assert "| Alert | Review Priority | Reason | Clinician Action | Sources |" not in content
    assert "| Domain | Evidence | Confidence | Finding |" in content
    assert "| Domain | Evidence | Confidence | Finding | Sources |" not in content
    assert "increase clopidogrel" not in content.casefold()
    assert "decrease clopidogrel" not in content.casefold()

    violations = validate_note_front_matter(report_path)
    assert violations == []
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["report_type"] == "medication_safety_alerts"
    assert front_matter["active_findings_count"] == 1
    assert front_matter["unreviewed_reports"] == ["medical_history_summary"]
    assert "Records/dummy_alice/Reports/current-medications.md" in front_matter["source_reports"]


def test_write_medication_safety_alerts_surfaces_missing_and_stale_reports(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        "## Prescriptions\n\n_None recorded._\n",
        extra_front_matter={"processing_state": "stale"},
    )
    write_category_index_note(
        reports_dir, "dummy_alice", "Reports", display_name="Dummy Alice Example"
    )

    report_path, finding_count = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        findings_ledger_path=tmp_path / "missing-ledger.jsonl",
        generated_at=datetime(2023, 5, 21, 13, 0, 0, tzinfo=UTC),
    )

    assert finding_count == 0
    content = report_path.read_text(encoding="utf-8")
    assert "Missing source reports:" in content
    assert "medication_conflicts" in content
    assert "genetics" in content
    assert "Stale source reports: current_medications" in content
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert "medication_conflicts" in front_matter["missing_reports"]
    assert "genetics" in front_matter["missing_reports"]
    assert front_matter["stale_reports"] == ["current_medications"]


def test_write_medication_safety_alerts_registers_reports_index(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)

    write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    index_content = (reports_dir / "_index.md").read_text(encoding="utf-8")
    assert "[[medication-safety-alerts|Medication safety alerts]]" in index_content


def test_generate_medication_safety_alerts_helper_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_medication_safety_alerts.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--findings-ledger",
            str(tmp_path / "missing-ledger.jsonl"),
        ],
    )

    assert generate_alerts_main() == 0
    captured = capsys.readouterr()
    assert "Wrote" in captured.out
    assert "Medication/PGx findings included: 0" in captured.out


def test_write_medication_safety_alerts_phenotype_alerts_avoid_dose_or_switch_language(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)
    _write_report(
        reports_dir,
        "genetics.md",
        "genetics",
        "Genetics",
        "## Medication Genes (wallet card)\n\n"
        "| Gene | Result | Affects current medications |\n| --- | --- | --- |\n"
        "| CYP2C19 | ultrarapid metabolizer | Atorvastatin |\n",
    )

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8").casefold()
    assert "genetics-medication flag" in content
    assert "cpic phenotype-drug alert" not in content
    assert "cpic recommendation:" not in content
    assert "adjust the dose significantly" not in content
    assert "switch medications" not in content
    assert "different dose or medication would work better" not in content
    assert "observed medication response matches this genetic result" in content


def test_write_medication_safety_alerts_surfaces_transporter_function_result(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)
    _write_report(
        reports_dir,
        "genetics.md",
        "genetics",
        "Genetics",
        "## Medication Genes (wallet card)\n\n"
        "| Gene | Result | Affects current medications |\n| --- | --- | --- |\n"
        "| SLCO1B1 | poor function | Atorvastatin |\n",
    )

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8").casefold()
    assert "genetics-medication flag" in content
    assert "poor function" in content
    assert "transporter" in content
    assert "recommend dosing" not in content


def test_write_medication_safety_alerts_skips_normal_metabolizer_finding_alert(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ledger = tmp_path / "ledger" / "findings.jsonl"
    record_finding(
        ledger,
        finding_id="normal-pgx",
        person_id="dummy_alice",
        domain="pgx_coverage",
        statement=(
            "Omeprazole: DNA data indicates this person processes this medication "
            "as normal metabolizer (CYP2C19)."
        ),
        evidence_tier="E1",
        confidence=0.85,
        evidence_check="verified",
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        findings_ledger_path=ledger,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    review_section = content.split("## Review Alerts\n\n", 1)[1].split("## Source Reports", 1)[0]
    assert (
        "Omeprazole: DNA data indicates this person processes this medication "
        "as normal metabolizer (CYP2C19)."
    ) not in review_section
    assert "## Active Medication/PGx Findings" in content
    assert "normal metabolizer (CYP2C19)." in content


def test_write_medication_safety_alerts_skips_normal_function_finding_alert(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ledger = tmp_path / "ledger" / "findings.jsonl"
    record_finding(
        ledger,
        finding_id="normal-function-pgx",
        person_id="dummy_alice",
        domain="pgx_coverage",
        statement="Atorvastatin: DNA data indicates predicted normal function (SLCO1B1).",
        evidence_tier="E1",
        confidence=0.85,
        evidence_check="verified",
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        findings_ledger_path=ledger,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    review_section = content.split("## Review Alerts\n\n", 1)[1].split("## Source Reports", 1)[0]
    assert "predicted normal function" not in review_section
    assert "predicted normal function" in content


# ---------------------------------------------------------------------------
# Anticholinergic / sedation burden alerts and Drug Interaction Alerts section
# ---------------------------------------------------------------------------


def _scaffold_with_medications(vault_root: Path, medication_names: list[str]) -> Path:
    """Scaffold a vault with a current-medications report listing the given drugs."""
    _write_person_note(vault_root)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    med_rows = "\n".join(f"| {name} | 50 mg |" for name in medication_names)
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        f"## Prescriptions\n\n| Medication | Dose |\n| --- | --- |\n{med_rows}\n",
    )
    for fn, rt in [
        ("medication-conflicts.md", "medication_conflicts"),
        ("genetics.md", "genetics"),
        ("latest-lab-results.md", "latest_lab_results"),
        ("lab-conflicts.md", "lab_conflicts"),
        ("medical-history-summary.md", "medical_history_summary"),
    ]:
        _write_report(reports_dir, fn, rt, "Report", "_No data._\n")
    write_category_index_note(
        reports_dir, "dummy_alice", "Reports", display_name="Dummy Alice Example"
    )
    return reports_dir


def test_write_medication_safety_alerts_anticholinergic_burden_for_known_drug(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Diphenhydramine"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "| Anticholinergic burden |" in content
    assert "Diphenhydramine: ACB 3" in content
    assert "Total anticholinergic burden score: 3" in content
    assert "2023 AGS Beers Criteria" in content
    assert "screening flag, not a medication change" in content


def test_write_medication_safety_alerts_sedation_burden_for_known_drug(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Lorazepam"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "| Sedation burden |" in content
    assert "Lorazepam: tier 3" in content
    assert "Total sedation burden tier: 3" in content
    assert "CNS depressant" in content


def test_write_medication_safety_alerts_cumulative_burden_for_multiple_drugs(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    # Diphenhydramine: ACB 3, Paroxetine: ACB 2 → total 5
    _scaffold_with_medications(vault_root, ["Diphenhydramine", "Paroxetine"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "Total anticholinergic burden score: 5" in content
    assert "Diphenhydramine: ACB 3" in content
    assert "Paroxetine: ACB 2" in content


def test_write_medication_safety_alerts_no_burden_alert_for_unknown_drug(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Atorvastatin"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "| Anticholinergic burden |" not in content
    assert "| Sedation burden |" not in content


def test_write_medication_safety_alerts_drug_interaction_section_present(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Lorazepam", "Gabapentin"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Drug Interaction Alerts" in content
    assert "| Sedation burden |" in content
    assert "Lorazepam: tier" in content


def test_write_medication_safety_alerts_interaction_alerts_not_duplicated_in_review_alerts(
    tmp_path: Path,
) -> None:
    """Drug Interaction Alerts and Review Alerts must not list the same alert twice."""
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Lorazepam"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Drug Interaction Alerts" in content
    assert "| Sedation burden |" in content
    review_section = content.split("## Review Alerts\n\n", 1)[1].split("## Source Reports", 1)[0]
    assert "Sedation burden" not in review_section


def test_write_medication_safety_alerts_interaction_section_empty_without_interaction_alerts(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Atorvastatin"])

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Drug Interaction Alerts" in content
    assert "No drug interaction or burden alerts detected" in content


def test_write_medication_safety_alerts_brand_name_triggers_burden_alert(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Ativan"])  # brand name for lorazepam

    report_path, _ = write_medication_safety_alerts(
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "| Sedation burden |" in content
    assert "Ativan: tier 3" in content


def test_write_medication_safety_alerts_burden_alerts_idempotent(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_with_medications(vault_root, ["Diphenhydramine", "Lorazepam"])
    ts = datetime(2023, 5, 21, 12, 0, 0, tzinfo=UTC)

    path1, _ = write_medication_safety_alerts(vault_root, "dummy_alice", generated_at=ts)
    path2, _ = write_medication_safety_alerts(vault_root, "dummy_alice", generated_at=ts)

    assert path1.read_text(encoding="utf-8") == path2.read_text(encoding="utf-8")
