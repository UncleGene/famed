from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from famed.clinical_interpreter import (
    FACT_EXTRACTION_SCHEMA_VERSION,
    SUPPORTED_FACT_CATEGORIES,
    CandidateFact,
    CandidateHistoryFact,
    CandidateLabFact,
    CandidateMedicationFact,
    CandidateVitalFact,
    InterpreterContractError,
    InterpreterFactExtraction,
    StaticFactInterpreter,
    validate_fact_extraction,
)


def _valid_lab_payload() -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": "dummy_alice", "confidence": 0.92},
        "category": "labs",
        "category_confidence": 0.92,
        "facts": [
            {
                "test_name": "Glucose",
                "value": "102",
                "unit": "mg/dL",
                "observed_at": "2023-04-01",
                "confidence": 0.88,
            }
        ],
        "evidence": [{"field": "lab_name", "snippet": "Glucose 102", "confidence": 0.9}],
        "overall_confidence": 0.92,
    }


def _valid_medication_payload() -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": "dummy_alice", "confidence": 0.92},
        "category": "medications",
        "category_confidence": 0.90,
        "facts": [
            {
                "name": "Sample Medication",
                "dose": "20 mg",
                "frequency": "daily",
                "route": "oral",
                "status": "active",
                "start_date": "2019-10-31",
                "confidence": 0.88,
                "evidence_snippet": "Sample Medication 20 mg daily oral",
            }
        ],
        "evidence": [
            {"field": "medication_name", "snippet": "Sample Medication", "confidence": 0.9}
        ],
        "overall_confidence": 0.90,
    }


def _valid_history_payload() -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": "dummy_alice", "confidence": 0.92},
        "category": "history",
        "category_confidence": 0.90,
        "facts": [
            {
                "description": "Sample Condition",
                "category": "diagnosis",
                "icd_10_code": "Z00.00",
                "recorded_date": "2019-10-31",
                "confidence": 0.88,
                "evidence_snippet": "Sample Condition Z00.00 diagnosis",
            }
        ],
        "evidence": [
            {"field": "diagnosis", "snippet": "Sample Condition Z00.00", "confidence": 0.9}
        ],
        "overall_confidence": 0.90,
    }


def _valid_vitals_payload() -> dict[str, Any]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": "dummy_alice", "confidence": 0.92},
        "category": "vitals",
        "category_confidence": 0.90,
        "facts": [
            {
                "vital_name": "temperature",
                "value": "98.6",
                "unit": "F",
                "reference_range_low": "97.0",
                "reference_range_high": "99.0",
                "observed_at": "2019-10-31",
                "confidence": 0.88,
                "evidence_snippet": "Temperature = 98.6 F",
            }
        ],
        "evidence": [
            {"field": "temperature", "snippet": "Temperature = 98.6 F", "confidence": 0.9}
        ],
        "overall_confidence": 0.90,
    }


class TestSupportedFactCategories:
    def test_includes_labs(self) -> None:
        assert "labs" in SUPPORTED_FACT_CATEGORIES

    def test_includes_medications(self) -> None:
        assert "medications" in SUPPORTED_FACT_CATEGORIES

    def test_includes_history(self) -> None:
        assert "history" in SUPPORTED_FACT_CATEGORIES

    def test_includes_vitals(self) -> None:
        assert "vitals" in SUPPORTED_FACT_CATEGORIES


