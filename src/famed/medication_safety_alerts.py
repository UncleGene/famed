"""Deterministic medication-safety alert composer over existing vault reports."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from famed.findings_ledger import FINDINGS_LEDGER_PATH, CurrentFinding, active_findings
from famed.lab_sync import write_category_index_note
from famed.medication_sync import write_current_medications_report
from famed.person_sync import load_person_display_name
from famed.report_rendering import markdown_table_cell, render_yaml_front_matter
from famed.report_sources import (
    ReportInput,
    has_conflict_rows,
    is_stale,
    load_report,
    needs_review,
    render_findings,
    report_reference_line,
    reports_dir,
    source_report_paths,
)


class MedicationSafetyAlertsError(ValueError):
    """Raised when medication-safety alerts cannot be generated safely."""


@dataclass(frozen=True)
class MedicationSafetyAlert:
    alert_type: str
    review_priority: str
    reason: str
    clinician_action: str


_CORE_REPORTS: tuple[tuple[str, str, str], ...] = (
    ("current_medications", "current-medications.md", "Current medications"),
    ("medication_conflicts", "medication-conflicts.md", "Medication conflicts"),
    ("genetics", "genetics.md", "Genetics"),
)

_CONTEXT_REPORTS: tuple[tuple[str, str, str], ...] = (
    ("lab_results", "lab-results.md", "Lab results"),
    ("lab_conflicts", "lab-conflicts.md", "Lab conflicts"),
    ("medical_history_summary", "medical-history-summary.md", "Medical history summary"),
)


def _relevant_findings(findings: list[CurrentFinding], person_id: str) -> list[CurrentFinding]:
    relevant_domains = {"pgx_coverage", "medication_safety", "medication"}
    return [
        finding
        for finding in findings
        if finding.person_id == person_id
        and (
            finding.domain.casefold() in relevant_domains
            or "medication" in finding.domain.casefold()
            or "pgx" in finding.domain.casefold()
        )
    ]


def _build_source_alerts(reports: list[ReportInput]) -> list[MedicationSafetyAlert]:
    alerts: list[MedicationSafetyAlert] = []
    missing = [report for report in reports if not report.exists]
    stale = [report for report in reports if is_stale(report)]
    unreviewed = [report for report in reports if needs_review(report)]

    if missing:
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Source coverage",
                review_priority="complete sources first",
                reason="Missing source reports: " + ", ".join(report.key for report in missing),
                clinician_action="Regenerate or ingest missing reports before relying on alerts.",
            )
        )
    if stale:
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Source freshness",
                review_priority="refresh before acting",
                reason="Stale source reports: " + ", ".join(report.key for report in stale),
                clinician_action="Regenerate stale reports before medication decisions.",
            )
        )
    if unreviewed:
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Unreviewed source content",
                review_priority="verify before acting",
                reason="Unreviewed source reports: "
                + ", ".join(report.key for report in unreviewed),
                clinician_action="Review unresolved source facts before treating them as settled.",
            )
        )
    return alerts


def _build_conflict_alerts(reports_by_key: dict[str, ReportInput]) -> list[MedicationSafetyAlert]:
    alerts: list[MedicationSafetyAlert] = []
    medication_conflicts = reports_by_key["medication_conflicts"]
    lab_conflicts = reports_by_key["lab_conflicts"]
    if has_conflict_rows(medication_conflicts):
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Medication conflict",
                review_priority="reconcile before acting",
                reason="Medication conflict rows are present.",
                clinician_action="Resolve medication conflicts before relying on dose or status.",
            )
        )
    if has_conflict_rows(lab_conflicts):
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Lab context conflict",
                review_priority="review before medication changes",
                reason="Lab conflict rows may affect medication review context.",
                clinician_action=(
                    "Resolve affected lab conflicts before using lab context clinically."
                ),
            )
        )
    return alerts


def _translate_pgx_finding_statement(statement: str) -> str:
    """Translate legacy technical pgx_coverage statements to plain language."""
    import re

    # Legacy format: "X has local CPIC coverage for GENE; genotype readiness is STATE; PHENOTYPE."
    m = re.match(
        r"^(.+?) has local CPIC coverage for ([^;]+);\s*"
        r"genotype readiness is ([^;]+?);\s*(.+)\.$",
        statement,
    )
    if not m:
        return statement
    med, gene, geno_state, phenotype_part = m.group(1, 2, 3, 4)
    if "phenotype remains not interpreted" in phenotype_part:
        return (
            f"{med}: DNA data is available for the gene ({gene}) that affects "
            f"how this medication is processed, but the local CPIC reference data is "
            f"incomplete for a validated phenotype call. Refresh the local CPIC "
            f"allele/reference cache and regenerate the PGx coverage report."
        )
    if "not interpretable" in phenotype_part:
        return (
            f"{med}: DNA data is available for the gene ({gene}), but the consumer "
            f"DNA test did not include the specific markers needed to predict how this "
            f"medication is processed. "
            f"A clinical pharmacogenetics test would give a definitive result."
        )
    pheno_match = re.search(r"conservative phenotype: (.+)", phenotype_part)
    if pheno_match:
        return (
            f"{med}: DNA data indicates this person processes this medication "
            f"as {pheno_match.group(1)} ({gene})."
        )
    return statement


_FINDING_DEFAULT_ACTION = "Bring this to the prescribing doctor's or pharmacist's attention."

_PGX_SYSTEM_KEYWORDS = (
    "genetic reference data has not been downloaded",
    "cpic_alleles download",
    "local cpic reference data is incomplete",
    "cpic allele/reference cache",
)
_PGX_NO_MARKERS_KEYWORDS = ("consumer DNA test did not include the specific markers",)


def _pgx_finding_action(statement: str) -> str:
    if any(kw in statement for kw in _PGX_SYSTEM_KEYWORDS):
        return (
            "No doctor action needed — this is a system setup item. "
            "Download the genetic reference data to complete the analysis."
        )
    if any(kw in statement for kw in _PGX_NO_MARKERS_KEYWORDS):
        return (
            "No immediate action needed. Mention to the prescribing doctor if "
            "precise dosing guidance is important — a clinical pharmacogenetics "
            "test can give a definitive result."
        )
    return (
        "Ask the prescribing doctor or pharmacist whether this genetic finding "
        "should be reviewed before the next refill or dose change."
    )


def _build_finding_alerts(findings: list[CurrentFinding]) -> list[MedicationSafetyAlert]:
    alerts: list[MedicationSafetyAlert] = []
    for finding in sorted(findings, key=lambda item: (item.domain, item.statement)):
        if finding.domain == "pgx_coverage":
            statement = _translate_pgx_finding_statement(finding.statement)
            if any(
                normal_result in statement.casefold()
                for normal_result in ("normal metabolizer", "normal function")
            ):
                continue
            action = _pgx_finding_action(statement)
        else:
            statement = finding.statement
            action = _FINDING_DEFAULT_ACTION
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Evidence finding",
                review_priority=f"{finding.evidence_tier}/{finding.evidence_check}",
                reason=statement,
                clinician_action=action,
            )
        )
    return alerts


def _extract_medication_names(medications_body: str) -> list[str]:
    import re

    name_pattern = re.compile(r"^\|\s*([^|]+?)\s*\|", re.MULTILINE)
    names = [m.group(1).strip() for m in name_pattern.finditer(medications_body)]
    return [n for n in names if n and "---" not in n and "Medication" not in n]


def _build_duplicate_therapy_alerts(
    medications_body: str,
) -> list[MedicationSafetyAlert]:
    """Detect active medications that appear more than once in the active list."""
    names = _extract_medication_names(medications_body)

    base_counts: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for name in names:
        base = name.casefold().split("(")[0].strip()
        base_counts[base] = base_counts.get(base, 0) + 1
        display_names[base] = name

    alerts: list[MedicationSafetyAlert] = []
    for base, count in base_counts.items():
        if count > 1:
            alerts.append(
                MedicationSafetyAlert(
                    alert_type="Duplicate therapy",
                    review_priority="review for intentionality",
                    reason=f"{display_names[base]} appears {count} times "
                    "as an active medication (different doses, sources, or periods)",
                    clinician_action=(
                        "Verify with prescriber whether multiple entries are intentional "
                        "dose titrations or represent duplicate documentation."
                    ),
                )
            )
    return alerts


_NON_NORMAL_PHENOTYPES: frozenset[str] = frozenset(
    {
        "poor metabolizer",
        "intermediate metabolizer",
        "rapid metabolizer",
        "ultrarapid metabolizer",
        "indeterminate",
        "decreased function",
        "poor function",
        "increased function",
        "possible decreased function",
    }
)

_PHENOTYPE_PLAIN: dict[str, str] = {
    "poor metabolizer": (
        "breaks down this medication much more slowly than average. "
        "This means the drug may build up to higher levels in the body than expected, "
        "which can cause stronger side effects or toxicity at a standard dose. "
        "This should be reviewed clinically before relying on the expected response."
    ),
    "intermediate metabolizer": (
        "breaks down this medication somewhat more slowly than average. "
        "The drug may stay in the body a bit longer than expected, "
        "which could lead to stronger effects or more noticeable side effects. "
        "This should be reviewed clinically if medication response is a concern."
    ),
    "rapid metabolizer": (
        "breaks down this medication faster than average. "
        "The drug is cleared from the body more quickly, "
        "so standard doses may not build up enough to be effective. "
        "This should be reviewed clinically if treatment response is weaker than expected."
    ),
    "ultrarapid metabolizer": (
        "breaks down this medication much faster than average. "
        "The drug may be cleared so quickly that the expected effect is reduced. "
        "This should be reviewed clinically before treating it as a dosing problem."
    ),
    "indeterminate": (
        "has DNA data that gives unclear results for how this medication is processed. "
        "It is difficult to predict whether the standard dose will be too much or too little. "
        "The doctor should be aware of this uncertainty."
    ),
    "decreased function": (
        "has decreased function for a transporter involved in this medication. "
        "This source-backed result may affect medication exposure and should be "
        "reviewed with the prescriber or pharmacist."
    ),
    "poor function": (
        "has poor function for a transporter involved in this medication. "
        "This source-backed result may affect medication exposure and should be "
        "reviewed with the prescriber or pharmacist."
    ),
    "increased function": (
        "has increased function for a transporter involved in this medication. "
        "This source-backed result may affect medication exposure and should be "
        "reviewed with the prescriber or pharmacist."
    ),
    "possible decreased function": (
        "has a possible decreased-function transporter result for this medication. "
        "The uncertainty and possible medication impact should be reviewed with "
        "the prescriber or pharmacist."
    ),
}


def _build_phenotype_alerts(
    genetics_body: str,
) -> list[MedicationSafetyAlert]:
    """Surface non-normal gene results that affect a current medication (genetics.md)."""
    alerts: list[MedicationSafetyAlert] = []
    for line in genetics_body.splitlines():
        cols = [c.strip() for c in line.strip().split("|")[1:-1]]
        if len(cols) != 3 or cols[0] in ("Gene", "---") or set(cols[0]) <= {"-", ":"}:
            continue
        gene, result, medications = cols
        matched = next((p for p in _NON_NORMAL_PHENOTYPES if p in result.casefold()), None)
        if not matched or medications in ("", "—"):
            continue
        plain = _PHENOTYPE_PLAIN.get(matched, matched)
        for med_name in (name.strip() for name in medications.split(",")):
            alerts.append(
                MedicationSafetyAlert(
                    alert_type="Genetics-medication flag",
                    review_priority="E1/phenotype",
                    reason=(
                        f"{med_name}: DNA data suggests this person's body {plain} ({gene}). "
                        "(Result from consumer DNA test — not a clinical genetics lab.)"
                    ),
                    clinician_action=(
                        "Bring this to the prescribing doctor's attention. "
                        "They can review whether the observed medication response "
                        "matches this genetic result and whether follow-up testing is needed."
                    ),
                )
            )
    return alerts


def _build_age_cautions(
    person_id: str,
    vault_root: Path,
) -> list[MedicationSafetyAlert]:
    """Flag age-related medication review prompts from the person's birth year."""
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.is_file():
        return []

    import re

    content = person_note.read_text(encoding="utf-8")
    match = re.search(r"birth_year:\s*(\d{4})", content)
    if not match:
        return []

    birth_year = int(match.group(1))
    import datetime as dt

    age = dt.datetime.now(dt.UTC).year - birth_year
    if age < 65:
        return []

    return [
        MedicationSafetyAlert(
            alert_type="Age-related caution",
            review_priority="review annually",
            reason=f"Person is approximately {age} years old. Standard prescribing "
            "guidance for older adults (Beers criteria, renal-adjusted dosing, "
            "polypharmacy review) may apply.",
            clinician_action=(
                "Discuss age-appropriate medication review with prescriber. "
                "This is a reminder, not a clinical assessment."
            ),
        )
    ]


