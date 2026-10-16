"""Deterministic FHIR R4 export for current local records."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import yaml

from famed.lab_sync import collapse_archive_restated_lab_rows


class FhirExportError(ValueError):
    """Raised when a FHIR export cannot be generated safely."""


FHIR_EXPORT_FILENAME = "fhir-export.json"
FHIR_SOURCE_PATH_EXTENSION_URL = "https://famed.local/fhir/StructureDefinition/source-path"
FHIR_SOURCE_TYPE_EXTENSION_URL = "https://famed.local/fhir/StructureDefinition/source-type"
# ADR-041: a prescription's dispense-authorization window. R4 models this on
# MedicationRequest.dispenseRequest.validityPeriod, which has no MedicationStatement
# equivalent, so it is carried as an extension rather than folded into effectivePeriod —
# that period means the therapy itself, and conflating the two is the defect ADR-041 fixes.
FHIR_AUTHORIZED_THROUGH_EXTENSION_URL = (
    "https://famed.local/fhir/StructureDefinition/authorized-through"
)
LOCAL_PERSON_IDENTIFIER_SYSTEM = "https://famed.local/person-id"
FHIR_CONDITION_ID_SYSTEM = "https://famed.local/history-condition-id"
FHIR_IMMUNIZATION_ID_SYSTEM = "https://famed.local/immunization-id"
ICD_10_CM_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
CVX_SYSTEM = "http://hl7.org/fhir/sid/cvx"
IMMUNIZATION_UNKNOWN_OCCURRENCE = "Administration date not recorded in local canonical row"
FHIR_IMMUNIZATION_STATUSES = {"completed", "entered-in-error", "not-done"}
CONDITION_FACT_CATEGORIES = {"diagnosis", "imaging_finding"}
FHIR_VALUE_KEYS = (
    "valueQuantity",
    "valueCodeableConcept",
    "valueString",
    "valueBoolean",
    "valueInteger",
    "valueRange",
    "valueRatio",
    "valueSampledData",
    "valueTime",
    "valueDateTime",
    "valuePeriod",
)


def _generated_iso(generated_at: datetime | None) -> str:
    generated = generated_at or datetime.now(UTC)
    return generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fhir_id(prefix: str, raw: str) -> str:
    base = re.sub(r"[^A-Za-z0-9\-.]+", "-", raw).strip("-.")
    if base:
        value = f"{prefix}-{base}"
    else:
        value = f"{prefix}-{uuid5(NAMESPACE_URL, raw).hex[:16]}"
    return value[:64]


def _full_url(resource_type: str, resource_id: str) -> str:
    return f"urn:uuid:{uuid5(NAMESPACE_URL, f'famed:{resource_type}:{resource_id}')}"


def _resource_reference(resource_type: str, resource_id: str) -> dict[str, str]:
    return {"reference": f"{resource_type}/{resource_id}"}


def _load_person(conn: sqlite3.Connection, person_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, display_name, birth_year, sex FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        raise FhirExportError(f"person not found in database: {person_id}")
    return {
        "person_id": str(row[0]),
        "display_name": str(row[1] or person_id),
        "birth_year": row[2],
        "sex": str(row[3] or ""),
    }


def _load_lab_rows(conn: sqlite3.Connection, person_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, loinc_code, test_name, value, value_text, unit,
               reference_range_low, reference_range_high, observed_at,
               source_type, source_path
        FROM lab_results
        WHERE person_id = ?
        ORDER BY observed_at, test_name COLLATE NOCASE, source_path, id
        """,
        (person_id,),
    ).fetchall()
    return collapse_archive_restated_lab_rows(
        [
            {
                "id": str(row[0]),
                "loinc_code": str(row[1] or ""),
                "test_name": str(row[2]),
                "value": row[3],
                "value_text": str(row[4] or ""),
                "unit": str(row[5] or ""),
                "reference_range_low": row[6],
                "reference_range_high": row[7],
                "observed_at": str(row[8]),
                "source_type": str(row[9] or ""),
                "source_path": str(row[10] or ""),
            }
            for row in rows
        ]
    )


