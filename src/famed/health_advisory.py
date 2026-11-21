"""Non-authoritative health advisory report from genotype, labs, and history."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from famed.annotation_cache import (
    normalize_grch38_variant,
    open_annotation_db,
    resolve_gencc_inheritance,
)
from famed.lab_sync import (
    build_lab_discussion_prompts,
    build_lab_observations_for_advisory,
    build_lab_trends_from_db,
    lab_analyte_keys,
    write_category_index_note,
)
from famed.person_sync import load_person_display_name
from famed.report_rendering import (
    markdown_table_cell,
    render_yaml_front_matter,
    split_front_matter,
)
from famed.report_sources import (
    ReportInput,
    load_report,
    report_reference_line,
    reports_dir,
    source_report_paths,
)
from famed.report_synthesis import (
    DEFAULT_SYNTHESIS_INSERT_BEFORE,
    carry_over_synthesis_as_stale,
    extract_synthesis_carryover,
    persist_report_synthesized_insights,
)
from famed.report_synthesis import (
    render_synthesis_section as _render_synthesis_section,
)
from famed.synthesis_contract import SynthesisSection

_REVIEW_STATUS_PRIORITY: dict[str, int] = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, conflicting classifications": 1,
    "criteria provided, single submitter": 1,
    "no assertion criteria provided": 0,
    "no assertion provided": 0,
}

_EXCLUDED_FLAG_CONDITION_MARKERS: tuple[str, ...] = (
    "not specified",
    "not provided",
    "condition not specified",
    "susceptibility",
    "polymorphism",
)

_EXCLUDED_FLAG_SIGNIFICANCE_MARKERS: tuple[str, ...] = (
    "protective",
    "risk factor",
    "other",
)


@dataclass(frozen=True)
class FamilyHistoryContext:
    relative_id: str
    relationship_type: str
    display_name: str
    conditions: list[str] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeneticRiskFlag:
    rsid: str
    gene: str | None
    condition: str
    significance: str
    review_status: str
    evidence_tier: str
    zygosity: str
    variation_id: str | None
    inheritance_mode: str | None
    inheritance_source_ids: tuple[str, ...]
    frequency_status: str = "unavailable"
    frequency_reason: str | None = "not_cached"
    frequency_source_release: str | None = None
    joint_ac: int | None = None
    joint_an: int | None = None
    joint_af: float | None = None
    exome_ac: int | None = None
    exome_an: int | None = None
    exome_af: float | None = None
    genome_ac: int | None = None
    genome_an: int | None = None
    genome_af: float | None = None


@dataclass(frozen=True)
class GeneticRiskAssessment:
    status: str
    flags: tuple[GeneticRiskFlag, ...] = ()
    candidate_matches: int = 0
    conflicting_classification: int = 0
    insufficient_review: int = 0
    policy_excluded: int = 0
    duplicate_condition: int = 0
    display_limit: int = 0

    @property
    def accepted(self) -> int:
        return len(self.flags)


# Human-facing zygosity labels. Inheritance refinement is applied only when the
# exact ClinVar gene+disease identifiers resolve to a conservative GenCC contract;
# all other findings retain ADR-022's carrier-or-risk-pending fallback.
_ZYGOSITY_LABEL: dict[str, str] = {
    "homozygous": "Homozygous (2 copies)",
    "heterozygous": "Heterozygous (1 copy)",
}


def _iso_z_timestamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _zygosity_from_alleles(allele1: str, allele2: str, alternate_allele: str) -> str:
    copies = (allele1, allele2).count(alternate_allele)
    return "homozygous" if copies == 2 else "heterozygous"


def _flag_exclusion_reason(
    condition: str,
    significance: str,
    review_status: str,
) -> str | None:
    condition_lc = condition.casefold()
    significance_lc = significance.casefold()
    review_status_lc = review_status.casefold()
    if any(marker in condition_lc for marker in _EXCLUDED_FLAG_CONDITION_MARKERS):
        return "policy_excluded"
    if "conflicting classifications" in significance_lc:
        return "conflicting_classification"
    if any(marker in significance_lc for marker in _EXCLUDED_FLAG_SIGNIFICANCE_MARKERS):
        return "policy_excluded"
    if review_status_lc.startswith("no assertion"):
        return "insufficient_review"
    return None


def _review_tier(review_status: str) -> str:
    priority = 0
    for prefix, pri in _REVIEW_STATUS_PRIORITY.items():
        if review_status.casefold().startswith(prefix.casefold()):
            priority = max(priority, pri)
            break
    if priority >= 2:
        return "E2"
    if priority >= 1:
        return "E3"
    return "E4"


def _gnomad_frequency_for_clinvar_row(
    ref_conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    identity = normalize_grch38_variant(
        str(row["chromosome"] or ""),
        int(row["position"] or 0),
        str(row["reference_allele"] or ""),
        str(row["alternate_allele"] or ""),
    )
    if identity is None:
        return {"status": "unavailable", "unavailable_reason": "invalid_clinvar_identity"}
    frequency = ref_conn.execute(
        """
        SELECT *
        FROM gnomad_frequencies
        WHERE chromosome = ? AND position = ?
          AND reference_allele = ? AND alternate_allele = ?
        """,
        (
            identity.chromosome,
            identity.position,
            identity.reference_allele,
            identity.alternate_allele,
        ),
    ).fetchone()
    if frequency is None:
        return {"status": "unavailable", "unavailable_reason": "not_cached"}
    return dict(frequency)


def assess_genetic_risk_flags(
    conn: sqlite3.Connection,
    ref_db_path: Path,
    person_id: str,
    *,
    top_n: int = 20,
) -> GeneticRiskAssessment:
    if not ref_db_path.is_file():
        return GeneticRiskAssessment(status="reference_unavailable")

    genotype_rsids = {
        str(r[0]): (str(r[1] or ""), str(r[2] or ""))
        for r in conn.execute(
            "SELECT rsid, allele1, allele2 FROM genotypes WHERE person_id = ?",
            (person_id,),
        ).fetchall()
    }
    if not genotype_rsids:
        return GeneticRiskAssessment(status="genotype_unavailable")

    ref_conn = open_annotation_db(ref_db_path)
    try:
        if ref_conn.execute("SELECT 1 FROM clinvar_variants LIMIT 1").fetchone() is None:
            return GeneticRiskAssessment(status="reference_unavailable")
        rows = ref_conn.execute(
            """
            SELECT rsid, significance, condition, review_status, gene,
                   gene_curie, phenotype_ids, reference_allele, alternate_allele,
                   variation_id, chromosome, position
            FROM clinvar_variants
            WHERE rsid IS NOT NULL AND rsid != ''
              AND (significance LIKE '%pathogenic%' OR significance LIKE '%Pathogenic%')
            ORDER BY
                CASE
                    WHEN review_status LIKE 'reviewed by expert panel%' THEN 0
                    WHEN review_status LIKE 'practice guideline%' THEN 0
                    WHEN review_status LIKE
                         'criteria provided, multiple submitters, no conflicts%' THEN 1
                    WHEN review_status LIKE 'criteria provided, single submitter%' THEN 2
                    WHEN review_status LIKE 'criteria provided, conflicting%' THEN 3
                    ELSE 4
                END,
                significance
            """
        ).fetchall()
        inheritance_by_rsid = {
            str(row["rsid"]): resolve_gencc_inheritance(
                ref_conn,
                str(row["gene_curie"]) if row["gene_curie"] else None,
                str(row["phenotype_ids"]) if row["phenotype_ids"] else None,
            )
            for row in rows
            if str(row["rsid"]) in genotype_rsids
        }
        frequency_by_rsid = {
            str(row["rsid"]): _gnomad_frequency_for_clinvar_row(ref_conn, row)
            for row in rows
            if str(row["rsid"]) in genotype_rsids
        }
    finally:
        ref_conn.close()

    seen_conditions: set[str] = set()
    flags: list[GeneticRiskFlag] = []
    counts = {
        "conflicting_classification": 0,
        "insufficient_review": 0,
        "policy_excluded": 0,
        "duplicate_condition": 0,
        "display_limit": 0,
    }
    candidate_matches = 0
    for row in rows:
        rsid = str(row["rsid"])
        genotype = genotype_rsids.get(rsid)
        if genotype is None:
            continue
        alt_allele = str(row["alternate_allele"] or "")
        if not alt_allele:
            continue
        a1, a2 = genotype
        if alt_allele not in (a1, a2):
            continue
        candidate_matches += 1
        condition = str(row["condition"] or "condition not specified")
        base_condition = condition.split("|")[0].strip()
        significance = str(row["significance"] or "")
        review_status = str(row["review_status"] or "")
        exclusion_reason = _flag_exclusion_reason(base_condition, significance, review_status)
        if exclusion_reason is not None:
            counts[exclusion_reason] += 1
            continue
        if base_condition in seen_conditions:
            counts["duplicate_condition"] += 1
            continue
        seen_conditions.add(base_condition)
        if len(flags) >= top_n:
            counts["display_limit"] += 1
            continue
        tier = _review_tier(review_status)
        inheritance = inheritance_by_rsid[rsid]
        frequency = frequency_by_rsid[rsid]
        flags.append(
            GeneticRiskFlag(
                rsid=rsid,
                gene=str(row["gene"]) if row["gene"] else None,
                condition=base_condition,
                significance=significance,
                review_status=review_status,
                evidence_tier=tier,
                zygosity=_zygosity_from_alleles(a1, a2, alt_allele),
                variation_id=str(row["variation_id"]) if row["variation_id"] else None,
                inheritance_mode=inheritance.moi_curie,
                inheritance_source_ids=inheritance.source_ids,
                frequency_status=str(frequency["status"]),
                frequency_reason=(
                    str(frequency["unavailable_reason"])
                    if frequency.get("unavailable_reason")
                    else None
                ),
                frequency_source_release=(
                    str(frequency["source_release"]) if frequency.get("source_release") else None
                ),
                joint_ac=frequency.get("joint_ac"),
                joint_an=frequency.get("joint_an"),
                joint_af=frequency.get("joint_af"),
                exome_ac=frequency.get("exome_ac"),
                exome_an=frequency.get("exome_an"),
                exome_af=frequency.get("exome_af"),
                genome_ac=frequency.get("genome_ac"),
                genome_an=frequency.get("genome_an"),
                genome_af=frequency.get("genome_af"),
            )
        )
    sorted_flags = tuple(
        sorted(
            flags,
            key=lambda f: _REVIEW_STATUS_PRIORITY.get(_best_review_key(f.review_status), 0),
            reverse=True,
        )
    )
    return GeneticRiskAssessment(
        status="evaluated",
        flags=sorted_flags,
        candidate_matches=candidate_matches,
        conflicting_classification=counts["conflicting_classification"],
        insufficient_review=counts["insufficient_review"],
        policy_excluded=counts["policy_excluded"],
        duplicate_condition=counts["duplicate_condition"],
        display_limit=counts["display_limit"],
    )


def build_genetic_risk_flags(
    conn: sqlite3.Connection,
    ref_db_path: Path,
    person_id: str,
    *,
    top_n: int = 20,
) -> list[GeneticRiskFlag]:
    return list(assess_genetic_risk_flags(conn, ref_db_path, person_id, top_n=top_n).flags)


def _best_review_key(status: str) -> str:
    for prefix in sorted(_REVIEW_STATUS_PRIORITY, key=lambda k: -len(k)):
        if status.casefold().startswith(prefix.casefold()):
            return prefix
    return ""


def _flag_confidence(review_status: str) -> str:
    for prefix in ("reviewed by expert panel", "practice guideline"):
        if review_status.casefold().startswith(prefix):
            return "higher"
    status = review_status.casefold()
    if "multiple submitters" in status and "conflicting" not in status:
        return "moderate"
    return "lower"


def _age_from_person_note(vault_root: Path, person_id: str) -> int | None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.is_file():
        return None
    content = person_note.read_text(encoding="utf-8")
    match = re.search(r"birth_year:\s*(\d{4})", content)
    if not match:
        return None
    return datetime.now(UTC).year - int(match.group(1))


_USPSTF_SCREENINGS: dict[str, tuple[str, int, int]] = {
    "Colorectal cancer screening": ("colonoscopy or stool-based test", 45, 75),
    "Lung cancer screening (if smoking history)": ("low-dose CT chest", 50, 80),
    "Hypertension screening": ("blood pressure measurement", 18, 999),
    "Lipid screening": ("lipid panel", 40, 75),
    "Diabetes screening": ("HbA1c or fasting glucose", 35, 70),
    "Depression screening": ("PHQ-9 or clinical interview", 18, 999),
    "Abdominal aortic aneurysm (if male, ever smoker)": ("one-time ultrasound", 65, 75),
    "Hepatitis C screening": ("HCV antibody test", 18, 79),
    "HIV screening": ("HIV antibody/antigen test", 15, 65),
    "Osteoporosis screening (if risk factors)": ("bone density DXA scan", 65, 999),
}


# A screening is redundant once its condition is diagnosed; keys are Digest labels
# (ADR-029 allowlist and event labels) from medical-history-summary.md.
_SCREENING_DIAGNOSES: dict[str, tuple[str, ...]] = {
    "Hypertension screening": ("Hypertension",),
    "Diabetes screening": ("Diabetes / prediabetes",),
    "Lipid screening": ("Hyperlipidemia / high cholesterol",),
    "Depression screening": ("Depression",),
    "Colorectal cancer screening": ("Colon cancer", "Colorectal cancer", "Rectal cancer"),
    "Osteoporosis screening (if risk factors)": ("Osteoporosis",),
}


def _digest_conditions(vault_root: Path, person_id: str) -> set[str]:
    """Condition labels listed in the person's history Digest."""
    summary = vault_root / "Records" / person_id / "Reports" / "medical-history-summary.md"
    try:
        lines = summary.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    labels: set[str] = set()
    in_digest = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_digest = stripped == "## Digest"
            continue
        if in_digest and stripped.startswith("- "):
            label = re.split(r" \[\[|\. ", stripped[2:], maxsplit=1)[0].rstrip(".")
            labels.add(label)
    return labels