# Anticholinergic Cognitive Burden (ACB) scores from the 2023 AGS Beers Criteria
# and ACB scale. Score 1 = mild, 2 = moderate, 3 = strongly anticholinergic.
# Includes common brand names as aliases. Bounded list — not exhaustive.
_ANTICHOLINERGIC_DRUGS: dict[str, int] = {
    # Score 3 — strongly anticholinergic
    "amitriptyline": 3,
    "elavil": 3,
    "nortriptyline": 3,
    "pamelor": 3,
    "imipramine": 3,
    "doxepin": 3,
    "diphenhydramine": 3,
    "benadryl": 3,
    "chlorpheniramine": 3,
    "brompheniramine": 3,
    "oxybutynin": 3,
    "ditropan": 3,
    "tolterodine": 3,
    "detrol": 3,
    "solifenacin": 3,
    "vesicare": 3,
    "trospium": 3,
    "scopolamine": 3,
    "hyoscyamine": 3,
    "dicyclomine": 3,
    "benztropine": 3,
    "trihexyphenidyl": 3,
    # Score 2 — moderately anticholinergic
    "paroxetine": 2,
    "paxil": 2,
    "promethazine": 2,
    "phenergan": 2,
    "cyclobenzaprine": 2,
    "flexeril": 2,
    "olanzapine": 2,
    "zyprexa": 2,
    "clozapine": 2,
    "meclizine": 2,
    # Score 1 — mildly anticholinergic
    "quetiapine": 1,
    "seroquel": 1,
    "mirtazapine": 1,
    "remeron": 1,
}

