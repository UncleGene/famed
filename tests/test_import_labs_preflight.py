from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from famed.clinical_interpreter import (
    FACT_EXTRACTION_SCHEMA_VERSION,
    CandidateLabFact,
    InterpreterEvidence,
    InterpreterFactExtraction,
)
from famed.import_labs import (
    LabSyncError,
    canonicalize_candidate_lab_facts,
    clean_pdf_range_bar_artifacts,
    fill_loinc_codes,
    parse_labs_from_mychart_text,
    preflight_pdf_dependencies_if_needed,
    review_pdf_extraction,
)


def _make_extraction(
    *,
    person_id: str = "dummy_alice",
    facts: list[CandidateLabFact] | None = None,
    document_date: str | None = None,
) -> InterpreterFactExtraction:
    if facts is None:
        facts = [
            CandidateLabFact(
                test_name="Glucose",
                value="102",
                unit="mg/dL",
                reference_range_low="70",
                reference_range_high="99",
                observed_at="2023-04-01",
                confidence=0.92,
            )
        ]
    return InterpreterFactExtraction(
        schema_version=FACT_EXTRACTION_SCHEMA_VERSION,
        person_id=person_id,
        person_confidence=0.92,
        category="labs",
        category_confidence=0.92,
        facts=tuple(facts),
        evidence=(
            InterpreterEvidence(field="patient_name", snippet="Dummy Alice", confidence=0.9),
        ),
        overall_confidence=0.92,
        document_date=document_date,
    )


def test_canonicalize_candidate_lab_facts_converts_valid_extraction() -> None:
    extraction = _make_extraction()
    rows = canonicalize_candidate_lab_facts(extraction, "test.pdf")
    assert len(rows) == 1
    assert rows[0]["person_id"] == "dummy_alice"
    assert rows[0]["test_name"] == "Glucose"
    assert rows[0]["value"] == 102.0
    assert rows[0]["value_text"] == "102"
    assert rows[0]["unit"] == "mg/dL"
    assert rows[0]["reference_range_low"] == 70.0
    assert rows[0]["reference_range_high"] == 99.0
    assert rows[0]["observed_at"] == "2023-04-01"
    assert rows[0]["source_type"] == "ai_extracted"
    assert rows[0]["loinc_code"] == ""


def test_canonicalize_candidate_lab_facts_uses_document_date_fallback() -> None:
    facts = [CandidateLabFact(test_name="Glucose", value="102", unit="mg/dL", confidence=0.9)]
    extraction = _make_extraction(facts=facts, document_date="2023-04-01")
    rows = canonicalize_candidate_lab_facts(extraction, "test.pdf")
    assert rows[0]["observed_at"] == "2023-04-01"


def test_canonicalize_candidate_lab_facts_raises_for_missing_date() -> None:
    facts = [CandidateLabFact(test_name="Glucose", confidence=0.9)]
    extraction = _make_extraction(facts=facts, document_date=None)
    with pytest.raises(LabSyncError, match="no observed_at or document_date"):
        canonicalize_candidate_lab_facts(extraction, "test.pdf")


def test_canonicalize_candidate_lab_facts_handles_qualitative_value() -> None:
    facts = [
        CandidateLabFact(
            test_name="urine_culture",
            value="negative",
            unit=None,
            observed_at="2023-04-01",
            confidence=0.9,
        )
    ]
    extraction = _make_extraction(facts=facts)
    rows = canonicalize_candidate_lab_facts(extraction, "test.pdf")
    assert rows[0]["value"] is None
    assert rows[0]["value_text"] == "negative"


def test_preflight_pdf_dependencies_skips_when_no_pdf_files() -> None:
    assert preflight_pdf_dependencies_if_needed([]) == []


def test_preflight_pdf_dependencies_ok_when_pypdf_installed() -> None:
    errors = preflight_pdf_dependencies_if_needed([Path("sample.pdf")])
    assert errors == []