class TestCandidateMedicationFact:
    def test_required_name_field(self) -> None:
        fact = CandidateMedicationFact(name="Sample Medication")
        assert fact.name == "Sample Medication"
        assert fact.dose is None
        assert fact.frequency is None
        assert fact.route is None
        assert fact.status is None
        assert fact.start_date is None
        assert fact.end_date is None
        assert fact.medication_type == "prescription"
        assert fact.confidence == 1.0
        assert fact.evidence_snippet is None

    def test_all_fields_populated(self) -> None:
        fact = CandidateMedicationFact(
            name="Pepcid",
            dose="20 mg",
            frequency="as needed",
            route="oral",
            status="active",
            start_date="2019-10-31",
            end_date="2020-01-15",
            medication_type="supplement",
            confidence=0.92,
            evidence_snippet="Pepcid 20 mg as needed",
        )
        assert fact.name == "Pepcid"
        assert fact.dose == "20 mg"
        assert fact.frequency == "as needed"
        assert fact.route == "oral"
        assert fact.status == "active"
        assert fact.start_date == "2019-10-31"
        assert fact.end_date == "2020-01-15"
        assert fact.medication_type == "supplement"
        assert fact.confidence == 0.92
        assert fact.evidence_snippet == "Pepcid 20 mg as needed"


class TestCandidateHistoryFact:
    def test_required_description_field_with_defaults(self) -> None:
        fact = CandidateHistoryFact(description="Annual physical")
        assert fact.description == "Annual physical"
        assert fact.category == "diagnosis"
        assert fact.icd_10_code is None
        assert fact.recorded_date is None
        assert fact.confidence == 1.0
        assert fact.evidence_snippet is None

    def test_all_fields_populated(self) -> None:
        fact = CandidateHistoryFact(
            description="Preventive screening",
            category="procedure",
            icd_10_code="Z12.11",
            recorded_date="2020-01-09",
            value="72",
            unit="/min",
            cvx_code="999",
            lot_number="SYNTHETIC",
            confidence=0.95,
            evidence_snippet="Preventive screening Z12.11",
        )
        assert fact.description == "Preventive screening"
        assert fact.category == "procedure"
        assert fact.icd_10_code == "Z12.11"
        assert fact.recorded_date == "2020-01-09"
        assert fact.value == "72"
        assert fact.unit == "/min"
        assert fact.cvx_code == "999"
        assert fact.lot_number == "SYNTHETIC"
        assert fact.confidence == 0.95
        assert fact.evidence_snippet == "Preventive screening Z12.11"


class TestCandidateVitalFact:
    def test_required_vital_name_field(self) -> None:
        fact = CandidateVitalFact(vital_name="temperature")
        assert fact.vital_name == "temperature"
        assert fact.value is None
        assert fact.unit is None
        assert fact.reference_range_low is None
        assert fact.reference_range_high is None
        assert fact.observed_at is None
        assert fact.confidence == 1.0
        assert fact.evidence_snippet is None

    def test_all_fields_populated(self) -> None:
        fact = CandidateVitalFact(
            vital_name="blood pressure systolic",
            value="120",
            unit="mmHg",
            reference_range_low="90",
            reference_range_high="140",
            observed_at="2019-10-31",
            confidence=0.95,
            evidence_snippet="BP: 120/80 mmHg",
        )
        assert fact.vital_name == "blood pressure systolic"
        assert fact.value == "120"
        assert fact.unit == "mmHg"
        assert fact.reference_range_low == "90"
        assert fact.reference_range_high == "140"
        assert fact.observed_at == "2019-10-31"
        assert fact.confidence == 0.95
        assert fact.evidence_snippet == "BP: 120/80 mmHg"


class TestCandidateFactUnion:
    def test_lab_fact_is_candidate_fact(self) -> None:
        fact: CandidateFact = CandidateLabFact(test_name="Glucose", value="102", confidence=0.9)
        assert isinstance(fact, CandidateLabFact)

    def test_medication_fact_is_candidate_fact(self) -> None:
        fact: CandidateFact = CandidateMedicationFact(name="Sample Medication", confidence=0.9)
        assert isinstance(fact, CandidateMedicationFact)

    def test_history_fact_is_candidate_fact(self) -> None:
        fact: CandidateFact = CandidateHistoryFact(description="Sample Condition", confidence=0.9)
        assert isinstance(fact, CandidateHistoryFact)

    def test_vital_fact_is_candidate_fact(self) -> None:
        fact: CandidateFact = CandidateVitalFact(
            vital_name="temperature", value="98.6", confidence=0.9
        )
        assert isinstance(fact, CandidateVitalFact)