# CNS depressant / sedation burden tiers from the 2023 AGS Beers Criteria.
# Tier 1 = mild, 2 = moderate, 3 = high. Includes common brand names.
# Bounded list — not exhaustive.
_SEDATION_DRUGS: dict[str, int] = {
    # Tier 3 — high sedation (benzodiazepines, z-drugs, opioids)
    "diazepam": 3,
    "valium": 3,
    "lorazepam": 3,
    "ativan": 3,
    "alprazolam": 3,
    "xanax": 3,
    "clonazepam": 3,
    "klonopin": 3,
    "temazepam": 3,
    "restoril": 3,
    "triazolam": 3,
    "zolpidem": 3,
    "ambien": 3,
    "eszopiclone": 3,
    "lunesta": 3,
    "zaleplon": 3,
    "morphine": 3,
    "oxycodone": 3,
    "oxycontin": 3,
    "hydrocodone": 3,
    "vicodin": 3,
    "norco": 3,
    "fentanyl": 3,
    "methadone": 3,
    "buprenorphine": 3,
    # Tier 2 — moderate sedation
    "hydroxyzine": 2,
    "vistaril": 2,
    "atarax": 2,
    "gabapentin": 2,
    "neurontin": 2,
    "pregabalin": 2,
    "lyrica": 2,
    "carisoprodol": 2,
    "soma": 2,
    "methocarbamol": 2,
    "robaxin": 2,
    "tizanidine": 2,
    "zanaflex": 2,
    # Tier 1 — mild sedation
    "diphenhydramine": 1,
    "benadryl": 1,
    "mirtazapine": 1,
    "remeron": 1,
}