def test_mychart_collapses_duplicated_numeric_artifact_value() -> None:
    text = """
MyChart
Collected: 02/12/2023
Cholesterol
Normal range: 100-199 mg/dL
200200
"""

    labs = parse_labs_from_mychart_text(text, person_id="dummy_alice", source_label="sample")
    assert len(labs) == 1
    assert labs[0]["test_name"] == "Cholesterol"
    assert labs[0]["value"] == 200.0
    assert labs[0]["value_text"] == "200"


def test_mychart_does_not_reuse_previous_plain_numeric_value() -> None:
    text = """
MyChart
Collected: 02/12/2023
Hemoglobin A1c
Normal value: <5.7 %
5.4
Estimated Average Glucose (eAG)
5.4
Normal value: <140 mg/dL
Comment
"""

    labs = parse_labs_from_mychart_text(text, person_id="dummy_alice", source_label="sample")
    assert [row["test_name"] for row in labs] == ["Hemoglobin A1c"]
    assert labs[0]["value"] == 5.4


def test_review_pdf_extraction_auto_approves_in_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_import_labs_preflight.py::dummy")

    approved = review_pdf_extraction(
        path=Path("sample.pdf"),
        person_id="dummy_alice",
        rows=[
            {
                "test_name": "Hemoglobin A1c",
                "value": 5.4,
                "unit": "%",
                "reference_range_low": 4.0,
                "reference_range_high": 5.6,
                "observed_at": "2023-02-12",
            }
        ],
        skip_prompt=False,
        review_only=False,
    )

    assert approved is True


def test_mychart_ignores_repeated_reference_marker_lines_for_values() -> None:
    text = """
MyChart
Collected on Feb 12, 2023 9:56 AM
Cholesterol
Normal range: 200 mg/dL or below
200200
255 High
Triglycerides
Normal range: 150 mg/dL or below
150150
89
LDL Calculated
Normal range: below 100 mg/dL
100100
175 High
HDL
Normal range: 40.0 mg/dL or above
4040
62
"""

    labs = parse_labs_from_mychart_text(text, person_id="dummy_alice", source_label="sample")

    assert [row["test_name"] for row in labs] == [
        "Cholesterol",
        "Triglycerides",
        "LDL Calculated",
        "HDL Cholesterol",
    ]
    assert [row["value"] for row in labs] == [255.0, 89.0, 175.0, 62.0]
    assert labs[0]["unit"] == "mg/dL"
    assert labs[1]["unit"] == "mg/dL"
    assert labs[2]["unit"] == "mg/dL"
    assert labs[3]["unit"] == "mg/dL"
    assert labs[0]["value_text"] == "255"
    assert labs[2]["value_text"] == "175"


_LOINC_JSONL = (
    '{"loinc_num": "2345-7", "component": "Glucose", '
    '"long_common_name": "Glucose [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "4548-4", "component": "Hemoglobin A1c", '
    '"long_common_name": "Hemoglobin A1c/Hemoglobin.total in Blood", '
    '"class": "HEM/BC", "system": "Bld", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
)


def _write_loinc_index(cache_dir: Path) -> None:
    loinc_dir = cache_dir / "loinc"
    loinc_dir.mkdir(parents=True, exist_ok=True)
    (loinc_dir / "loinc_concepts.jsonl").write_text(_LOINC_JSONL, encoding="utf-8")


class TestFillLoincCodes:
    def test_fills_blank_code_from_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from famed.reference_cache import load_loinc_index

            _write_loinc_index(Path(tmp))
            index = load_loinc_index(Path(tmp))
            rows: list[dict] = [{"test_name": "Glucose", "loinc_code": ""}]
            fill_loinc_codes(rows, index)
            assert rows[0]["loinc_code"] == "2345-7"

    def test_does_not_overwrite_existing_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from famed.reference_cache import load_loinc_index

            _write_loinc_index(Path(tmp))
            index = load_loinc_index(Path(tmp))
            rows: list[dict] = [{"test_name": "Glucose", "loinc_code": "9999-9"}]
            fill_loinc_codes(rows, index)
            assert rows[0]["loinc_code"] == "9999-9"

    def test_no_op_for_empty_index(self) -> None:
        rows: list[dict] = [{"test_name": "Glucose", "loinc_code": ""}]
        fill_loinc_codes(rows, {})
        assert rows[0]["loinc_code"] == ""

    def test_unknown_test_name_leaves_code_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from famed.reference_cache import load_loinc_index

            _write_loinc_index(Path(tmp))
            index = load_loinc_index(Path(tmp))
            rows: list[dict] = [{"test_name": "Unobtainium", "loinc_code": ""}]
            fill_loinc_codes(rows, index)
            assert rows[0]["loinc_code"] == ""


