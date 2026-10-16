"""Synthesized-insight contract and deterministic validation gate (REQ-014, ADR-026).

A live AI agent executing an `fm-generate-*` advice skill is the
producer of `SynthesizedInsight` objects; this module is the consumer-side
deterministic validation gate that must accept or reject them before they are
added to a report's `SynthesisSection`. No model is invoked here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from famed.findings_ledger import VALID_EVIDENCE_TIERS, EvidenceTier

HealthDomain = Literal[
    "Heart & Circulation",
    "Energy & Vitamins",
    "Digestion & Diet",
    "Sleep & Brain",
    "Medication Safety",
    "Oncology & Genitourinary",
    "Systemic Interactions",
]
VALID_HEALTH_DOMAINS: tuple[HealthDomain, ...] = (
    "Heart & Circulation",
    "Energy & Vitamins",
    "Digestion & Diet",
    "Sleep & Brain",
    "Medication Safety",
    "Oncology & Genitourinary",
    "Systemic Interactions",
)

SynthesisStatus = Literal["current", "stale"]

# An insight citing no tier-bearing source (weakest_cited_tier is None) is
# capped here rather than left to claim E1/E2 unsupported.
CITATION_ONLY_DEFAULT_TIER: EvidenceTier = "E3"
_TIER_ORDER: tuple[EvidenceTier, ...] = ("E1", "E2", "E3", "E4", "E5")


@dataclass(frozen=True)
class SynthesizedInsight:
    """One agent-authored, cross-domain advice statement (ADR-026)."""

    statement: str
    linked_evidence: tuple[str, ...]
    domains_linked: tuple[HealthDomain, ...]
    evidence_tier: EvidenceTier
    confidence: float


@dataclass(frozen=True)
class SynthesisSection:
    """The agent-authored synthesis section of an advice report (ADR-026)."""

    person_id: str
    insights: tuple[SynthesizedInsight, ...]
    generated_at: str
    status: SynthesisStatus


# Forbidden-language gate (ADR-020 boundary). Fixed report templates already
# keep this boundary via static disclaimers (health_advisory.py); this
# is the equivalent check for agent-authored free text, where no fixed
# template exists to keep the boundary for free. Deliberately conservative —
# it may reject legitimate restatements of an already-documented diagnosis
# (e.g. "her documented diagnosis of X") along with genuine new-diagnosis
# assertions, since distinguishing the two reliably is not a v1 goal.
_DOSING_VERB_STEMS = (
    r"start\w*",
    r"stopp?\w*",
    r"increas(?:e|es|ed|ing)",
    r"decreas(?:e|es|ed|ing)",
    r"reduc(?:e|es|ed|ing)",
    r"adjust\w*",
    r"switch\w*",
    r"discontinu(?:e|es|ed|ing)",
)
_DOSING_VERB_ALTERNATION = "|".join(_DOSING_VERB_STEMS)
_DOSING_INSTRUCTION_PATTERN = re.compile(
    rf"\b(?:{_DOSING_VERB_ALTERNATION})\b"
    r"(?:\s+\w+){0,4}\s+(?:dose|dosage|mg|mcg|microgram\w*|milligram\w*|tablet\w*)\b"
    r"|\btake\s+\d+\s*(?:mg|mcg|ml)\b"
    rf"|\b(?:{_DOSING_VERB_ALTERNATION})\b\s+taking\b",
    re.IGNORECASE,
)
_DIAGNOSIS_ASSERTION_PATTERN = re.compile(
    r"\bdiagnos(?:e|es|ed|ing|is)\b(?!\s*-?\s*(?:support|report)\b)",
    re.IGNORECASE,
)
_TREATMENT_DIRECTIVE_PATTERN = re.compile(
    r"\b(?:begin|start)\s+(?:treatment|therapy)\b"
    r"|\bshould\s+(?:take|start|stop|begin|switch)\b"
    r"|\b(?:recommends?|suggests?|advises?)\s+"
    r"(?:starting|stopping|switching|discontinuing|increasing|decreasing)\b",
    re.IGNORECASE,
)

_FORBIDDEN_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dosing instruction", _DOSING_INSTRUCTION_PATTERN),
    ("diagnosis assertion", _DIAGNOSIS_ASSERTION_PATTERN),
    ("treatment directive", _TREATMENT_DIRECTIVE_PATTERN),
)


def find_forbidden_language(statement: str) -> tuple[str, ...]:
    """Return the forbidden-language categories matched in *statement*, if any."""
    return tuple(
        name for name, pattern in _FORBIDDEN_LANGUAGE_PATTERNS if pattern.search(statement)
    )


def validate_insight(
    insight: SynthesizedInsight,
    *,
    vault_root: Path,
    weakest_cited_tier: EvidenceTier | None,
) -> tuple[str, ...]:
    """Return the rejection reasons for *insight*, or an empty tuple if it passes.

    *weakest_cited_tier* is the weakest evidence tier among the insight's
    tier-bearing citations, already resolved by the caller (e.g. from each
    cited `GeneticRiskFlag.evidence_tier`, `health_advisory.py:184`) — this
    gate does not re-derive a tier from citation paths. Pass ``None`` when
    every citation is a raw, untiered fact (labs, medications, family-history
    facts); the insight is then capped at `CITATION_ONLY_DEFAULT_TIER`.
    """
    reasons: list[str] = []

    if not insight.statement.strip():
        reasons.append("statement is empty")

    if not insight.linked_evidence:
        reasons.append("linked_evidence is empty")
    else:
        unresolved = tuple(
            path for path in insight.linked_evidence if not (vault_root / path).is_file()
        )
        if unresolved:
            reasons.append(f"unresolved citation(s): {', '.join(unresolved)}")

    distinct_domains = set(insight.domains_linked)
    if len(distinct_domains) < 2:
        reasons.append("domains_linked must name at least 2 distinct domains")
    invalid_domains = distinct_domains - set(VALID_HEALTH_DOMAINS)
    if invalid_domains:
        reasons.append(
            f"domains_linked has unknown domain(s): {', '.join(sorted(invalid_domains))}"
        )

    if insight.evidence_tier not in VALID_EVIDENCE_TIERS:
        reasons.append(
            f"evidence_tier {insight.evidence_tier!r} is not one of {VALID_EVIDENCE_TIERS}"
        )
    else:
        ceiling = (
            weakest_cited_tier if weakest_cited_tier is not None else CITATION_ONLY_DEFAULT_TIER
        )
        if _TIER_ORDER.index(insight.evidence_tier) < _TIER_ORDER.index(ceiling):
            reasons.append(
                f"evidence_tier {insight.evidence_tier!r} overstates the weakest cited tier "
                f"{ceiling!r}"
            )

    if not 0 <= insight.confidence <= 1:
        reasons.append("confidence must be between 0 and 1")

    forbidden = find_forbidden_language(insight.statement)
    if forbidden:
        reasons.append(f"statement contains forbidden language: {', '.join(forbidden)}")

    return tuple(reasons)