_INTERACTION_ALERT_TYPES: frozenset[str] = frozenset(
    {
        "Anticholinergic burden",
        "Sedation burden",
    }
)


def _normalize_burden_name(name: str) -> str:
    import re

    name = name.casefold().strip()
    name = re.sub(r"\s*\([^)]*\)", "", name)  # strip parenthetical (brand/alias)
    name = re.sub(r"\s+\d+.*$", "", name)  # strip dose suffix
    return name.strip()


def _build_burden_alerts(medications_body: str) -> list[MedicationSafetyAlert]:
    """Compute cumulative anticholinergic and sedation burden from active medications."""
    raw_names = _extract_medication_names(medications_body)

    acb_hits: list[tuple[str, int]] = []
    sed_hits: list[tuple[str, int]] = []
    for raw in raw_names:
        norm = _normalize_burden_name(raw)
        acb = _ANTICHOLINERGIC_DRUGS.get(norm)
        if acb:
            acb_hits.append((raw, acb))
        sed = _SEDATION_DRUGS.get(norm)
        if sed:
            sed_hits.append((raw, sed))

    alerts: list[MedicationSafetyAlert] = []

    if acb_hits:
        total = sum(s for _, s in acb_hits)
        contributors = "; ".join(f"{name}: ACB {score}" for name, score in acb_hits)
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Anticholinergic burden",
                review_priority="E2/established-reference",
                reason=(
                    f"Total anticholinergic burden score: {total}. "
                    f"Contributing medications: {contributors}. "
                    "(2023 AGS Beers Criteria / Anticholinergic Cognitive Burden scale)"
                ),
                clinician_action=(
                    "Review anticholinergic load with prescriber, particularly for older adults. "
                    "Cumulative burden increases fall and cognitive impairment risk. "
                    "This is a screening flag, not a medication change recommendation."
                ),
            )
        )

    if sed_hits:
        total = sum(s for _, s in sed_hits)
        contributors = "; ".join(f"{name}: tier {score}" for name, score in sed_hits)
        alerts.append(
            MedicationSafetyAlert(
                alert_type="Sedation burden",
                review_priority="E2/established-reference",
                reason=(
                    f"Total sedation burden tier: {total}. "
                    f"Contributing medications: {contributors}. "
                    "(2023 AGS Beers Criteria / CNS depressant risk classification)"
                ),
                clinician_action=(
                    "Review cumulative CNS depressant load with prescriber. "
                    "Multiple sedating medications increase fall, impairment, and overdose risk. "
                    "This is a screening flag, not a medication change recommendation."
                ),
            )
        )

    return alerts