class TestCanonicalizeCandidateLabFactsWithLoincIndex:
    def _make_extraction(self, test_name: str = "Glucose") -> InterpreterFactExtraction:
        facts = [
            CandidateLabFact(
                test_name=test_name,
                value="102",
                unit="mg/dL",
                observed_at="2023-04-01",
                confidence=0.92,
            )
        ]
        return InterpreterFactExtraction(
            schema_version=FACT_EXTRACTION_SCHEMA_VERSION,
            person_id="dummy_alice",
            person_confidence=0.92,
            category="labs",
            category_confidence=0.92,
            facts=tuple(facts),
            evidence=(
                InterpreterEvidence(field="patient_name", snippet="Dummy Alice", confidence=0.9),
            ),
            overall_confidence=0.92,
        )

    def test_loinc_code_populated_when_index_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from famed.reference_cache import load_loinc_index

            _write_loinc_index(Path(tmp))
            index = load_loinc_index(Path(tmp))
            extraction = self._make_extraction("Glucose")
            rows = canonicalize_candidate_lab_facts(extraction, "test.pdf", loinc_index=index)
            assert rows[0]["loinc_code"] == "2345-7"

    def test_loinc_code_empty_when_no_index(self) -> None:
        extraction = self._make_extraction("Glucose")
        rows = canonicalize_candidate_lab_facts(extraction, "test.pdf")
        assert rows[0]["loinc_code"] == ""

    def test_loinc_code_empty_when_test_name_not_in_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from famed.reference_cache import load_loinc_index

            _write_loinc_index(Path(tmp))
            index = load_loinc_index(Path(tmp))
            extraction = self._make_extraction("Unobtainium")
            rows = canonicalize_candidate_lab_facts(extraction, "test.pdf", loinc_index=index)
            assert rows[0]["loinc_code"] == ""


class TestCleanPdfRangeBarArtifacts:
    """MyChart/LabCorp graphical range bars extract each boundary label twice."""

    def test_deduplicates_decimal_range(self) -> None:
        # Range bar: low 3.4, high 10.8 each duplicated; result value 6 on next line
        assert clean_pdf_range_bar_artifacts("3.43.4 10.810.8\n6") == "3.4 10.8\n6"

    def test_deduplicates_integer_range(self) -> None:
        # Range bar: low 150, high 450 each duplicated; result value 305 on next line
        assert clean_pdf_range_bar_artifacts("150150 450450\n305") == "150 450\n305"

    def test_preserves_prose_with_dates(self) -> None:
        # Lines with letters, slashes, or colons must never be modified
        text = "Collected on Jan 01, 1997 8:00 AM\nDOB: 01/01/1990\nSpecimen 01/01/1997 00:00:"
        assert clean_pdf_range_bar_artifacts(text) == text

    def test_preserves_bare_phone_number(self) -> None:
        # A 10-digit phone number on its own line: internal repeated pairs are
        # always preceded by a digit, so the lookaround prevents any match.
        assert clean_pdf_range_bar_artifacts("1234567890") == "1234567890"

    def test_preserves_standalone_result_value(self) -> None:
        # A single measured value on its own line must survive unchanged
        assert clean_pdf_range_bar_artifacts("6") == "6"
        assert clean_pdf_range_bar_artifacts("4.78") == "4.78"
