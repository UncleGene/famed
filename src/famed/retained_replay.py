"""Batch replay of retained medical sources into isolated rebuild storage."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from famed.clinical_interpreter import (
    FactInterpreter,
    InterpreterContractError,
    validate_fact_extraction,
)
from famed.dependent_reports import (
    refresh_fhir_dependent_export,
    refresh_lab_dependent_reports,
    refresh_medication_dependent_reports,
)
from famed.history_sync import (
    HistorySyncError,
    LabObservationResolution,
    LabObservationSelector,
    extract_ccda_lab_rows,
    reprocess_retained_history_archive,
    reprocess_retained_history_source,
    validate_rejected_lab_observation_resolutions,
    write_medical_history_summary_report,
)
from famed.import_labs import (
    canonicalize_candidate_lab_facts,
    persist_lab_rows,
    reprocess_retained_lab_source,
)
from famed.lab_sync import source_file_checksum
from famed.medication_sync import (
    canonicalize_candidate_medication_facts,
    fill_rxnorm_cuis,
    medication_document_date,
    parse_medication_markdown_table,
    persist_medication_rows,
    reprocess_retained_supplement_source,
    write_current_medications_report,
    write_medication_conflicts_report,
)
from famed.person_sync import PersonSyncError, parse_front_matter
from famed.reference_cache import load_loinc_index, load_rxnorm_index
from famed.reprocess_sources import (
    ReprocessError,
    ReprocessRegistry,
    ReprocessResult,
    restore_person_vault_tree,
    retained_source_reference,
    snapshot_person_vault_tree,
)
from famed.source_paths import split_archive_source_path
from famed.storage_contract import PROCESSED_ROOT
from famed.user_update_sync import (
    reprocess_retained_user_update_history_source,
    reprocess_retained_user_update_lab_source,
)

ReplayOutcome = Literal["imported", "redundant", "zero_fact", "unsupported", "failed"]
HISTORY_DECISIONS_SCHEMA = "famed.history-extraction-decisions.v1"
RETAINED_SEMANTIC_DECISIONS_SCHEMA = "famed.retained-semantic-decisions.v2"
RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3 = "famed.retained-semantic-decisions.v3"
RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4 = "famed.retained-semantic-decisions.v4"
RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5 = "famed.retained-semantic-decisions.v5"
_HISTORY_HANDLER_NAMES = frozenset({"history", "history_from_labs", "history_archive"})
_LAB_PDF_DOCUMENT_PREFIXES = {
    "radiology": ("CT ", "XR ", "US "),
    "ophthalmology": ("HUMPHREY VISUAL FIELD", "OCT, "),
    "cardiology": ("ECG ", "TRANSTHORACIC ECHO"),
}
_MEDICATION_TIMELINE_ENTRY = re.compile(
    r"(?P<dose>\d+(?:\.\d+)?\s*(?:mg|mcg|µg|g|IU|kg))\s+from\s+"
    r"(?P<start_date>\d{4}-\d{2}-\d{2})",
    flags=re.IGNORECASE,
)


def retained_lab_pdf_document_domain(source_path: Path) -> str | None:
    """Classify the bounded non-lab study filenames currently retained under Labs."""
    if source_path.suffix.lower() != ".pdf":
        return None
    label = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", source_path.name).upper()
    for domain, prefixes in _LAB_PDF_DOCUMENT_PREFIXES.items():
        if label.startswith(prefixes):
            return domain
    return None


def _is_retained_history_visit_summary_pdf(source_path: Path) -> bool:
    return bool(
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}-visitsummary\.pdf",
            source_path.name,
            flags=re.IGNORECASE,
        )
    )


@dataclass(frozen=True)
class RetainedReplayItem:
    source_path: Path
    outcome: ReplayOutcome
    handler_name: str | None = None
    imported_rows: int = 0
    affected_person_ids: tuple[str, ...] = ()
    message: str | None = None


@dataclass(frozen=True)
class RetainedReplaySummary:
    items: tuple[RetainedReplayItem, ...]

    def count(self, outcome: ReplayOutcome) -> int:
        return sum(item.outcome == outcome for item in self.items)

    @property
    def imported_rows(self) -> int:
        return sum(item.imported_rows for item in self.items)

    @property
    def failed(self) -> bool:
        return any(item.outcome == "failed" for item in self.items)


@dataclass(frozen=True)
class RetainedHistoryDecisionInterpreter:
    """Serve source-bound attended decisions through category-native contracts."""

    payloads: Mapping[tuple[str, str], Mapping[str, Any]]
    processed_root: Path
    lab_observation_resolutions: Mapping[str, tuple[LabObservationResolution, ...]] = field(
        default_factory=dict
    )
    lab_observation_resolution_checksums: Mapping[str, str] = field(default_factory=dict)

    def extract_facts(self, source_path: Path) -> Mapping[str, Any]:
        """Return the History extraction expected by the History reprocessor."""
        try:
            text = source_path.read_text(encoding="utf-8")
            front_matter = parse_front_matter(text)
        except (OSError, PersonSyncError, yaml.YAMLError) as exc:
            raise ReprocessError(
                f"could not read History decision target front matter: {source_path}"
            ) from exc
        retained_source = str(front_matter.get("source_path") or "")
        payload = self.payloads.get((retained_source, "history"))
        if payload is None:
            return {}
        validated = validate_fact_extraction(payload)
        if validated.person_id != str(front_matter.get("person_id") or ""):
            raise ReprocessError(
                f"History decision person does not match generated note: {retained_source}"
            )
        return payload

    def has_history_decision_for_source(self, source_path: str) -> bool:
        """Return whether an exact source-bound History extraction was reviewed."""
        return (source_path, "history") in self.payloads

    def payload_for_source(
        self,
        source_path: Path,
        category: str,
    ) -> Mapping[str, Any] | None:
        """Return a validated category extraction for a retained source file."""
        try:
            relative = source_path.resolve().relative_to(self.processed_root.resolve())
        except ValueError as exc:
            raise ReprocessError(
                f"retained decision source is outside processed root: {source_path}"
            ) from exc
        canonical_source = (PROCESSED_ROOT / relative).as_posix()
        return self.payloads.get((canonical_source, category))

    def lab_observation_resolutions_for_source(
        self,
        source_path: str,
    ) -> tuple[LabObservationResolution, ...]:
        """Return exact attended lab-observation decisions for one archive member."""
        return self.lab_observation_resolutions.get(source_path, ())

    def validate_lab_observation_resolution_archive(
        self,
        archive_source_path: str,
        source_sha256: str,
        member_source_paths: Sequence[str],
    ) -> None:
        """Fail if reviewed archive bytes or member identities changed after preflight."""
        member_prefix = f"{archive_source_path}#"
        expected_members = {
            source_path
            for source_path in self.lab_observation_resolution_checksums
            if source_path.startswith(member_prefix)
        }
        if not expected_members:
            return
        expected_checksums = {
            self.lab_observation_resolution_checksums[source_path]
            for source_path in expected_members
        }
        if expected_checksums != {source_sha256}:
            raise ReprocessError(
                f"retained observation resolution checksum became stale: {archive_source_path}"
            )
        missing_members = expected_members.difference(member_source_paths)
        if missing_members:
            raise ReprocessError(
                "retained observation resolution member became stale: "
                + ", ".join(sorted(missing_members))
            )


def _required_resolution_text(
    mapping: Mapping[str, Any],
    field_name: str,
    *,
    decision_index: int,
) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ReprocessError(
            f"retained decision {decision_index} resolution {field_name} must be nonblank text"
        )
    return value.strip()


def _optional_resolution_text(
    mapping: Mapping[str, Any],
    field_name: str,
    *,
    decision_index: int,
) -> str | None:
    value = mapping.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReprocessError(
            f"retained decision {decision_index} selector {field_name} must be text or null"
        )
    return value.strip() or None


def validate_lab_observation_resolution(
    raw_resolution: Mapping[str, Any],
    *,
    decision_index: int,
    person_id: str,
) -> LabObservationResolution:
    allowed_resolution_fields = {
        "category",
        "person_id",
        "action",
        "selector",
        "expected_occurrences",
        "resolved_by",
        "resolved_at",
        "reason",
    }
    if set(raw_resolution) != allowed_resolution_fields:
        raise ReprocessError(
            f"retained decision {decision_index} resolution fields must be exactly "
            f"{sorted(allowed_resolution_fields)}"
        )
    if raw_resolution.get("category") != "labs":
        raise ReprocessError(
            f"retained decision {decision_index} observation resolution category must be labs"
        )
    if raw_resolution.get("person_id") != person_id:
        raise ReprocessError(
            f"retained decision {decision_index} person does not match retained source path"
        )
    action = raw_resolution.get("action")
    if not isinstance(action, str) or action not in {"accept_verbatim", "reject", "quarantine"}:
        raise ReprocessError(f"retained decision {decision_index} resolution action is unsupported")
    expected_occurrences = raw_resolution.get("expected_occurrences")
    if (
        isinstance(expected_occurrences, bool)
        or not isinstance(expected_occurrences, int)
        or expected_occurrences < 1
    ):
        raise ReprocessError(
            f"retained decision {decision_index} expected_occurrences must be a positive integer"
        )
    resolved_at = _required_resolution_text(
        raw_resolution,
        "resolved_at",
        decision_index=decision_index,
    )
    try:
        parsed_resolved_at = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReprocessError(
            f"retained decision {decision_index} resolved_at must be an ISO timestamp"
        ) from exc
    if parsed_resolved_at.tzinfo is None:
        raise ReprocessError(
            f"retained decision {decision_index} resolved_at must include a timezone"
        )

    raw_selector = raw_resolution.get("selector")
    if not isinstance(raw_selector, Mapping):
        raise ReprocessError(
            f"retained decision {decision_index} resolution selector must be an object"
        )
    selector_fields = {
        "code_system",
        "loinc_code",
        "test_name",
        "value_text",
        "value_type",
        "unit",
        "observed_at",
        "reference_range_low",
        "reference_range_high",
    }
    if set(raw_selector) != selector_fields:
        raise ReprocessError(
            f"retained decision {decision_index} selector fields must be exactly "
            f"{sorted(selector_fields)}"
        )
    selector = LabObservationSelector(
        code_system=_required_resolution_text(
            raw_selector, "code_system", decision_index=decision_index
        ),
        loinc_code=_required_resolution_text(
            raw_selector, "loinc_code", decision_index=decision_index
        ),
        test_name=_required_resolution_text(
            raw_selector, "test_name", decision_index=decision_index
        ),
        value_text=_required_resolution_text(
            raw_selector, "value_text", decision_index=decision_index
        ),
        value_type=_required_resolution_text(
            raw_selector, "value_type", decision_index=decision_index
        ),
        unit=_optional_resolution_text(raw_selector, "unit", decision_index=decision_index),
        observed_at=_required_resolution_text(
            raw_selector, "observed_at", decision_index=decision_index
        ),
        reference_range_low=_optional_resolution_text(
            raw_selector, "reference_range_low", decision_index=decision_index
        ),
        reference_range_high=_optional_resolution_text(
            raw_selector, "reference_range_high", decision_index=decision_index
        ),
    )
    if selector.code_system != "2.16.840.1.113883.6.1":
        raise ReprocessError(
            f"retained decision {decision_index} selector must carry a source LOINC code"
        )
    if selector.value_type != "ST":
        raise ReprocessError(
            f"retained decision {decision_index} selector must target a C-CDA ST value"
        )
    return LabObservationResolution(
        action=action,
        selector=selector,
        expected_occurrences=expected_occurrences,
        resolved_by=_required_resolution_text(
            raw_resolution,
            "resolved_by",
            decision_index=decision_index,
        ),
        resolved_at=resolved_at,
        reason=_required_resolution_text(
            raw_resolution,
            "reason",
            decision_index=decision_index,
        ),
    )


def load_retained_history_decisions(
    *,
    decision_file: Path,
    processed_root: Path,
) -> RetainedHistoryDecisionInterpreter:
    """Load attended semantic decisions bound to isolated retained-source bytes."""
    try:
        raw = json.loads(decision_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReprocessError(f"could not read History decision bundle: {decision_file}") from exc
    supported_schemas = {
        HISTORY_DECISIONS_SCHEMA,
        RETAINED_SEMANTIC_DECISIONS_SCHEMA,
        RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
        RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
        RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
    }
    if not isinstance(raw, dict) or raw.get("schema_version") not in supported_schemas:
        raise ReprocessError(
            "retained decision bundle must use schema_version "
            + ", ".join(repr(version) for version in sorted(supported_schemas))
        )
    schema_version = str(raw["schema_version"])
    supported_categories = {"history"}
    if schema_version != HISTORY_DECISIONS_SCHEMA:
        supported_categories.add("labs")
    if schema_version in {
        RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
        RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
        RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
    }:
        supported_categories.add("medications")
    decisions = raw.get("decisions")
    if not isinstance(decisions, list):
        raise ReprocessError("History decision bundle decisions must be a list")

    resolved_root = processed_root.resolve()
    payloads: dict[tuple[str, str], Mapping[str, Any]] = {}
    lab_observation_resolutions: dict[str, list[LabObservationResolution]] = {}
    lab_observation_resolution_checksums: dict[str, str] = {}
    resolution_keys: set[tuple[str, LabObservationSelector]] = set()
    for index, raw_decision in enumerate(decisions):
        if not isinstance(raw_decision, dict):
            raise ReprocessError(f"History decision {index} must be an object")
        source_path = str(raw_decision.get("source_path") or "").strip()
        archive_path, member_name = split_archive_source_path(source_path)
        try:
            relative = Path(archive_path).relative_to(PROCESSED_ROOT)
        except ValueError as exc:
            raise ReprocessError(
                f"History decision {index} source_path is outside {PROCESSED_ROOT}"
            ) from exc
        allowed_source_categories = {"History", "HealthSummary"}
        if schema_version != HISTORY_DECISIONS_SCHEMA:
            allowed_source_categories.add("Labs")
        if schema_version in {
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
        }:
            allowed_source_categories.add("Medications")
        if len(relative.parts) < 3 or relative.parts[1] not in allowed_source_categories:
            raise ReprocessError(
                f"retained decision {index} source_path is not supported retained evidence"
            )
        retained_file = (resolved_root / relative).resolve()
        if retained_file != resolved_root and resolved_root not in retained_file.parents:
            raise ReprocessError(f"History decision {index} source_path escapes processed root")
        if not retained_file.is_file():
            raise ReprocessError(
                f"History decision {index} retained source is unavailable: {source_path}"
            )
        if relative.parts[1] == "Labs" and retained_lab_pdf_document_domain(retained_file) is None:
            raise ReprocessError(
                f"retained decision {index} Labs source is not a bounded domain-study PDF"
            )
        if member_name is not None:
            try:
                with zipfile.ZipFile(retained_file) as archive:
                    archive.getinfo(member_name)
            except (KeyError, OSError, zipfile.BadZipFile) as exc:
                raise ReprocessError(
                    f"History decision {index} archive member is unavailable: {source_path}"
                ) from exc

        expected_checksum = str(raw_decision.get("source_sha256") or "").strip().lower()
        if expected_checksum != source_file_checksum(retained_file):
            raise ReprocessError(
                f"History decision {index} source checksum does not match retained evidence"
            )
        raw_resolution = raw_decision.get("resolution")
        if raw_resolution is not None:
            if schema_version != RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5:
                raise ReprocessError(
                    f"retained decision {index} observation resolutions require schema v5"
                )
            if raw_decision.get("extraction") is not None:
                raise ReprocessError(
                    f"retained decision {index} cannot contain extraction and resolution"
                )
            if set(raw_decision) != {"source_path", "source_sha256", "resolution"}:
                raise ReprocessError(
                    f"retained decision {index} observation resolution has unsupported "
                    "top-level fields"
                )
            if relative.parts[1] != "HealthSummary" or member_name is None:
                raise ReprocessError(
                    f"retained decision {index} observation resolution requires an exact "
                    "HealthSummary archive member"
                )
            if not isinstance(raw_resolution, Mapping):
                raise ReprocessError(f"retained decision {index} resolution must be an object")
            resolution = validate_lab_observation_resolution(
                raw_resolution,
                decision_index=index,
                person_id=relative.parts[0],
            )
            resolution_key = (source_path, resolution.selector)
            if resolution_key in resolution_keys:
                raise ReprocessError(
                    f"duplicate retained observation resolution selector: {source_path}"
                )
            resolution_keys.add(resolution_key)
            lab_observation_resolutions.setdefault(source_path, []).append(resolution)
            lab_observation_resolution_checksums[source_path] = expected_checksum
            continue
        extraction = raw_decision.get("extraction")
        if not isinstance(extraction, Mapping):
            raise ReprocessError(f"History decision {index} extraction must be an object")
        try:
            validated = validate_fact_extraction(extraction)
        except InterpreterContractError as exc:
            raise ReprocessError(f"History decision {index} violates fact schema: {exc}") from exc
        if schema_version == HISTORY_DECISIONS_SCHEMA and validated.category != "history":
            raise ReprocessError(f"History decision {index} category must be history")
        if validated.category not in supported_categories:
            raise ReprocessError(
                f"retained decision {index} category {validated.category!r} is unsupported"
            )
        if validated.category == "labs" and relative.parts[1] != "Labs":
            raise ReprocessError(
                f"retained decision {index} labs extraction requires a bounded Labs study PDF"
            )
        if validated.category == "medications":
            if relative.parts[1] == "History":
                if schema_version not in {
                    RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
                    RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                }:
                    raise ReprocessError(
                        f"retained decision {index} medications extraction requires a retained "
                        "Medications source"
                    )
                if not _is_retained_history_visit_summary_pdf(retained_file):
                    raise ReprocessError(
                        f"retained decision {index} History medication extraction requires "
                        "a bounded visit-summary PDF"
                    )
            elif relative.parts[1] != "Medications":
                raise ReprocessError(
                    f"retained decision {index} medications extraction requires a retained "
                    "Medications source"
                )
            elif retained_file.suffix:
                raise ReprocessError(
                    f"retained decision {index} medications extraction requires a registered "
                    "extensionless Medications list"
                )
        if relative.parts[1] == "Medications" and validated.category != "medications":
            raise ReprocessError(
                f"retained decision {index} Medications source only supports medication extraction"
            )
        if validated.person_id != relative.parts[0]:
            raise ReprocessError(
                f"History decision {index} person does not match retained source path"
            )
        if not validated.evidence:
            raise ReprocessError(f"History decision {index} must include source evidence")
        if validated.below_threshold():
            raise ReprocessError(f"History decision {index} contains a below-threshold proposal")
        payload_key = (source_path, validated.category)
        if payload_key in payloads:
            raise ReprocessError(
                f"duplicate retained decision source/category: {source_path} ({validated.category})"
            )
        payloads[payload_key] = extraction

    for source_path, resolutions in lab_observation_resolutions.items():
        archive_path, member_name = split_archive_source_path(source_path)
        assert member_name is not None
        relative = Path(archive_path).relative_to(PROCESSED_ROOT)
        retained_file = resolved_root / relative
        with zipfile.ZipFile(retained_file) as archive:
            xml_bytes = archive.read(member_name)
        with tempfile.TemporaryDirectory(prefix="famed-resolution-preflight-") as temp_dir:
            source_file = Path(temp_dir) / Path(member_name).name
            source_file.write_bytes(xml_bytes)
            rejected: list[dict[str, Any]] = []
            extract_ccda_lab_rows(source_file, relative.parts[0], rejected=rejected)
        try:
            validate_rejected_lab_observation_resolutions(
                rejected=rejected,
                resolutions=resolutions,
                source_label=source_path,
            )
        except HistorySyncError as exc:
            raise ReprocessError(
                f"retained observation resolution does not match source: {exc}"
            ) from exc

    return RetainedHistoryDecisionInterpreter(
        payloads=payloads,
        processed_root=processed_root.resolve(),
        lab_observation_resolutions={
            source_path: tuple(resolutions)
            for source_path, resolutions in lab_observation_resolutions.items()
        },
        lab_observation_resolution_checksums=lab_observation_resolution_checksums,
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    return first == second or first in second.parents or second in first.parents


def validate_replay_storage_paths(
    *,
    source_vault: Path,
    source_db: Path,
    source_processed: Path,
    target_vault: Path,
    target_db: Path,
    target_processed: Path,
) -> None:
    """Reject every overlap between replay write targets and source or peer storage."""
    sources = (source_vault, source_db, source_processed)
    targets = (target_vault, target_db, target_processed)
    for target in targets:
        for source in sources:
            if _paths_overlap(target, source):
                raise ReprocessError(f"rebuild target {target} overlaps source storage {source}")
    for index, first in enumerate(targets):
        for second in targets[index + 1 :]:
            if _paths_overlap(first, second):
                raise ReprocessError(f"rebuild target paths overlap: {first} and {second}")


def _replay_retained_history_archive(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    history_fact_interpreter: FactInterpreter | None,
    decision_interpreter: RetainedHistoryDecisionInterpreter | None,
    source_path_prefix: Path | None,
    refresh_reports: bool,
) -> ReprocessResult:
    """Fail cleanly if reviewed archive evidence disappears before replay."""
    if not source_path.is_file():
        raise ReprocessError(f"retained history archive no longer exists: {source_path}")
    return reprocess_retained_history_archive(
        root=root,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        archive_path=source_path,
        fact_interpreter=history_fact_interpreter,
        lab_resolution_provider=decision_interpreter,
        source_path_prefix=source_path_prefix,
        refresh_reports=refresh_reports,
    )


def _replay_bounded_study_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    history_fact_interpreter: FactInterpreter | None,
    decision_interpreter: RetainedHistoryDecisionInterpreter | None,
    source_path_prefix: Path | None,
    refresh_reports: bool,
) -> ReprocessResult:
    """Replay a routed study through History and any attended Lab decision."""
    history_result = reprocess_retained_history_source(
        root=root,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        source_path=source_path,
        fact_interpreter=history_fact_interpreter,
        source_path_prefix=source_path_prefix,
        refresh_reports=refresh_reports,
        source_category="Labs",
    )
    if decision_interpreter is None:
        return history_result

    payload = decision_interpreter.payload_for_source(source_path, "labs")
    if payload is None:
        return history_result
    extraction = validate_fact_extraction(payload)
    source_reference = retained_source_reference(
        root=root,
        processed_root=processed_root,
        source_path=source_path,
        source_path_prefix=source_path_prefix,
    )
    rows = canonicalize_candidate_lab_facts(
        extraction,
        source_reference,
        loinc_index=load_loinc_index(root / "data" / "reference"),
    )
    dedup_stats: dict[str, int] = {}
    persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id=extraction.person_id,
        source_file=source_path,
        processed_source_path=source_reference,
        checksum=source_file_checksum(source_path),
        source_type="ai_extracted",
        rows=rows,
        retained_source=True,
        source_file_name=source_path.name,
        refresh_reports=refresh_reports,
        dedup_stats=dedup_stats,
    )
    return ReprocessResult(
        imported_rows=history_result.imported_rows + dedup_stats.get("inserted", 0),
        affected_person_ids={
            *history_result.affected_person_ids,
            extraction.person_id,
        },
    )


def _replay_retained_medication_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    decision_interpreter: RetainedHistoryDecisionInterpreter | None,
    source_path_prefix: Path | None,
    refresh_reports: bool,
) -> ReprocessResult:
    """Replay a retained medication list from a decision or bounded parser."""
    payload = (
        decision_interpreter.payload_for_source(source_path, "medications")
        if decision_interpreter is not None
        else None
    )
    if payload is None:
        return reprocess_retained_supplement_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            refresh_reports=refresh_reports,
            source_path_prefix=source_path_prefix,
        )

    return _persist_retained_medication_decision(
        root=root,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        source_path=source_path,
        payload=payload,
        source_path_prefix=source_path_prefix,
        refresh_reports=refresh_reports,
    )


def _persist_retained_medication_decision(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    payload: Mapping[str, Any],
    source_path_prefix: Path | None,
    refresh_reports: bool,
) -> ReprocessResult:
    extraction = validate_fact_extraction(payload)
    source_reference = retained_source_reference(
        root=root,
        processed_root=processed_root,
        source_path=source_path,
        source_path_prefix=source_path_prefix,
    )
    rows = canonicalize_candidate_medication_facts(extraction, source_reference)
    fill_rxnorm_cuis(rows, load_rxnorm_index(root / "data" / "reference"))
    document_date = medication_document_date(
        rows,
        extraction.document_date,
        source_reference,
    )
    persist_medication_rows(
        conn=conn,
        vault_root=vault_root,
        person_id=extraction.person_id,
        source_file=source_path,
        processed_source_path=source_reference,
        checksum=source_file_checksum(source_path),
        source_type="ai_extracted",
        rows=rows,
        document_date=document_date,
        retained_source=True,
        source_file_name=source_path.name,
        refresh_reports=refresh_reports,
    )
    return ReprocessResult(
        imported_rows=len(rows),
        affected_person_ids={extraction.person_id},
    )


def _replay_retained_history_source(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    source_path: Path,
    history_fact_interpreter: FactInterpreter | None,
    decision_interpreter: RetainedHistoryDecisionInterpreter | None,
    source_path_prefix: Path | None,
    refresh_reports: bool,
) -> ReprocessResult:
    history_result = reprocess_retained_history_source(
        root=root,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        source_path=source_path,
        fact_interpreter=history_fact_interpreter,
        source_path_prefix=source_path_prefix,
        refresh_reports=refresh_reports,
    )
    payload = (
        decision_interpreter.payload_for_source(source_path, "medications")
        if decision_interpreter is not None
        else None
    )
    if payload is None:
        return history_result
    medication_result = _persist_retained_medication_decision(
        root=root,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        source_path=source_path,
        payload=payload,
        source_path_prefix=source_path_prefix,
        refresh_reports=refresh_reports,
    )
    return ReprocessResult(
        imported_rows=history_result.imported_rows + medication_result.imported_rows,
        affected_person_ids={
            *history_result.affected_person_ids,
            *medication_result.affected_person_ids,
        },
    )


def build_retained_reprocess_registry(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    batch_mode: bool = False,
    history_fact_interpreter: FactInterpreter | None = None,
) -> ReprocessRegistry:
    """Build the shared retained-source registry for single and batch replay."""
    refresh_reports = not batch_mode
    source_path_prefix = PROCESSED_ROOT if batch_mode else None
    decision_interpreter = (
        history_fact_interpreter
        if isinstance(history_fact_interpreter, RetainedHistoryDecisionInterpreter)
        else None
    )
    registry = ReprocessRegistry()
    registry.register(
        name="labs",
        category_parts=("Labs",),
        suffixes={".csv", ".pdf"},
        path_predicate=lambda source_path: retained_lab_pdf_document_domain(source_path) is None,
        handler=lambda source_path: reprocess_retained_lab_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            requested_person_id=None,
            approve_pdf=True,
            review_only=False,
            refresh_reports=refresh_reports,
            show_review=refresh_reports,
            source_path_prefix=source_path_prefix,
        ),
    )
    registry.register(
        name="history_from_labs",
        category_parts=("Labs",),
        suffixes={".pdf"},
        path_predicate=lambda source_path: (
            retained_lab_pdf_document_domain(source_path) is not None
        ),
        handler=lambda source_path: _replay_bounded_study_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            history_fact_interpreter=history_fact_interpreter,
            decision_interpreter=decision_interpreter,
            source_path_prefix=source_path_prefix,
            refresh_reports=refresh_reports,
        ),
    )
    registry.register(
        name="supplement_list",
        category_parts=("Medications",),
        suffixes={""},
        handler=lambda source_path: _replay_retained_medication_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            decision_interpreter=decision_interpreter,
            source_path_prefix=source_path_prefix,
            refresh_reports=refresh_reports,
        ),
    )
    if not batch_mode:
        registry.register(
            name="user_update_labs",
            category_parts=("Labs",),
            suffixes={""},
            handler=lambda source_path: reprocess_retained_user_update_lab_source(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            ),
        )
    registry.register(
        name="history",
        category_parts=("History",),
        suffixes={".pdf", ".tif", ".tiff", ".xml"},
        handler=lambda source_path: _replay_retained_history_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            history_fact_interpreter=history_fact_interpreter,
            decision_interpreter=decision_interpreter,
            source_path_prefix=source_path_prefix,
            refresh_reports=refresh_reports,
        ),
    )
    if not batch_mode:
        registry.register(
            name="user_update_history",
            category_parts=("History",),
            suffixes={""},
            handler=lambda source_path: reprocess_retained_user_update_history_source(
                root=root,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            ),
        )
    registry.register(
        name="history_archive",
        category_parts=("HealthSummary",),
        suffixes={".zip"},
        handler=lambda source_path: _replay_retained_history_archive(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
            history_fact_interpreter=history_fact_interpreter,
            decision_interpreter=decision_interpreter,
            source_path_prefix=source_path_prefix,
            refresh_reports=refresh_reports,
        ),
    )
    return registry


def copy_retained_sources(
    *,
    source_root: Path,
    target_root: Path,
    allowed_target_files: set[Path] | None = None,
) -> list[Path]:
    """Copy retained evidence into the isolated target without mutating the source."""
    source_root = source_root.resolve()
    target_root = target_root.resolve()
    source_files = _validate_retained_source_copy(
        source_root,
        target_root,
        allowed_target_files=allowed_target_files,
    )

    for relative_path, source_path in source_files.items():
        target_path = target_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
    return [target_root / relative_path for relative_path in sorted(source_files)]


def validate_retained_source_copy(
    *,
    source_root: Path,
    target_root: Path,
    allowed_target_files: set[Path] | None = None,
) -> None:
    """Validate retained-copy compatibility without mutating either root."""
    _validate_retained_source_copy(
        source_root.resolve(),
        target_root.resolve(),
        allowed_target_files=allowed_target_files,
    )


def _validate_retained_source_copy(
    source_root: Path,
    target_root: Path,
    *,
    allowed_target_files: set[Path] | None = None,
) -> dict[Path, Path]:
    if not source_root.is_dir():
        raise ReprocessError(f"source processed root does not exist: {source_root}")
    if _paths_overlap(source_root, target_root):
        raise ReprocessError("source and target processed roots must not overlap")

    source_files = {
        path.relative_to(source_root): path for path in source_root.rglob("*") if path.is_file()
    }
    target_files = (
        {path.relative_to(target_root) for path in target_root.rglob("*") if path.is_file()}
        if target_root.exists()
        else set()
    )
    allowed = allowed_target_files or set()
    if any(path.is_absolute() or ".." in path.parts for path in allowed):
        raise ReprocessError("allowed target files must be relative paths within the target root")
    unexpected = sorted(target_files - set(source_files) - allowed)
    if unexpected:
        raise ReprocessError(
            "target processed root contains files outside the source corpus: "
            + ", ".join(str(path) for path in unexpected)
        )
    return source_files


def _user_update_snapshot_front_matter(source_path: Path) -> dict[str, Any] | None:
    if source_path.suffix:
        return None
    try:
        content = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not content.startswith("---\n"):
        return None
    closing = content.find("\n---\n", 4)
    if closing < 0:
        return None
    try:
        front_matter = yaml.safe_load(content[4:closing])
    except yaml.YAMLError:
        return None
    if not isinstance(front_matter, dict):
        return None
    return front_matter


def is_user_update_snapshot(source_path: Path) -> bool:
    front_matter = _user_update_snapshot_front_matter(source_path)
    if front_matter is None:
        return False
    if front_matter.get("source_type") == "user_update":
        return True
    if front_matter.get("record_type") == "user_update":
        return True
    return (
        front_matter.get("record_type") == "medication"
        and front_matter.get("category") == "medications"
        and bool(front_matter.get("date"))
    )


def _parse_medication_timeline_semantics(
    content: str,
) -> tuple[tuple[str, str, str, str, str, str], ...] | None:
    candidates = [
        line.strip()
        for line in content.splitlines()
        if len(_MEDICATION_TIMELINE_ENTRY.findall(line)) >= 2
    ]
    if len(candidates) != 1:
        return None
    outer = re.fullmatch(
        r"(?P<body>.+?)\s+\((?P<frequency>[^()]+)\)",
        candidates[0],
    )
    if not outer:
        return None
    body = outer.group("body")
    frequency = outer.group("frequency").strip().casefold()
    entries = list(_MEDICATION_TIMELINE_ENTRY.finditer(body))
    if len(entries) < 2:
        return None
    name = body[: entries[0].start()].strip().casefold()
    if not name:
        return None
    if body[entries[-1].end() :].strip():
        return None
    if any(
        body[left.end() : right.start()].strip() != ";"
        for left, right in zip(entries, entries[1:], strict=False)
    ):
        return None

    starts: list[date] = []
    try:
        starts = [date.fromisoformat(entry.group("start_date")) for entry in entries]
    except ValueError:
        return None
    if starts != sorted(starts) or len(starts) != len(set(starts)):
        return None

    semantics = []
    for index, (entry, start) in enumerate(zip(entries, starts, strict=True)):
        end = starts[index + 1] - timedelta(days=1) if index + 1 < len(starts) else None
        semantics.append(
            (
                name,
                " ".join(entry.group("dose").split()).casefold(),
                frequency,
                start.isoformat(),
                end.isoformat() if end else "",
                "active",
            )
        )
    return tuple(semantics)


def authority_backed_medication_narrative_matches_snapshot(
    source_path: Path,
    processed_root: Path,
) -> bool:
    """Return whether a strict raw timeline exactly matches its authority snapshot."""
    try:
        relative = source_path.resolve().relative_to(processed_root.resolve())
    except ValueError:
        return False
    if len(relative.parts) != 3 or relative.parts[1] != "Medications" or source_path.suffix:
        return False
    source_date = re.match(r"(?P<date>\d{4}-\d{2}-\d{2})(?:\s|-)", source_path.name)
    if not source_date:
        return False
    person_id = relative.parts[0]
    snapshot = (
        processed_root
        / person_id
        / "Medications"
        / f"{source_date.group('date')}-{person_id}-medications"
    )
    front_matter = _user_update_snapshot_front_matter(snapshot)
    if (
        front_matter is None
        or str(front_matter.get("person_id") or "") != person_id
        or str(front_matter.get("date") or "") != source_date.group("date")
        or front_matter.get("record_type") != "medication"
        or front_matter.get("category") != "medications"
    ):
        return False
    try:
        raw_semantics = _parse_medication_timeline_semantics(
            source_path.read_text(encoding="utf-8")
        )
        snapshot_facts = parse_medication_markdown_table(snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if raw_semantics is None or not snapshot_facts:
        return False
    snapshot_semantics = tuple(
        (
            fact.name.strip().casefold(),
            " ".join((fact.dose or "").split()).casefold(),
            (fact.frequency or "").strip().casefold(),
            (fact.start_date or "").strip(),
            (fact.end_date or "").strip(),
            (fact.status or "").strip().casefold(),
        )
        for fact in snapshot_facts
    )
    return raw_semantics == snapshot_semantics


def _rollback_source_replay(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    snapshot: dict[Path, bytes],
) -> None:
    conn.execute("ROLLBACK TO retained_replay_source")
    conn.execute("RELEASE retained_replay_source")
    restore_person_vault_tree(vault_root, person_id, snapshot)


def replay_retained_medical_sources(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
    history_fact_interpreter: FactInterpreter | None = None,
) -> RetainedReplaySummary:
    """Replay every retained file with per-source outcomes and savepoints."""
    registry = build_retained_reprocess_registry(
        root=root,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        batch_mode=True,
        history_fact_interpreter=history_fact_interpreter,
    )
    items: list[RetainedReplayItem] = []
    lab_person_ids: set[str] = set()
    medication_person_ids: set[str] = set()
    history_person_ids: set[str] = set()
    for source_path in sorted(path for path in processed_root.rglob("*") if path.is_file()):
        relative_path = source_path.relative_to(processed_root)
        if is_user_update_snapshot(source_path):
            items.append(
                RetainedReplayItem(
                    source_path=relative_path,
                    outcome="unsupported",
                    message="user-update snapshot deferred to the authority-overlay stage",
                )
            )
            continue
        if authority_backed_medication_narrative_matches_snapshot(
            source_path,
            processed_root,
        ):
            items.append(
                RetainedReplayItem(
                    source_path=relative_path,
                    outcome="unsupported",
                    message=(
                        "authority-backed medication narrative deferred to the "
                        "authority-overlay stage"
                    ),
                )
            )
            continue
        try:
            registration = registry.handler_for(source_path, processed_root)
        except ReprocessError as exc:
            items.append(
                RetainedReplayItem(
                    source_path=relative_path,
                    outcome="unsupported",
                    message=str(exc),
                )
            )
            continue

        person_id = relative_path.parts[0]
        vault_snapshot = snapshot_person_vault_tree(vault_root, person_id)
        conn.execute("SAVEPOINT retained_replay_source")
        try:
            result = registration.handler(source_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            _rollback_source_replay(conn, vault_root, person_id, vault_snapshot)
            items.append(
                RetainedReplayItem(
                    source_path=relative_path,
                    outcome="failed",
                    handler_name=registration.name,
                    message=str(exc),
                )
            )
            continue
        except Exception:
            _rollback_source_replay(conn, vault_root, person_id, vault_snapshot)
            raise
        conn.execute("RELEASE retained_replay_source")
        conn.commit()

        if result.imported_rows > 0:
            outcome: ReplayOutcome = "imported"
        elif not result.affected_person_ids:
            outcome = "zero_fact"
        else:
            outcome = "redundant"
        items.append(
            RetainedReplayItem(
                source_path=relative_path,
                outcome=outcome,
                handler_name=registration.name,
                imported_rows=result.imported_rows,
                affected_person_ids=tuple(sorted(result.affected_person_ids)),
            )
        )
        if registration.name == "labs" or registration.name in _HISTORY_HANDLER_NAMES:
            lab_person_ids.update(result.affected_person_ids)
        if registration.name == "supplement_list" or registration.name in _HISTORY_HANDLER_NAMES:
            medication_person_ids.update(result.affected_person_ids)
        if registration.name in _HISTORY_HANDLER_NAMES:
            history_person_ids.update(result.affected_person_ids)

    refresh_person_ids = lab_person_ids | medication_person_ids | history_person_ids
    refresh_snapshots = {
        person_id: snapshot_person_vault_tree(vault_root, person_id)
        for person_id in refresh_person_ids
    }
    conn.execute("SAVEPOINT retained_replay_reports")
    try:
        for person_id in sorted(lab_person_ids):
            refresh_lab_dependent_reports(conn, vault_root, person_id)
        for person_id in sorted(medication_person_ids):
            write_current_medications_report(vault_root, person_id, conn)
            write_medication_conflicts_report(conn, vault_root, person_id)
            refresh_medication_dependent_reports(conn, vault_root, person_id)
            if person_id not in lab_person_ids:
                refresh_fhir_dependent_export(conn, vault_root, person_id)
        for person_id in sorted(history_person_ids):
            write_medical_history_summary_report(vault_root, person_id)
    except Exception:
        conn.execute("ROLLBACK TO retained_replay_reports")
        conn.execute("RELEASE retained_replay_reports")
        for person_id, snapshot in refresh_snapshots.items():
            restore_person_vault_tree(vault_root, person_id, snapshot)
        raise
    conn.execute("RELEASE retained_replay_reports")
    conn.commit()
    return RetainedReplaySummary(items=tuple(items))
