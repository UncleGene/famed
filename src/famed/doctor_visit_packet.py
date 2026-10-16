#!/usr/bin/env python3
"""Printable doctor-visit packet composed from existing vault reports (ADR-031).

The packet is the one doctor-facing artifact: a curated summary of about two
printed pages. Each section reads the vault report that owns the data and links
back to it; whole sub-report bodies are never inlined (REQ-016, ADR-031).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from famed.allergies import render_allergy_section
from famed.lab_sync import lab_range_distance, write_category_index_note
from famed.person_sync import load_person_display_name, load_person_from_note
from famed.report_rendering import (
    markdown_table_cell,
    render_yaml_front_matter,
    split_front_matter,
    strip_leading_report_chrome,
)


class DoctorVisitPacketError(ValueError):
    """Raised when a doctor-visit packet cannot be generated safely."""


# (key, filename, title); the first three are core inputs.
_SOURCE_REPORTS: tuple[tuple[str, str, str], ...] = (
    ("medical_history_summary", "medical-history-summary.md", "Medical history summary"),
    ("current_medications", "current-medications.md", "Current medications"),
    ("health_advisory", "health-advisory.md", "Health advisory"),
    ("lab_results", "lab-results.md", "Lab results"),
    ("flagged_for_discussion", "flagged-for-discussion.md", "Flagged for discussion"),
    ("medication_safety_alerts", "medication-safety-alerts.md", "Medication safety alerts"),
    ("genetics", "genetics.md", "Genetics"),
    ("immunizations", "immunizations.md", "Immunizations"),
    ("pending_review", "pending-review.md", "Pending review"),
    ("lab_conflicts", "lab-conflicts.md", "Lab conflicts"),
    ("medication_conflicts", "medication-conflicts.md", "Medication conflicts"),
)
_CORE_KEYS = ("medical_history_summary", "current_medications", "health_advisory")
_PACKET_SUBDIR = "doctor-visit-packets"
_PACKET_INDEX_NAME = "_index.md"
_PACKET_SUFFIX = "-doctor-visit-packet"
_MAX_LABS = 12
_MAX_IMMUNIZATIONS = 5


@dataclass(frozen=True)
class ReportInput:
    key: str
    filename: str
    title: str
    path: Path
    vault_link: str
    exists: bool
    front_matter: dict[str, Any]
    body: str

    @property
    def link(self) -> str:
        return f"[[{self.vault_link}|{self.title}]]"


@dataclass(frozen=True)
class LabObservation:
    name: str
    latest: str
    unit: str
    reference_range: str
    trend: str
    in_range: bool | None
    previous: str
    date: str
    severity: float


def _reports_dir(vault_root: Path, person_id: str) -> Path:
    return vault_root / "Records" / person_id / "Reports"


def _packet_dir(vault_root: Path, person_id: str) -> Path:
    return _reports_dir(vault_root, person_id) / _PACKET_SUBDIR


def _validate_allowed_packet_dir(vault_root: Path, person_id: str, target_dir: Path) -> None:
    allowed = _packet_dir(vault_root, person_id).resolve()
    resolved = target_dir.resolve()
    if resolved != allowed:
        raise DoctorVisitPacketError(
            "doctor-visit packet output must stay under "
            f"Records/{person_id}/Reports/{_PACKET_SUBDIR}/"
        )


def _load_report(
    vault_root: Path,
    person_id: str,
    key: str,
    filename: str,
    title: str,
) -> ReportInput:
    path = _reports_dir(vault_root, person_id) / filename
    link_path = f"Records/{person_id}/Reports/{path.stem}"
    if not path.exists():
        return ReportInput(key, filename, title, path, link_path, False, {}, "")
    front_matter, body = split_front_matter(path.read_text(encoding="utf-8"))
    return ReportInput(
        key, filename, title, path, link_path, True, front_matter, strip_leading_report_chrome(body)
    )


def _section(body: str, heading: str) -> str:
    """Return the text under ``heading`` up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = body.splitlines()
    for start, line in enumerate(lines):
        if line.strip() == heading:
            collected: list[str] = []
            for following in lines[start + 1 :]:
                stripped = following.lstrip()
                if stripped.startswith("#"):
                    following_level = len(stripped) - len(stripped.lstrip("#"))
                    if following_level <= level:
                        break
                collected.append(following)
            return "\n".join(collected).strip()
    return ""