# Lab-based currency checks: screening name -> (test_name LIKE patterns, re-screening
# interval in years, or None for a one-time screening that never recurs).
_LAB_SCREENING_CHECKS: dict[str, tuple[tuple[str, ...], int | None]] = {
    "Lipid screening": (("%cholesterol%", "%triglyceride%"), 5),
    "Diabetes screening": (("%a1c%",), 3),
    "Hepatitis C screening": (("%hepatitis c%",), None),
}

# ADR-019: narrowly-scoped phrase matching for colonoscopy follow-up text. Limited to
# these two phrasings; a third phrasing observed in future imports should move to
# AI-assisted extraction per ADR-018's review gate rather than growing this list.
_COLONOSCOPY_RE = re.compile(r"colonoscop|colon cancer screening", re.IGNORECASE)
_POLYP_RE = re.compile(r"polyp|adenoma", re.IGNORECASE)
_FOLLOWUP_IN_YEARS_RE = re.compile(r"\bin (\d+) years?\b", re.IGNORECASE)
_FOLLOWUP_DUE_DATE_RE = re.compile(r"\bdue ([A-Za-z]+ \d{4})\b", re.IGNORECASE)

_COLONOSCOPY_DEFAULT_INTERVAL_WITH_POLYP_YEARS = 5
_COLONOSCOPY_DEFAULT_INTERVAL_WITHOUT_POLYP_YEARS = 10


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # Feb 29 on a non-leap target year.
        return value.replace(month=2, day=28, year=value.year + years)


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _currency_annotation(last_done: date, next_due: date | None, today: date) -> str | None:
    if next_due is None:
        return f"already done {last_done.isoformat()}; one-time screening"
    if next_due <= today:
        return None
    return f"last done {last_done.isoformat()}; not due again until ~{next_due.year}"


