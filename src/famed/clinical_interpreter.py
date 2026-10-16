#!/usr/bin/env python3
"""Schema-constrained Clinical Interpreter contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

FACT_EXTRACTION_SCHEMA_VERSION = "clinical_interpreter.fact_extraction.v1"
SUPPORTED_FACT_CATEGORIES = frozenset({"history", "labs", "medications", "vitals"})

DEFAULT_FACT_CONFIDENCE_THRESHOLD = 0.75

KNOWN_VITAL_NAMES = frozenset(
    {
        "temperature",
        "blood pressure systolic",
        "blood pressure diastolic",
        "height",
        "weight",
        "bmi",
        "pulse",
        "oxygen saturation",
        "height-cm",
        "weight-kg",
    }
)


class InterpreterContractError(ValueError):
    """Raised when Clinical Interpreter output violates a contract."""


class FactInterpreter(Protocol):
    """Interpreter that extracts structured candidate facts from documents.

    Output is untrusted — callers must pass it through
    ``validate_fact_extraction()`` before acting on it.
    """

    def extract_facts(self, source_path: Path) -> Mapping[str, Any]:
        """Return an untrusted fact extraction payload for schema validation."""


@dataclass(frozen=True)
class InterpreterEvidence:
    field: str
    snippet: str
    confidence: float


def _validate_evidence(evidence_items: Sequence[Any]) -> tuple[InterpreterEvidence, ...]:
    evidence: list[InterpreterEvidence] = []
    for index, raw_item in enumerate(evidence_items):
        item = _coerce_mapping(raw_item, f"evidence[{index}]")
        evidence.append(
            InterpreterEvidence(
                field=_required_str(item, "field"),
                snippet=_required_str(item, "snippet"),
                confidence=_required_confidence(item, "confidence"),
            )
        )
    return tuple(evidence)


@dataclass(frozen=True)
class CandidateLabFact:
    """A single candidate lab observation extracted by the interpreter.

    All values are raw (as found in the document). The Canonicalizer
    handles unit normalization, LOINC mapping, and type coercion.
    """

    test_name: str
    value: str | None = None
    unit: str | None = None
    reference_range_low: str | None = None
    reference_range_high: str | None = None
    observed_at: str | None = None
    confidence: float = 1.0
    evidence_snippet: str | None = None


@dataclass(frozen=True)
class CandidateVitalFact:
    """A single candidate vital-sign measurement extracted by the interpreter.

    All values are raw (as found in the document). The Canonicalizer
    handles unit normalization and type coercion.
    """

    vital_name: str
    value: str | None = None
    unit: str | None = None
    reference_range_low: str | None = None
    reference_range_high: str | None = None
    observed_at: str | None = None
    confidence: float = 1.0
    evidence_snippet: str | None = None


MedicationType = Literal["prescription", "otc", "supplement", "herbal", "vaccine"]
VALID_MEDICATION_TYPES: frozenset[str] = frozenset(
    {"prescription", "otc", "supplement", "herbal", "vaccine"}
)


@dataclass(frozen=True)
class CandidateMedicationFact:
    """A single candidate medication extracted by the interpreter.

    All values are raw (as found in the document). The Canonicalizer
    handles RxNorm mapping, dose normalization, and status resolution.

    medication_type defaults to "prescription" for legacy/manual payloads. AI-assisted
    medication extraction asks for an explicit type; low-confidence inferences stay
    below the persistence threshold and route to human verification.
    """

    name: str
    dose: str | None = None
    frequency: str | None = None
    route: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    medication_type: MedicationType = "prescription"
    confidence: float = 1.0
    evidence_snippet: str | None = None


@dataclass(frozen=True)
class CandidateHistoryFact:
    """A single candidate history/diagnosis/procedure item extracted by the interpreter.

    All values are raw (as found in the document). The Canonicalizer
    handles ICD-10 mapping, date normalization, and category classification.
    """

    description: str
    category: str = "diagnosis"
    icd_10_code: str | None = None
    recorded_date: str | None = None
    value: str | None = None
    unit: str | None = None
    cvx_code: str | None = None
    lot_number: str | None = None
    confidence: float = 1.0
    evidence_snippet: str | None = None


CandidateFact = (
    CandidateLabFact | CandidateMedicationFact | CandidateHistoryFact | CandidateVitalFact
)


@dataclass(frozen=True)
class InterpreterFactExtraction:
    """Validated fact-extraction output from the Clinical Interpreter.

    ``facts`` holds category-specific candidate records. Supported
    categories: labs (CandidateLabFact), medications (CandidateMedicationFact),
    history (CandidateHistoryFact), vitals (CandidateVitalFact).
    """

    schema_version: str
    person_id: str
    person_confidence: float
    category: str
    category_confidence: float
    facts: tuple[CandidateFact, ...]
    evidence: tuple[InterpreterEvidence, ...]
    overall_confidence: float
    document_date: str | None = None

    def below_threshold(self, threshold: float = DEFAULT_FACT_CONFIDENCE_THRESHOLD) -> bool:
        return (
            self.overall_confidence < threshold
            or self.person_confidence < threshold
            or self.category_confidence < threshold
            or any(fact.confidence < threshold for fact in self.facts)
        )


@dataclass(frozen=True)
class StaticFactInterpreter:
    """Deterministic test provider for the fact-extraction contract."""

    payloads: Mapping[str, Mapping[str, Any]]

    def extract_facts(self, source_path: Path) -> Mapping[str, Any]:
        return self.payloads.get(source_path.name, self.payloads.get(str(source_path), {}))


def validate_fact_extraction(payload: Mapping[str, Any]) -> InterpreterFactExtraction:
    """Validate untrusted Clinical Interpreter fact-extraction output."""
    schema_version = _required_str(payload, "schema_version")
    if schema_version != FACT_EXTRACTION_SCHEMA_VERSION:
        raise InterpreterContractError(
            f"unsupported fact_extraction schema_version {schema_version!r}"
        )

    person = _required_mapping(payload, "person")
    person_id = _required_str(person, "person_id")
    person_confidence = _required_confidence(person, "confidence")

    category = _required_str(payload, "category").strip().lower()
    if category not in SUPPORTED_FACT_CATEGORIES:
        raise InterpreterContractError(
            f"unsupported category {category!r}; supported: {sorted(SUPPORTED_FACT_CATEGORIES)}"
        )
    category_confidence = _required_confidence(payload, "category_confidence")

    raw_facts = _required_sequence(payload, "facts")
    if not raw_facts:
        raise InterpreterContractError("facts must include at least one item")

    facts: list[CandidateFact] = []
    for index, raw_fact in enumerate(raw_facts):
        fact_map = _coerce_mapping(raw_fact, f"facts[{index}]")
        if category == "labs":
            facts.append(_validate_candidate_lab_fact(fact_map, index))
        elif category == "medications":
            facts.append(_validate_candidate_medication_fact(fact_map, index))
        elif category == "history":
            facts.append(_validate_candidate_history_fact(fact_map, index))
        elif category == "vitals":
            facts.append(_validate_candidate_vital_fact(fact_map, index))
        else:
            raise InterpreterContractError(f"unsupported category {category!r}")

    overall_confidence = _required_confidence(payload, "overall_confidence")
    evidence = _validate_evidence(_required_sequence(payload, "evidence"))
    document_date = payload.get("document_date")
    if document_date is not None and not isinstance(document_date, str):
        raise InterpreterContractError("document_date must be a string when present")

    return InterpreterFactExtraction(
        schema_version=schema_version,
        person_id=person_id,
        person_confidence=person_confidence,
        category=category,
        category_confidence=category_confidence,
        facts=tuple(facts),
        evidence=evidence,
        overall_confidence=overall_confidence,
        document_date=document_date,
    )


def _validate_candidate_lab_fact(fact_map: Mapping[str, Any], index: int) -> CandidateLabFact:
    """Validate a single candidate lab fact from the interpreter."""
    test_name = _required_str(fact_map, "test_name")
    observed_at = fact_map.get("observed_at")
    if observed_at is not None and not isinstance(observed_at, str):
        raise InterpreterContractError(f"facts[{index}].observed_at must be a string when present")

    value = fact_map.get("value")
    if value is not None and not isinstance(value, str | int | float):
        raise InterpreterContractError(
            f"facts[{index}].value must be a string or number when present"
        )

    unit = fact_map.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise InterpreterContractError(f"facts[{index}].unit must be a string when present")
    reference_range_low = fact_map.get("reference_range_low")
    reference_range_high = fact_map.get("reference_range_high")
    confidence = _required_confidence(fact_map, "confidence")
    evidence_snippet = fact_map.get("evidence_snippet")
    if evidence_snippet is not None and not isinstance(evidence_snippet, str):
        raise InterpreterContractError(
            f"facts[{index}].evidence_snippet must be a string when present"
        )

    return CandidateLabFact(
        test_name=test_name,
        value=str(value) if value is not None else None,
        unit=unit.strip() if unit else None,
        reference_range_low=str(reference_range_low) if reference_range_low is not None else None,
        reference_range_high=(
            str(reference_range_high) if reference_range_high is not None else None
        ),
        observed_at=observed_at.strip() if observed_at else None,
        confidence=confidence,
        evidence_snippet=evidence_snippet.strip() if evidence_snippet else None,
    )


def _validate_candidate_medication_fact(
    fact_map: Mapping[str, Any], index: int
) -> CandidateMedicationFact:
    name = _required_str(fact_map, "name")

    dose = fact_map.get("dose")
    if dose is not None and not isinstance(dose, str):
        raise InterpreterContractError(f"facts[{index}].dose must be a string when present")

    frequency = fact_map.get("frequency")
    if frequency is not None and not isinstance(frequency, str):
        raise InterpreterContractError(f"facts[{index}].frequency must be a string when present")

    route = fact_map.get("route")
    if route is not None and not isinstance(route, str):
        raise InterpreterContractError(f"facts[{index}].route must be a string when present")

    status = fact_map.get("status")
    if status is not None and not isinstance(status, str):
        raise InterpreterContractError(f"facts[{index}].status must be a string when present")

    start_date = fact_map.get("start_date")
    if start_date is not None and not isinstance(start_date, str):
        raise InterpreterContractError(f"facts[{index}].start_date must be a string when present")

    end_date = fact_map.get("end_date")
    if end_date is not None and not isinstance(end_date, str):
        raise InterpreterContractError(f"facts[{index}].end_date must be a string when present")

    medication_type_raw = fact_map.get("medication_type")
    if medication_type_raw is not None:
        if not isinstance(medication_type_raw, str):
            raise InterpreterContractError(
                f"facts[{index}].medication_type must be a string when present"
            )
        if medication_type_raw not in VALID_MEDICATION_TYPES:
            raise InterpreterContractError(
                f"facts[{index}].medication_type must be one of "
                f"{sorted(VALID_MEDICATION_TYPES)}, got: {medication_type_raw!r}"
            )

    confidence = _required_confidence(fact_map, "confidence")
    evidence_snippet = fact_map.get("evidence_snippet")
    if evidence_snippet is not None and not isinstance(evidence_snippet, str):
        raise InterpreterContractError(
            f"facts[{index}].evidence_snippet must be a string when present"
        )

    return CandidateMedicationFact(
        name=name,
        dose=dose.strip() if dose else None,
        frequency=frequency.strip() if frequency else None,
        route=route.strip() if route else None,
        status=status.strip() if status else None,
        start_date=start_date.strip() if start_date else None,
        end_date=end_date.strip() if end_date else None,
        medication_type=medication_type_raw or "prescription",  # type: ignore[arg-type]
        confidence=confidence,
        evidence_snippet=evidence_snippet.strip() if evidence_snippet else None,
    )


def _validate_candidate_history_fact(
    fact_map: Mapping[str, Any], index: int
) -> CandidateHistoryFact:
    description = _required_str(fact_map, "description")

    category = fact_map.get("category")
    if category is not None and not isinstance(category, str):
        raise InterpreterContractError(f"facts[{index}].category must be a string when present")

    icd_10_code = fact_map.get("icd_10_code")
    if icd_10_code is not None and not isinstance(icd_10_code, str):
        raise InterpreterContractError(f"facts[{index}].icd_10_code must be a string when present")

    recorded_date = fact_map.get("recorded_date")
    if recorded_date is not None and not isinstance(recorded_date, str):
        raise InterpreterContractError(
            f"facts[{index}].recorded_date must be a string when present"
        )
    value = fact_map.get("value")
    if value is not None and not isinstance(value, str | int | float):
        raise InterpreterContractError(
            f"facts[{index}].value must be a string or number when present"
        )
    unit = fact_map.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise InterpreterContractError(f"facts[{index}].unit must be a string when present")
    cvx_code = fact_map.get("cvx_code")
    if cvx_code is not None and not isinstance(cvx_code, str):
        raise InterpreterContractError(f"facts[{index}].cvx_code must be a string when present")
    lot_number = fact_map.get("lot_number")
    if lot_number is not None and not isinstance(lot_number, str):
        raise InterpreterContractError(f"facts[{index}].lot_number must be a string when present")

    confidence = _required_confidence(fact_map, "confidence")
    evidence_snippet = fact_map.get("evidence_snippet")
    if evidence_snippet is not None and not isinstance(evidence_snippet, str):
        raise InterpreterContractError(
            f"facts[{index}].evidence_snippet must be a string when present"
        )

    return CandidateHistoryFact(
        description=description,
        category=category.strip() if category else "diagnosis",
        icd_10_code=icd_10_code.strip() if icd_10_code else None,
        recorded_date=recorded_date.strip() if recorded_date else None,
        value=str(value).strip() if value is not None else None,
        unit=unit.strip() if unit else None,
        cvx_code=cvx_code.strip() if cvx_code else None,
        lot_number=lot_number.strip() if lot_number else None,
        confidence=confidence,
        evidence_snippet=evidence_snippet.strip() if evidence_snippet else None,
    )


def _validate_candidate_vital_fact(fact_map: Mapping[str, Any], index: int) -> CandidateVitalFact:
    vital_name = _required_str(fact_map, "vital_name")
    if vital_name not in KNOWN_VITAL_NAMES:
        raise InterpreterContractError(
            f"facts[{index}].vital_name must be one of {sorted(KNOWN_VITAL_NAMES)!r}"
        )

    observed_at = fact_map.get("observed_at")
    if observed_at is not None and not isinstance(observed_at, str):
        raise InterpreterContractError(f"facts[{index}].observed_at must be a string when present")

    value = fact_map.get("value")
    if value is not None and not isinstance(value, str | int | float):
        raise InterpreterContractError(
            f"facts[{index}].value must be a string or number when present"
        )

    unit = fact_map.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise InterpreterContractError(f"facts[{index}].unit must be a string when present")
    reference_range_low = fact_map.get("reference_range_low")
    reference_range_high = fact_map.get("reference_range_high")
    confidence = _required_confidence(fact_map, "confidence")
    evidence_snippet = fact_map.get("evidence_snippet")
    if evidence_snippet is not None and not isinstance(evidence_snippet, str):
        raise InterpreterContractError(
            f"facts[{index}].evidence_snippet must be a string when present"
        )

    return CandidateVitalFact(
        vital_name=vital_name,
        value=str(value) if value is not None else None,
        unit=unit.strip() if unit else None,
        reference_range_low=str(reference_range_low) if reference_range_low is not None else None,
        reference_range_high=(
            str(reference_range_high) if reference_range_high is not None else None
        ),
        observed_at=observed_at.strip() if observed_at else None,
        confidence=confidence,
        evidence_snippet=evidence_snippet.strip() if evidence_snippet else None,
    )


# ── Shared validation helpers ───────────────────────────────────────────


def _required_mapping(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if field_name not in payload:
        raise InterpreterContractError(f"{field_name} is required")
    return _coerce_mapping(payload[field_name], field_name)


def _coerce_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InterpreterContractError(f"{field_name} must be an object")
    return value


def _required_sequence(payload: Mapping[str, Any], field_name: str) -> Sequence[Any]:
    if field_name not in payload:
        raise InterpreterContractError(f"{field_name} is required")
    return _coerce_sequence(payload[field_name], field_name)


def _coerce_sequence(value: Any, field_name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise InterpreterContractError(f"{field_name} must be a list")
    return value


def _required_str(payload: Mapping[str, Any], field_name: str) -> str:
    if field_name not in payload:
        raise InterpreterContractError(f"{field_name} is required")
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise InterpreterContractError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_confidence(payload: Mapping[str, Any], field_name: str) -> float:
    if field_name not in payload:
        raise InterpreterContractError(f"{field_name} is required")
    value = payload[field_name]
    if not isinstance(value, int | float):
        raise InterpreterContractError(f"{field_name} must be a number")
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise InterpreterContractError(f"{field_name} must be between 0 and 1")
    return confidence