class TestValidateMedicationExtraction:
    def test_valid_medication_payload_accepted(self) -> None:
        payload = _valid_medication_payload()
        result = validate_fact_extraction(payload)
        assert isinstance(result, InterpreterFactExtraction)
        assert result.person_id == "dummy_alice"
        assert result.category == "medications"
        assert len(result.facts) == 1
        assert isinstance(result.facts[0], CandidateMedicationFact)
        assert result.facts[0].name == "Sample Medication"
        assert result.facts[0].dose == "20 mg"
        assert result.facts[0].frequency == "daily"
        assert result.facts[0].route == "oral"
        assert result.facts[0].status == "active"

    def test_rejects_missing_name(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"] = [{"confidence": 0.88}]
        with pytest.raises(InterpreterContractError, match="name is required"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_dose(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["dose"] = 20
        with pytest.raises(InterpreterContractError, match="dose must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_frequency(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["frequency"] = 1
        with pytest.raises(InterpreterContractError, match="frequency must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_route(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["route"] = 42
        with pytest.raises(InterpreterContractError, match="route must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_status(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["status"] = True
        with pytest.raises(InterpreterContractError, match="status must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_start_date(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["start_date"] = 20230101
        with pytest.raises(InterpreterContractError, match="start_date must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_end_date(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["end_date"] = 0
        with pytest.raises(InterpreterContractError, match="end_date must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_evidence_snippet(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["evidence_snippet"] = 123
        with pytest.raises(InterpreterContractError, match="evidence_snippet must be a string"):
            validate_fact_extraction(payload)

    def test_optional_fields_omitted(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"] = [{"name": "Aspirin", "confidence": 0.88}]
        result = validate_fact_extraction(payload)
        fact = result.facts[0]
        assert isinstance(fact, CandidateMedicationFact)
        assert fact.dose is None
        assert fact.frequency is None
        assert fact.route is None
        assert fact.status is None
        assert fact.medication_type == "prescription"

    def test_medication_type_supplement_accepted(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["medication_type"] = "supplement"
        result = validate_fact_extraction(payload)
        fact = result.facts[0]
        assert isinstance(fact, CandidateMedicationFact)
        assert fact.medication_type == "supplement"

    def test_medication_type_defaults_to_prescription_when_absent(self) -> None:
        payload = _valid_medication_payload()
        result = validate_fact_extraction(payload)
        fact = result.facts[0]
        assert isinstance(fact, CandidateMedicationFact)
        assert fact.medication_type == "prescription"

    def test_rejects_invalid_medication_type(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["medication_type"] = "unknown_type"
        with pytest.raises(InterpreterContractError, match="medication_type must be one of"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_medication_type(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["medication_type"] = 42
        with pytest.raises(InterpreterContractError, match="medication_type must be a string"):
            validate_fact_extraction(payload)

    def test_below_threshold_fact(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"][0]["confidence"] = 0.5
        result = validate_fact_extraction(payload)
        assert result.below_threshold()

    def test_below_threshold_overall(self) -> None:
        payload = _valid_medication_payload()
        payload["overall_confidence"] = 0.5
        result = validate_fact_extraction(payload)
        assert result.below_threshold()

    def test_multiple_medication_facts_accepted(self) -> None:
        payload = _valid_medication_payload()
        payload["facts"].append(
            {"name": "Pepcid", "dose": "20 mg", "frequency": "as needed", "confidence": 0.85}
        )
        result = validate_fact_extraction(payload)
        assert len(result.facts) == 2
        fact0 = result.facts[0]
        fact1 = result.facts[1]
        assert isinstance(fact0, CandidateMedicationFact)
        assert isinstance(fact1, CandidateMedicationFact)
        assert fact0.name == "Sample Medication"
        assert fact1.name == "Pepcid"

    def test_static_interpreter_returns_medication_payload(self) -> None:
        payload = _valid_medication_payload()
        interpreter = StaticFactInterpreter({"test.pdf": payload})
        raw = interpreter.extract_facts(Path("test.pdf"))
        assert raw["category"] == "medications"
        result = validate_fact_extraction(raw)
        assert isinstance(result.facts[0], CandidateMedicationFact)


class TestValidateHistoryExtraction:
    def test_valid_history_payload_accepted(self) -> None:
        payload = _valid_history_payload()
        result = validate_fact_extraction(payload)
        assert isinstance(result, InterpreterFactExtraction)
        assert result.person_id == "dummy_alice"
        assert result.category == "history"
        assert len(result.facts) == 1
        assert isinstance(result.facts[0], CandidateHistoryFact)
        assert result.facts[0].description == "Sample Condition"
        assert result.facts[0].category == "diagnosis"
        assert result.facts[0].icd_10_code == "Z00.00"

    def test_rejects_missing_description(self) -> None:
        payload = _valid_history_payload()
        payload["facts"] = [{"confidence": 0.88}]
        with pytest.raises(InterpreterContractError, match="description is required"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_category(self) -> None:
        payload = _valid_history_payload()
        payload["facts"][0]["category"] = 42
        with pytest.raises(InterpreterContractError, match="category must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_icd_10_code(self) -> None:
        payload = _valid_history_payload()
        payload["facts"][0]["icd_10_code"] = 2119
        with pytest.raises(InterpreterContractError, match="icd_10_code must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_recorded_date(self) -> None:
        payload = _valid_history_payload()
        payload["facts"][0]["recorded_date"] = 20230101
        with pytest.raises(InterpreterContractError, match="recorded_date must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_evidence_snippet(self) -> None:
        payload = _valid_history_payload()
        payload["facts"][0]["evidence_snippet"] = []
        with pytest.raises(InterpreterContractError, match="evidence_snippet must be a string"):
            validate_fact_extraction(payload)

    def test_optional_fields_omitted(self) -> None:
        payload = _valid_history_payload()
        payload["facts"] = [{"description": "Screening for colon cancer", "confidence": 0.88}]
        result = validate_fact_extraction(payload)
        fact = result.facts[0]
        assert isinstance(fact, CandidateHistoryFact)
        assert fact.category == "diagnosis"
        assert fact.icd_10_code is None
        assert fact.recorded_date is None

    def test_below_threshold(self) -> None:
        payload = _valid_history_payload()
        payload["overall_confidence"] = 0.5
        result = validate_fact_extraction(payload)
        assert result.below_threshold()

    def test_multiple_history_facts_accepted(self) -> None:
        payload = _valid_history_payload()
        payload["facts"].append(
            {
                "description": "Screening for colon cancer",
                "category": "diagnosis",
                "icd_10_code": "Z12.11",
                "confidence": 0.85,
            }
        )
        result = validate_fact_extraction(payload)
        assert len(result.facts) == 2
        fact1 = result.facts[1]
        assert isinstance(fact1, CandidateHistoryFact)
        assert fact1.description == "Screening for colon cancer"

    def test_static_interpreter_returns_history_payload(self) -> None:
        payload = _valid_history_payload()
        interpreter = StaticFactInterpreter({"test.pdf": payload})
        raw = interpreter.extract_facts(Path("test.pdf"))
        assert raw["category"] == "history"
        result = validate_fact_extraction(raw)
        assert isinstance(result.facts[0], CandidateHistoryFact)


class TestValidateVitalExtraction:
    def test_valid_vitals_payload_accepted(self) -> None:
        payload = _valid_vitals_payload()
        result = validate_fact_extraction(payload)
        assert isinstance(result, InterpreterFactExtraction)
        assert result.person_id == "dummy_alice"
        assert result.category == "vitals"
        assert len(result.facts) == 1
        assert isinstance(result.facts[0], CandidateVitalFact)
        assert result.facts[0].vital_name == "temperature"
        assert result.facts[0].value == "98.6"
        assert result.facts[0].unit == "F"
        assert result.facts[0].reference_range_low == "97.0"
        assert result.facts[0].reference_range_high == "99.0"

    def test_rejects_missing_vital_name(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"] = [{"confidence": 0.88}]
        with pytest.raises(InterpreterContractError, match="vital_name is required"):
            validate_fact_extraction(payload)

    def test_rejects_unsupported_vital_name(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"][0]["vital_name"] = "eye color"
        with pytest.raises(InterpreterContractError, match="vital_name must be one of"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_unit(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"][0]["unit"] = 100
        with pytest.raises(InterpreterContractError, match="unit must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_string_evidence_snippet(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"][0]["evidence_snippet"] = 42
        with pytest.raises(InterpreterContractError, match="evidence_snippet must be a string"):
            validate_fact_extraction(payload)

    def test_optional_fields_omitted(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"] = [{"vital_name": "pulse", "confidence": 0.88}]
        result = validate_fact_extraction(payload)
        fact = result.facts[0]
        assert isinstance(fact, CandidateVitalFact)
        assert fact.vital_name == "pulse"
        assert fact.value is None
        assert fact.unit is None
        assert fact.reference_range_low is None
        assert fact.reference_range_high is None
        assert fact.observed_at is None

    def test_below_threshold(self) -> None:
        payload = _valid_vitals_payload()
        payload["overall_confidence"] = 0.5
        result = validate_fact_extraction(payload)
        assert result.below_threshold()

    def test_multiple_vital_facts_accepted(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"].append(
            {
                "vital_name": "pulse",
                "value": "72",
                "unit": "bpm",
                "confidence": 0.85,
            }
        )
        result = validate_fact_extraction(payload)
        assert len(result.facts) == 2
        fact1 = result.facts[1]
        assert isinstance(fact1, CandidateVitalFact)
        assert fact1.vital_name == "pulse"
        assert fact1.value == "72"

    def test_static_interpreter_returns_vitals_payload(self) -> None:
        payload = _valid_vitals_payload()
        interpreter = StaticFactInterpreter({"test.pdf": payload})
        raw = interpreter.extract_facts(Path("test.pdf"))
        assert raw["category"] == "vitals"
        result = validate_fact_extraction(raw)
        assert isinstance(result.facts[0], CandidateVitalFact)

    def test_rejects_non_string_observed_at(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"][0]["observed_at"] = 20230101
        with pytest.raises(InterpreterContractError, match="observed_at must be a string"):
            validate_fact_extraction(payload)

    def test_rejects_non_numeric_value(self) -> None:
        payload = _valid_vitals_payload()
        payload["facts"][0]["value"] = []
        with pytest.raises(InterpreterContractError, match="value must be a string or number"):
            validate_fact_extraction(payload)


class TestExtractionCategoryDispatch:
    def test_rejects_empty_string_category(self) -> None:
        payload = _valid_lab_payload()
        payload["category"] = "  "
        with pytest.raises(InterpreterContractError, match="must be a non-empty string"):
            validate_fact_extraction(payload)

    def test_rejects_unlisted_category(self) -> None:
        payload = _valid_lab_payload()
        payload["category"] = "dna"
        with pytest.raises(InterpreterContractError, match="unsupported category"):
            validate_fact_extraction(payload)

    def test_lab_category_dispatches_to_lab_validator(self) -> None:
        result = validate_fact_extraction(_valid_lab_payload())
        for fact in result.facts:
            assert isinstance(fact, CandidateLabFact)

    def test_medication_category_dispatches_to_medication_validator(self) -> None:
        result = validate_fact_extraction(_valid_medication_payload())
        for fact in result.facts:
            assert isinstance(fact, CandidateMedicationFact)

    def test_history_category_dispatches_to_history_validator(self) -> None:
        result = validate_fact_extraction(_valid_history_payload())
        for fact in result.facts:
            assert isinstance(fact, CandidateHistoryFact)

    def test_vitals_category_dispatches_to_vital_validator(self) -> None:
        result = validate_fact_extraction(_valid_vitals_payload())
        for fact in result.facts:
            assert isinstance(fact, CandidateVitalFact)