def build_medication_safety_alerts(
    *,
    reports: list[ReportInput],
    reports_by_key: dict[str, ReportInput],
    findings: list[CurrentFinding],
    person_id: str,
    vault_root: Path,
) -> list[MedicationSafetyAlert]:
    """Build conservative review alerts from existing reports and findings."""
    medications_body = reports_by_key["current_medications"].body
    genetics_body = reports_by_key["genetics"].body
    return (
        _build_source_alerts(reports)
        + _build_conflict_alerts(reports_by_key)
        + _build_duplicate_therapy_alerts(medications_body)
        + _build_phenotype_alerts(genetics_body)
        + _build_burden_alerts(medications_body)
        + _build_age_cautions(person_id, vault_root)
        + _build_finding_alerts(findings)
    )


def _render_alert_table(
    alerts: list[MedicationSafetyAlert],
    *,
    empty: str = "_No deterministic medication-safety alerts were generated from current sources._",
) -> str:
    if not alerts:
        return empty

    lines = [
        "| Alert | Review Priority | Reason | Clinician Action |",
        "| --- | --- | --- | --- |",
    ]
    for alert in alerts:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(alert.alert_type),
                    markdown_table_cell(alert.review_priority),
                    markdown_table_cell(alert.reason),
                    markdown_table_cell(alert.clinician_action),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_medication_safety_alerts(
    *,
    person_id: str,
    display_name: str,
    generated_iso: str,
    core_reports: list[ReportInput],
    context_reports: list[ReportInput],
    findings: list[CurrentFinding],
    vault_root: Path,
) -> str:
    reports = core_reports + context_reports
    reports_by_key = {report.key: report for report in reports}
    relevant_findings = _relevant_findings(findings, person_id)
    alerts = build_medication_safety_alerts(
        reports=reports,
        reports_by_key=reports_by_key,
        findings=relevant_findings,
        person_id=person_id,
        vault_root=vault_root,
    )
    missing_reports = [report.key for report in reports if not report.exists]
    stale_reports = [report.key for report in reports if is_stale(report)]
    unreviewed_reports = [report.key for report in reports if needs_review(report)]
    source_reports = source_report_paths(reports)

    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "medication_safety_alerts",
        "generated_at": generated_iso,
        "source_reports": source_reports,
        "missing_reports": missing_reports,
        "stale_reports": stale_reports,
        "unreviewed_reports": unreviewed_reports,
        "active_findings_count": len(relevant_findings),
    }

    current_medications = reports_by_key["current_medications"]
    medication_conflicts = reports_by_key["medication_conflicts"]
    genetics = reports_by_key["genetics"]
    latest_labs = reports_by_key["lab_results"]
    rendered_findings = render_findings(
        relevant_findings,
        empty_message="_No active medication or PGx findings._",
    )
    interaction_alerts = [a for a in alerts if a.alert_type in _INTERACTION_ALERT_TYPES]
    non_interaction_alerts = [a for a in alerts if a.alert_type not in _INTERACTION_ALERT_TYPES]
    rendered_interactions = _render_alert_table(
        interaction_alerts,
        empty="_No drug interaction or burden alerts detected._",
    )

    return (
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n\n"
        f"# Medication Safety Alerts - {display_name}\n\n"
        "[[_index|<- Reports]]\n\n"
        "> [!warning] Clinical Safety\n"
        "> These are conservative caregiver-prepared review alerts from existing vault "
        "reports and active findings. They are not medical advice, do not recommend "
        "specific dose changes, and do not replace clinical pharmacogenetic assessment. Contact a "
        "healthcare professional for urgent symptoms, high-risk results, medication decisions, or "
        "treatment changes.\n\n"
        "## Alert Snapshot\n\n"
        f"- Person: [[{person_id}|{display_name}]]\n"
        f"- Generated at: {generated_iso}\n"
        f"- Deterministic alerts: {len(alerts)}\n"
        f"- Medication/PGx findings included: {len(relevant_findings)}\n"
        f"- Missing source reports: {', '.join(missing_reports) if missing_reports else 'none'}\n"
        f"- Stale source reports: {', '.join(stale_reports) if stale_reports else 'none'}\n"
        f"- Unreviewed source reports: "
        f"{', '.join(unreviewed_reports) if unreviewed_reports else 'none'}\n\n"
        "## Drug Interaction Alerts\n\n"
        f"{rendered_interactions}\n\n"
        "## Review Alerts\n\n"
        f"{_render_alert_table(non_interaction_alerts)}\n\n"
        "## Source Reports\n\n"
        + "\n".join(
            report_reference_line(r)
            for r in [current_medications, medication_conflicts, genetics, latest_labs]
        )
        + "\n\n"
        "## Active Medication/PGx Findings\n\n"
        f"{rendered_findings}\n\n"
        "## Boundaries\n\n"
        "- Source-backed PGx phenotype/Function prompts and burden alerts are conservative "
        "review prompts; they do not recommend specific dose changes or alternative medications.\n"
        "- Anticholinergic and sedation burden scores are cumulative screening estimates; actual "
        "clinical impact depends on dosing, renal function, and individual patient factors.\n"
        "- Dosing recommendations and drug-drug interaction severity scoring remain out of scope.\n"
        "- Resolve source conflicts and stale reports before relying on the alert snapshot.\n"
    )