def _lab_screening_currency(
    conn: sqlite3.Connection, person_id: str, screening_name: str, today: date
) -> str | None:
    check = _LAB_SCREENING_CHECKS.get(screening_name)
    if check is None:
        return None
    patterns, interval_years = check
    placeholders = " OR ".join("LOWER(test_name) LIKE ?" for _ in patterns)
    row = conn.execute(
        f"SELECT MAX(observed_at) FROM lab_results WHERE person_id = ? AND ({placeholders})",
        (person_id, *patterns),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    last_done = _parse_date(row[0])
    if last_done is None:
        return None
    next_due = None if interval_years is None else _add_years(last_done, interval_years)
    return _currency_annotation(last_done, next_due, today)


def _read_accepted_facts(note_path: Path) -> list[dict[str, Any]]:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return []
    front_matter, _ = split_front_matter(text)
    raw_facts = front_matter.get("accepted_facts")
    if not isinstance(raw_facts, list):
        return []
    return [fact for fact in raw_facts if isinstance(fact, dict)]


def _colonoscopy_next_due(facts: list[dict[str, Any]], last_done: date) -> date | None:
    """Return the explicit next-due date from a follow_up fact, if one is parseable."""
    for fact in facts:
        if str(fact.get("category") or "").strip().casefold() != "follow_up":
            continue
        description = str(fact.get("description") or "")
        if not _COLONOSCOPY_RE.search(description):
            continue
        years_match = _FOLLOWUP_IN_YEARS_RE.search(description)
        if years_match:
            return _add_years(last_done, int(years_match.group(1)))
        due_match = _FOLLOWUP_DUE_DATE_RE.search(description)
        if due_match:
            try:
                return datetime.strptime(due_match.group(1), "%B %Y").date()
            except ValueError:
                continue
    return None


def _colorectal_screening_currency(vault_root: Path, person_id: str, today: date) -> str | None:
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return None

    most_recent: tuple[date, date] | None = None  # (last_done, next_due)
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        facts = _read_accepted_facts(note_path)

        last_done: date | None = None
        for fact in facts:
            if str(fact.get("category") or "").strip().casefold() == "follow_up":
                continue
            if not _COLONOSCOPY_RE.search(str(fact.get("description") or "")):
                continue
            recorded = _parse_date(fact.get("recorded_date"))
            if recorded is not None and (last_done is None or recorded > last_done):
                last_done = recorded
        if last_done is None:
            continue

        next_due = _colonoscopy_next_due(facts, last_done)
        if next_due is None:
            has_polyp = any(_POLYP_RE.search(str(fact.get("description") or "")) for fact in facts)
            interval = (
                _COLONOSCOPY_DEFAULT_INTERVAL_WITH_POLYP_YEARS
                if has_polyp
                else _COLONOSCOPY_DEFAULT_INTERVAL_WITHOUT_POLYP_YEARS
            )
            next_due = _add_years(last_done, interval)

        if most_recent is None or last_done > most_recent[0]:
            most_recent = (last_done, next_due)

    if most_recent is None:
        return None
    last_done, next_due = most_recent
    return _currency_annotation(last_done, next_due, today)


def _screening_currency(
    name: str,
    vault_root: Path,
    person_id: str,
    conn: sqlite3.Connection,
    today: date,
) -> str | None:
    if name == "Colorectal cancer screening":
        return _colorectal_screening_currency(vault_root, person_id, today)
    return _lab_screening_currency(conn, person_id, name, today)


def _build_screening_recommendations(
    vault_root: Path,
    person_id: str,
    conn: sqlite3.Connection,
    today: date,
) -> list[str]:
    age = _age_from_person_note(vault_root, person_id)
    if age is None:
        return ["_No birth year available for age-based screening recommendations._"]

    diagnosed = _digest_conditions(vault_root, person_id)
    lines: list[str] = []
    omitted: list[str] = []
    for name, (test, age_low, age_high) in _USPSTF_SCREENINGS.items():
        if age_low <= age <= age_high:
            already = [label for label in _SCREENING_DIAGNOSES.get(name, ()) if label in diagnosed]
            if already:
                omitted.extend(already)
                continue
            line = f"- **{name}**: {test}"
            currency = _screening_currency(name, vault_root, person_id, conn, today)
            if currency:
                line += f" — _{currency}_"
            lines.append(line)
    if omitted:
        lines.append(f"- _Not listed, already diagnosed: {', '.join(omitted)}._")
    if not lines:
        return ["_No age-appropriate USPSTF preventive screenings identified._"]
    return lines


def _build_doctor_questions(
    genetic_flags: list[GeneticRiskFlag],
    person_id: str,
    lab_prompts: Sequence[str] = (),
) -> list[str]:
    """Prompts from out-of-range labs first, then genetic flags."""
    if not genetic_flags:
        return list(lab_prompts) or [
            "_No out-of-range results or genetic risk flags to generate prompts from._"
        ]

    questions = [
        *lab_prompts,
        "- Review the genetic risk flags above with a clinician "
        "before making any screening or treatment decisions.",
        "- Ask whether any flagged conditions have been observed in "
        f"[[{person_id}|the patient]]'s family.",
    ]
    for flag in genetic_flags[:5]:
        gene_note = f" ({flag.gene})" if flag.gene else ""
        confidence = _flag_confidence(flag.review_status)
        interpretation = _genetic_flag_interpretation(flag)
        questions.append(
            f"- Discuss ClinVar variant {flag.rsid}{gene_note}: "
            f"{flag.condition}. {interpretation}; ask whether confirmatory clinical "
            "testing is appropriate. "
            f"(ClinVar confidence: {confidence})"
        )
    return questions


def _genetic_flag_interpretation(flag: GeneticRiskFlag) -> str:
    if flag.inheritance_mode == "HP:0000007":
        if flag.zygosity == "heterozygous":
            return "Carrier-compatible observation (autosomal recessive inheritance)"
        return "Two-copy risk-associated observation (autosomal recessive inheritance)"
    if flag.inheritance_mode == "HP:0000006":
        return "Risk-associated observation (autosomal dominant inheritance)"
    if flag.zygosity == "heterozygous":
        return "Carrier or risk pending inheritance evidence"
    return "Two-copy observation; inheritance evidence unresolved"


def _genetic_flag_inheritance_evidence(flag: GeneticRiskFlag) -> str:
    if not flag.inheritance_source_ids:
        return "Not resolved"
    return "GenCC " + ", ".join(flag.inheritance_source_ids)


def _format_population_frequency(af: float | None, ac: int | None, an: int | None) -> str:
    if af is None or ac is None or an is None or an <= 0:
        return "unavailable"
    percent = af * 100
    display = f"{percent:.2e}%" if 0 < percent < 0.001 else f"{percent:.4g}%"
    return f"{display} (AC {ac:,} / AN {an:,})"


def _genetic_flag_population_frequency(flag: GeneticRiskFlag) -> str:
    release = (
        f"gnomAD v{flag.frequency_source_release}" if flag.frequency_source_release else "gnomAD"
    )
    if flag.frequency_status == "discordant":
        return f"Discordant — {release} source flag; frequency not interpreted"
    if flag.frequency_status != "available":
        return f"Unavailable — {release}"

    joint = _format_population_frequency(flag.joint_af, flag.joint_ac, flag.joint_an)
    exome = _format_population_frequency(flag.exome_af, flag.exome_ac, flag.exome_an)
    genome = _format_population_frequency(flag.genome_af, flag.genome_ac, flag.genome_an)
    return f"{joint}; exomes {exome}; genomes {genome}; {release}"


def _render_genetic_evidence_disposition(assessment: GeneticRiskAssessment) -> str:
    if assessment.status == "genotype_unavailable":
        return (
            "> [!info] Genetic evidence not evaluated\n"
            "> No normalized genotype data is available for this person. This is an "
            "unknown-data state, not a negative result."
        )
    if assessment.status == "reference_unavailable":
        return (
            "> [!warning] Genetic evidence not evaluated\n"
            "> The local ClinVar reference cache is missing or empty. This is an "
            "unknown-reference state, not a negative result."
        )

    lines = [
        "> [!note] Genetic evidence disposition",
        "> Candidate allele matches are ClinVar pathogenic-text rows whose "
        "alternate allele is present in the imported genotype. Excluded candidates "
        "are not findings or diagnoses and are not used for doctor prompts.",
        ">",
        f"> - Candidate allele matches: {assessment.candidate_matches}",
        f"> - Accepted flags: {assessment.accepted}",
        (
            "> - Excluded — conflicting ClinVar classification: "
            f"{assessment.conflicting_classification}"
        ),
        f"> - Excluded — insufficient review support: {assessment.insufficient_review}",
        f"> - Excluded — established report policy: {assessment.policy_excluded}",
        f"> - Excluded — duplicate condition: {assessment.duplicate_condition}",
    ]
    if assessment.display_limit:
        lines.append(f"> - Eligible but beyond the display limit: {assessment.display_limit}")
    return "\n".join(lines)


_FAMILY_HISTORY_CATEGORIES: frozenset[str] = frozenset({"diagnosis", ""})

_FAMILY_HISTORY_NON_ISSUE_PREFIXES: tuple[str, ...] = (
    "no active problems",
    "no known active problems",
    "no known medical problems",
    "no known problems",
    "no medical problems",
    "no significant medical history",
)

_FAMILY_HISTORY_NON_HERITABLE_TERMS: tuple[str, ...] = (
    "acute ankle sprain",
    "appendectomy",
    "brittle nails",
    "cholecystectomy",
    "contracture of palmar fascia",
    "cough",
    "dizziness",
    "epigastric pain",
    "fall",
    "feeling of chest tightness",
    "foreign body",
    "fracture",
    "helicobacter pylori gastritis",
    "hospital discharge follow-up",
    "impingement syndrome",
    "infectious and parasitic diseases",
    "injury",
    "iron deficiency anemia",
    "laceration",
    "localized swelling",
    "lump",
    "medical examination",
    "overdose",
    "pain",
    "plantar fasciopathy",
    "preprocedural",
    "shortness of breath",
    "sprain",
    "sob",
    "tongue lesion",
    "vertigo",
    "vaginal bleeding",
    "uterine bleeding",
)

_FAMILY_HISTORY_NON_HERITABLE_PREFIXES: tuple[str, ...] = (
    "encounter for",
    "persons encountering health services",
    "screen for",
    "screening for",
)

_FAMILY_HISTORY_FEMALE_SPECIFIC_TERMS: tuple[str, ...] = (
    "abnormal uterine bleeding",
    "cervical dysplasia",
    "endometriosis",
    "fibroid",
    "irregular bleeding",
    "leiomyoma",
    "menorrhagia",
    "metrorrhagia",
    "ovarian cyst",
    "pregnancy",
    "uterus",
    "uterine bleeding",
    "uterine fibroid",
    "vaginal bleeding",
)

_FAMILY_HISTORY_MALE_SPECIFIC_TERMS: tuple[str, ...] = (
    "benign prostatic hyperplasia",
    "bph",
    "erectile dysfunction",
    "prostatitis",
    "testicular torsion",
)

_FAMILY_HISTORY_CANCER_TERMS: tuple[str, ...] = ("cancer", "carcinoma", "malignancy")

_FAMILY_HISTORY_HERITABLE_SIGNAL_TERMS: tuple[str, ...] = (
    "add",
    "adhd",
    "adenoma",
    "anxiety",
    "asthma",
    "atrial fibrillation",
    "cad",
    "cholesterol",
    "coronary artery disease",
    "depression",
    "diabetes",
    "familial",
    "hypercholesterolemia",
    "hyperlipidemia",
    "hypertension",
    "htn",
    "myocardial infarction",
    "nstemi",
    "paf",
    "polyp",
    "stroke",
    "varicose veins",
)

_FAMILY_HISTORY_CANONICAL_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Hyperlipidemia / high cholesterol",
        ("cholesterol", "hypercholesterolemia", "hyperlipidemia"),
    ),
    ("Colon polyps / adenomas", ("adenoma", "polyp")),
    ("Coronary artery disease", ("cad", "coronary artery disease")),
    ("Hypertension", ("htn", "hypertension")),
    ("Atrial fibrillation", ("atrial fibrillation", "paf")),
    ("Myocardial infarction", ("myocardial infarction", "nstemi")),
    ("Stroke", ("stroke",)),
    ("Prostate cancer", ("prostate cancer",)),
    ("Diabetes / prediabetes", ("diabetes", "prediabetes")),
    ("Depression", ("depression",)),
    ("Anxiety", ("anxiety", "gad")),
    ("ADHD / ADD", ("add", "adhd", "attention deficit")),
    ("Asthma", ("asthma",)),
    ("Varicose veins", ("varicose veins",)),
)


