from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from famed.export_fhir import main as export_fhir_main
from famed.fhir_export import (
    FHIR_EXPORT_FILENAME,
    FHIR_SOURCE_PATH_EXTENSION_URL,
    FHIR_SOURCE_TYPE_EXTENSION_URL,
    FhirExportError,
    build_fhir_bundle,
    validate_fhir_bundle,
    validate_fhir_export_file,
    write_fhir_export,
)
from famed.setup import SCHEMA_SQL


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    return conn


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


def _write_history_note(
    vault_root: Path,
    *,
    person_id: str = "dummy_alice",
    name: str = "2023-04-20-history-review.md",
    verification_status: str = "accepted",
    accepted_facts: list[dict[str, Any]] | None = None,
    source_path: str = "data/processed/dummy_alice/History/2023-04-20-history.pdf",
) -> Path:
    history_note = vault_root / "Records" / person_id / "History" / name
    history_note.parent.mkdir(parents=True, exist_ok=True)
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "history",
        "source_file_name": Path(source_path).name,
        "source_path": source_path,
        "event_date": "2023-04-20",
        "title": "History Review",
        "verification_status": verification_status,
    }
    if accepted_facts is not None:
        front_matter["accepted_facts"] = accepted_facts
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    history_note.write_text(
        f"---\n{yaml_block}\n---\n\n# History Review\n",
        encoding="utf-8",
    )
    return history_note


def _insert_person(
    conn: sqlite3.Connection,
    person_id: str = "dummy_alice",
    *,
    display_name: str = "Dummy Alice Example",
    birth_year: int = 1986,
    sex: str = "female",
) -> None:
    conn.execute(
        "INSERT INTO persons (id, display_name, birth_year, sex) VALUES (?, ?, ?, ?)",
        (person_id, display_name, birth_year, sex),
    )


def _insert_family_link(
    conn: sqlite3.Connection,
    *,
    link_id: str,
    source_person_id: str,
    target_person_id: str,
    relationship_type: str,
) -> None:
    conn.execute(
        """
        INSERT INTO family_links (id, source_person_id, target_person_id, relationship_type)
        VALUES (?, ?, ?, ?)
        """,
        (link_id, source_person_id, target_person_id, relationship_type),
    )


def _insert_lab(
    conn: sqlite3.Connection,
    *,
    lab_id: str = "lab-1",
    person_id: str = "dummy_alice",
    loinc_code: str | None = "4548-4",
    test_name: str = "Hemoglobin A1c",
    value: float | None = 5.8,
    value_text: str | None = None,
    unit: str | None = "%",
    reference_range_low: float | None = 4.0,
    reference_range_high: float | None = 5.6,
    observed_at: str = "2023-04-20",
    source_type: str = "manual",
    source_path: str = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
) -> None:
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, value_text, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lab_id,
            person_id,
            loinc_code,
            test_name,
            value,
            value_text,
            unit,
            reference_range_low,
            reference_range_high,
            observed_at,
            source_type,
            source_path,
        ),
    )


