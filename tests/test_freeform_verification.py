from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from famed.freeform_verification import (
    FreeformFixtureError,
    audit_current_inbox,
    classify_source_family,
    resolve_source_identifier,
    validate_fixture_payload,
)


def _reviewer_decision() -> dict[str, str]:
    return {
        "decision": "accept",
        "reviewed_by": "local_manual_fixture_review",
        "reviewed_at": "2023-04-30",
        "rationale": "Accepted as a fixture expectation only; no canonical persistence.",
    }


def _base_fixture(source_request: str) -> dict[str, Any]:
    return {
        "fixtures": [
            {
                "source_request": source_request,
                "expected": {
                    "person_id": "test-person",
                    "categories": ["labs"],
                    "document_type": "CBC W/AUTO DIFFERENTIAL",
                },
                "candidates": [
                    {
                        "item": "Auto WBC",
                        "kind": "lab",
                        "type": "candidate_fact",
                        "value": "6",
                        "unit": "x10E3/uL",
                        "reference": "3.4 - 10.8",
                        "confidence": "high",
                        "evidence": "Auto WBC Normal range: 3.4 - 10.8 x10E3/uL value 6",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }


def test_resolve_source_identifier_matches_punctuation_variant(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "inbox" / "sample CBC W AUTO DIFFERENTIAL.pdf"
    source.parent.mkdir(parents=True)
    source.write_text("fixture", encoding="utf-8")

    match = resolve_source_identifier(data_root, "sample CBC W:AUTO DIFFERENTIAL.pdf")

    assert match.path == source.resolve()
    assert match.matched_by == "normalized_filename"


def test_resolve_source_identifier_matches_normalized_substring(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "inbox" / "sample CBC W:AUTO DIFFERENTIAL.pdf"
    source.parent.mkdir(parents=True)
    source.write_text("fixture", encoding="utf-8")

    match = resolve_source_identifier(data_root, "CBC W AUTO DIFFERENTIAL")

    assert match.path == source.resolve()
    assert match.matched_by == "normalized_substring"


def test_resolve_source_identifier_ignores_hidden_metadata_files(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    hidden_file = data_root / "inbox" / ".DS_Store"
    hidden_dir_source = data_root / "quarantine" / ".metadata" / "panel.pdf"
    for path in [hidden_file, hidden_dir_source]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")

    with pytest.raises(FreeformFixtureError, match="source not found"):
        resolve_source_identifier(data_root, ".DS_Store")
    with pytest.raises(FreeformFixtureError, match="source not found"):
        resolve_source_identifier(data_root, "panel.pdf")


def test_validate_fixture_requires_lab_reference(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "panel.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("panel.pdf")
    candidate = payload["fixtures"][0]["candidates"][0]
    del candidate["reference"]

    with pytest.raises(FreeformFixtureError, match="reference"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_requires_separate_expected_vitals(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visitNote.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = {
        "fixtures": [
            {
                "source_request": "visitNote.pdf",
                "expected": {
                    "person_id": "test-person",
                    "categories": ["history"],
                    "document_type": "visit note",
                },
                "expected_vitals": ["Temperature", "Blood pressure systolic"],
                "candidates": [
                    {
                        "item": "Temperature",
                        "kind": "vital",
                        "type": "candidate_fact",
                        "value": "96.4",
                        "unit": "F",
                        "confidence": "high",
                        "evidence": "Vital Signs Temperature 96.4 degrees Fahrenheit",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }

    with pytest.raises(FreeformFixtureError, match="Blood pressure systolic"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_accepts_complete_lab_payload(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "panel.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")

    matches = validate_fixture_payload(_base_fixture("panel.pdf"), data_root=data_root)

    assert matches[0].path == source.resolve()


def test_validate_fixture_requires_manual_verification_for_unknown_person(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    source = data_root / "unknown.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("unknown.pdf")
    payload["fixtures"][0]["outcome"] = "needs_person_resolution"

    with pytest.raises(FreeformFixtureError, match="manual_verification_request"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_accepts_manual_verification_request(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    source = data_root / "unknown.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("unknown.pdf")
    payload["fixtures"][0]["outcome"] = "needs_person_resolution"
    payload["fixtures"][0]["manual_verification_request"] = {
        "reason": "Person match is below high confidence.",
        "prompt": "Verify whether this source belongs to the expected test person.",
        "options": [
            "dismiss_wrong_person",
            "assign_existing_person",
            "update_person_matching_data",
            "refine_logic",
            "quarantine",
        ],
    }

    matches = validate_fixture_payload(payload, data_root=data_root)

    assert matches[0].path == source.resolve()


def test_validate_fixture_requires_medication_value_field(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visit.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = {
        "fixtures": [
            {
                "source_request": "visit.pdf",
                "expected": {
                    "person_id": "test-person",
                    "categories": ["medications"],
                    "document_type": "visit note",
                },
                "candidates": [
                    {
                        "item": "Sample Medication",
                        "kind": "medication",
                        "type": "candidate_fact",
                        "confidence": "high",
                        "evidence": "sample note started a medication 20 mg daily",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }

    with pytest.raises(FreeformFixtureError, match="value"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_accepts_medication_with_value(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visit.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = {
        "fixtures": [
            {
                "source_request": "visit.pdf",
                "expected": {
                    "person_id": "test-person",
                    "categories": ["medications"],
                    "document_type": "visit note",
                },
                "candidates": [
                    {
                        "item": "Sample Medication",
                        "kind": "medication",
                        "type": "candidate_fact",
                        "value": "20 mg daily",
                        "confidence": "high",
                        "evidence": "sample note started a medication 20 mg daily",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }

    matches = validate_fixture_payload(payload, data_root=data_root)
    assert matches[0].path == source.resolve()


_PORTAL_EXPORTS = {
    "portal-medication-list.txt": (
        "Patient Medication List",
        "Patient Medication List\n\nAtorvastatin 20 mg nightly\nLisinopril 10 mg daily\n",
        [("Atorvastatin", "20 mg nightly"), ("Lisinopril", "10 mg daily")],
    ),
    "portal-prescription-summary.txt": (
        "My Medications and Prescriptions",
        "My Medications and Prescriptions\nDownloaded from Example Patient Portal\n\n"
        "Levothyroxine 50 mcg daily\nMagnesium 250 mg daily\n",
        [("Levothyroxine", "50 mcg daily"), ("Magnesium", "250 mg daily")],
    ),
}


@pytest.mark.parametrize("source_request", sorted(_PORTAL_EXPORTS))
def test_validate_portal_medication_fixture_from_corpus(
    tmp_path: Path,
    source_request: str,
) -> None:
    data_root = tmp_path / "manual-testing"
    data_root.mkdir()
    document_type, text, medications = _PORTAL_EXPORTS[source_request]
    (data_root / source_request).write_text(text, encoding="utf-8")
    portal_fixture = {
        "source_request": source_request,
        "reviewer_decision": {
            "decision": "accept",
            "reviewed_by": "local_manual_fixture_review",
            "reviewed_at": "2024-01-15",
            "rationale": "Representative portal medication export fixture.",
        },
        "expected": {
            "person_id": "dummy_alice",
            "categories": ["medications"],
            "document_type": document_type,
            "document_date": "2024-01-10",
        },
        "candidates": [
            {
                "item": name,
                "kind": "medication",
                "type": "candidate_fact",
                "value": value,
                "confidence": "high",
                "evidence": f"{name} {value}",
            }
            for name, value in medications
        ],
    }

    matches = validate_fixture_payload({"fixtures": [portal_fixture]}, data_root=data_root)

    assert matches[0].path == (data_root / source_request).resolve()


def test_validate_fixture_accepts_problem_candidate(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visit.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = {
        "fixtures": [
            {
                "source_request": "visit.pdf",
                "expected": {
                    "person_id": "test-person",
                    "categories": ["history"],
                    "document_type": "visit note",
                },
                "candidates": [
                    {
                        "item": "Sample Condition",
                        "kind": "problem",
                        "type": "candidate_fact",
                        "value": "Z00.00",
                        "confidence": "high",
                        "evidence": "Sample Condition Z00.00 diagnosis from test note",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }

    matches = validate_fixture_payload(payload, data_root=data_root)
    assert matches[0].path == source.resolve()


def test_validate_fixture_requires_problem_value_field(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visit.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = {
        "fixtures": [
            {
                "source_request": "visit.pdf",
                "expected": {
                    "person_id": "test-person",
                    "categories": ["history"],
                    "document_type": "visit note",
                },
                "candidates": [
                    {
                        "item": "Sample Condition",
                        "kind": "problem",
                        "type": "candidate_fact",
                        "confidence": "high",
                        "evidence": "Sample Condition appears in test note with diagnosis context",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }

    with pytest.raises(FreeformFixtureError, match="value"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_rejects_unknown_candidate_kind(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visit.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("visit.pdf")
    payload["fixtures"][0]["candidates"][0]["kind"] = "unknown_kind"

    with pytest.raises(FreeformFixtureError, match="kind"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_rejects_unknown_outcome(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "panel.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("panel.pdf")
    payload["fixtures"][0]["outcome"] = "unknown_outcome"

    with pytest.raises(FreeformFixtureError, match="outcome"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_rejects_short_evidence(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "visit.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = {
        "fixtures": [
            {
                "source_request": "visit.pdf",
                "expected": {
                    "person_id": "test-person",
                    "categories": ["history"],
                    "document_type": "visit note",
                },
                "candidates": [
                    {
                        "item": "Sample Condition",
                        "kind": "problem",
                        "type": "candidate_fact",
                        "value": "Z00.00",
                        "confidence": "high",
                        "evidence": "short",
                    }
                ],
                "reviewer_decision": _reviewer_decision(),
            }
        ]
    }

    with pytest.raises(FreeformFixtureError, match="evidence is too short"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_requires_reviewer_decision(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "panel.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("panel.pdf")
    del payload["fixtures"][0]["reviewer_decision"]

    with pytest.raises(FreeformFixtureError, match="reviewer_decision"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_rejects_unknown_reviewer_decision(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "panel.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("panel.pdf")
    payload["fixtures"][0]["reviewer_decision"]["decision"] = "maybe"

    with pytest.raises(FreeformFixtureError, match="decision"):
        validate_fixture_payload(payload, data_root=data_root)


def test_validate_fixture_accepts_cancelled_reviewer_decision(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "missing-source.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("missing-source.pdf")
    payload["fixtures"][0]["reviewer_decision"]["decision"] = "cancelled"
    payload["fixtures"][0]["reviewer_decision"]["cancelled_at"] = "2023-05-02"
    payload["fixtures"][0]["reviewer_decision"]["cancellation_reason"] = (
        "Source PDF not on disk; fixture retained as cancelled history."
    )

    matches = validate_fixture_payload(payload, data_root=data_root)

    assert matches[0].requested == "missing-source.pdf"


def test_validate_fixture_rejects_cancelled_without_reason(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "missing-source.pdf"
    data_root.mkdir()
    source.write_text("fixture", encoding="utf-8")
    payload = _base_fixture("missing-source.pdf")
    payload["fixtures"][0]["reviewer_decision"]["decision"] = "cancelled"

    with pytest.raises(FreeformFixtureError, match="cancelled_at"):
        validate_fixture_payload(payload, data_root=data_root)


def test_classify_source_family_covers_current_plan_families() -> None:
    assert classify_source_family(Path("sample COMPREHENSIVE METABOLIC PANEL.pdf")) == "lab_cmp"
    cbc_path = Path("sample COMPLETE BLOOD COUNT WITH AUTO DIFFERENTIAL.pdf")
    assert classify_source_family(cbc_path) == "lab_cbc"
    assert classify_source_family(Path("sample ZZHIST AUTOMATED DIFF.pdf")) == "lab_cbc"
    assert classify_source_family(Path("sample HEMOGLOBIN A1C.pdf")) == "lab_a1c"
    assert classify_source_family(Path("sample PSA, TOTAL AND FREE.pdf")) == "lab_psa"
    assert classify_source_family(Path("sample VisitSummary.xml")) == "visit_summary_xml"
    assert classify_source_family(Path("sample Colonoscopy.pdf")) == "colonoscopy_pdf"
    assert classify_source_family(Path("sample Colonoscopy.TIF")) == "colonoscopy_tif"
    assert classify_source_family(Path("sample XR Knee Right 3 Vw.pdf")) == "imaging_xray"
    assert classify_source_family(Path("sample MRI Knee Right wo Contrast.pdf")) == "imaging_mri"
    assert classify_source_family(Path("sample US EXTREMITY NONVASCULAR COMPLETE LEFT.PDF")) == (
        "imaging_ultrasound"
    )
    assert classify_source_family(Path("sample Coronary Artery Calcium Scoring.TIF")) == (
        "imaging_cac_tif"
    )


def test_audit_current_inbox_reports_outcomes_and_fixture_gaps(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    inbox = data_root / "inbox"
    quarantine = data_root / "quarantine" / "test-person" / "Labs"
    manual = data_root / "manual-testing"
    inbox.mkdir(parents=True)
    quarantine.mkdir(parents=True)
    manual.mkdir(parents=True)
    cmp = inbox / "sample COMPREHENSIVE METABOLIC PANEL.pdf"
    visit = inbox / "sample visitNote.pdf"
    scan = inbox / "sample Colonoscopy.TIF"
    ignored = inbox / ".DS_Store"
    ignored_nested = inbox / "portal" / ".sync-state"
    ignored_quarantine = quarantine / ".DS_Store"
    ignored_quarantine_dir = data_root / "quarantine" / ".metadata" / "legacy.pdf"
    legacy = quarantine / "undated-sample-psa.pdf"
    for path in [
        cmp,
        visit,
        scan,
        ignored,
        ignored_nested,
        ignored_quarantine,
        ignored_quarantine_dir,
        legacy,
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    fixture_path = manual / "freeform-inbox-fixtures.yaml"
    fixture_path.write_text(
        """
fixtures:
  - source_request: "sample COMPREHENSIVE METABOLIC PANEL.pdf"
    reviewer_decision:
      decision: accept
      reviewed_by: local_manual_fixture_review
      reviewed_at: "2023-04-30"
      rationale: "Accepted as a fixture expectation only; no canonical persistence."
    expected:
      person_id: test-person
      categories: [labs]
      document_type: "Comprehensive Metabolic Panel"
    candidates:
      - item: Sodium
        kind: lab
        type: candidate_fact
        value: "139"
        unit: mmol/L
        reference: "134 - 144"
        confidence: high
        evidence: "Sodium Normal range 134 to 144 mmol/L value 139"
  - source_request: "sample visitNote.pdf"
    reviewer_decision:
      decision: accept
      reviewed_by: local_manual_fixture_review
      reviewed_at: "2023-04-30"
      rationale: "Accepted as a fixture expectation only; no canonical persistence."
    outcome: needs_user_verification
    manual_verification_request:
      reason: "Visit notes require human verification before canonicalization."
      prompt: "Review the extracted visit note candidates."
      options: [accept, edit, reject, quarantine]
    expected:
      person_id: ""
      categories: [history]
      document_type: "visit note"
    candidates: []
""",
        encoding="utf-8",
    )

    report = audit_current_inbox(data_root, fixture_path=fixture_path)

    by_name = {item.path.name: item for item in report.items}
    assert set(by_name) == {
        "sample COMPREHENSIVE METABOLIC PANEL.pdf",
        "sample visitNote.pdf",
        "sample Colonoscopy.TIF",
        "undated-sample-psa.pdf",
    }
    assert by_name[cmp.name].outcome == "deterministic_handler_candidate"
    assert by_name[cmp.name].fixture_covered is True
    assert by_name[visit.name].outcome == "needs_user_verification"
    assert by_name[visit.name].fixture_covered is True
    assert by_name[visit.name].note == "recorded fixture outcome"
    assert by_name[scan.name].outcome == "needs_user_verification"
    assert by_name[legacy.name].outcome == "quarantine_recommended"
    assert by_name[legacy.name].path in report.stale_quarantine_paths
    assert "colonoscopy_tif" in report.missing_required_families
    assert "unsupported_content" in report.missing_required_families
    assert "lab_cmp" not in report.missing_required_families


def test_audit_current_inbox_includes_family_inventory_with_unresolved_status(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    inbox = data_root / "inbox"
    quarantine = data_root / "quarantine" / "test-person" / "Labs"
    inbox.mkdir(parents=True)
    quarantine.mkdir(parents=True)

    cmp = inbox / "sample COMPREHENSIVE METABOLIC PANEL.pdf"
    category_gap = inbox / "sample troponin panel.pdf"
    unsupported = inbox / "sample unknown-source.rtf"
    legacy = quarantine / "undated-sample-psa.pdf"
    for path in [cmp, category_gap, unsupported, legacy]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")

    report = audit_current_inbox(data_root)

    summary_by_family = {summary.family: summary for summary in report.family_summaries}
    assert summary_by_family["lab_cmp"].status == "supported-now"
    assert summary_by_family["lab_cmp"].count == 1
    assert summary_by_family["needs_category_resolution"].status == "blocked-by-parser"
    assert summary_by_family["unsupported_content"].status == "intentionally-deferred"
    assert summary_by_family["lab_psa"].status == "blocked-by-person-resolution"

    unresolved = {summary.family for summary in report.unresolved_families}
    assert unresolved == {"lab_psa", "needs_category_resolution", "unsupported_content"}
