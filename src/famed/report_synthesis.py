"""Shared synthesis-section helpers for ADR-026 advice reports."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from famed.findings_ledger import EvidenceTier
from famed.report_rendering import render_yaml_front_matter, split_front_matter
from famed.report_sources import reports_dir, vault_link_for_source_path
from famed.synthesis_contract import (
    SynthesisSection,
    SynthesisStatus,
    SynthesizedInsight,
    validate_insight,
)

SYNTHESIS_HEADING = "## Synthesized Insights"
DEFAULT_SYNTHESIS_INSERT_BEFORE: tuple[str, ...] = ("## Source Reports",)
NO_SYNTHESIS_PLACEHOLDER = "_No live-agent-synthesized cross-domain insights yet._"


def iso_z_timestamp(value: datetime) -> str:
    """Return the repo-standard UTC ISO timestamp with a trailing ``Z``."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def insight_from_dict(data: dict[str, Any]) -> tuple[SynthesizedInsight, EvidenceTier | None]:
    """Parse one raw agent-authored candidate dict into the validation contract."""
    insight = SynthesizedInsight(
        statement=str(data["statement"]),
        linked_evidence=tuple(data.get("linked_evidence", [])),
        domains_linked=tuple(data.get("domains_linked", [])),
        evidence_tier=data["evidence_tier"],
        confidence=float(data["confidence"]),
    )
    return insight, data.get("weakest_cited_tier")


OUT_OF_DATE_SYNTHESIS_DAYS = 30


def _synthesis_age_days(generated_at: str, now: datetime | None) -> int | None:
    if now is None:
        return None
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    return (now - generated).days


def render_stale_banner(generated_at: str, now: datetime | None = None) -> str:
    """Stale-synthesis banner; past 30 days it becomes a dated warning (ADR-031)."""
    age = _synthesis_age_days(generated_at, now)
    if age is not None and age > OUT_OF_DATE_SYNTHESIS_DAYS:
        return (
            f"> [!warning] Out-of-date synthesis ({age} days old)\n"
            f"> Generated {generated_at} by a live agent. The records have been updated"
            " since; re-run this report with a live agent before relying on these insights.\n"
        )
    return (
        f"> [!note] Stale synthesis\n"
        f"> Generated {generated_at} by a live agent; not refreshed on"
        f" this unattended report regeneration.\n"
    )


def _is_synthesis_banner_line(line: str) -> bool:
    return line.startswith(("> [!note] Stale synthesis", "> [!warning] Out-of-date synthesis"))


def render_synthesis_section(section: SynthesisSection) -> str:
    lines = [SYNTHESIS_HEADING, ""]
    if section.status == "stale":
        lines.append(render_stale_banner(section.generated_at))
    if not section.insights:
        lines.append(NO_SYNTHESIS_PLACEHOLDER)
        return "\n".join(lines)
    for insight in section.insights:
        citations = ", ".join(vault_link_for_source_path(path) for path in insight.linked_evidence)
        domains = ", ".join(insight.domains_linked)
        lines.append(
            f"- {insight.statement} _({domains}; {insight.evidence_tier}, "
            f"confidence {insight.confidence:.2f}; {citations})_"
        )
    return "\n".join(lines)