def _table_rows(text: str) -> list[dict[str, str]]:
    """Parse every pipe table in ``text`` into header-keyed rows."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            header = [cell.casefold() for cell in cells]
            continue
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


def _load_person(vault_root: Path, person_id: str) -> dict[str, Any]:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.exists():
        raise DoctorVisitPacketError(f"person note not found for {person_id}")
    return load_person_from_note(person_note)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _identity_lines(
    person_id: str, display_name: str, person: dict[str, Any], generated: datetime
) -> list[str]:
    details = [f"[[{person_id}|{display_name}]]"]
    birth_year = person.get("birth_year")
    if isinstance(birth_year, int):
        details.append(f"born {birth_year} (about {generated.year - birth_year})")
    sex = person.get("sex")
    if isinstance(sex, str) and sex.strip():
        details.append(sex.strip())
    return [
        f"- Patient: {', '.join(details)}",
        f"- Prepared: {generated.date().isoformat()} from the famed vault (caregiver-prepared, "
        "not a clinical record)",
    ]


def _problem_lines(history: ReportInput) -> list[str]:
    if not history.exists:
        return ["_Medical history summary not available yet._"]
    digest = [line for line in _section(history.body, "## Digest").splitlines() if line.strip()]
    if not digest:
        return ["_No problem list in the medical history summary._"]
    cleaned = [re.sub(r" \[\[#[^\]]*\]\]", "", line) for line in digest]
    return [*cleaned, "", f"Detail: {history.link}"]


def _medication_lines(medications: ReportInput) -> list[str]:
    if not medications.exists:
        return ["_Current medications report not available yet._"]
    rows = _table_rows(medications.body)
    if not rows:
        return ["_No current medications recorded._", "", f"Detail: {medications.link}"]
    lines = ["| Medication | Dose | How often | Refills through |", "| --- | --- | --- | --- |"]
    for row in rows:
        name = row.get("medication", "")
        refill = row.get("refills through", "")
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(value)
                for value in (name, row.get("dose", ""), row.get("frequency", ""), refill)
            )
            + " |"
        )
    return [*lines, "", f"Detail: {medications.link}"]


_LAB_LINE_RE = re.compile(r"^- \*\*(?P<name>.+?)\*\* — (?P<fields>.+)$")
_LAB_FIELD_RE = re.compile(r"\*\*(?P<key>[^*]+):\*\* (?P<value>[^·]+)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_number(text: str) -> float | None:
    match = _NUMBER_RE.search(text.replace(",", ""))
    return float(match.group()) if match else None


def _range_severity(latest: float | None, reference_range: str, in_range: bool | None) -> float:
    """Shared distance-from-range measure (``lab_range_distance``) on a rendered range."""
    if in_range is not False:
        return 0.0
    bounds = [float(n) for n in _NUMBER_RE.findall(reference_range.replace(",", ""))]
    if len(bounds) >= 2:
        return lab_range_distance(latest, bounds[0], bounds[1])
    if len(bounds) == 1:
        if reference_range.lstrip().startswith(("≥", ">")):
            return lab_range_distance(latest, bounds[0], None)
        return lab_range_distance(latest, None, bounds[0])
    return 0.0


def _lab_observations(advisory: ReportInput) -> list[LabObservation]:
    observations: list[LabObservation] = []
    for line in _section(advisory.body, "## Lab Observations").splitlines():
        match = _LAB_LINE_RE.match(line.strip())
        if match is None:
            continue
        fields = {
            field.group("key").strip().casefold(): field.group("value").strip()
            for field in _LAB_FIELD_RE.finditer(match.group("fields"))
        }
        latest_text = fields.get("latest", "")
        latest_number = _parse_number(latest_text)
        unit = latest_text[latest_text.find(" ") + 1 :] if " " in latest_text else ""
        in_range_text = fields.get("in range", "").casefold()
        in_range = True if in_range_text == "yes" else False if in_range_text == "no" else None
        reference_range = fields.get("reference range", "")
        observations.append(
            LabObservation(
                name=match.group("name"),
                latest=latest_text.split(" ")[0] if latest_text else "",
                unit=unit,
                reference_range=reference_range,
                trend=fields.get("trend", ""),
                in_range=in_range,
                previous=fields.get("previous", ""),
                date=fields.get("date", ""),
                severity=_range_severity(latest_number, reference_range, in_range),
            )
        )
    # Out-of-range first, furthest from range first; then changing in-range values.
    return sorted(
        observations,
        key=lambda obs: (obs.in_range is not False, -obs.severity, obs.name.casefold()),
    )


def _lab_lines(
    advisory: ReportInput, labs: list[LabObservation], results: ReportInput
) -> list[str]:
    if not advisory.exists:
        return ["_Health advisory not available yet; see lab reports._"]
    if not labs:
        return ["_No out-of-range or changing lab results flagged._"]
    lines = [
        "| Test | Latest | Range | Prior | Trend | Date |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for obs in labs[:_MAX_LABS]:
        flag = " **(out of range)**" if obs.in_range is False else ""
        latest = f"{obs.latest} {obs.unit}".strip() + flag
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(value)
                for value in (
                    obs.name,
                    latest,
                    obs.reference_range,
                    obs.previous,
                    obs.trend,
                    obs.date,
                )
            )
            + " |"
        )
    if len(labs) > _MAX_LABS:
        lines.append(f"\n_{len(labs) - _MAX_LABS} more flagged result(s) in the detail reports._")
    detail = results.link if results.exists else advisory.link
    return [*lines, "", f"Detail: {detail}"]


def _vital_lines(history: ReportInput) -> list[str]:
    snapshot = _section(history.body, "## Vital Signs Snapshot") if history.exists else ""
    rows = _table_rows(snapshot)
    if not rows:
        return ["_No vital signs recorded._"]
    lines = ["| Measurement | Latest | Date |", "| --- | --- | --- |"]
    for row in rows:
        lines.append(
            f"| {row.get('measurement', '')} | {row.get('latest', '')} | {row.get('date', '')} |"
        )
    return lines


def _pgx_lines(genetics: ReportInput) -> list[str]:
    if not genetics.exists:
        return ["_No DNA-based medication results._"]
    rows = [
        row
        for row in _table_rows(_section(genetics.body, "## Medication Genes (wallet card)"))
        if row.get("gene")
    ]
    if not rows:
        return ["_No gene phenotypes interpreted from the available DNA data._"]
    # Print what matters: non-normal results and genes for current medications.
    shown, normal = [], []
    for row in rows:
        medications = row.get("affects current medications", "—")
        if not row["result"].startswith("normal") or medications not in ("", "—"):
            suffix = f" (affects {medications})" if medications not in ("", "—") else ""
            shown.append(f"- {row['gene']}: {row['result']}{suffix}")
        else:
            normal.append(row["gene"])
    lines = shown + ([f"- Other genes normal: {', '.join(normal)}"] if normal else [])
    return [
        *lines,
        "- Consumer genotyping, not clinical-grade testing; confirm before acting.",
        "",
        f"Detail: {genetics.link}",
    ]


def _immunization_lines(immunizations: ReportInput) -> list[str]:
    rows = _table_rows(immunizations.body) if immunizations.exists else []
    if not rows:
        return ["_No immunizations recorded._"]
    latest = sorted(rows, key=lambda row: row.get("administered_date", ""), reverse=True)
    lines = [
        f"- {row.get('administered_date', '') or 'undated'}: {row.get('vaccine_name', '')}"
        for row in latest[:_MAX_IMMUNIZATIONS]
    ]
    return [*lines, "", f"Detail: {immunizations.link}"]


def _family_lines(advisory: ReportInput) -> list[str]:
    section = _section(advisory.body, "## Family History Context") if advisory.exists else ""
    lines = [
        line.rstrip()
        for line in section.splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]
    return lines or ["_No blood-relative family history recorded._"]


def _pending_review_count(pending: ReportInput) -> int:
    value = pending.front_matter.get("pending_review_count") if pending.exists else None
    return value if isinstance(value, int) else 0


def _has_conflicts(report: ReportInput) -> bool:
    return report.exists and "no conflicting" not in report.body.casefold()


def _calcium_lines(flagged: ReportInput) -> list[str]:
    section = _section(flagged.body, "## Coronary Calcium") if flagged.exists else ""
    lines = [line for line in section.splitlines() if line.strip()]
    return [*lines, "", f"Detail: {flagged.link}"] if lines else []


def _record_status_lines(pending: ReportInput, reports: dict[str, ReportInput]) -> list[str]:
    """Factual record-completeness notes; no generated questions (ADR-031)."""
    lines: list[str] = []
    pending_count = _pending_review_count(pending)
    if pending_count:
        lines.append(f"- {pending_count} record item(s) not yet reviewed: {pending.link}")
    for key in ("medication_conflicts", "lab_conflicts"):
        if _has_conflicts(reports[key]):
            lines.append(f"- Sources disagree: {reports[key].link}")
    return lines


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _packet_filename(base_timestamp: str, existing_dir: Path) -> str:
    stem = f"{base_timestamp}{_PACKET_SUFFIX}"
    candidate = f"{stem}.md"
    if not (existing_dir / candidate).exists():
        return candidate

    suffix = 2
    while True:
        candidate = f"{stem}-{suffix}.md"
        if not (existing_dir / candidate).exists():
            return candidate
        suffix += 1


def write_doctor_visit_packets_index(
    packet_dir: Path, person_id: str, *, display_name: str | None = None
) -> Path:
    packet_paths = sorted(
        path for path in packet_dir.glob("*.md") if path.name != _PACKET_INDEX_NAME
    )
    link_lines = [f"- [[{path.stem}]]" for path in packet_paths]
    links_block = "\n".join(link_lines) if link_lines else "_No doctor-visit packets yet._"
    name = display_name or person_id
    content = (
        f"---\nperson_id: {person_id}\nrecord_type: doctor_visit_packets_index\n---\n\n"
        f"# Doctor Visit Packets — {name}\n\n"
        "[[../_index|← Reports]]\n\n"
        "## Packets\n\n"
        f"{links_block}\n"
    )
    index_path = packet_dir / _PACKET_INDEX_NAME
    index_path.write_text(content, encoding="utf-8")
    return index_path


def write_doctor_visit_packet(
    vault_root: Path,
    person_id: str,
    *,
    generated_at: datetime | None = None,
) -> Path:
    reports_dir = _reports_dir(vault_root, person_id)
    if not reports_dir.is_dir():
        raise DoctorVisitPacketError(f"reports directory not found for {person_id}")

    person = _load_person(vault_root, person_id)
    display_name = load_person_display_name(vault_root, person_id)
    reports = {
        key: _load_report(vault_root, person_id, key, filename, title)
        for key, filename, title in _SOURCE_REPORTS
    }
    missing_reports = [key for key in _CORE_KEYS if not reports[key].exists]
    source_reports = [
        report.path.relative_to(vault_root).as_posix()
        for report in reports.values()
        if report.exists
    ]

    generated = generated_at or datetime.now(UTC)
    generated_iso = generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    generated_stamp = generated.astimezone(UTC).strftime("%Y%m%d-%H%M%S")

    labs = _lab_observations(reports["health_advisory"])
    medication_lines = _medication_lines(reports["current_medications"])
    allergy_lines = render_allergy_section(vault_root, person_id)[2:]

    packet_dir = _packet_dir(vault_root, person_id)
    packet_dir.mkdir(parents=True, exist_ok=True)
    _validate_allowed_packet_dir(vault_root, person_id, packet_dir)
    packet_path = packet_dir / _packet_filename(generated_stamp, packet_dir)

    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "doctor_visit_packet",
        "generated_at": generated_iso,
        "source_reports": source_reports,
        "missing_reports": missing_reports,
    }
    sections: list[tuple[str, list[str]]] = [
        ("Patient", _identity_lines(person_id, display_name, person, generated)),
        ("Allergies", [line for line in allergy_lines if line.strip()]),
        ("Problem List", _problem_lines(reports["medical_history_summary"])),
        ("Current Medications", medication_lines),
        ("Labs To Discuss", _lab_lines(reports["health_advisory"], labs, reports["lab_results"])),
        ("Coronary Calcium", _calcium_lines(reports["flagged_for_discussion"])),
        ("Latest Vital Signs", _vital_lines(reports["medical_history_summary"])),
        ("DNA-Based Medication Results", _pgx_lines(reports["genetics"])),
        ("Recent Immunizations", _immunization_lines(reports["immunizations"])),
        ("Record Status", _record_status_lines(reports["pending_review"], reports)),
        ("Family History (Blood Relatives)", _family_lines(reports["health_advisory"])),
        (
            "Detail Reports",
            [" · ".join(report.link for report in reports.values() if report.exists)]
            if source_reports
            else ["_No detail reports available._"],
        ),
    ]
    body = "\n\n".join(f"## {title}\n\n" + "\n".join(lines) for title, lines in sections if lines)
    content = (
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n\n"
        f"# Doctor Visit Packet — {display_name}\n\n"
        "[[../_index|← Reports]]\n\n"
        "> [!warning] Caregiver-prepared summary\n"
        "> Compiled from the family's records; not medical advice. Verify against the "
        "linked reports.\n\n"
        f"{body}\n"
    )
    packet_path.write_text(content, encoding="utf-8")

    write_doctor_visit_packets_index(packet_dir, person_id, display_name=display_name)
    write_category_index_note(
        reports_dir,
        person_id,
        "Reports",
        note_label="report",
        display_name=display_name,
    )
    return packet_path
