#!/usr/bin/env python3
"""Manual free-form inbox verification fixture checks."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from famed.clinical_interpreter import KNOWN_VITAL_NAMES
from famed.source_paths import is_hidden_source_path


class FreeformFixtureError(ValueError):
    """Raised when a manual free-form verification fixture is incomplete."""


@dataclass(frozen=True)
class SourceMatch:
    requested: str
    path: Path
    matched_by: str


@dataclass(frozen=True)
class InboxAuditItem:
    path: Path
    family: str
    outcome: str
    fixture_covered: bool
    note: str


@dataclass(frozen=True)
class InboxAuditFamilySummary:
    family: str
    status: str
    count: int
    required_action: str
    outcomes: tuple[str, ...]


@dataclass(frozen=True)
class InboxAuditReport:
    items: list[InboxAuditItem]
    family_summaries: list[InboxAuditFamilySummary]
    unresolved_families: list[InboxAuditFamilySummary]
    missing_required_families: list[str]
    stale_quarantine_paths: list[Path]

    @property
    def outcome_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.outcome] = counts.get(item.outcome, 0) + 1
        return counts


_SEPARATORS = re.compile(r"[\s/\\:_\-]+")
_VITAL_NAMES = KNOWN_VITAL_NAMES
_OUTCOMES_REQUIRING_MANUAL_VERIFICATION = frozenset(
    {
        "needs_user_verification",
        "needs_person_resolution",
        "needs_category_resolution",
        "needs_logic_refinement",
        "needs_ocr_or_parser",
        "unsupported_content",
        "quarantine_recommended",
    }
)
_KNOWN_FIXTURE_OUTCOMES = _OUTCOMES_REQUIRING_MANUAL_VERIFICATION | frozenset(
    {
        "deterministic_handler_candidate",
        "unsupported_content",
    }
)
_KNOWN_CANDIDATE_KINDS = frozenset(
    {
        "allergy",
        "encounter",
        "lab",
        "medication",
        "plan",
        "problem",
        "procedure",
        "procedure_order",
        "provider",
        "risk_factor",
        "vital",
    }
)
_MANUAL_VERIFICATION_OPTIONS = frozenset(
    {
        "accept",
        "approve",
        "edit",
        "reject",
        "quarantine",
        "clarify",
        "dismiss_wrong_person",
        "assign_existing_person",
        "update_person_matching_data",
        "refine_logic",
        "refine_preprocessing",
        "refine_fixture",
        "run_ocr",
    }
)
_REVIEWER_DECISIONS = frozenset({"accept", "edit", "reject", "quarantine", "cancelled"})
_REQUIRED_FIXTURE_FAMILIES = (
    "lab_cmp",
    "lab_cbc",
    "lab_lipid",
    "lab_a1c",
    "lab_tsh",
    "lab_psa",
    "lab_lipase",
    "lab_liver_panel",
    "lab_crp",
    "lab_nmr_lipoprofile",
    "lab_ana",
    "lab_b12_folate",
    "lab_gfr",
    "lab_hepatitis_c_antibody",
    "lab_occult_blood_or_urine_dipstick",
    "visit_note_pdf",
    "visit_summary_pdf",
    "visit_summary_xml",
    "pathology_or_histology",
    "colonoscopy_pdf",
    "colonoscopy_tif",
    "imaging_xray",
    "imaging_mri",
    "imaging_ultrasound",
    "imaging_cac_tif",
    "scanned_tif_ocr",
    "unsupported_content",
)
_LAB_FAMILIES = frozenset(
    family for family in _REQUIRED_FIXTURE_FAMILIES if family.startswith("lab_")
)


def load_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise FreeformFixtureError("fixture root must be a mapping")
    return payload


def validate_fixture_file(path: Path, *, data_root: Path) -> list[SourceMatch]:
    payload = load_fixture(path)
    return validate_fixture_payload(payload, data_root=data_root)


def validate_fixture_payload(payload: dict[str, Any], *, data_root: Path) -> list[SourceMatch]:
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise FreeformFixtureError("fixtures must be a non-empty list")

    matches: list[SourceMatch] = []
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, dict):
            raise FreeformFixtureError(f"fixtures[{index}] must be a mapping")
        matches.append(_validate_fixture(fixture, index=index, data_root=data_root))
    return matches


def audit_current_inbox(data_root: Path, *, fixture_path: Path | None = None) -> InboxAuditReport:
    """Return proposal-only coverage audit for current inbox and quarantine sources."""
    data_root = data_root.resolve()
    fixture_families: set[str] = set()
    fixture_paths: set[Path] = set()
    fixture_outcomes: dict[Path, str] = {}
    if fixture_path is not None and fixture_path.exists():
        payload = load_fixture(fixture_path)
        for match in validate_fixture_payload(payload, data_root=data_root):
            fixture_paths.add(match.path.resolve())
            fixture_families.add(classify_source_family(match.path))
        for fixture in payload["fixtures"]:
            if not isinstance(fixture, dict):
                continue
            outcome = str(fixture.get("outcome") or "").strip()
            if not outcome:
                continue
            source_request = str(fixture.get("source_request") or "")
            match = resolve_source_identifier(data_root, source_request)
            fixture_outcomes[match.path.resolve()] = outcome

    items: list[InboxAuditItem] = []
    for source_path in _iter_audit_sources(data_root):
        family = classify_source_family(source_path)
        outcome, note = _outcome_for_source(source_path, family=family, data_root=data_root)
        fixture_outcome = fixture_outcomes.get(source_path.resolve())
        if fixture_outcome:
            outcome = fixture_outcome
            note = "recorded fixture outcome"
        items.append(
            InboxAuditItem(
                path=source_path,
                family=family,
                outcome=outcome,
                fixture_covered=source_path.resolve() in fixture_paths,
                note=note,
            )
        )

    covered_families = {item.family for item in items if item.fixture_covered} | fixture_families
    missing = [
        family
        for family in _REQUIRED_FIXTURE_FAMILIES
        if family not in covered_families and _family_present(family, items)
    ]
    stale_quarantine = [
        item.path
        for item in items
        if (
            _is_under(item.path, data_root / "quarantine")
            and item.outcome == "quarantine_recommended"
        )
    ]
    family_summaries = _summarize_families(items)
    unresolved_families = [
        summary for summary in family_summaries if summary.status != "supported-now"
    ]
    return InboxAuditReport(
        items=items,
        family_summaries=family_summaries,
        unresolved_families=unresolved_families,
        missing_required_families=missing,
        stale_quarantine_paths=stale_quarantine,
    )


def _summarize_families(items: list[InboxAuditItem]) -> list[InboxAuditFamilySummary]:
    grouped: dict[str, list[InboxAuditItem]] = {}
    for item in items:
        grouped.setdefault(item.family, []).append(item)

    summaries: list[InboxAuditFamilySummary] = []
    for family, family_items in sorted(grouped.items()):
        outcomes = tuple(sorted({item.outcome for item in family_items}))
        status, required_action = _family_status_and_action(outcomes)
        summaries.append(
            InboxAuditFamilySummary(
                family=family,
                status=status,
                count=len(family_items),
                required_action=required_action,
                outcomes=outcomes,
            )
        )
    return summaries


def _family_status_and_action(outcomes: tuple[str, ...]) -> tuple[str, str]:
    outcome_set = set(outcomes)
    if "needs_category_resolution" in outcome_set or "needs_logic_refinement" in outcome_set:
        return (
            "blocked-by-parser",
            "Add or refine deterministic family/category routing for this source family.",
        )
    if "needs_person_resolution" in outcome_set or "quarantine_recommended" in outcome_set:
        return (
            "blocked-by-person-resolution",
            "Run interactive re-evaluation and record a resolved fixture outcome.",
        )
    if "unsupported_content" in outcome_set:
        return (
            "intentionally-deferred",
            "Document deferral or add support when this family is prioritized.",
        )
    return (
        "supported-now",
        "Keep fixture coverage current and continue proposal verification.",
    )


def classify_source_family(path: Path) -> str:
    normalized = _normalize_filename(path.name)
    suffix = path.suffix.casefold()

    if suffix in {".tif", ".tiff"}:
        if "coronary artery calcium" in normalized or "calcium scoring" in normalized:
            return "imaging_cac_tif"
        if "colonoscopy" in normalized:
            return "colonoscopy_tif"
        return "scanned_tif_ocr"
    if suffix == ".xml" and "visitsummary" in normalized:
        return "visit_summary_xml"
    if suffix not in {".pdf", ".txt", ".md", ".xml"}:
        return "unsupported_content"

    if "visitnote" in normalized or "visit note" in normalized:
        return "visit_note_pdf"
    if "visitsummary" in normalized or "visit summary" in normalized:
        return "visit_summary_pdf"
    if "colonoscopy" in normalized:
        return "colonoscopy_pdf"
    if "pathology" in normalized or "histology" in normalized:
        return "pathology_or_histology"
    if normalized.startswith("xr ") or " xr " in normalized:
        return "imaging_xray"
    if "mri " in normalized:
        return "imaging_mri"
    if normalized.startswith("us ") or " us " in normalized or " ultrasound" in normalized:
        return "imaging_ultrasound"
    if "comprehensive metabolic panel" in normalized or "poc group chem" in normalized:
        return "lab_cmp"
    if (
        "complete blood count" in normalized
        or "cbc w" in normalized
        or "automated diff" in normalized
    ):
        return "lab_cbc"
    if "lipid panel" in normalized:
        return "lab_lipid"
    if "hemoglobin a1c" in normalized:
        return "lab_a1c"
    if "tsh" in normalized:
        return "lab_tsh"
    if "psa" in normalized:
        return "lab_psa"
    if "lipase" in normalized:
        return "lab_lipase"
    if "liver panel" in normalized:
        return "lab_liver_panel"
    if "high sensitivity crp" in normalized or " crp" in normalized:
        return "lab_crp"
    if "nmr lipo" in normalized or "lipoprofile" in normalized:
        return "lab_nmr_lipoprofile"
    if "ana screen" in normalized or " ana " in normalized:
        return "lab_ana"
    if "vitamin b12" in normalized or "folate" in normalized:
        return "lab_b12_folate"
    if "gfr" in normalized:
        return "lab_gfr"
    if "hepatitis c antibody" in normalized:
        return "lab_hepatitis_c_antibody"
    if "occult blood" in normalized or "urine dipstick" in normalized:
        return "lab_occult_blood_or_urine_dipstick"
    if "troponin" in normalized or "miscellaneous lc" in normalized:
        return "needs_category_resolution"
    return "unsupported_content"


def resolve_source_identifier(data_root: Path, source_request: str) -> SourceMatch:
    data_root = data_root.resolve()
    request_path = Path(source_request)
    if request_path.is_absolute() and request_path.exists():
        resolved = request_path.resolve()
        if _is_relative_to(resolved, data_root) and not is_hidden_source_path(resolved, data_root):
            return SourceMatch(source_request, resolved, "exact_path")

    relative_candidate = (data_root / source_request).resolve()
    if (
        relative_candidate.exists()
        and _is_relative_to(relative_candidate, data_root)
        and not is_hidden_source_path(relative_candidate, data_root)
    ):
        return SourceMatch(source_request, relative_candidate, "exact_path")

    files = [
        path
        for path in data_root.rglob("*")
        if path.is_file() and not is_hidden_source_path(path, data_root)
    ]
    exact = [path for path in files if path.name == source_request]
    if len(exact) == 1:
        return SourceMatch(source_request, exact[0].resolve(), "exact_filename")
    if len(exact) > 1:
        raise FreeformFixtureError(_ambiguous_message(source_request, exact))

    globbed = [path for path in files if fnmatch.fnmatch(path.name, source_request)]
    if len(globbed) == 1:
        return SourceMatch(source_request, globbed[0].resolve(), "glob")
    if len(globbed) > 1:
        raise FreeformFixtureError(_ambiguous_message(source_request, globbed))

    lowered = source_request.casefold()
    substring = [path for path in files if lowered in path.name.casefold()]
    if len(substring) == 1:
        return SourceMatch(source_request, substring[0].resolve(), "substring")
    if len(substring) > 1:
        raise FreeformFixtureError(_ambiguous_message(source_request, substring))

    normalized_request = _normalize_filename(source_request)
    normalized = [path for path in files if _normalize_filename(path.name) == normalized_request]
    if len(normalized) == 1:
        return SourceMatch(source_request, normalized[0].resolve(), "normalized_filename")
    if len(normalized) > 1:
        raise FreeformFixtureError(_ambiguous_message(source_request, normalized))

    normalized_substring = [
        path for path in files if normalized_request in _normalize_filename(path.name)
    ]
    if len(normalized_substring) == 1:
        return SourceMatch(
            source_request, normalized_substring[0].resolve(), "normalized_substring"
        )
    if len(normalized_substring) > 1:
        raise FreeformFixtureError(_ambiguous_message(source_request, normalized_substring))

    raise FreeformFixtureError(f"source not found under {data_root}: {source_request}")


def _iter_audit_sources(data_root: Path) -> list[Path]:
    roots = [data_root / "inbox", data_root / "quarantine"]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not is_hidden_source_path(path, root):
                files.append(path.resolve())
    return sorted(files, key=lambda path: str(path.relative_to(data_root)))


def _outcome_for_source(path: Path, *, family: str, data_root: Path) -> tuple[str, str]:
    if _is_under(path, data_root / "quarantine"):
        return "quarantine_recommended", "legacy quarantine source requires re-evaluation"
    if family in _LAB_FAMILIES:
        return "deterministic_handler_candidate", "regular lab-like source"
    if family in {
        "visit_note_pdf",
        "visit_summary_pdf",
        "visit_summary_xml",
        "pathology_or_histology",
        "colonoscopy_pdf",
        "imaging_xray",
        "imaging_mri",
        "imaging_ultrasound",
        "colonoscopy_tif",
        "imaging_cac_tif",
        "scanned_tif_ocr",
    }:
        return "needs_user_verification", "free-form medical source needs reviewed proposal"
    if family == "needs_category_resolution":
        return "needs_category_resolution", "recognized medical source needs category mapping"
    return "unsupported_content", "no supported source family matched"


def _family_present(family: str, items: list[InboxAuditItem]) -> bool:
    if family == "unsupported_content":
        return True
    return any(item.family == family for item in items)


def _is_under(path: Path, parent: Path) -> bool:
    return _is_relative_to(path.resolve(), parent.resolve())


def _validate_fixture(fixture: dict[str, Any], *, index: int, data_root: Path) -> SourceMatch:
    source_request = _required_str(fixture, "source_request", f"fixtures[{index}]")
    match = resolve_source_identifier(data_root, source_request)

    expected = _required_mapping(fixture, "expected", f"fixtures[{index}]")
    outcome = str(fixture.get("outcome") or "").strip()
    if outcome and outcome not in _KNOWN_FIXTURE_OUTCOMES:
        raise FreeformFixtureError(
            f"fixtures[{index}].outcome must be one of {sorted(_KNOWN_FIXTURE_OUTCOMES)!r}"
        )
    _validate_reviewer_decision(fixture, index=index)
    _validate_manual_verification_request(fixture, outcome=outcome, index=index)
    _optional_or_required_str(expected, f"fixtures[{index}].expected", "person_id", outcome)
    _optional_or_required_str(expected, f"fixtures[{index}].expected", "document_type", outcome)
    categories = _required_list(expected, "categories", f"fixtures[{index}].expected")
    if not all(isinstance(category, str) and category.strip() for category in categories):
        raise FreeformFixtureError(f"fixtures[{index}].expected.categories must be strings")

    candidates = _required_list(fixture, "candidates", f"fixtures[{index}]")
    if not candidates and (not outcome or outcome not in _OUTCOMES_REQUIRING_MANUAL_VERIFICATION):
        raise FreeformFixtureError(f"fixtures[{index}].candidates must not be empty")
    candidate_items: set[str] = set()
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise FreeformFixtureError(
                f"fixtures[{index}].candidates[{candidate_index}] must be a mapping"
            )
        item = _validate_candidate(candidate, fixture_index=index, candidate_index=candidate_index)
        candidate_items.add(item.casefold())

    expected_vitals = fixture.get("expected_vitals") or []
    if expected_vitals:
        if not isinstance(expected_vitals, list):
            raise FreeformFixtureError(f"fixtures[{index}].expected_vitals must be a list")
        for vital in expected_vitals:
            if not isinstance(vital, str) or vital.casefold() not in _VITAL_NAMES:
                raise FreeformFixtureError(
                    f"fixtures[{index}].expected_vitals contains unsupported vital {vital!r}"
                )
            if vital.casefold() not in candidate_items:
                raise FreeformFixtureError(
                    f"fixtures[{index}] missing separate vital candidate: {vital}"
                )

    return match


def _validate_reviewer_decision(fixture: dict[str, Any], *, index: int) -> None:
    base = f"fixtures[{index}].reviewer_decision"
    reviewer_decision = _required_mapping(fixture, "reviewer_decision", f"fixtures[{index}]")
    decision = _required_str(reviewer_decision, "decision", base)
    if decision not in _REVIEWER_DECISIONS:
        raise FreeformFixtureError(
            f"{base}.decision must be one of {sorted(_REVIEWER_DECISIONS)!r}"
        )
    if decision == "cancelled":
        _required_str(reviewer_decision, "cancelled_at", base)
        _required_str(reviewer_decision, "cancellation_reason", base)

    _required_str(reviewer_decision, "reviewed_by", base)
    reviewed_at = _required_str(reviewer_decision, "reviewed_at", base)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reviewed_at):
        raise FreeformFixtureError(f"{base}.reviewed_at must be YYYY-MM-DD")
    rationale = _required_str(reviewer_decision, "rationale", base)
    if len(rationale) < 8:
        raise FreeformFixtureError(f"{base}.rationale is too short to be useful")


def _validate_manual_verification_request(
    fixture: dict[str, Any], *, outcome: str, index: int
) -> None:
    if outcome not in _OUTCOMES_REQUIRING_MANUAL_VERIFICATION:
        return

    base = f"fixtures[{index}].manual_verification_request"
    request = _required_mapping(fixture, "manual_verification_request", f"fixtures[{index}]")
    reason = _required_str(request, "reason", base)
    if len(reason) < 8:
        raise FreeformFixtureError(f"{base}.reason is too short to be useful")
    prompt = _required_str(request, "prompt", base)
    if len(prompt) < 8:
        raise FreeformFixtureError(f"{base}.prompt is too short to be useful")

    options = _required_list(request, "options", base)
    if not options:
        raise FreeformFixtureError(f"{base}.options must not be empty")
    invalid_options = [
        option
        for option in options
        if not isinstance(option, str) or option not in _MANUAL_VERIFICATION_OPTIONS
    ]
    if invalid_options:
        raise FreeformFixtureError(
            f"{base}.options contains unsupported option(s): {invalid_options!r}"
        )


def _validate_candidate(
    candidate: dict[str, Any], *, fixture_index: int, candidate_index: int
) -> str:
    base = f"fixtures[{fixture_index}].candidates[{candidate_index}]"
    item = _required_str(candidate, "item", base)
    _required_str(candidate, "type", base)
    _required_str(candidate, "confidence", base)
    evidence = _required_str(candidate, "evidence", base)
    if len(evidence) < 8:
        raise FreeformFixtureError(f"{base}.evidence is too short to be useful")

    kind = str(candidate.get("kind") or "").strip().lower()
    if kind and kind not in _KNOWN_CANDIDATE_KINDS:
        raise FreeformFixtureError(f"{base}.kind must be one of {sorted(_KNOWN_CANDIDATE_KINDS)!r}")
    if kind == "lab":
        _required_str(candidate, "unit", base)
        _required_str(candidate, "reference", base)
    elif kind == "vital":
        _required_str(candidate, "unit", base)
        if "," in item or ";" in item:
            raise FreeformFixtureError(f"{base}.item appears to bundle multiple vitals")
    elif kind == "medication":
        _required_str(candidate, "value", base)
    elif kind == "problem":
        _required_str(candidate, "value", base)

    return item


def _normalize_filename(name: str) -> str:
    stem = Path(name).stem.casefold()
    suffix = Path(name).suffix.casefold()
    normalized_stem = _SEPARATORS.sub(" ", stem).strip()
    return f"{normalized_stem}{suffix}"


def _required_mapping(payload: dict[str, Any], field: str, base: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise FreeformFixtureError(f"{base}.{field} must be a mapping")
    return value


def _required_list(payload: dict[str, Any], field: str, base: str) -> list[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise FreeformFixtureError(f"{base}.{field} must be a list")
    return value


def _optional_or_required_str(
    payload: dict[str, Any], base: str, field: str, outcome: str
) -> str | None:
    value = payload.get(field)
    if not value:
        if outcome and outcome in _OUTCOMES_REQUIRING_MANUAL_VERIFICATION:
            return None
        raise FreeformFixtureError(f"{base}.{field} must be a non-empty string")
    if not isinstance(value, str) or not value.strip():
        raise FreeformFixtureError(f"{base}.{field} must be a non-empty string")
    return value.strip()


def _required_str(payload: dict[str, Any], field: str, base: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FreeformFixtureError(f"{base}.{field} must be a non-empty string")
    return value.strip()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _ambiguous_message(source_request: str, paths: list[Path]) -> str:
    candidates = ", ".join(str(path) for path in paths[:5])
    return f"source request {source_request!r} matched multiple files: {candidates}"