def _load_medication_rows(conn: sqlite3.Connection, person_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, rxnorm_cui, medication_name, dose, frequency,
               start_date, end_date, authorized_through, status, source_type, source_path
        FROM medications
        WHERE person_id = ?
        ORDER BY medication_name COLLATE NOCASE, start_date, source_path, id
        """,
        (person_id,),
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "rxnorm_cui": str(row[1] or ""),
            "medication_name": str(row[2]),
            "dose": str(row[3] or ""),
            "frequency": str(row[4] or ""),
            "start_date": str(row[5] or ""),
            "end_date": str(row[6] or ""),
            "authorized_through": str(row[7] or ""),
            "status": str(row[8] or ""),
            "source_type": str(row[9] or ""),
            "source_path": str(row[10] or ""),
        }
        for row in rows
    ]


def _load_immunization_rows(conn: sqlite3.Connection, person_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, vaccine_name, cvx_code, administered_date, status,
               lot_number, source_type, source_path, source_paths
        FROM immunizations
        WHERE person_id = ?
        ORDER BY administered_date, vaccine_name COLLATE NOCASE, source_path, id
        """,
        (person_id,),
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "vaccine_name": str(row[1] or ""),
            "cvx_code": str(row[2] or ""),
            "administered_date": str(row[3] or ""),
            "status": str(row[4] or "").strip().casefold(),
            "lot_number": str(row[5] or ""),
            "source_type": str(row[6] or ""),
            "source_path": str(row[7] or ""),
            "source_paths": str(row[8] or ""),
        }
        for row in rows
    ]


