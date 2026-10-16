"""Shared helpers for composing derived reports from existing vault reports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from famed.findings_ledger import CurrentFinding
from famed.report_rendering import (
    markdown_table_cell,
    split_front_matter,
    strip_leading_report_chrome,
)


@dataclass(frozen=True)
class ReportInput:
    key: str
    filename: str
    title: str
    path: Path
    source_path: str
    vault_link: str
    exists: bool
    front_matter: dict[str, Any]
    body: str


def reports_dir(vault_root: Path, person_id: str) -> Path:
    return vault_root / "Records" / person_id / "Reports"


def load_report(
    vault_root: Path,
    person_id: str,
    key: str,
    filename: str,
    title: str,
) -> ReportInput:
    path = reports_dir(vault_root, person_id) / filename
    source_path = f"Records/{person_id}/Reports/{filename}"
    link_path = str(Path(source_path).with_suffix(""))
    if not path.exists():
        return ReportInput(
            key=key,
            filename=filename,
            title=title,
            path=path,
            source_path=source_path,
            vault_link=link_path,
            exists=False,
            front_matter={},
            body="",
        )

    markdown = path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(markdown)
    return ReportInput(
        key=key,
        filename=filename,
        title=title,
        path=path,
        source_path=source_path,
        vault_link=link_path,
        exists=True,
        front_matter=front_matter,
        body=strip_leading_report_chrome(body),
    )


def is_stale(report: ReportInput) -> bool:
    if not report.exists:
        return False
    values = {
        str(report.front_matter.get("processing_state", "")).casefold(),
        str(report.front_matter.get("status", "")).casefold(),
    }
    return "stale" in values


def needs_review(report: ReportInput) -> bool:
    if not report.exists:
        return False
    return (
        "needs_review" in str(report.front_matter.get("verification_status", "")).casefold()
        or "Status: needs_review" in report.body
        or "[UNVERIFIED]" in report.body
    )


def has_conflict_rows(report: ReportInput) -> bool:
    if not report.exists:
        return False
    body = report.body.casefold()
    if "no conflicting" in body or "no lab conflicts" in body or "no conflicts" in body:
        return False
    return "| --- |" in report.body or "conflict" in body


def report_reference_line(report: ReportInput) -> str:
    """Return a compact wikilink reference line with optional status indicators."""
    if not report.exists:
        return f"- {report.title} — _missing_"
    status: list[str] = []
    if is_stale(report):
        status.append("stale")
    if needs_review(report):
        status.append("needs review")
    suffix = f" — _{', '.join(status)}_" if status else ""
    return f"- [[{report.vault_link}|{report.title}]]{suffix}"


def vault_link_for_source_path(source_path: str) -> str:
    path = Path(source_path)
    if path.suffix == ".md":
        stem_path = path.with_suffix("").as_posix()
        return f"[[{stem_path}|{path.name}]]"
    return f"`{source_path}`"


def source_report_paths(reports: list[ReportInput]) -> list[str]:
    return [report.source_path for report in reports if report.exists]


_GENE_FINDING_RE = re.compile(r"^(?P<prefix>.+?) for (?P<gene>[A-Z0-9]+); (?P<suffix>.+)$")


def is_informational_finding(statement: str) -> bool:
    """Return True for findings that are informational rather than actionable.

    Informational findings (normal metabolizer results, no local CPIC match) are
    sorted after actionable findings so caregivers see what needs attention first.
    """
    lowered = statement.casefold()
    return "normal metabolizer" in lowered or "no cpic match" in lowered


def is_normal_metabolizer_pgx_finding(finding: CurrentFinding) -> bool:
    return (
        finding.domain.casefold() == "pgx_coverage"
        and "normal metabolizer" in finding.statement.casefold()
    )


def _display_finding_rows(findings: list[CurrentFinding]) -> list[tuple[str, str, str, str]]:
    rows_by_key: dict[tuple[str, str, float, str, str], set[str]] = {}
    plain_rows: list[tuple[str, str, str, str]] = []
    for finding in sorted(findings, key=lambda item: (item.domain, item.statement)):
        evidence = f"{finding.evidence_tier}/{finding.evidence_check}"
        confidence = round(finding.confidence, 2)
        match = _GENE_FINDING_RE.match(finding.statement)
        if match:
            key = (
                finding.domain,
                evidence,
                confidence,
                match.group("prefix"),
                match.group("suffix"),
            )
            rows_by_key.setdefault(key, set()).add(match.group("gene"))
            continue
        plain_rows.append(
            (finding.domain, evidence, f"{finding.confidence:.2f}", finding.statement)
        )

    grouped_rows = [
        (
            domain,
            evidence,
            f"{confidence:.2f}",
            f"{prefix} for {', '.join(sorted(genes))}; {suffix}",
        )
        for (domain, evidence, confidence, prefix, suffix), genes in rows_by_key.items()
    ]
    return sorted(
        plain_rows + grouped_rows,
        key=lambda row: (is_informational_finding(row[3]), row[0], row[3]),
    )


def display_finding_count(findings: list[CurrentFinding]) -> int:
    return len(_display_finding_rows(findings))


def render_findings(findings: list[CurrentFinding], *, empty_message: str) -> str:
    if not findings:
        return empty_message

    lines = [
        "| Domain | Evidence | Confidence | Finding |",
        "| --- | --- | --- | --- |",
    ]
    for domain, evidence, confidence, statement in _display_finding_rows(findings):
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(domain),
                    markdown_table_cell(evidence),
                    confidence,
                    markdown_table_cell(statement),
                ]
            )
            + " |"
        )
    return "\n".join(lines)