def _normalized_family_history_issue(description: str) -> str:
    return " ".join(description.casefold().split())


def _family_history_term_matches(normalized: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized) is not None


def _family_history_issue_has_cancer_signal(normalized: str) -> bool:
    return any(term in normalized for term in _FAMILY_HISTORY_CANCER_TERMS)


def _family_history_issue_has_heritable_signal(normalized: str) -> bool:
    return any(
        _family_history_term_matches(normalized, term)
        for term in _FAMILY_HISTORY_HERITABLE_SIGNAL_TERMS
    )


def _canonical_family_history_issue(description: str) -> str:
    normalized = _normalized_family_history_issue(description)
    for label, terms in _FAMILY_HISTORY_CANONICAL_LABELS:
        if any(_family_history_term_matches(normalized, term) for term in terms):
            return label
    return description


def _markdown_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _questionable_family_history_labels(vault_root: Path, relative_id: str) -> set[str]:
    summary_path = vault_root / "Records" / relative_id / "Reports" / "medical-history-summary.md"
    try:
        lines = summary_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()

    labels: set[str] = set()
    in_review_section = False
    fact_index: int | None = None
    for line in lines:
        stripped = line.strip()
        if stripped == "## Diagnosis Support Review":
            in_review_section = True
            continue
        if in_review_section and stripped.startswith("## "):
            break
        if not in_review_section:
            continue

        cells = _markdown_table_cells(line)
        if not cells:
            continue
        normalized_headers = [cell.casefold() for cell in cells]
        if "fact" in normalized_headers:
            fact_index = normalized_headers.index("fact")
            continue
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if fact_index is None or fact_index >= len(cells):
            continue
        fact = cells[fact_index].strip()
        if fact:
            labels.add(_canonical_family_history_issue(fact))
    return labels


