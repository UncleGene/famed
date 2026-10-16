from __future__ import annotations

from pathlib import Path

import pytest

from famed.synthesis_contract import (
    CITATION_ONLY_DEFAULT_TIER,
    SynthesisSection,
    SynthesizedInsight,
    find_forbidden_language,
    validate_insight,
)


def _vault_with_citation(
    tmp_path: Path, relative: str = "Records/dummy_alice/Reports/health-advisory.md"
) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Health Advisory\n", encoding="utf-8")
    return tmp_path


def _insight(**overrides: object) -> SynthesizedInsight:
    defaults: dict[str, object] = {
        "statement": (
            "Her recent LDL elevation combines with a heterozygous genetic risk flag "
            "to suggest discussing lipid-focused follow-up at the next visit."
        ),
        "linked_evidence": ("Records/dummy_alice/Reports/health-advisory.md",),
        "domains_linked": ("Heart & Circulation", "Systemic Interactions"),
        "evidence_tier": "E2",
        "confidence": 0.6,
    }
    defaults.update(overrides)
    return SynthesizedInsight(**defaults)  # type: ignore[arg-type]


def test_valid_insight_passes(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    reasons = validate_insight(_insight(), vault_root=vault_root, weakest_cited_tier="E2")
    assert reasons == ()


def test_low_confidence_but_valid_insight_passes(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(evidence_tier="E4", confidence=0.15)
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E4")
    assert reasons == ()


def test_oncology_genitourinary_domain_passes_with_second_domain(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(
        statement=(
            "The longitudinal lab pattern and recorded family history may be worth "
            "reviewing together at the next visit."
        ),
        domains_linked=("Oncology & Genitourinary", "Systemic Interactions"),
        evidence_tier="E3",
    )

    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier=None)

    assert reasons == ()


def test_oncology_genitourinary_domain_alone_is_not_cross_domain(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(domains_linked=("Oncology & Genitourinary",))

    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E3")

    assert any("at least 2 distinct domains" in reason for reason in reasons)


def test_rejects_empty_linked_evidence(tmp_path: Path) -> None:
    insight = _insight(linked_evidence=())
    reasons = validate_insight(insight, vault_root=tmp_path, weakest_cited_tier="E2")
    assert any("linked_evidence is empty" in reason for reason in reasons)


def test_rejects_unresolved_citation(tmp_path: Path) -> None:
    insight = _insight(linked_evidence=("Records/dummy_alice/Reports/does-not-exist.md",))
    reasons = validate_insight(insight, vault_root=tmp_path, weakest_cited_tier="E2")
    assert any("unresolved citation" in reason for reason in reasons)


def test_rejects_single_domain(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(domains_linked=("Heart & Circulation",))
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("at least 2 distinct domains" in reason for reason in reasons)


def test_rejects_unknown_domain(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(domains_linked=("Heart & Circulation", "Made-Up Domain"))
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("unknown domain" in reason for reason in reasons)


def test_rejects_evidence_tier_outside_valid_set(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(evidence_tier="E9")
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("is not one of" in reason for reason in reasons)


def test_rejects_evidence_tier_overstating_weakest_cited_tier(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(evidence_tier="E1")
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("overstates the weakest cited tier" in reason for reason in reasons)


def test_citation_only_insight_capped_at_default_tier(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    overstating = _insight(evidence_tier="E1")
    reasons = validate_insight(overstating, vault_root=vault_root, weakest_cited_tier=None)
    assert any("overstates the weakest cited tier" in reason for reason in reasons)

    at_cap = _insight(evidence_tier=CITATION_ONLY_DEFAULT_TIER)
    reasons = validate_insight(at_cap, vault_root=vault_root, weakest_cited_tier=None)
    assert reasons == ()


def test_rejects_confidence_out_of_bounds(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(confidence=1.5)
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("confidence must be between 0 and 1" in reason for reason in reasons)


def test_rejects_dosing_instruction_language(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(statement="Consider increasing her metformin dose at the next visit.")
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("dosing instruction" in reason for reason in reasons)


def test_rejects_diagnosis_assertion_language(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(statement="This diagnoses her with early-stage kidney disease.")
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("diagnosis assertion" in reason for reason in reasons)


def test_diagnosis_support_phrase_not_flagged() -> None:
    text = "Excluded per the relative's diagnosis-support review."
    assert find_forbidden_language(text) == ()


def test_rejects_treatment_directive_language(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(statement="You should start treatment with insulin right away.")
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("treatment directive" in reason for reason in reasons)


def test_rejects_empty_statement(tmp_path: Path) -> None:
    vault_root = _vault_with_citation(tmp_path)
    insight = _insight(statement="   ")
    reasons = validate_insight(insight, vault_root=vault_root, weakest_cited_tier="E2")
    assert any("statement is empty" in reason for reason in reasons)


def test_multiple_rejection_reasons_accumulate(tmp_path: Path) -> None:
    insight = _insight(
        linked_evidence=(),
        domains_linked=("Heart & Circulation",),
        confidence=2.0,
    )
    reasons = validate_insight(insight, vault_root=tmp_path, weakest_cited_tier="E2")
    assert len(reasons) >= 3


@pytest.mark.parametrize(
    ("statement", "expected_category"),
    [
        ("Stop taking the medication immediately.", "dosing instruction"),
        ("Take 50mg twice daily.", "dosing instruction"),
        ("Please decrease his Synthroid to 50 micrograms daily.", "dosing instruction"),
        ("She is diagnosed with hypertension.", "diagnosis assertion"),
        ("We recommend switching her statin.", "treatment directive"),
        ("She should stop the current medication.", "treatment directive"),
        ("This suggests starting insulin right away.", "treatment directive"),
    ],
)
def test_find_forbidden_language_categories(statement: str, expected_category: str) -> None:
    assert expected_category in find_forbidden_language(statement)


def test_find_forbidden_language_clean_statement() -> None:
    text = "Her lab trend and family history together may be worth raising at the next visit."
    assert find_forbidden_language(text) == ()


def test_synthesis_section_holds_accepted_insights() -> None:
    insight = _insight()
    section = SynthesisSection(
        person_id="dummy_alice",
        insights=(insight,),
        generated_at="2023-06-24T00:00:00Z",
        status="current",
    )
    assert section.insights == (insight,)
    assert section.status == "current"