def _insert_medication(
    conn: sqlite3.Connection,
    *,
    medication_id: str = "med-1",
    person_id: str = "dummy_alice",
    rxnorm_cui: str | None = "617320",
    medication_name: str = "Atorvastatin",
    dose: str | None = "10 mg",
    frequency: str | None = "daily",
    start_date: str | None = "2023-01-01",
    end_date: str | None = None,
    status: str = "active",
    source_path: str = "data/processed/dummy_alice/Medications/2023-01-01-meds.txt",
) -> None:
    conn.execute(
        """
        INSERT INTO medications (
            id, person_id, rxnorm_cui, medication_name, medication_type,
            dose, frequency, start_date, end_date, status, source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            medication_id,
            person_id,
            rxnorm_cui,
            medication_name,
            "prescription",
            dose,
            frequency,
            start_date,
            end_date,
            status,
            "manual",
            source_path,
        ),
    )


def _insert_immunization(
    conn: sqlite3.Connection,
    *,
    immunization_id: str = "imm-1",
    person_id: str = "dummy_alice",
    vaccine_name: str = "Influenza vaccine",
    cvx_code: str | None = "140",
    administered_date: str | None = "2023-01-15",
    status: str = "completed",
    lot_number: str | None = "FLU-2026",
    source_type: str = "source_import",
    source_path: str | None = ("data/processed/dummy_alice/History/2023-02-03-summary.xml"),
    source_paths: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO immunizations (
            id, person_id, vaccine_name, cvx_code, administered_date,
            status, lot_number, source_type, source_path, source_paths
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            immunization_id,
            person_id,
            vaccine_name,
            cvx_code,
            administered_date,
            status,
            lot_number,
            source_type,
            source_path,
            source_paths,
        ),
    )


def _resources_by_type(bundle: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == resource_type
    ]


def test_write_fhir_export_writes_patient_and_lab_observations(tmp_path: Path) -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn)
    _insert_lab(
        conn,
        lab_id="lab-2",
        loinc_code="1558-6",
        test_name="Fasting Glucose",
        value=102,
        unit="mg/dL",
        source_path="Records/dummy_alice/Labs/2023-04-20-panel.md",
    )
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)

    (
        export_path,
        observation_count,
        medication_statement_count,
        diagnostic_report_count,
        condition_count,
        family_member_history_count,
        immunization_count,
    ) = write_fhir_export(
        conn,
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert observation_count == 2
    assert medication_statement_count == 0
    assert diagnostic_report_count == 2
    assert condition_count == 0
    assert family_member_history_count == 0
    assert immunization_count == 0
    assert export_path == vault_root / "Records" / "dummy_alice" / "Reports" / FHIR_EXPORT_FILENAME
    bundle = json.loads(export_path.read_text(encoding="utf-8"))
    assert validate_fhir_bundle(bundle) == []
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    assert bundle["timestamp"] == "2023-05-21T12:00:00Z"

    patients = _resources_by_type(bundle, "Patient")
    assert patients == [
        {
            "resourceType": "Patient",
            "id": "patient-dummy-alice",
            "identifier": [{"system": "https://famed.local/person-id", "value": "dummy_alice"}],
            "name": [{"text": "Dummy Alice Example"}],
            "gender": "female",
            "birthDate": "1986",
        }
    ]

    observations = _resources_by_type(bundle, "Observation")
    assert [observation["code"]["text"] for observation in observations] == [
        "Fasting Glucose",
        "Hemoglobin A1c",
    ]
    a1c_observation = next(
        observation
        for observation in observations
        if observation["code"]["text"] == "Hemoglobin A1c"
    )
    assert a1c_observation["code"]["coding"][0] == {
        "system": "http://loinc.org",
        "code": "4548-4",
        "display": "Hemoglobin A1c",
    }
    assert a1c_observation["subject"] == {"reference": "Patient/patient-dummy-alice"}
    assert a1c_observation["valueQuantity"] == {"value": 5.8, "unit": "%"}
    assert a1c_observation["referenceRange"] == [
        {"low": {"value": 4.0, "unit": "%"}, "high": {"value": 5.6, "unit": "%"}}
    ]
    assert {
        "url": FHIR_SOURCE_PATH_EXTENSION_URL,
        "valueString": "data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
    } in a1c_observation["extension"]

    diagnostic_reports = _resources_by_type(bundle, "DiagnosticReport")
    assert [report["code"]["text"] for report in diagnostic_reports] == [
        "2023-04-20-panel",
        "2023-04-20-panel",
    ]
    assert all(
        report["subject"] == {"reference": "Patient/patient-dummy-alice"}
        for report in diagnostic_reports
    )
    assert all(report["status"] == "final" for report in diagnostic_reports)
    assert [len(report["result"]) for report in diagnostic_reports] == [1, 1]


def test_build_fhir_bundle_collapses_archive_restated_lab_duplicate() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(
        conn,
        lab_id="lab-pdf",
        loinc_code="100158-5",
        test_name="Albumin",
        value=4.7,
        unit="g/dL",
        reference_range_low=3.5,
        reference_range_high=5.7,
        observed_at="2023-02-12",
        source_type="pdf:mychart",
        source_path="data/processed/dummy_alice/Labs/2023-02-12-CMP.pdf",
    )
    _insert_lab(
        conn,
        lab_id="lab-archive",
        loinc_code="1751-7",
        test_name="Albumin",
        value=4.7,
        unit="g/dL",
        reference_range_low=3.5,
        reference_range_high=5.7,
        observed_at="2023-02-13T03:32:42",
        source_type="source_import",
        source_path="data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML",
    )

    bundle, observation_count, _, diagnostic_report_count, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert observation_count == 1
    assert diagnostic_report_count == 1
    observations = _resources_by_type(bundle, "Observation")
    assert observations[0]["code"]["coding"][0]["code"] == "1751-7"
    assert validate_fhir_bundle(bundle) == []


def test_build_fhir_bundle_keeps_distinct_same_value_lab_facts_for_same_test() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(
        conn,
        lab_id="lab-1",
        test_name="Albumin",
        loinc_code="1751-7",
        value=4.7,
        unit="g/dL",
        observed_at="2023-02-12",
        source_type="pdf:mychart",
        source_path="data/processed/dummy_alice/Labs/2023-02-12-CMP.pdf",
    )
    _insert_lab(
        conn,
        lab_id="lab-2",
        test_name="Albumin",
        loinc_code="1751-7",
        value=4.7,
        unit="g/dL",
        observed_at="2023-08-01",
        source_type="source_import",
        source_path="data/processed/dummy_alice/HealthSummary/archive.zip#DOC0002.XML",
    )

    bundle, observation_count, _, diagnostic_report_count, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 9, 1, tzinfo=UTC),
    )

    assert observation_count == 2
    assert diagnostic_report_count == 2
    assert validate_fhir_bundle(bundle) == []


@pytest.mark.parametrize("value_text", ["Yellow", "<0.1"])
def test_build_fhir_bundle_uses_value_string_for_textual_rows(value_text: str) -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(
        conn,
        lab_id="textual",
        loinc_code=None,
        test_name="Dummy textual result",
        value=None,
        value_text=value_text,
        unit=None,
    )

    (
        bundle,
        observation_count,
        medication_statement_count,
        diagnostic_report_count,
        condition_count,
        _,
        _,
    ) = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert observation_count == 1
    assert medication_statement_count == 0
    assert diagnostic_report_count == 1
    assert condition_count == 0
    observation = _resources_by_type(bundle, "Observation")[0]
    assert observation["code"] == {"text": "Dummy textual result"}
    assert "valueQuantity" not in observation
    assert observation["valueString"] == value_text
    assert "dataAbsentReason" not in observation
    assert validate_fhir_bundle(bundle) == []


def test_build_fhir_bundle_uses_data_absent_reason_when_all_values_are_missing() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn, lab_id="missing", test_name="Synthetic result", value=None, unit=None)

    bundle, *_ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    observation = _resources_by_type(bundle, "Observation")[0]
    assert observation["dataAbsentReason"]["coding"][0]["code"] == "unknown"
    assert "valueString" not in observation
    assert validate_fhir_bundle(bundle) == []


def test_build_fhir_bundle_groups_same_source_labs_into_diagnostic_report() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn, lab_id="lab-1", test_name="Hemoglobin A1c")
    _insert_lab(conn, lab_id="lab-2", test_name="Fasting Glucose")
    _insert_lab(
        conn,
        lab_id="lab-3",
        test_name="TSH",
        observed_at="2023-05-01",
        source_path="data/processed/dummy_alice/Labs/2023-05-01-thyroid.csv",
    )

    bundle, observation_count, _, diagnostic_report_count, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert observation_count == 3
    assert diagnostic_report_count == 2
    reports = _resources_by_type(bundle, "DiagnosticReport")
    grouped = {report["code"]["text"]: report for report in reports}
    panel = grouped["2023-04-20-panel"]
    thyroid = grouped["2023-05-01-thyroid"]
    assert len(panel["result"]) == 2
    assert len(thyroid["result"]) == 1
    assert panel["effectiveDateTime"] == "2023-04-20"
    assert thyroid["effectiveDateTime"] == "2023-05-01"
    assert {
        "url": FHIR_SOURCE_PATH_EXTENSION_URL,
        "valueString": "data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
    } in panel["extension"]
    assert all(result["reference"].startswith("Observation/") for result in panel["result"])
    assert validate_fhir_bundle(bundle) == []


def test_diagnostic_report_ids_do_not_collide_for_long_source_paths() -> None:
    conn = _connect_db()
    _insert_person(conn)
    prefix = "data/processed/dummy_alice/Labs/2023-04-20-" + ("very-long-panel-name-" * 4)
    _insert_lab(conn, lab_id="lab-1", source_path=f"{prefix}-one.csv")
    _insert_lab(conn, lab_id="lab-2", source_path=f"{prefix}-two.csv")

    bundle, _, _, diagnostic_report_count, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    reports = _resources_by_type(bundle, "DiagnosticReport")
    report_ids = [report["id"] for report in reports]
    assert diagnostic_report_count == 2
    assert len(report_ids) == len(set(report_ids))
    assert validate_fhir_bundle(bundle) == []


def test_validate_fhir_bundle_rejects_diagnostic_report_missing_observation_reference() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn)
    bundle, _, _, _, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )
    report = _resources_by_type(bundle, "DiagnosticReport")[0]
    report["result"] = [{"reference": "Observation/missing"}]

    assert validate_fhir_bundle(bundle) == [
        f"DiagnosticReport/{report['id']} result references missing Observation/missing"
    ]


def test_validate_fhir_bundle_rejects_diagnostic_report_source_mismatch() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn)
    bundle, _, _, _, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )
    report = _resources_by_type(bundle, "DiagnosticReport")[0]
    source_path_extension = next(
        extension
        for extension in report["extension"]
        if extension["url"] == FHIR_SOURCE_PATH_EXTENSION_URL
    )
    source_path_extension["valueString"] = "data/processed/dummy_alice/Labs/wrong.csv"

    assert validate_fhir_bundle(bundle) == [
        f"DiagnosticReport/{report['id']} result Observation/lab-lab-1 source path must match"
    ]


def test_build_fhir_bundle_includes_medication_statements() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_medication(conn)
    _insert_medication(
        conn,
        medication_id="med-2",
        rxnorm_cui=None,
        medication_name="Vitamin D3",
        dose=None,
        frequency=None,
        start_date=None,
        end_date="2023-03-01",
        status="discontinued",
        source_path="Records/dummy_alice/Medications/2023-03-01-vitamin-d3.md",
    )

    (
        bundle,
        observation_count,
        medication_statement_count,
        diagnostic_report_count,
        condition_count,
        _,
        _,
    ) = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert observation_count == 0
    assert medication_statement_count == 2
    assert diagnostic_report_count == 0
    assert condition_count == 0
    statements = _resources_by_type(bundle, "MedicationStatement")
    assert [statement["medicationCodeableConcept"]["text"] for statement in statements] == [
        "Atorvastatin",
        "Vitamin D3",
    ]

    atorvastatin = statements[0]
    assert atorvastatin["status"] == "active"
    assert atorvastatin["subject"] == {"reference": "Patient/patient-dummy-alice"}
    assert atorvastatin["medicationCodeableConcept"]["coding"][0] == {
        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
        "code": "617320",
        "display": "Atorvastatin",
    }
    assert atorvastatin["effectivePeriod"] == {"start": "2023-01-01"}
    assert atorvastatin["dosage"] == [{"text": "10 mg daily"}]
    assert {
        "url": FHIR_SOURCE_PATH_EXTENSION_URL,
        "valueString": "data/processed/dummy_alice/Medications/2023-01-01-meds.txt",
    } in atorvastatin["extension"]

    vitamin_d = statements[1]
    assert vitamin_d["status"] == "stopped"
    assert vitamin_d["medicationCodeableConcept"] == {"text": "Vitamin D3"}
    assert vitamin_d["effectivePeriod"] == {"end": "2023-03-01"}
    assert "dosage" not in vitamin_d
    assert validate_fhir_bundle(bundle) == []


def test_medication_statement_uses_unknown_for_unmapped_status() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_medication(conn, status="")

    bundle, _, medication_statement_count, _, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert medication_statement_count == 1
    statement = _resources_by_type(bundle, "MedicationStatement")[0]
    assert statement["status"] == "unknown"
    assert validate_fhir_bundle(bundle) == []


def test_build_fhir_bundle_includes_family_member_history_resources() -> None:
    conn = _connect_db()
    _insert_person(
        conn, "dummy_alice", display_name="Dummy Alice Example", birth_year=1986, sex="female"
    )
    _insert_person(conn, "dummy_bob", display_name="Dummy Bob Example", birth_year=1960, sex="male")
    _insert_person(conn, "cara", display_name="Cara Example", birth_year=1990, sex="female")
    _insert_family_link(
        conn,
        link_id="fam-parent",
        source_person_id="dummy_alice",
        target_person_id="dummy_bob",
        relationship_type="parent",
    )
    _insert_family_link(
        conn,
        link_id="fam-sibling",
        source_person_id="dummy_alice",
        target_person_id="cara",
        relationship_type="sibling",
    )

    (
        bundle,
        observation_count,
        medication_statement_count,
        diagnostic_report_count,
        condition_count,
        family_member_history_count,
        immunization_count,
    ) = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert observation_count == 0
    assert medication_statement_count == 0
    assert diagnostic_report_count == 0
    assert condition_count == 0
    assert family_member_history_count == 2
    assert immunization_count == 0
    family_history = _resources_by_type(bundle, "FamilyMemberHistory")
    assert [resource["id"] for resource in family_history] == [
        "fam-fam-parent",
        "fam-fam-sibling",
    ]
    parent = family_history[0]
    assert parent["status"] == "completed"
    assert parent["patient"] == {"reference": "Patient/patient-dummy-alice"}
    assert parent["relationship"] == {"text": "parent"}
    assert parent["name"] == "Dummy Bob Example"
    assert parent["bornDate"] == "1960"
    assert parent["sex"] == {"text": "male"}
    assert {
        "url": FHIR_SOURCE_PATH_EXTENSION_URL,
        "valueString": "Persons/dummy_alice.md",
    } in parent["extension"]
    assert {
        "url": FHIR_SOURCE_TYPE_EXTENSION_URL,
        "valueString": "person_relationship",
    } in parent["extension"]
    assert validate_fhir_bundle(bundle) == []


def test_validate_fhir_bundle_rejects_family_member_history_missing_relationship() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_person(conn, "dummy_bob", display_name="Dummy Bob Example", birth_year=1960, sex="male")
    _insert_family_link(
        conn,
        link_id="fam-parent",
        source_person_id="dummy_alice",
        target_person_id="dummy_bob",
        relationship_type="parent",
    )

    bundle, _, _, _, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )
    family_history = _resources_by_type(bundle, "FamilyMemberHistory")[0]
    family_history.pop("relationship", None)

    assert validate_fhir_bundle(bundle) == [
        f"FamilyMemberHistory/{family_history['id']} relationship is required"
    ]


def test_build_fhir_bundle_includes_canonical_immunizations_with_all_provenance() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_person(conn, "dummy_bob")
    primary_source = "data/processed/dummy_alice/History/2023-02-03-summary.xml"
    second_source = "data/processed/dummy_alice/History/2023-02-04-summary.xml"
    _insert_immunization(
        conn,
        source_path=primary_source,
        source_paths=json.dumps([second_source, primary_source]),
    )
    _insert_immunization(
        conn,
        immunization_id="imm-2",
        vaccine_name="COVID-19 vaccine",
        cvx_code=None,
        administered_date=None,
        lot_number=None,
        source_path="data/processed/dummy_alice/History/2023-03-01-summary.xml",
    )
    _insert_immunization(conn, immunization_id="other-person", person_id="dummy_bob")

    result = build_fhir_bundle(
        conn,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )
    bundle = result[0]
    immunizations = _resources_by_type(bundle, "Immunization")

    assert result[-1] == 2
    assert [resource["id"] for resource in immunizations] == ["imm-imm-2", "imm-imm-1"]
    dated = immunizations[1]
    assert dated["status"] == "completed"
    assert dated["patient"] == {"reference": "Patient/patient-dummy-alice"}
    assert dated["identifier"] == [
        {"system": "https://famed.local/immunization-id", "value": "imm-1"}
    ]
    assert dated["vaccineCode"] == {
        "text": "Influenza vaccine",
        "coding": [
            {
                "system": "http://hl7.org/fhir/sid/cvx",
                "code": "140",
                "display": "Influenza vaccine",
            }
        ],
    }
    assert dated["occurrenceDateTime"] == "2023-01-15"
    assert dated["lotNumber"] == "FLU-2026"
    assert [
        extension["valueString"]
        for extension in dated["extension"]
        if extension["url"] == FHIR_SOURCE_PATH_EXTENSION_URL
    ] == [primary_source, second_source]

    undated = immunizations[0]
    assert undated["vaccineCode"] == {"text": "COVID-19 vaccine"}
    assert undated["occurrenceString"] == (
        "Administration date not recorded in local canonical row"
    )
    assert "lotNumber" not in undated
    assert validate_fhir_bundle(bundle) == []


@pytest.mark.parametrize("status", ["completed", "entered-in-error", "not-done"])
def test_build_fhir_bundle_preserves_supported_immunization_status(status: str) -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_immunization(conn, status=status)

    bundle, *_ = build_fhir_bundle(conn, "dummy_alice")

    assert _resources_by_type(bundle, "Immunization")[0]["status"] == status
    assert validate_fhir_bundle(bundle) == []


def test_write_fhir_export_does_not_replace_valid_file_for_invalid_immunization(
    tmp_path: Path,
) -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_immunization(conn, status="unknown")
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    export_path = vault_root / "Records" / "dummy_alice" / "Reports" / FHIR_EXPORT_FILENAME
    export_path.parent.mkdir(parents=True)
    export_path.write_text("previous valid export\n", encoding="utf-8")

    with pytest.raises(FhirExportError, match="status is not a valid R4 code"):
        write_fhir_export(conn, vault_root, "dummy_alice")

    assert export_path.read_text(encoding="utf-8") == "previous valid export\n"


def test_build_fhir_bundle_rejects_malformed_immunization_source_paths() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_immunization(conn, source_paths="{not json")

    with pytest.raises(FhirExportError, match="source_paths must be a JSON list of strings"):
        build_fhir_bundle(conn, "dummy_alice")


def test_validate_fhir_bundle_rejects_invalid_immunization_fields() -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_immunization(conn)
    bundle, *_ = build_fhir_bundle(conn, "dummy_alice")
    immunization = _resources_by_type(bundle, "Immunization")[0]

    immunization["occurrenceDateTime"] = "not-a-date"
    immunization["vaccineCode"] = {"text": ""}
    immunization["patient"] = {"reference": "Patient/other"}
    immunization["extension"] = [
        extension
        for extension in immunization["extension"]
        if extension["url"] != FHIR_SOURCE_PATH_EXTENSION_URL
    ]

    issues = validate_fhir_bundle(bundle)
    label = f"Immunization/{immunization['id']}"
    assert f"{label} readable vaccineCode text is required" in issues
    assert f"{label} patient must reference Patient" in issues
    assert f"{label} occurrenceDateTime must be ISO date or datetime" in issues
    assert f"{label} source path and source type are required" in issues


def test_unreviewed_history_immunization_candidate_is_not_exported(tmp_path: Path) -> None:
    conn = _connect_db()
    _insert_person(conn)
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    _write_history_note(
        vault_root,
        verification_status="needs_review",
        accepted_facts=[
            {
                "description": "Influenza vaccine",
                "category": "immunization",
                "recorded_date": "2023-01-15",
                "confidence": 0.8,
            }
        ],
    )

    bundle, *_ = build_fhir_bundle(conn, "dummy_alice", vault_root=vault_root)

    assert _resources_by_type(bundle, "Immunization") == []


def test_write_fhir_export_includes_conditions_from_accepted_history(
    tmp_path: Path,
) -> None:
    conn = _connect_db()
    _insert_person(conn)
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    _write_history_note(
        vault_root,
        accepted_facts=[
            {
                "description": "Essential hypertension",
                "category": "diagnosis",
                "icd_10_code": "I10",
                "recorded_date": "2023-04-19",
                "confidence": 0.96,
            },
            {
                "description": "Mild medial compartment narrowing",
                "category": "imaging_finding",
                "icd_10_code": None,
                "recorded_date": None,
                "confidence": 0.91,
            },
            {
                "description": "Colonoscopy",
                "category": "procedure",
                "recorded_date": "2023-04-18",
                "confidence": 0.94,
            },
        ],
    )
    _write_history_note(
        vault_root,
        name="2023-04-21-unreviewed-history-review.md",
        verification_status="needs_review",
        accepted_facts=[
            {
                "description": "Unreviewed condition",
                "category": "diagnosis",
                "recorded_date": "2023-04-21",
                "confidence": 0.8,
            }
        ],
        source_path="data/processed/dummy_alice/History/2023-04-21-history.pdf",
    )

    (
        export_path,
        observation_count,
        medication_statement_count,
        diagnostic_report_count,
        condition_count,
        family_member_history_count,
        immunization_count,
    ) = write_fhir_export(
        conn,
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert observation_count == 0
    assert medication_statement_count == 0
    assert diagnostic_report_count == 0
    assert condition_count == 2
    assert family_member_history_count == 0
    assert immunization_count == 0
    bundle = json.loads(export_path.read_text(encoding="utf-8"))
    conditions = _resources_by_type(bundle, "Condition")
    assert [condition["code"]["text"] for condition in conditions] == [
        "Essential hypertension",
        "Mild medial compartment narrowing",
    ]

    hypertension = conditions[0]
    assert hypertension["subject"] == {"reference": "Patient/patient-dummy-alice"}
    assert hypertension["verificationStatus"]["coding"][0]["code"] == "unconfirmed"
    assert hypertension["category"] == [{"text": "Diagnosis"}]
    assert hypertension["code"]["coding"][0] == {
        "system": "http://hl7.org/fhir/sid/icd-10-cm",
        "code": "I10",
        "display": "Essential hypertension",
    }
    assert hypertension["recordedDate"] == "2023-04-19"
    assert {
        "url": FHIR_SOURCE_PATH_EXTENSION_URL,
        "valueString": "data/processed/dummy_alice/History/2023-04-20-history.pdf",
    } in hypertension["extension"]

    imaging_finding = conditions[1]
    assert imaging_finding["category"] == [{"text": "Imaging finding"}]
    assert imaging_finding["recordedDate"] == "2023-04-20"
    assert "coding" not in imaging_finding["code"]
    assert validate_fhir_bundle(bundle) == []


def test_validate_fhir_bundle_rejects_condition_missing_source_path(
    tmp_path: Path,
) -> None:
    conn = _connect_db()
    _insert_person(conn)
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    _write_history_note(
        vault_root,
        accepted_facts=[
            {
                "description": "Essential hypertension",
                "category": "diagnosis",
                "recorded_date": "2023-04-19",
                "confidence": 0.96,
            }
        ],
    )
    bundle, _, _, _, _, _, _ = build_fhir_bundle(
        conn,
        "dummy_alice",
        vault_root=vault_root,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )
    condition = _resources_by_type(bundle, "Condition")[0]
    condition["extension"] = [
        extension
        for extension in condition["extension"]
        if extension["url"] != FHIR_SOURCE_PATH_EXTENSION_URL
    ]

    assert validate_fhir_bundle(bundle) == [
        f"Condition/{condition['id']} source path and source type are required"
    ]


def test_validate_fhir_export_file_reports_invalid_json(tmp_path: Path) -> None:
    export_path = tmp_path / "fhir-export.json"
    export_path.write_text("{not json", encoding="utf-8")

    expected = (
        f"FHIR export is not valid JSON: {export_path}: "
        "Expecting property name enclosed in double quotes"
    )
    assert validate_fhir_export_file(export_path) == [expected]


def test_export_fhir_validate_only_reports_existing_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn)
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    write_fhir_export(
        conn,
        vault_root,
        "dummy_alice",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_fhir.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--validate-only",
        ],
    )

    assert export_fhir_main() == 0
    captured = capsys.readouterr()
    assert "Validation passed:" in captured.out
    assert captured.err == ""


def test_export_fhir_reports_family_and_immunization_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _insert_person(conn)
        _insert_immunization(conn)
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_fhir.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--db-path",
            str(db_path),
        ],
    )

    assert export_fhir_main() == 0
    captured = capsys.readouterr()
    assert "FamilyMemberHistory resources included: 0" in captured.out
    assert "Immunization resources included: 1" in captured.out
    assert captured.err == ""


def test_export_fhir_validate_only_reports_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "vault"
    export_path = vault_root / "Records" / "dummy_alice" / "Reports" / FHIR_EXPORT_FILENAME
    export_path.parent.mkdir(parents=True)
    export_path.write_text('{"resourceType": "Bundle"}\n', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_fhir.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--validate-only",
        ],
    )

    assert export_fhir_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Validation failed: {export_path}" in captured.err
    assert "- bundle.type must be collection" in captured.err
    assert "- bundle.entry must contain at least one resource" in captured.err


def test_write_fhir_export_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    conn = _connect_db()
    _insert_person(conn)
    _insert_lab(conn)
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    generated_at = datetime(2023, 5, 21, 12, 0, 0, tzinfo=UTC)

    first_path, _, _, _, _, _, _ = write_fhir_export(
        conn,
        vault_root,
        "dummy_alice",
        generated_at=generated_at,
    )
    first_content = first_path.read_text(encoding="utf-8")
    second_path, _, _, _, _, _, _ = write_fhir_export(
        conn,
        vault_root,
        "dummy_alice",
        generated_at=generated_at,
    )

    assert second_path == first_path
    assert second_path.read_text(encoding="utf-8") == first_content


def test_write_fhir_export_requires_person_note_and_database_person(tmp_path: Path) -> None:
    conn = _connect_db()
    vault_root = tmp_path / "vault"

    with pytest.raises(FhirExportError, match="person note not found"):
        write_fhir_export(conn, vault_root, "dummy_alice")

    _write_person_note(vault_root)
    with pytest.raises(FhirExportError, match="person not found in database"):
        write_fhir_export(conn, vault_root, "dummy_alice")


def test_fhir_export_module_does_not_import_ai_or_ingestion_paths() -> None:
    module_text = Path("src/famed/fhir_export.py").read_text(encoding="utf-8")
    forbidden = (
        "clinical_interpreter",
        "inbox_orchestrator",
        "import_labs",
    )
    for token in forbidden:
        assert token not in module_text