def _load_family_rows(conn: sqlite3.Connection, person_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            family_links.id,
            family_links.relationship_type,
            family_links.target_person_id,
            persons.display_name,
            persons.birth_year,
            persons.sex
        FROM family_links
        LEFT JOIN persons ON persons.id = family_links.target_person_id
        WHERE family_links.source_person_id = ?
        ORDER BY family_links.relationship_type, family_links.target_person_id, family_links.id
        """,
        (person_id,),
    ).fetchall()
    source_note_path = f"Persons/{person_id}.md"
    return [
        {
            "id": str(row[0]),
            "relationship_type": str(row[1] or ""),
            "target_person_id": str(row[2] or ""),
            "target_display_name": str(row[3] or row[2] or ""),
            "target_birth_year": row[4],
            "target_sex": str(row[5] or ""),
            "source_type": "person_relationship",
            "source_path": source_note_path,
        }
        for row in rows
    ]


def _front_matter(note_path: Path) -> dict[str, Any] | None:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        value = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return None
    return value if isinstance(value, dict) else None


def _load_condition_rows(vault_root: Path, person_id: str) -> list[dict[str, Any]]:
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name.startswith("_"):
            continue
        fm = _front_matter(note_path)
        if not fm or fm.get("record_type") != "history":
            continue
        if str(fm.get("verification_status") or "") != "accepted":
            continue
        accepted_facts = fm.get("accepted_facts")
        if not isinstance(accepted_facts, list):
            continue
        note_source_path = str(
            fm.get("source_path") or note_path.relative_to(vault_root).as_posix()
        )
        event_date = str(fm.get("event_date") or "")
        for index, fact in enumerate(accepted_facts):
            if not isinstance(fact, dict):
                continue
            category = str(fact.get("category") or "")
            description = str(fact.get("description") or "").strip()
            if category not in CONDITION_FACT_CATEGORIES or not description:
                continue
            recorded_date = str(fact.get("recorded_date") or event_date)
            row_key = f"{note_path.relative_to(vault_root).as_posix()}:{index}"
            rows.append(
                {
                    "id": row_key,
                    "description": description,
                    "category": category,
                    "icd_10_code": str(fact.get("icd_10_code") or ""),
                    "recorded_date": recorded_date,
                    "source_type": "history",
                    "source_path": note_source_path,
                }
            )
    return rows


def _patient_resource(person: dict[str, Any], patient_id: str) -> dict[str, Any]:
    gender = str(person["sex"]).casefold()
    if gender not in {"male", "female", "other", "unknown"}:
        gender = "unknown"

    patient: dict[str, Any] = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [
            {
                "system": LOCAL_PERSON_IDENTIFIER_SYSTEM,
                "value": person["person_id"],
            }
        ],
        "name": [{"text": person["display_name"]}],
        "gender": gender,
    }
    if person.get("birth_year") is not None:
        patient["birthDate"] = str(person["birth_year"])
    return patient


def _lab_code(row: dict[str, Any]) -> dict[str, Any]:
    code: dict[str, Any] = {"text": row["test_name"]}
    if row["loinc_code"]:
        code["coding"] = [
            {
                "system": "http://loinc.org",
                "code": row["loinc_code"],
                "display": row["test_name"],
            }
        ]
    return code


def _medication_code(row: dict[str, Any]) -> dict[str, Any]:
    code: dict[str, Any] = {"text": row["medication_name"]}
    if row["rxnorm_cui"]:
        code["coding"] = [
            {
                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "code": row["rxnorm_cui"],
                "display": row["medication_name"],
            }
        ]
    return code


def _condition_code(row: dict[str, Any]) -> dict[str, Any]:
    code: dict[str, Any] = {"text": row["description"]}
    if row["icd_10_code"]:
        code["coding"] = [
            {
                "system": ICD_10_CM_SYSTEM,
                "code": row["icd_10_code"],
                "display": row["description"],
            }
        ]
    return code


def _immunization_code(row: dict[str, Any]) -> dict[str, Any]:
    code: dict[str, Any] = {"text": row["vaccine_name"]}
    if row["cvx_code"]:
        code["coding"] = [
            {
                "system": CVX_SYSTEM,
                "code": row["cvx_code"],
                "display": row["vaccine_name"],
            }
        ]
    return code


def _quantity(value: Any, unit: str) -> dict[str, Any]:
    quantity: dict[str, Any] = {"value": value}
    if unit:
        quantity["unit"] = unit
    return quantity


def _reference_range(row: dict[str, Any]) -> list[dict[str, Any]]:
    ref_range: dict[str, Any] = {}
    if row["reference_range_low"] is not None:
        ref_range["low"] = _quantity(row["reference_range_low"], row["unit"])
    if row["reference_range_high"] is not None:
        ref_range["high"] = _quantity(row["reference_range_high"], row["unit"])
    return [ref_range] if ref_range else []


def _source_extensions(row: dict[str, Any]) -> list[dict[str, Any]]:
    extensions: list[dict[str, Any]] = []
    if row["source_path"]:
        extensions.append(
            {"url": FHIR_SOURCE_PATH_EXTENSION_URL, "valueString": row["source_path"]}
        )
    if row["source_type"]:
        extensions.append(
            {"url": FHIR_SOURCE_TYPE_EXTENSION_URL, "valueString": row["source_type"]}
        )
    return extensions


def _immunization_source_paths(row: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    serialized = row["source_paths"]
    if serialized:
        try:
            loaded = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise FhirExportError(
                f"Immunization/{row['id']} source_paths must be a JSON list of strings"
            ) from exc
        if not isinstance(loaded, list) or any(
            not isinstance(item, str) or not item.strip() for item in loaded
        ):
            raise FhirExportError(
                f"Immunization/{row['id']} source_paths must be a JSON list of strings"
            )
        paths.extend(item.strip() for item in loaded)
    if row["source_path"]:
        paths.append(row["source_path"])
    return sorted(dict.fromkeys(paths))


def _immunization_source_extensions(row: dict[str, Any]) -> list[dict[str, Any]]:
    extensions = [
        {"url": FHIR_SOURCE_PATH_EXTENSION_URL, "valueString": path}
        for path in _immunization_source_paths(row)
    ]
    if row["source_type"]:
        extensions.append(
            {"url": FHIR_SOURCE_TYPE_EXTENSION_URL, "valueString": row["source_type"]}
        )
    return extensions


def _extension_value(resource: dict[str, Any], url: str) -> str:
    extensions = resource.get("extension", [])
    if not isinstance(extensions, list):
        return ""
    for extension in extensions:
        if isinstance(extension, dict) and extension.get("url") == url:
            value = extension.get("valueString")
            return value if isinstance(value, str) else ""
    return ""


def _source_path(resource: dict[str, Any]) -> str:
    return _extension_value(resource, FHIR_SOURCE_PATH_EXTENSION_URL)


def _source_type(resource: dict[str, Any]) -> str:
    return _extension_value(resource, FHIR_SOURCE_TYPE_EXTENSION_URL)


def _has_local_provenance(resource: dict[str, Any]) -> bool:
    return bool(_source_path(resource)) and bool(_source_type(resource))


def _resource_label(resource: dict[str, Any]) -> str:
    return f"{resource.get('resourceType')}/{resource.get('id')}"


def _lab_observation(row: dict[str, Any], patient_id: str) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "resourceType": "Observation",
        "id": _fhir_id("lab", row["id"]),
        "identifier": [{"system": "https://famed.local/lab-result-id", "value": row["id"]}],
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory",
                        "display": "Laboratory",
                    }
                ],
                "text": "Laboratory",
            }
        ],
        "code": _lab_code(row),
        "subject": _resource_reference("Patient", patient_id),
        "effectiveDateTime": row["observed_at"],
    }
    if row["value"] is not None:
        observation["valueQuantity"] = _quantity(row["value"], row["unit"])
    elif row["value_text"]:
        observation["valueString"] = row["value_text"]
    else:
        observation["dataAbsentReason"] = {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                    "code": "unknown",
                    "display": "Unknown",
                }
            ],
            "text": "No value available in local canonical row",
        }

    ranges = _reference_range(row)
    if ranges:
        observation["referenceRange"] = ranges

    extensions = _source_extensions(row)
    if extensions:
        observation["extension"] = extensions
    return observation


def _medication_statement_status(status: str) -> str:
    normalized = status.strip().casefold()
    if normalized == "active":
        return "active"
    if normalized in {"discontinued", "stopped"}:
        return "stopped"
    if normalized in {"completed", "finished"}:
        return "completed"
    if normalized in {"paused", "held", "on hold", "on-hold"}:
        return "on-hold"
    if normalized in {"intended", "planned"}:
        return "intended"
    if normalized in {"not taken", "not-taken"}:
        return "not-taken"
    if normalized == "entered-in-error":
        return "entered-in-error"
    return "unknown"


def _medication_dosage(row: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part for part in (row["dose"], row["frequency"]) if part]
    return [{"text": " ".join(parts)}] if parts else []


def _medication_effective_period(row: dict[str, Any]) -> dict[str, str]:
    period: dict[str, str] = {}
    if row["start_date"]:
        period["start"] = row["start_date"]
    if row["end_date"]:
        period["end"] = row["end_date"]
    return period


def _medication_statement(row: dict[str, Any], patient_id: str) -> dict[str, Any]:
    statement: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": _fhir_id("med", row["id"]),
        "identifier": [{"system": "https://famed.local/medication-id", "value": row["id"]}],
        "status": _medication_statement_status(row["status"]),
        "medicationCodeableConcept": _medication_code(row),
        "subject": _resource_reference("Patient", patient_id),
    }
    effective_period = _medication_effective_period(row)
    if effective_period:
        statement["effectivePeriod"] = effective_period
    dosage = _medication_dosage(row)
    if dosage:
        statement["dosage"] = dosage
    extensions = _source_extensions(row)
    if row.get("authorized_through"):
        extensions.append(
            {
                "url": FHIR_AUTHORIZED_THROUGH_EXTENSION_URL,
                "valueDate": row["authorized_through"],
            }
        )
    if extensions:
        statement["extension"] = extensions
    return statement


def _immunization(row: dict[str, Any], patient_id: str) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Immunization",
        "id": _fhir_id("imm", row["id"]),
        "identifier": [{"system": FHIR_IMMUNIZATION_ID_SYSTEM, "value": row["id"]}],
        "status": row["status"],
        "vaccineCode": _immunization_code(row),
        "patient": _resource_reference("Patient", patient_id),
    }
    if row["administered_date"]:
        resource["occurrenceDateTime"] = row["administered_date"]
    else:
        resource["occurrenceString"] = IMMUNIZATION_UNKNOWN_OCCURRENCE
    if row["lot_number"]:
        resource["lotNumber"] = row["lot_number"]
    extensions = _immunization_source_extensions(row)
    if extensions:
        resource["extension"] = extensions
    return resource


def _family_relationship_code(relationship_type: str) -> dict[str, Any]:
    text = relationship_type.strip().casefold().replace("-", " ").strip()
    if not text:
        text = "related person"
    return {"text": text}


def _family_member_gender(sex: str) -> str:
    normalized = sex.strip().casefold()
    if normalized in {"male", "female", "other", "unknown"}:
        return normalized
    return "unknown"


def _family_member_history(row: dict[str, Any], patient_id: str) -> dict[str, Any]:
    family_member: dict[str, Any] = {
        "resourceType": "FamilyMemberHistory",
        "id": _fhir_id("fam", row["id"]),
        "identifier": [{"system": "https://famed.local/family-link-id", "value": row["id"]}],
        "status": "completed",
        "patient": _resource_reference("Patient", patient_id),
        "relationship": _family_relationship_code(row["relationship_type"]),
        "name": row["target_display_name"] or row["target_person_id"],
    }
    target_birth_year = row.get("target_birth_year")
    if target_birth_year is not None:
        family_member["bornDate"] = str(target_birth_year)
    target_sex = row.get("target_sex")
    if isinstance(target_sex, str) and target_sex:
        family_member["sex"] = {
            "text": _family_member_gender(target_sex),
        }
    extensions = _source_extensions(row)
    if extensions:
        family_member["extension"] = extensions
    return family_member


def _condition_id(row: dict[str, Any]) -> str:
    raw = f"famed:Condition:{row['id']}"
    return f"cond-{uuid5(NAMESPACE_URL, raw).hex[:32]}"


def _condition_category(row: dict[str, Any]) -> dict[str, Any]:
    if row["category"] == "imaging_finding":
        text = "Imaging finding"
    else:
        text = "Diagnosis"
    return {"text": text}


def _condition(row: dict[str, Any], patient_id: str) -> dict[str, Any]:
    condition: dict[str, Any] = {
        "resourceType": "Condition",
        "id": _condition_id(row),
        "identifier": [{"system": FHIR_CONDITION_ID_SYSTEM, "value": row["id"]}],
        "verificationStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                    "code": "unconfirmed",
                    "display": "Unconfirmed",
                }
            ],
            "text": "Accepted structured history fact; clinical verification not inferred",
        },
        "category": [_condition_category(row)],
        "code": _condition_code(row),
        "subject": _resource_reference("Patient", patient_id),
    }
    if row["recorded_date"]:
        condition["recordedDate"] = row["recorded_date"]
    extensions = _source_extensions(row)
    if extensions:
        condition["extension"] = extensions
    return condition


def _diagnostic_report_key(row: dict[str, Any]) -> str:
    return "|".join(
        (
            row["observed_at"],
            row["source_path"],
            row["source_type"],
        )
    )


def _diagnostic_report_id(key: str) -> str:
    return f"diag-{uuid5(NAMESPACE_URL, f'famed:DiagnosticReport:{key}').hex[:32]}"


def _diagnostic_report_code(row: dict[str, Any]) -> dict[str, Any]:
    text = "Laboratory report"
    if row["source_path"]:
        stem = Path(row["source_path"]).stem
        if stem:
            text = stem
    return {"text": text}


def _diagnostic_reports(
    lab_rows: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    patient_id: str,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, observation in zip(lab_rows, observations, strict=True):
        grouped.setdefault(_diagnostic_report_key(row), []).append((row, observation))

    for key, pairs in grouped.items():
        first_row = pairs[0][0]
        report: dict[str, Any] = {
            "resourceType": "DiagnosticReport",
            "id": _diagnostic_report_id(key),
            "identifier": [{"system": "https://famed.local/diagnostic-report-key", "value": key}],
            "status": "final",
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                            "code": "LAB",
                            "display": "Laboratory",
                        }
                    ],
                    "text": "Laboratory",
                }
            ],
            "code": _diagnostic_report_code(first_row),
            "subject": _resource_reference("Patient", patient_id),
            "effectiveDateTime": first_row["observed_at"],
            "result": [
                _resource_reference("Observation", observation["id"]) for _, observation in pairs
            ],
        }
        extensions = _source_extensions(first_row)
        if extensions:
            report["extension"] = extensions
        reports.append(report)
    return reports


def build_fhir_bundle(
    conn: sqlite3.Connection,
    person_id: str,
    *,
    vault_root: Path | None = None,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], int, int, int, int, int, int]:
    person = _load_person(conn, person_id)
    lab_rows = _load_lab_rows(conn, person_id)
    medication_rows = _load_medication_rows(conn, person_id)
    immunization_rows = _load_immunization_rows(conn, person_id)
    family_rows = _load_family_rows(conn, person_id)
    condition_rows = _load_condition_rows(vault_root, person_id) if vault_root else []
    generated_iso = _generated_iso(generated_at)
    patient_id = _fhir_id("patient", person_id)
    patient = _patient_resource(person, patient_id)
    observations = [_lab_observation(row, patient_id) for row in lab_rows]
    diagnostic_reports = _diagnostic_reports(lab_rows, observations, patient_id)
    medication_statements = [_medication_statement(row, patient_id) for row in medication_rows]
    immunizations = [_immunization(row, patient_id) for row in immunization_rows]
    family_histories = [_family_member_history(row, patient_id) for row in family_rows]
    conditions = [_condition(row, patient_id) for row in condition_rows]

    entries = [{"fullUrl": _full_url("Patient", patient_id), "resource": patient}]
    entries.extend(
        {"fullUrl": _full_url("Observation", observation["id"]), "resource": observation}
        for observation in observations
    )
    entries.extend(
        {
            "fullUrl": _full_url("DiagnosticReport", report["id"]),
            "resource": report,
        }
        for report in diagnostic_reports
    )
    entries.extend(
        {
            "fullUrl": _full_url("MedicationStatement", statement["id"]),
            "resource": statement,
        }
        for statement in medication_statements
    )
    entries.extend(
        {
            "fullUrl": _full_url("FamilyMemberHistory", family_history["id"]),
            "resource": family_history,
        }
        for family_history in family_histories
    )
    entries.extend(
        {
            "fullUrl": _full_url("Immunization", immunization["id"]),
            "resource": immunization,
        }
        for immunization in immunizations
    )
    entries.extend(
        {"fullUrl": _full_url("Condition", condition["id"]), "resource": condition}
        for condition in conditions
    )
    bundle = {
        "resourceType": "Bundle",
        "id": _fhir_id("bundle", f"{person_id}-export"),
        "type": "collection",
        "timestamp": generated_iso,
        "entry": entries,
    }
    issues = validate_fhir_bundle(bundle)
    if issues:
        raise FhirExportError("; ".join(issues))
    return (
        bundle,
        len(observations),
        len(medication_statements),
        len(diagnostic_reports),
        len(conditions),
        len(family_histories),
        len(immunizations),
    )


def validate_fhir_bundle(bundle: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if bundle.get("resourceType") != "Bundle":
        issues.append("bundle.resourceType must be Bundle")
    if bundle.get("type") != "collection":
        issues.append("bundle.type must be collection")
    if not bundle.get("id"):
        issues.append("bundle.id is required")
    if not bundle.get("timestamp"):
        issues.append("bundle.timestamp is required")
    entries = bundle.get("entry")
    if not isinstance(entries, list) or not entries:
        issues.append("bundle.entry must contain at least one resource")
        return issues

    full_urls = [entry.get("fullUrl") for entry in entries if isinstance(entry, dict)]
    if len(full_urls) != len(set(full_urls)):
        issues.append("bundle.entry.fullUrl values must be unique")

    resource_keys: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(f"bundle.entry[{index}] must be an object")
            continue
        full_url = entry.get("fullUrl")
        if not isinstance(full_url, str) or not full_url:
            issues.append(f"bundle.entry[{index}].fullUrl is required")
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            issues.append(f"bundle.entry[{index}].resource must be an object")
            continue
        resource_type = resource.get("resourceType")
        resource_id = resource.get("id")
        if not isinstance(resource_type, str) or not resource_type:
            issues.append(f"bundle.entry[{index}].resource.resourceType is required")
            continue
        if not isinstance(resource_id, str) or not resource_id:
            issues.append(f"{resource_type} resource id is required")
            continue
        resource_key = (resource_type, resource_id)
        if resource_key in resource_keys:
            issues.append(f"{resource_type}/{resource_id} appears more than once")
        resource_keys.add(resource_key)

    patient_ids = [
        entry["resource"].get("id")
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and entry["resource"].get("resourceType") == "Patient"
    ]
    if len(patient_ids) != 1:
        issues.append("bundle must contain exactly one Patient resource")
        return issues
    patient_reference = f"Patient/{patient_ids[0]}"
    observations_by_reference = {
        f"Observation/{entry['resource'].get('id')}": entry["resource"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and entry["resource"].get("resourceType") == "Observation"
    }

    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            issues.append("bundle entries must contain resource objects")
            continue
        resource_type = resource.get("resourceType")
        if resource_type == "Patient":
            if not resource.get("identifier"):
                issues.append("Patient.identifier is required for local export")
        elif resource_type == "Observation":
            label = _resource_label(resource)
            if resource.get("status") != "final":
                issues.append(f"{label} status must be final")
            if not resource.get("code"):
                issues.append(f"{label} code is required")
            if resource.get("subject", {}).get("reference") != patient_reference:
                issues.append(f"{label} subject must reference Patient")
            if not resource.get("effectiveDateTime"):
                issues.append(f"{label} effectiveDateTime is required")
            value_keys = [key for key in FHIR_VALUE_KEYS if key in resource]
            if len(value_keys) > 1:
                issues.append(f"{label} must not contain multiple value[x] fields")
            if not value_keys and "dataAbsentReason" not in resource:
                issues.append(f"{label} needs value[x] or dataAbsentReason")
            if value_keys and "dataAbsentReason" in resource:
                issues.append(f"{label} must not contain dataAbsentReason with value[x]")
            if not _has_local_provenance(resource):
                issues.append(f"{label} source path and source type are required")
        elif resource_type == "MedicationStatement":
            label = _resource_label(resource)
            if resource.get("status") not in {
                "active",
                "completed",
                "entered-in-error",
                "intended",
                "stopped",
                "on-hold",
                "unknown",
                "not-taken",
            }:
                issues.append(f"{label} status is not a valid R4 code")
            if resource.get("subject", {}).get("reference") != patient_reference:
                issues.append(f"{label} subject must reference Patient")
            medication = resource.get("medicationCodeableConcept")
            if not medication:
                issues.append(f"{label} medicationCodeableConcept is required")
            elif not isinstance(medication, dict) or not medication.get("text"):
                issues.append(f"{label} readable medication text is required")
            if not _has_local_provenance(resource):
                issues.append(f"{label} source path and source type are required")
        elif resource_type == "DiagnosticReport":
            label = _resource_label(resource)
            if resource.get("status") != "final":
                issues.append(f"{label} status must be final")
            if not resource.get("code"):
                issues.append(f"{label} code is required")
            if resource.get("subject", {}).get("reference") != patient_reference:
                issues.append(f"{label} subject must reference Patient")
            if not resource.get("effectiveDateTime"):
                issues.append(f"{label} effectiveDateTime is required")
            if not _has_local_provenance(resource):
                issues.append(f"{label} source path and source type are required")
            result_references = [
                result.get("reference")
                for result in resource.get("result", [])
                if isinstance(result, dict)
            ]
            if not result_references:
                issues.append(f"{label} result is required")
            for reference in result_references:
                if not isinstance(reference, str) or not reference.startswith("Observation/"):
                    issues.append(f"{label} results must reference Observation")
                elif reference not in observations_by_reference:
                    issues.append(f"{label} result references missing {reference}")
                else:
                    observation = observations_by_reference[reference]
                    if observation.get("effectiveDateTime") != resource.get("effectiveDateTime"):
                        issues.append(f"{label} result {reference} effectiveDateTime must match")
                    if _source_path(observation) != _source_path(resource):
                        issues.append(f"{label} result {reference} source path must match")
                    if _source_type(observation) != _source_type(resource):
                        issues.append(f"{label} result {reference} source type must match")
        elif resource_type == "Condition":
            label = _resource_label(resource)
            if resource.get("subject", {}).get("reference") != patient_reference:
                issues.append(f"{label} subject must reference Patient")
            if not resource.get("code"):
                issues.append(f"{label} code is required")
            if not resource.get("identifier"):
                issues.append(f"{label} identifier is required")
            if not resource.get("verificationStatus"):
                issues.append(f"{label} verificationStatus is required")
            if not _has_local_provenance(resource):
                issues.append(f"{label} source path and source type are required")
        elif resource_type == "FamilyMemberHistory":
            label = _resource_label(resource)
            if resource.get("status") != "completed":
                issues.append(f"{label} status must be completed")
            if resource.get("patient", {}).get("reference") != patient_reference:
                issues.append(f"{label} patient must reference Patient")
            if not resource.get("relationship"):
                issues.append(f"{label} relationship is required")
            if not resource.get("name"):
                issues.append(f"{label} name is required")
            if not _has_local_provenance(resource):
                issues.append(f"{label} source path and source type are required")
        elif resource_type == "Immunization":
            label = _resource_label(resource)
            if resource.get("status") not in FHIR_IMMUNIZATION_STATUSES:
                issues.append(f"{label} status is not a valid R4 code")
            vaccine_code = resource.get("vaccineCode")
            if not isinstance(vaccine_code, dict) or not vaccine_code.get("text"):
                issues.append(f"{label} readable vaccineCode text is required")
            if resource.get("patient", {}).get("reference") != patient_reference:
                issues.append(f"{label} patient must reference Patient")
            if not resource.get("identifier"):
                issues.append(f"{label} identifier is required")
            occurrence_keys = [
                key for key in ("occurrenceDateTime", "occurrenceString") if resource.get(key)
            ]
            if len(occurrence_keys) != 1:
                issues.append(f"{label} requires exactly one occurrence value")
            elif occurrence_keys[0] == "occurrenceDateTime":
                try:
                    datetime.fromisoformat(
                        str(resource["occurrenceDateTime"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    issues.append(f"{label} occurrenceDateTime must be ISO date or datetime")
            if not _has_local_provenance(resource):
                issues.append(f"{label} source path and source type are required")
        else:
            issues.append(f"unsupported resource type in local FHIR export: {resource_type}")
    return issues


def validate_fhir_export_file(export_path: Path) -> list[str]:
    try:
        raw_bundle = json.loads(export_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"FHIR export cannot be read: {export_path}: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"FHIR export is not valid JSON: {export_path}: {exc.msg}"]
    if not isinstance(raw_bundle, dict):
        return [f"FHIR export root must be a JSON object: {export_path}"]
    return validate_fhir_bundle(raw_bundle)


def write_fhir_export(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    *,
    generated_at: datetime | None = None,
) -> tuple[Path, int, int, int, int, int, int]:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.is_file():
        raise FhirExportError(f"person note not found for {person_id}")

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
        person_id,
        vault_root=vault_root,
        generated_at=generated_at,
    )
    reports_dir = vault_root / "Records" / person_id / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    export_path = reports_dir / FHIR_EXPORT_FILENAME
    export_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return (
        export_path,
        observation_count,
        medication_statement_count,
        diagnostic_report_count,
        condition_count,
        family_member_history_count,
        immunization_count,
    )