def find_synthesis_section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return start/end line indices for an existing synthesis section."""
    start = next((i for i, line in enumerate(lines) if line.strip() == SYNTHESIS_HEADING), None)
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return start, end


def splice_synthesis_section(
    body: str,
    rendered_section: str,
    *,
    insert_before_headings: tuple[str, ...] = DEFAULT_SYNTHESIS_INSERT_BEFORE,
) -> str:
    """Insert or replace a rendered synthesis section in a Markdown report body."""
    lines = body.splitlines()
    bounds = find_synthesis_section_bounds(lines)
    if bounds is None:
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if any(line.strip() == heading for heading in insert_before_headings)
            ),
            len(lines),
        )
        end = start
    else:
        start, end = bounds
    return "\n".join([*lines[:start], rendered_section, "", *lines[end:]])


def write_synthesis_section(
    report_path: Path,
    *,
    status: SynthesisStatus,
    generated_at: str,
    rendered_section: str,
    insert_before_headings: tuple[str, ...] = DEFAULT_SYNTHESIS_INSERT_BEFORE,
) -> None:
    """Splice *rendered_section* into *report_path* and update front matter."""
    front_matter, body = split_front_matter(report_path.read_text(encoding="utf-8"))
    front_matter["synthesis_status"] = status
    front_matter["synthesis_generated_at"] = generated_at
    new_body = splice_synthesis_section(
        body, rendered_section, insert_before_headings=insert_before_headings
    )
    report_path.write_text(
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n{new_body}",
        encoding="utf-8",
    )


def persist_report_synthesized_insights(
    vault_root: Path,
    person_id: str,
    *,
    report_filename: str,
    candidates: list[dict[str, Any]],
    insert_before_headings: tuple[str, ...] = DEFAULT_SYNTHESIS_INSERT_BEFORE,
    generated_at: str | None = None,
) -> tuple[SynthesisSection, tuple[str, ...]]:
    """Validate live-agent-authored candidates and persist accepted insights."""
    report_path = reports_dir(vault_root, person_id) / report_filename
    if not report_path.is_file():
        raise FileNotFoundError(f"{report_path} does not exist yet; generate the report first")

    generated = generated_at or iso_z_timestamp(datetime.now(UTC))
    accepted: list[SynthesizedInsight] = []
    rejections: list[str] = []
    for raw in candidates:
        try:
            insight, weakest_cited_tier = insight_from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            rejections.append(f"{raw!r}: malformed candidate ({exc})")
            continue
        reasons = validate_insight(
            insight, vault_root=vault_root, weakest_cited_tier=weakest_cited_tier
        )
        if reasons:
            rejections.append(f"{insight.statement!r}: {'; '.join(reasons)}")
        else:
            accepted.append(insight)

    section = SynthesisSection(
        person_id=person_id,
        insights=tuple(accepted),
        generated_at=generated,
        status="current",
    )
    write_synthesis_section(
        report_path,
        status=section.status,
        generated_at=section.generated_at,
        rendered_section=render_synthesis_section(section),
        insert_before_headings=insert_before_headings,
    )
    return section, tuple(rejections)


def extract_synthesis_carryover(report_path: Path) -> tuple[str, str] | None:
    """Return existing rendered synthesis content and its generated timestamp."""
    if not report_path.is_file():
        return None
    front_matter, body = split_front_matter(report_path.read_text(encoding="utf-8"))
    generated_at = front_matter.get("synthesis_generated_at")
    if generated_at is None:
        return None
    lines = body.splitlines()
    bounds = find_synthesis_section_bounds(lines)
    if bounds is None:
        return None
    start, end = bounds
    section_lines = lines[start + 1 : end]
    while section_lines and not section_lines[0].strip():
        section_lines.pop(0)
    if section_lines and _is_synthesis_banner_line(section_lines[0]):
        while section_lines and section_lines[0].startswith(">"):
            section_lines.pop(0)
        while section_lines and not section_lines[0].strip():
            section_lines.pop(0)
    while section_lines and not section_lines[-1].strip():
        section_lines.pop()
    return "\n".join(section_lines), str(generated_at)


# Reports retired by ADR-031 and the lab-report consolidation, mapped to the
# report that now holds the same evidence. Carried-over insights keep their
# wording; only citations to a retired report are pointed at its successor.
REPORT_SUCCESSORS: dict[str, str] = {
    "latest-lab-results": "lab-results",
    "lab-trends": "lab-results",
    "pgx-coverage": "genetics",
    "pgx-wallet-card": "genetics",
    "carrier-screening": "genetics",
    "dna-profile-summary": "genetics",
}
_RETIRED_REPORT_LINK_RE = re.compile(
    r"(Reports/)(" + "|".join(map(re.escape, REPORT_SUCCESSORS)) + r")(?=(?:\.md)?[|\]#])"
)


def _repoint_retired_report_links(content: str) -> str:
    return _RETIRED_REPORT_LINK_RE.sub(
        lambda m: m.group(1) + REPORT_SUCCESSORS[m.group(2)], content
    )


def carry_over_synthesis_as_stale(
    report_path: Path,
    carryover: tuple[str, str] | None,
    *,
    insert_before_headings: tuple[str, ...] = DEFAULT_SYNTHESIS_INSERT_BEFORE,
    now: datetime | None = None,
) -> None:
    """Re-splice prior synthesis content into a freshly regenerated report as stale."""
    if carryover is None:
        return
    content, insight_generated_at = carryover
    content = _repoint_retired_report_links(content)
    rendered = "\n".join(
        [SYNTHESIS_HEADING, "", render_stale_banner(insight_generated_at, now), content]
    )
    write_synthesis_section(
        report_path,
        status="stale",
        generated_at=insight_generated_at,
        rendered_section=rendered,
        insert_before_headings=insert_before_headings,
    )