def _family_history_issue_is_reportable(description: str) -> bool:
    normalized = _normalized_family_history_issue(description)
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in _FAMILY_HISTORY_NON_ISSUE_PREFIXES):
        return False
    if any(normalized.startswith(prefix) for prefix in _FAMILY_HISTORY_NON_HERITABLE_PREFIXES):
        return False
    if any(term in normalized for term in _FAMILY_HISTORY_NON_HERITABLE_TERMS):
        return False

    if _family_history_issue_has_cancer_signal(normalized):
        return True
    if any(term in normalized for term in _FAMILY_HISTORY_FEMALE_SPECIFIC_TERMS):
        return False
    if any(term in normalized for term in _FAMILY_HISTORY_MALE_SPECIFIC_TERMS):
        return False
    return _family_history_issue_has_heritable_signal(normalized)


def _load_relative_conditions(vault_root: Path, relative_id: str) -> list[tuple[str, str]]:
    """Return (condition, source_note_path) pairs, one per distinct reportable condition."""
    history_dir = vault_root / "Records" / relative_id / "History"
    if not history_dir.is_dir():
        return []

    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    questionable_labels = _questionable_family_history_labels(vault_root, relative_id)
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        for fact in _read_accepted_facts(note_path):
            category = str(fact.get("category") or "").strip().casefold()
            if category not in _FAMILY_HISTORY_CATEGORIES:
                continue
            description = str(fact.get("description") or "").strip()
            if not _family_history_issue_is_reportable(description):
                continue
            canonical = _canonical_family_history_issue(description)
            if canonical in questionable_labels:
                continue
            if canonical not in seen:
                seen.add(canonical)
                pairs.append((canonical, f"Records/{relative_id}/History/{note_path.name}"))
                if len(pairs) >= 10:
                    return pairs
    return pairs