def write_medication_safety_alerts(
    vault_root: Path,
    person_id: str,
    *,
    findings_ledger_path: Path | None = FINDINGS_LEDGER_PATH,
    generated_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[Path, int]:
    report_dir = reports_dir(vault_root, person_id)
    if not report_dir.is_dir():
        raise MedicationSafetyAlertsError(f"reports directory not found for {person_id}")
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.is_file():
        raise MedicationSafetyAlertsError(f"person note not found for {person_id}")

    generated = generated_at or datetime.now(UTC)
    generated_iso = generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    display_name = load_person_display_name(vault_root, person_id)
    if conn is not None:
        write_current_medications_report(vault_root, person_id, conn)
    core_reports = [
        load_report(vault_root, person_id, key, filename, title)
        for key, filename, title in _CORE_REPORTS
    ]
    context_reports = [
        load_report(vault_root, person_id, key, filename, title)
        for key, filename, title in _CONTEXT_REPORTS
    ]
    findings = active_findings(findings_ledger_path) if findings_ledger_path is not None else []

    report_path = report_dir / "medication-safety-alerts.md"
    report_path.write_text(
        render_medication_safety_alerts(
            person_id=person_id,
            display_name=display_name,
            generated_iso=generated_iso,
            core_reports=core_reports,
            context_reports=context_reports,
            findings=findings,
            vault_root=vault_root,
        ),
        encoding="utf-8",
    )
    write_category_index_note(
        report_dir,
        person_id,
        "Reports",
        note_label="report",
        display_name=display_name,
    )
    return report_path, len(_relevant_findings(findings, person_id))