# Relationship types that carry meaningful genetic signal for the subject's own risk.
# Spouses share no genes. Children's conditions predict the parent's risk only indirectly
# (carrier status for recessives); that signal is covered by the Genetics report.
# Only ascending and lateral blood relatives are included here.
_HERITABLE_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {"parent", "grandparent", "sibling", "half_sibling"}
)


def load_family_history_context(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> list[FamilyHistoryContext]:
    placeholders = ",".join("?" * len(_HERITABLE_RELATIONSHIP_TYPES))
    rows = conn.execute(
        f"""
        SELECT target_person_id, relationship_type
        FROM family_links
        WHERE source_person_id = ?
          AND relationship_type IN ({placeholders})
        ORDER BY relationship_type, target_person_id
        """,
        (person_id, *_HERITABLE_RELATIONSHIP_TYPES),
    ).fetchall()

    contexts: list[FamilyHistoryContext] = []
    for row in rows:
        relative_id = str(row[0])
        relationship_type = str(row[1])
        pairs = _load_relative_conditions(vault_root, relative_id)
        if not pairs:
            continue
        conditions, source_notes = (list(values) for values in zip(*pairs, strict=True))
        display_name = load_person_display_name(vault_root, relative_id)
        contexts.append(
            FamilyHistoryContext(
                relative_id=relative_id,
                relationship_type=relationship_type,
                display_name=display_name,
                conditions=conditions,
                source_notes=source_notes,
            )
        )
    return contexts


def load_heritable_dependents(conn: sqlite3.Connection, person_id: str) -> list[str]:
    """Return people whose family-history context reads *person_id*'s history."""
    placeholders = ",".join("?" * len(_HERITABLE_RELATIONSHIP_TYPES))
    rows = conn.execute(
        f"""
        SELECT DISTINCT source_person_id
        FROM family_links
        WHERE target_person_id = ?
          AND relationship_type IN ({placeholders})
        ORDER BY source_person_id
        """,
        (person_id, *_HERITABLE_RELATIONSHIP_TYPES),
    ).fetchall()
    return [str(row[0]) for row in rows]


def family_history_source_notes(family_history: Sequence[FamilyHistoryContext]) -> list[str]:
    """Vault History note paths consulted for family-history context, for front-matter citation."""
    return list(dict.fromkeys(note for ctx in family_history for note in ctx.source_notes))


def _build_family_history_notes(contexts: Sequence[FamilyHistoryContext]) -> list[str]:
    lines: list[str] = []
    for ctx in contexts:
        lines.append(
            f"**{ctx.display_name}** ({ctx.relationship_type}) — [[{ctx.relative_id}|view record]]"
        )
        for condition in ctx.conditions:
            lines.append(f"  - {condition}")
    return lines


def render_health_advisory(
    *,
    person_id: str,
    display_name: str,
    generated_iso: str,
    genetic_flags: list[GeneticRiskFlag],
    genetic_assessment: GeneticRiskAssessment,
    source_reports: list[ReportInput],
    vault_root: Path,
    conn: sqlite3.Connection,
    today: date,
    lab_observations: list[str] | None = None,
    lab_prompts: Sequence[str] = (),
    family_history: Sequence[FamilyHistoryContext] = (),
) -> str:
    source_paths = source_report_paths(list(source_reports))
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "health_advisory",
        "generated_at": generated_iso,
        "source_reports": source_paths,
        "family_history_sources": family_history_source_notes(family_history),
    }

    sections: list[str] = [
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n",
        f"# Health Advisory — {display_name}\n",
        f"[[{person_id}|<- {display_name}]]\n",
        "> [!warning] Clinical Safety\n"
        "> These are population-level risk observations and screening reminders, "
        "not individual diagnoses or treatment plans. Every item is a conversation "
        "starter for your next doctor visit, not a clinical conclusion. Confirm "
        "clinically significant findings with a healthcare professional.\n",
        "## Genetic Risk Flags\n",
    ]

    if genetic_flags:
        sections.append(
            "> [!note] Reading these flags\n"
            "> **Zygosity** shows how many copies of the pathogenic allele were"
            " observed. **Interpretation** uses inheritance only when exact ClinVar"
            " gene and disease identifiers match consistent Strong/Definitive ClinGen"
            " assertions in the local GenCC cache. Unmatched, weak, conflicting, or"
            " complex evidence remains explicitly unresolved. **Population Frequency**"
            " is display-only global context from the versioned local gnomAD cache;"
            " unavailable or discordant data never changes the flag. These are"
            " population-level observations, not personal diagnoses.\n"
        )
        sections.append(
            "| Variant | Gene | Condition | Zygosity | Interpretation | Significance "
            "| Review Confidence | Evidence | ClinVar ID | Inheritance Evidence "
            "| Population Frequency |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for flag in genetic_flags:
            sections.append(
                "| "
                + " | ".join(
                    [
                        markdown_table_cell(flag.rsid),
                        markdown_table_cell(flag.gene),
                        markdown_table_cell(flag.condition),
                        markdown_table_cell(_ZYGOSITY_LABEL.get(flag.zygosity, flag.zygosity)),
                        markdown_table_cell(_genetic_flag_interpretation(flag)),
                        markdown_table_cell(flag.significance),
                        markdown_table_cell(flag.review_status),
                        f"{flag.evidence_tier}/{_flag_confidence(flag.review_status)}",
                        markdown_table_cell(flag.variation_id),
                        markdown_table_cell(_genetic_flag_inheritance_evidence(flag)),
                        markdown_table_cell(_genetic_flag_population_frequency(flag)),
                    ]
                )
                + " |"
            )
        sections.extend(["", _render_genetic_evidence_disposition(genetic_assessment), ""])
    elif genetic_assessment.status == "evaluated":
        sections.extend(
            [
                "_No variants met the conservative ClinVar flag criteria._\n",
                _render_genetic_evidence_disposition(genetic_assessment),
                "",
            ]
        )
    else:
        sections.extend([_render_genetic_evidence_disposition(genetic_assessment), ""])

    sections.extend(
        [
            "",
            "## Preventive Screening\n",
            "Age-based USPSTF preventive screening recommendations:\n",
            *_build_screening_recommendations(vault_root, person_id, conn, today),
            "",
        ]
    )

    if lab_observations:
        sections.extend(
            [
                "## Lab Observations\n",
                "Out-of-range, notably trending, or recently normalized lab results"
                " that may be relevant for discussion:\n",
                *lab_observations,
                "",
            ]
        )

    if family_history:
        family_notes = _build_family_history_notes(family_history)
        sections.extend(
            [
                "## Family History Context\n",
                "> [!note] Family history observations\n"
                "> Conditions recorded for family members — not individual inherited risk "
                "assessments. Discuss with your doctor.\n",
                "",
                *family_notes,
                "",
            ]
        )

    sections.extend(
        [
            "## Doctor Discussion Prompts\n",
            *_build_doctor_questions(genetic_flags, person_id, lab_prompts),
            "",
            "## Source Reports\n",
        ]
        + [report_reference_line(report) for report in source_reports]
        + [""]
    )

    return "\n".join(sections)


def write_health_advisory(
    vault_root: Path,
    person_id: str,
    *,
    conn: sqlite3.Connection,
    ref_db_path: Path,
    generated_at: datetime | None = None,
) -> tuple[Path, int]:
    """Deterministically (re)compose health-advisory.md from current sources.

    Any existing "## Synthesized Insights" section is carried over verbatim
    with its status flipped to ``stale`` (ADR-026): a deterministic rewrite
    never drops live-agent synthesis; only a new live run replaces it via
    :func:`persist_synthesized_insights`.
    """
    report_dir = reports_dir(vault_root, person_id)
    if not report_dir.is_dir():
        report_dir.mkdir(parents=True, exist_ok=True)

    generated = generated_at or datetime.now(UTC)
    generated_iso = _iso_z_timestamp(generated)
    display_name = load_person_display_name(vault_root, person_id)

    genetic_assessment = assess_genetic_risk_flags(conn, ref_db_path, person_id)
    genetic_flags = list(genetic_assessment.flags)
    family_ctx = load_family_history_context(conn, vault_root, person_id)

    source_keys: list[tuple[str, str, str]] = [
        ("genetics", "genetics.md", "Genetics"),
        ("current_medications", "current-medications.md", "Current medications"),
        ("medical_history_summary", "medical-history-summary.md", "Medical history summary"),
        ("lab_results", "lab-results.md", "Lab results"),
        ("flagged_for_discussion", "flagged-for-discussion.md", "Flagged for discussion"),
    ]
    sources = [
        load_report(vault_root, person_id, key, filename, title)
        for key, filename, title in source_keys
    ]

    trend_rows = build_lab_trends_from_db(
        conn, person_id, analyte_keys=lab_analyte_keys(vault_root)
    )
    lab_obs = build_lab_observations_for_advisory(trend_rows) or None
    lab_prompts = build_lab_discussion_prompts(trend_rows)

    report_path = report_dir / "health-advisory.md"
    carryover = extract_synthesis_carryover(report_path)
    report_path.write_text(
        render_health_advisory(
            person_id=person_id,
            display_name=display_name,
            generated_iso=generated_iso,
            genetic_flags=genetic_flags,
            genetic_assessment=genetic_assessment,
            source_reports=sources,
            vault_root=vault_root,
            conn=conn,
            today=generated.date(),
            lab_observations=lab_obs,
            lab_prompts=lab_prompts,
            family_history=family_ctx,
        ),
        encoding="utf-8",
    )
    carry_over_synthesis_as_stale(report_path, carryover, now=generated)
    write_category_index_note(
        report_dir,
        person_id,
        "Reports",
        note_label="report",
        display_name=display_name,
    )
    return report_path, len(genetic_flags)


# ---------------------------------------------------------------------------
# Synthesized insights (ADR-026) -- persisted by a live agent after
# the deterministic body above has already been written. No model is
# invoked here; the agent authors candidates, this only validates + persists.
# ---------------------------------------------------------------------------


def render_synthesis_section(section: SynthesisSection) -> str:
    return _render_synthesis_section(section)


def persist_synthesized_insights(
    vault_root: Path,
    person_id: str,
    candidates: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> tuple[SynthesisSection, tuple[str, ...]]:
    """Validate live-agent-authored *candidates* and persist the accepted ones.

    *candidates* is a list of raw dicts (statement, linked_evidence,
    domains_linked, evidence_tier, confidence, optional weakest_cited_tier) --
    the agent-authored side of the ADR-026 contract. Requires
    ``write_health_advisory`` to have already written the report this
    generation; the deterministic body is read back and only the synthesis
    section is replaced.
    """
    return persist_report_synthesized_insights(
        vault_root,
        person_id,
        report_filename="health-advisory.md",
        candidates=candidates,
        insert_before_headings=DEFAULT_SYNTHESIS_INSERT_BEFORE,
        generated_at=generated_at,
    )
