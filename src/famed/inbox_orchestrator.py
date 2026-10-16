#!/usr/bin/env python3
"""Read-only unified inbox inspection and routing."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from famed import import_labs, import_person
from famed.clinical_interpreter import InterpreterFactExtraction
from famed.dependent_reports import refresh_dna_dependent_reports, refresh_lab_dependent_reports
from famed.dna_sync import (
    DnaSyncError,
    detect_provider,
    dna_file_checksum,
    is_already_imported,
    persist_dna_source,
)
from famed.history_sync import (
    HistoryReviewDocument,
    HistorySyncError,
    auto_accept_history_review_notes_for_source_prefix,
    extract_ccda_immunization_rows,
    extract_ccda_lab_rows,
    extract_ccda_medication_rows,
    extract_ccda_vital_rows,
    match_history_document_to_person,
    parse_ccda_review_document,
    parse_text_history_review_document,
    persist_history_document_review_note,
    processed_history_source_path,
    remove_linked_category_placeholder_note,
    reprocess_retained_history_archive,
)
from famed.immunization_sync import (
    ImmunizationSyncError,
    immunization_document_date,
    persist_immunization_rows,
    validate_immunization_rows_for_persistence,
)
from famed.lab_sync import log_ingestion_event, source_file_checksum, write_category_index_note
from famed.medication_sync import (
    MedicationSyncError,
    build_supplement_extraction,
    canonicalize_candidate_medication_facts,
    fill_rxnorm_cuis,
    medication_document_date,
    parse_medication_csv,
    persist_medication_rows,
    quarantine_medication_import_error,
    retain_medication_source_for_import,
)
from famed.person_sync import (
    PersonSyncError,
    apply_relationship_closure,
    check_relationship_age_plausibility,
    create_person_interactive,
    ensure_person_record_scaffold,
    load_person_display_name,
    load_person_from_note,
    replace_relationships,
    write_person_note_idempotent,
)
from famed.reference_cache import load_loinc_index, load_rxnorm_index
from famed.source_paths import is_hidden_source_path
from famed.storage_contract import (
    AUDIT_INBOX_ROUTE_QUARANTINED,
    AUDIT_LAB_OBSERVATION_REJECTED,
    AUDIT_PLACEHOLDER_IMPORT_ACCEPTED,
    STATE_AWAITING_USER_DECISION,
    STATE_PROCESSED,
    STATE_QUARANTINED,
    ProcessingState,
    configured_storage_roots,
    log_audit_event,
)
from famed.vital_sync import (
    persist_vital_rows,
    vital_document_date,
)

InboxScope = Literal["person", "shared"]
FinalState = ProcessingState | Literal["dry_run", "skipped", "failed"]
ATTENTION_FINAL_STATES: set[FinalState] = {
    STATE_AWAITING_USER_DECISION,
    STATE_QUARANTINED,
    "failed",
    "skipped",
}

PERSON_IMPORT_EXTENSIONS = {".yaml", ".yml"}
LAB_IMPORT_EXTENSIONS = {".csv", ".pdf"}
MEDICATION_IMPORT_EXTENSIONS = {".csv"}
DNA_IMPORT_EXTENSIONS = {".txt", ".csv", ".vcf", ".gz", ".zip"}
FACT_EXTRACT_SOURCE_EXTENSIONS = {
    "",
    ".htm",
    ".html",
    ".jpg",
    ".jpeg",
    ".md",
    ".pdf",
    ".png",
    ".txt",
}
MEDICATION_FACT_EXTRACT_EXTENSIONS = FACT_EXTRACT_SOURCE_EXTENSIONS
HISTORY_REVIEW_EXTENSIONS = {".pdf", ".tif", ".tiff", ".xml"}
PLACEHOLDER_IMPORT_EXTENSIONS = {".csv", ".md", ".pdf", ".tsv", ".txt"}
PERSON_CATEGORY_HANDLERS: dict[str, tuple[str, set[str]]] = {
    "labs": ("import_labs", LAB_IMPORT_EXTENSIONS),
    "history": ("history_review", HISTORY_REVIEW_EXTENSIONS),
    "medications": ("import_medications_csv", MEDICATION_IMPORT_EXTENSIONS),
    "dna": ("import_dna", DNA_IMPORT_EXTENSIONS),
}
PLACEHOLDER_CATEGORY_DIRS = {
    "dna": "Genes",
    "history": "History",
    "medications": "Medications",
}


@dataclass(frozen=True)
class InboxRoute:
    source_path: Path
    relative_path: Path
    scope: InboxScope
    person_id: str | None
    category: str
    status: ProcessingState | Literal["routed"]
    handler: str | None
    reason: str


@dataclass(frozen=True)
class InboxExecution:
    route: InboxRoute
    exit_code: int | None
    action: Literal["dry_run", "executed", "skipped"]
    final_state: FinalState
    message: str


@dataclass(frozen=True)
class InboxResolution:
    person_id: str
    category: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _route_person_import(path: Path, inbox_root: Path) -> InboxRoute:
    if path.suffix.lower() not in PERSON_IMPORT_EXTENSIONS:
        return InboxRoute(
            source_path=path,
            relative_path=path.relative_to(inbox_root),
            scope="shared",
            person_id=None,
            category="persons",
            status=STATE_QUARANTINED,
            handler=None,
            reason="unsupported person import extension",
        )

    return InboxRoute(
        source_path=path,
        relative_path=path.relative_to(inbox_root),
        scope="shared",
        person_id=None,
        category="persons",
        status="routed",
        handler="import_person",
        reason="person YAML import",
    )


def _route_person_scoped(path: Path, inbox_root: Path, parts: tuple[str, ...]) -> InboxRoute:
    person_id, category = parts[0], parts[1].lower()
    handler_config = PERSON_CATEGORY_HANDLERS.get(category)
    if handler_config is None:
        placeholder_category = PLACEHOLDER_CATEGORY_DIRS.get(category)
        if placeholder_category and path.suffix.lower() in PLACEHOLDER_IMPORT_EXTENSIONS:
            return InboxRoute(
                source_path=path,
                relative_path=path.relative_to(inbox_root),
                scope="person",
                person_id=person_id,
                category=category,
                status="routed",
                handler="category_placeholder",
                reason="unsupported category routed to reviewed placeholder",
            )
        return InboxRoute(
            source_path=path,
            relative_path=path.relative_to(inbox_root),
            scope="person",
            person_id=person_id,
            category=category,
            status=STATE_QUARANTINED,
            handler=None,
            reason="unsupported person-scoped inbox category",
        )

    handler, supported_extensions = handler_config
    if path.suffix.lower() not in supported_extensions:
        if category == "medications" and path.suffix.lower() in MEDICATION_FACT_EXTRACT_EXTENSIONS:
            return InboxRoute(
                source_path=path,
                relative_path=path.relative_to(inbox_root),
                scope="person",
                person_id=person_id,
                category=category,
                status="routed",
                handler="medication_fact_extract",
                reason="person-scoped medication fact extraction route",
            )
        placeholder_category = PLACEHOLDER_CATEGORY_DIRS.get(category)
        if placeholder_category and path.suffix.lower() in PLACEHOLDER_IMPORT_EXTENSIONS:
            return InboxRoute(
                source_path=path,
                relative_path=path.relative_to(inbox_root),
                scope="person",
                person_id=person_id,
                category=category,
                status="routed",
                handler="category_placeholder",
                reason="unsupported category routed to reviewed placeholder",
            )
        return InboxRoute(
            source_path=path,
            relative_path=path.relative_to(inbox_root),
            scope="person",
            person_id=person_id,
            category=category,
            status=STATE_QUARANTINED,
            handler=None,
            reason="unsupported file extension for category",
        )

    return InboxRoute(
        source_path=path,
        relative_path=path.relative_to(inbox_root),
        scope="person",
        person_id=person_id,
        category=category,
        status="routed",
        handler=handler,
        reason="person-scoped handler route",
    )


def _looks_like_health_summary_archive(path: Path) -> bool:
    if path.suffix.lower() != ".zip":
        return False
    normalized_name = path.name.casefold().replace("-", " ").replace("_", " ")
    compact_name = normalized_name.replace(" ", "")
    return "healthsummary" in compact_name or "health summary" in normalized_name


def _person_route_from_inferred_category(
    path: Path,
    inbox_root: Path,
    *,
    person_id: str,
    category: str,
    reason: str,
) -> InboxRoute:
    synthetic_parts = (person_id, category, path.name)
    route = _route_person_scoped(path, inbox_root, synthetic_parts)
    if route.status != "routed":
        return route
    return InboxRoute(
        source_path=route.source_path,
        relative_path=route.relative_path,
        scope=route.scope,
        person_id=route.person_id,
        category=route.category,
        status=route.status,
        handler=route.handler,
        reason=reason,
    )


def _looks_like_person_root_dna(path: Path) -> bool:
    if path.suffix.lower() not in DNA_IMPORT_EXTENSIONS:
        return False
    return detect_provider(path) is not None


def _looks_like_medication_csv(path: Path, person_id: str) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    try:
        parse_medication_csv(path, person_id)
    except MedicationSyncError:
        return False
    return True


def _route_person_root_file(path: Path, inbox_root: Path, person_id: str) -> InboxRoute:
    if _looks_like_health_summary_archive(path):
        return InboxRoute(
            source_path=path,
            relative_path=path.relative_to(inbox_root),
            scope="person",
            person_id=person_id,
            category="history",
            status="routed",
            handler="import_history_archive",
            reason="person-scoped health summary archive route",
        )

    if _looks_like_person_root_dna(path):
        return _person_route_from_inferred_category(
            path,
            inbox_root,
            person_id=person_id,
            category="dna",
            reason="person-root inferred DNA route",
        )

    if (
        _looks_like_supplement_source(path)
        or _looks_like_medication_source(path)
        or _looks_like_medication_csv(path, person_id)
    ):
        return _person_route_from_inferred_category(
            path,
            inbox_root,
            person_id=person_id,
            category="medications",
            reason="person-root inferred medication route",
        )

    if _looks_like_history_review_document(path):
        return _person_route_from_inferred_category(
            path,
            inbox_root,
            person_id=person_id,
            category="history",
            reason="person-root inferred history route",
        )

    if path.suffix.lower() == ".csv" or _looks_like_lab_pdf(path):
        return _person_route_from_inferred_category(
            path,
            inbox_root,
            person_id=person_id,
            category="labs",
            reason="person-root inferred lab route",
        )

    return InboxRoute(
        source_path=path,
        relative_path=path.relative_to(inbox_root),
        scope="person",
        person_id=person_id,
        category="unknown",
        status=STATE_QUARANTINED,
        handler=None,
        reason="unsupported person-root inbox file",
    )


def _route_resolved_shared(
    path: Path,
    inbox_root: Path,
    *,
    person_id: str,
    category: str,
) -> InboxRoute:
    categories = _split_resolved_categories(category)
    if len(categories) > 1:
        return _route_resolved_shared_multi_category(
            path,
            inbox_root,
            person_id=person_id,
            categories=categories,
        )

    synthetic_parts = (person_id, category, path.name)
    route = _route_person_scoped(path, inbox_root, synthetic_parts)
    if route.status != "routed":
        return route
    reason = (
        route.reason
        if route.handler == "category_placeholder"
        else "shared inbox item resolved by user"
    )
    return InboxRoute(
        source_path=route.source_path,
        relative_path=route.relative_path,
        scope="shared",
        person_id=route.person_id,
        category=route.category,
        status=route.status,
        handler=route.handler,
        reason=reason,
    )


def _route_resolved_shared_multi_category(
    path: Path,
    inbox_root: Path,
    *,
    person_id: str,
    categories: list[str],
) -> InboxRoute:
    unsupported_categories = [
        category for category in categories if category not in PLACEHOLDER_CATEGORY_DIRS
    ]
    if unsupported_categories:
        return InboxRoute(
            source_path=path,
            relative_path=path.relative_to(inbox_root),
            scope="shared",
            person_id=person_id,
            category=",".join(categories),
            status=STATE_QUARANTINED,
            handler=None,
            reason=(
                "multi-category resolution supports placeholder categories only; "
                f"unsupported: {', '.join(unsupported_categories)}"
            ),
        )

    if path.suffix.lower() not in PLACEHOLDER_IMPORT_EXTENSIONS:
        return InboxRoute(
            source_path=path,
            relative_path=path.relative_to(inbox_root),
            scope="shared",
            person_id=person_id,
            category=",".join(categories),
            status=STATE_QUARANTINED,
            handler=None,
            reason="unsupported file extension for multi-category placeholder route",
        )

    return InboxRoute(
        source_path=path,
        relative_path=path.relative_to(inbox_root),
        scope="shared",
        person_id=person_id,
        category=",".join(categories),
        status="routed",
        handler="multi_category_placeholder",
        reason="shared inbox item resolved to multiple placeholder categories",
    )


def _split_resolved_categories(category: str) -> list[str]:
    return [part.strip().lower() for part in category.split(",") if part.strip()]


def inspect_inbox_file(path: Path, inbox_root: Path) -> InboxRoute:
    relative_path = path.relative_to(inbox_root)
    parts = relative_path.parts
    if not parts:
        return InboxRoute(
            source_path=path,
            relative_path=relative_path,
            scope="shared",
            person_id=None,
            category="unknown",
            status=STATE_QUARANTINED,
            handler=None,
            reason="empty inbox path",
        )

    if parts[0] == "persons":
        return _route_person_import(path, inbox_root)

    if parts[0] in {"shared", "other"}:
        category = parts[1] if len(parts) > 1 else "unknown"
        return InboxRoute(
            source_path=path,
            relative_path=relative_path,
            scope="shared",
            person_id=None,
            category=category,
            status=STATE_AWAITING_USER_DECISION,
            handler=None,
            reason="shared inbox item requires person/category resolution",
        )

    if len(parts) == 1 and _looks_like_history_review_document(path):
        return InboxRoute(
            source_path=path,
            relative_path=relative_path,
            scope="shared",
            person_id=None,
            category="history",
            status=STATE_AWAITING_USER_DECISION,
            handler=None,
            reason="history review document requires person resolution",
        )

    if len(parts) == 1 and _looks_like_lab_pdf(path):
        return InboxRoute(
            source_path=path,
            relative_path=relative_path,
            scope="shared",
            person_id=None,
            category="labs",
            status=STATE_AWAITING_USER_DECISION,
            handler=None,
            reason="lab PDF requires person resolution",
        )

    if len(parts) == 1 and _looks_like_supplement_source(path):
        return InboxRoute(
            source_path=path,
            relative_path=relative_path,
            scope="shared",
            person_id=None,
            category="medications",
            status=STATE_AWAITING_USER_DECISION,
            handler=None,
            reason="supplement list requires person resolution",
        )

    if len(parts) == 1 and _looks_like_medication_source(path):
        return InboxRoute(
            source_path=path,
            relative_path=relative_path,
            scope="shared",
            person_id=None,
            category="medications",
            status=STATE_AWAITING_USER_DECISION,
            handler=None,
            reason="medication list requires person resolution",
        )

    if len(parts) == 2:
        return _route_person_root_file(path, inbox_root, parts[0])

    if len(parts) >= 3:
        return _route_person_scoped(path, inbox_root, parts)

    return InboxRoute(
        source_path=path,
        relative_path=relative_path,
        scope="shared",
        person_id=None,
        category="unknown",
        status=STATE_QUARANTINED,
        handler=None,
        reason="unsupported inbox layout",
    )


def _looks_like_history_review_document(path: Path) -> bool:
    normalized_name = path.name.casefold().replace(" ", "").replace("-", "").replace("_", "")
    if path.suffix.lower() == ".xml":
        return "visitsummary" in normalized_name
    if path.suffix.lower() == ".pdf":
        return _has_history_review_filename_keyword(normalized_name)
    if path.suffix.lower() in {".tif", ".tiff"}:
        return _has_history_review_filename_keyword(normalized_name)
    return False


def _has_history_review_filename_keyword(normalized_name: str) -> bool:
    return any(
        keyword in normalized_name
        for keyword in (
            "calciumscoring",
            "colonoscopy",
            "gihistology",
            "mri",
            "pathology",
            "usextremity",
            "vaccine",
            "visitnote",
            "visitsummary",
            "xr",
        )
    )


def _looks_like_lab_pdf(path: Path) -> bool:
    if path.suffix.lower() != ".pdf":
        return False
    normalized_name = path.name.casefold().replace(" ", "").replace(":", "")
    return any(
        keyword in normalized_name
        for keyword in (
            "ana",
            "automateddiff",
            "bloodcount",
            "cbc",
            "chem8",
            "comprehensivemetabolicpanel",
            "crp",
            "gfr",
            "hemoglobina1c",
            "hepatitiscantibody",
            "lipase",
            "lipid",
            "liverpanel",
            "miscellaneouslc",
            "nmr",
            "poct",
            "psa",
            "t4free",
            "thyroxine",
            "tsh",
            "troponin",
            "vitaminb12",
        )
    )


def _looks_like_supplement_source(path: Path) -> bool:
    return path.suffix.lower() in {"", ".md", ".txt"} and "supplement" in path.name.casefold()


def _looks_like_medication_source(path: Path) -> bool:
    if path.suffix.lower() not in MEDICATION_FACT_EXTRACT_EXTENSIONS:
        return False
    normalized_name = path.name.casefold().replace("_", "-").replace(" ", "-")
    return any(
        keyword in normalized_name
        for keyword in (
            "current-med",
            "drug-list",
            "med-list",
            "medication",
            "medications",
            "prescription",
            "prescriptions",
            "rx",
        )
    )


def inspect_inbox_file_with_resolution(
    path: Path,
    inbox_root: Path,
    *,
    resolve_person: str | None = None,
    resolve_category: str | None = None,
    resolutions: dict[Path, InboxResolution] | None = None,
) -> InboxRoute:
    route = inspect_inbox_file(path, inbox_root)
    if route.status != STATE_AWAITING_USER_DECISION:
        return route
    resolution = (resolutions or {}).get(route.relative_path)
    if resolution:
        return _route_resolved_shared(
            path,
            inbox_root,
            person_id=resolution.person_id,
            category=resolution.category,
        )
    # A shared file is only resolvable when both halves are supplied; either
    # missing leaves it awaiting a user decision.
    if not resolve_person or not resolve_category:
        return route
    return _route_resolved_shared(
        path,
        inbox_root,
        person_id=resolve_person,
        category=resolve_category,
    )


def inspect_inbox(
    inbox_root: Path,
    *,
    resolve_person: str | None = None,
    resolve_category: str | None = None,
    resolutions: dict[Path, InboxResolution] | None = None,
) -> list[InboxRoute]:
    if not inbox_root.exists():
        return []
    return [
        inspect_inbox_file_with_resolution(
            path,
            inbox_root,
            resolve_person=resolve_person,
            resolve_category=resolve_category,
            resolutions=resolutions,
        )
        for path in sorted(inbox_root.rglob("*"))
        if path.is_file() and not is_hidden_source_path(path, inbox_root)
    ]


def load_resolution_file(path: Path) -> dict[Path, InboxResolution]:
    """Load per-file shared inbox resolutions from a TSV file."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required_columns = {"relative_path", "person_id", "category"}
        fieldnames = set(reader.fieldnames or [])
        missing_columns = sorted(required_columns - fieldnames)
        if missing_columns:
            raise ValueError(
                "resolution file is missing required column(s): " + ", ".join(missing_columns)
            )

        resolutions: dict[Path, InboxResolution] = {}
        for row_number, row in enumerate(reader, start=2):
            relative_path = (row.get("relative_path") or "").strip()
            person_id = (row.get("person_id") or "").strip()
            category = (row.get("category") or "").strip()
            if not relative_path or not person_id or not category:
                raise ValueError(
                    "resolution file rows must include relative_path, person_id, "
                    f"and category; row {row_number} is incomplete"
                )
            resolutions[Path(relative_path)] = InboxResolution(
                person_id=person_id,
                category=category,
            )

    return resolutions


def _ledger_path(root: Path) -> Path:
    return root / "data" / "inbox_state.tsv"


def append_inbox_state_ledger(root: Path, executions: list[InboxExecution]) -> Path:
    ledger_path = _ledger_path(root)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not ledger_path.exists()
    with ledger_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        if write_header:
            writer.writerow(
                [
                    "recorded_at",
                    "source_path",
                    "relative_path",
                    "scope",
                    "person_id",
                    "category",
                    "route_status",
                    "handler",
                    "action",
                    "final_state",
                    "exit_code",
                    "reason",
                    "message",
                ]
            )
        recorded_at = datetime.now(UTC).isoformat()
        for execution in executions:
            route = execution.route
            writer.writerow(
                [
                    recorded_at,
                    str(route.source_path),
                    str(route.relative_path),
                    route.scope,
                    route.person_id or "",
                    route.category,
                    route.status,
                    route.handler or "",
                    execution.action,
                    execution.final_state,
                    "" if execution.exit_code is None else str(execution.exit_code),
                    route.reason,
                    execution.message,
                ]
            )
    return ledger_path


def format_batch_summary(executions: list[InboxExecution]) -> list[str]:
    """Return human-readable batch totals and attention-needed rows."""
    counts = Counter(execution.final_state for execution in executions)
    state_order: tuple[FinalState, ...] = (
        STATE_PROCESSED,
        STATE_QUARANTINED,
        STATE_AWAITING_USER_DECISION,
        "dry_run",
        "skipped",
        "failed",
    )
    lines = ["", "Batch summary:"]
    for state in state_order:
        count = counts.get(state, 0)
        if count:
            lines.append(f"- {state}: {count}")

    attention_items = [
        execution for execution in executions if execution.final_state in ATTENTION_FINAL_STATES
    ]
    if attention_items:
        lines.append("Attention needed:")
        for execution in attention_items:
            route = execution.route
            lines.append(f"- {execution.final_state}: {route.relative_path} - {execution.message}")

    return lines


@contextmanager
def _temporary_cli(module: Any, root: Path, argv: list[str]) -> Generator[None]:
    original_argv = sys.argv
    original_repo_root = module.repo_root
    module.repo_root = lambda: root
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = original_argv
        module.repo_root = original_repo_root


def _execute_person_route(route: InboxRoute, root: Path) -> int:
    with _temporary_cli(
        import_person,
        root,
        ["import_person", "--person-file", str(route.source_path)],
    ):
        return import_person.main()


def _execute_lab_route(
    route: InboxRoute,
    root: Path,
    inbox_root: Path,
    approve_pdf: bool,
) -> InboxExecution:
    if route.source_path.suffix.lower() == ".pdf" and not approve_pdf:
        return InboxExecution(
            route=route,
            exit_code=None,
            action="skipped",
            final_state="skipped",
            message="PDF route requires --approve-pdf for non-interactive execution",
        )

    try:
        imported_rows = _process_lab_source_through_orchestrator(
            route,
            root=root,
            inbox_root=inbox_root,
            approve_pdf=approve_pdf,
        )
    except (OSError, sqlite3.Error, import_labs.LabSyncError) as exc:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message=f"orchestrator lab lifecycle failed: {exc}",
        )

    return InboxExecution(
        route=route,
        exit_code=0,
        action="executed",
        final_state="processed" if imported_rows else STATE_QUARANTINED,
        message=(
            f"orchestrator retained source and imported {imported_rows} lab row(s)"
            if imported_rows
            else "orchestrator quarantined source"
        ),
    )


def _process_lab_source_through_orchestrator(
    route: InboxRoute,
    *,
    root: Path,
    inbox_root: Path,
    approve_pdf: bool,
) -> int:
    storage_roots = configured_storage_roots(root)
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    source_file = route.source_path
    source_type = source_file.suffix.lower().lstrip(".")
    person_id = route.person_id

    with sqlite3.connect(db_path) as conn:
        try:
            rows = _load_lab_rows_for_route(
                route,
                inbox_root=inbox_root,
                approve_pdf=approve_pdf,
            )
            if not rows:
                raise import_labs.LabSyncError("lab source did not produce any rows")

            person_ids = {str(row["person_id"]) for row in rows}
            if len(person_ids) != 1:
                raise import_labs.LabSyncError(
                    f"all rows in a lab import must share one person_id; found {sorted(person_ids)}"
                )
            resolved_person_id = next(iter(person_ids))
            if person_id and person_id != resolved_person_id:
                raise import_labs.LabSyncError(
                    "resolved inbox person_id does not match lab rows; "
                    f"expected {person_id!r}, found {resolved_person_id!r}"
                )

            observed_at = min(str(row["observed_at"]) for row in rows)
            checksum, processed_source_path = import_labs.retain_lab_source_for_import(
                root,
                storage_roots.processed,
                resolved_person_id,
                observed_at,
                source_file,
            )
            import_labs.persist_lab_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=resolved_person_id,
                source_file=source_file,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type=source_type,
                rows=rows,
            )
            conn.commit()
            return len(rows)
        except (OSError, sqlite3.Error, import_labs.LabSyncError) as exc:
            if source_file.exists():
                import_labs.quarantine_lab_import_error(
                    conn,
                    root,
                    storage_roots.quarantine,
                    person_id,
                    source_file,
                    source_type,
                    f"orchestrator validation failed: {exc}",
                )
                conn.commit()
                return 0
            raise


def _load_lab_rows_for_route(
    route: InboxRoute,
    *,
    inbox_root: Path,
    approve_pdf: bool,
) -> list[dict[str, Any]]:
    source_file = route.source_path
    if source_file.suffix.lower() == ".csv":
        inferred_person_id = route.person_id or import_labs.infer_person_id_from_inbox_path(
            source_file,
            inbox_root,
        )
        return import_labs.load_labs_from_csv(
            source_file,
            default_person_id=inferred_person_id,
        )

    if source_file.suffix.lower() == ".pdf":
        person_id = route.person_id or import_labs.infer_person_id_from_pdf_filename(source_file)
        if not person_id:
            raise import_labs.LabSyncError("person_id is required for PDF import")
        text = import_labs.extract_pdf_text(source_file)
        rows = import_labs.parse_labs_from_pdf_text(text, person_id, str(source_file))
        approved = import_labs.review_pdf_extraction(
            source_file,
            person_id,
            rows,
            skip_prompt=approve_pdf,
            review_only=False,
        )
        if not approved:
            raise import_labs.LabSyncError("import cancelled during review")
        return rows

    raise import_labs.LabSyncError(f"unsupported lab source extension: {source_file.suffix}")


def execute_route(
    route: InboxRoute,
    *,
    root: Path,
    inbox_root: Path,
    execute: bool,
    approve_pdf: bool = False,
    interactive: bool = False,
) -> InboxExecution:
    if route.status != "routed" or route.handler is None:
        if execute and route.status == STATE_AWAITING_USER_DECISION:
            if _can_try_history_review(route):
                result = _execute_history_review_route(route, root, skip_quarantine=interactive)
                if result.final_state == STATE_QUARANTINED and interactive:
                    return _retry_with_interactive_resolution(
                        route, root, inbox_root, execute, approve_pdf
                    )
                return result
            if _can_try_lab_pdf_route(route):
                result = _execute_unresolved_lab_pdf_route(
                    route,
                    root=root,
                    inbox_root=inbox_root,
                    approve_pdf=approve_pdf,
                )
                if result.final_state == STATE_QUARANTINED and interactive:
                    return _retry_with_interactive_resolution(
                        route, root, inbox_root, execute, approve_pdf
                    )
                return result
            if _can_try_supplement_route(route):
                result = _execute_supplement_route(route, root)
                if result.final_state == STATE_QUARANTINED and interactive:
                    return _retry_with_interactive_resolution(
                        route, root, inbox_root, execute, approve_pdf
                    )
                return result
            if interactive:
                resolved_person_id = _attempt_interactive_person_resolution(route, root, inbox_root)
                if resolved_person_id is not None:
                    resolved_route = InboxRoute(
                        source_path=route.source_path,
                        relative_path=route.relative_path,
                        scope="person",
                        person_id=resolved_person_id,
                        category=route.category if route.category != "unknown" else "labs",
                        status="routed",
                        handler=(
                            "import_labs"
                            if route.source_path.suffix.lower() in FACT_EXTRACT_SOURCE_EXTENSIONS
                            else "category_placeholder"
                        ),
                        reason="person resolved interactively",
                    )
                    return execute_route(
                        resolved_route,
                        root=root,
                        inbox_root=inbox_root,
                        execute=execute,
                        approve_pdf=approve_pdf,
                        interactive=interactive,
                    )

            return _execute_unresolved_route_quarantine(route, root)

        final_state: FinalState = "skipped"
        if route.status == STATE_AWAITING_USER_DECISION:
            final_state = STATE_AWAITING_USER_DECISION
        elif route.status == STATE_QUARANTINED:
            final_state = STATE_QUARANTINED
        return InboxExecution(
            route=route,
            exit_code=None,
            action="skipped",
            final_state=final_state,
            message=route.reason,
        )

    if not execute:
        return InboxExecution(
            route=route,
            exit_code=None,
            action="dry_run",
            final_state="dry_run",
            message="handler not invoked",
        )

    if route.handler == "import_person":
        exit_code = _execute_person_route(route, root)
        message = f"handler exited with {exit_code}"
        if exit_code == 0:
            storage_roots = configured_storage_roots(root)
            checksum = source_file_checksum(route.source_path)
            retained_source = import_labs.retain_source_file(
                route.source_path,
                storage_roots.processed / "persons" / route.source_path.name,
                checksum,
            )
            message = f"person import processed: {retained_source.relative_to(root)}"
        return InboxExecution(
            route=route,
            exit_code=exit_code,
            action="executed",
            final_state="processed" if exit_code == 0 else "failed",
            message=message,
        )

    if route.handler == "import_labs":
        return _execute_lab_route(route, root, inbox_root, approve_pdf)

    if route.handler == "history_review":
        return _execute_history_review_route(route, root)

    if route.handler == "import_history_archive":
        return _execute_history_archive_route(route, root)

    if route.handler == "import_dna":
        return _execute_dna_route(route, root)

    if route.handler == "import_medications_csv":
        return _execute_medication_csv_route(route, root)

    if route.handler == "medication_fact_extract":
        return _execute_placeholder_route(
            _as_placeholder_route(
                route, "medication facts require attended review; placeholder written"
            ),
            root,
        )

    if route.handler == "category_placeholder":
        return _execute_placeholder_route(route, root)

    if route.handler == "multi_category_placeholder":
        return _execute_multi_category_placeholder_route(route, root)

    return InboxExecution(
        route=route,
        exit_code=None,
        action="skipped",
        final_state="skipped",
        message=f"unknown handler: {route.handler}",
    )


def execute_routes(
    routes: list[InboxRoute],
    *,
    root: Path,
    inbox_root: Path,
    execute: bool,
    approve_pdf: bool = False,
    interactive: bool = False,
) -> list[InboxExecution]:
    return [
        execute_route(
            route,
            root=root,
            inbox_root=inbox_root,
            execute=execute,
            approve_pdf=approve_pdf,
            interactive=interactive,
        )
        for route in routes
    ]


def _can_try_history_review(route: InboxRoute) -> bool:
    return route.category in {"history", "unknown"} and route.source_path.suffix.lower() in {
        ".pdf",
        ".tif",
        ".tiff",
        ".xml",
    }


def _can_try_lab_pdf_route(route: InboxRoute) -> bool:
    return route.category in {"labs", "unknown"} and _looks_like_lab_pdf(route.source_path)


def _execute_unresolved_lab_pdf_route(
    route: InboxRoute,
    *,
    root: Path,
    inbox_root: Path,
    approve_pdf: bool,
) -> InboxExecution:
    if not approve_pdf:
        return InboxExecution(
            route=route,
            exit_code=None,
            action="skipped",
            final_state="skipped",
            message="PDF route requires --approve-pdf for non-interactive execution",
        )
    try:
        person_id = _match_pdf_patient_to_person(root / "famed-vault", route.source_path)
    except (import_labs.LabSyncError, HistorySyncError) as exc:
        _quarantine_unresolved_route(
            route,
            root,
            extra_reason=f"lab PDF person matching failed: {exc}",
        )
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="lab PDF quarantined: could not extract text for person matching",
        )
    if not person_id:
        _quarantine_unresolved_route(
            route,
            root,
            extra_reason="lab PDF patient did not match exactly one known person",
        )
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="lab PDF quarantined: ambiguous patient match",
        )
    routed = InboxRoute(
        source_path=route.source_path,
        relative_path=route.relative_path,
        scope=route.scope,
        person_id=person_id,
        category="labs",
        status="routed",
        handler="import_labs",
        reason="lab PDF patient matched exactly one known person",
    )
    return _execute_lab_route(routed, root, inbox_root, approve_pdf)


def _match_pdf_patient_to_person(vault_root: Path, source_file: Path) -> str | None:
    text = import_labs.extract_pdf_text(source_file)
    document = parse_text_history_review_document(source_file, text)
    return match_history_document_to_person(vault_root, document)


def _can_try_supplement_route(route: InboxRoute) -> bool:
    return route.category in {"medications", "unknown"} and _looks_like_supplement_source(
        route.source_path
    )


def _execute_supplement_route(route: InboxRoute, root: Path) -> InboxExecution:
    person_id = _match_filename_to_person(root / "famed-vault", route.source_path)
    if not person_id:
        _quarantine_unresolved_route(
            route,
            root,
            extra_reason="supplement filename did not match exactly one known person",
        )
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="supplement list quarantined: ambiguous patient match",
        )
    routed = InboxRoute(
        source_path=route.source_path,
        relative_path=route.relative_path,
        scope=route.scope,
        person_id=person_id,
        category="medications",
        status="routed",
        handler="supplement_canonical",
        reason="supplement filename matched exactly one known person",
    )
    extraction = build_supplement_extraction(person_id, route.source_path)
    if extraction is not None:
        note_path = _process_medication_fact_extraction(
            routed, root, extraction, source_type="supplement_list"
        )
        if note_path is not None:
            return InboxExecution(
                route=routed,
                exit_code=0,
                action="executed",
                final_state=STATE_PROCESSED,
                message=f"supplement medication note: {note_path}",
            )
        # Persistence failed; _process_medication_fact_extraction already
        # quarantined or logged the failure.
        return InboxExecution(
            route=routed,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="supplement list quarantined: canonical persistence failed",
        )
    # No parseable supplement lines — fall back to placeholder so the source
    # still reaches a documented outcome (REQ-001 no silent loss).
    return _execute_placeholder_route(
        _as_placeholder_route(
            routed, "supplement file had no parseable items; placeholder for review"
        ),
        root,
    )


def _execute_dna_route(route: InboxRoute, root: Path) -> InboxExecution:
    person_id = route.person_id
    if not person_id:
        return _execute_unresolved_route_quarantine(route, root)

    provider = detect_provider(route.source_path)
    if provider is None:
        try:
            _quarantine_unresolved_route(
                route,
                root,
                extra_reason=(
                    "DNA format not recognised — expected 23andMe, AncestryDNA, "
                    "MyHeritage, Genotek, or VCF/VCF.GZ"
                ),
            )
        except (OSError, sqlite3.Error):
            pass
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="DNA file format not recognised; quarantined",
        )

    db_path = root / "data" / "famed.db"
    vault_root = root / "famed-vault"
    storage_roots = configured_storage_roots(root)

    try:
        checksum = dna_file_checksum(route.source_path)
        conn = sqlite3.connect(str(db_path))
        try:
            if is_already_imported(conn, person_id, checksum):
                return InboxExecution(
                    route=route,
                    exit_code=0,
                    action="skipped",
                    final_state=STATE_PROCESSED,
                    message=f"DNA file already imported for {person_id} (duplicate checksum)",
                )

            today = datetime.now(UTC).date().isoformat()

            processed_dir = storage_roots.processed / person_id / "Genes"
            processed_dir.mkdir(parents=True, exist_ok=True)
            processed_dest = processed_dir / f"{today}-{route.source_path.name}"
            processed_source_str = str(processed_dest.relative_to(root))
            variant_count = persist_dna_source(
                conn,
                vault_root,
                person_id=person_id,
                source_path=route.source_path,
                canonical_source_path=processed_source_str,
                source_file_name=route.source_path.name,
                note_name=(
                    f"{today}-{import_labs.slugify_filename(route.source_path.stem)}-dna.md"
                ),
                imported_at=today,
                provider=provider,
            )

            refresh_dna_dependent_reports(conn, vault_root, person_id)

            import_labs.retain_source_file(route.source_path, processed_dest, checksum)
            conn.commit()
        finally:
            conn.close()

        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_PROCESSED,
            message=f"DNA imported: {variant_count:,} variants from {provider}",
        )

    except (DnaSyncError, OSError, sqlite3.Error) as exc:
        try:
            _quarantine_unresolved_route(route, root, extra_reason=str(exc))
        except (OSError, sqlite3.Error):
            pass
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state=STATE_QUARANTINED,
            message=f"DNA import failed: {exc}",
        )


def _execute_medication_csv_route(route: InboxRoute, root: Path) -> InboxExecution:
    if route.person_id is None:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message="medication CSV route requires resolved person_id",
        )

    storage_roots = configured_storage_roots(root)
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    source_file = route.source_path
    person_id = route.person_id
    source_type = "medication_csv"

    try:
        rows = parse_medication_csv(source_file, person_id)
        document_date = medication_document_date(rows, None, str(route.relative_path))
    except MedicationSyncError as exc:
        with sqlite3.connect(db_path) as conn:
            quarantine_medication_import_error(
                conn,
                root,
                storage_roots.quarantine,
                person_id,
                source_file,
                source_type,
                f"medication CSV validation failed: {exc}",
            )
            conn.commit()
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="medication CSV quarantined: validation failed",
        )

    with sqlite3.connect(db_path) as conn:
        try:
            checksum, processed_source_path = retain_medication_source_for_import(
                root,
                storage_roots.processed,
                person_id,
                document_date,
                source_file,
            )
            note_path = persist_medication_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=source_file,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type=source_type,
                rows=rows,
                document_date=document_date,
            )
            _delete_superseded_placeholder(vault_root, person_id, "Medications", source_file)
            conn.commit()
            return InboxExecution(
                route=route,
                exit_code=0,
                action="executed",
                final_state=STATE_PROCESSED,
                message=f"medication CSV note: {note_path.relative_to(vault_root)}",
            )
        except (OSError, sqlite3.Error, MedicationSyncError) as exc:
            if source_file.exists():
                quarantine_medication_import_error(
                    conn,
                    root,
                    storage_roots.quarantine,
                    person_id,
                    source_file,
                    source_type,
                    f"medication CSV persist failed: {exc}",
                )
                conn.commit()
                return InboxExecution(
                    route=route,
                    exit_code=0,
                    action="executed",
                    final_state=STATE_QUARANTINED,
                    message="medication CSV quarantined: persistence failed",
                )
            raise


def resolve_person_interactive(
    source_path: Path,
    text: str,
    *,
    root: Path,
    vault_root: Path,
    inbox_root: Path,
    _stdin: object = None,
) -> str | None:
    """Interactive person resolution for a source file.

    Displays extracted/OCR'd text and the file path, lists existing persons
    from the vault, and offers:

    - Select an existing person
    - Create a new person (with relationship prompts)
    - Cancel (quarantine)

    Works on a cold start (no existing persons).

    Returns the resolved ``person_id``, or ``None`` when the user cancels.
    """
    stdin = _stdin or sys.stdin

    print(f"\n{'=' * 60}")
    print(f"Person resolution needed for: {source_path}")
    if text:
        cleaned = text[:1000].strip()
        print(f"Document text:\n{cleaned}")
        if len(text) > 1000:
            print("... [truncated]")
    print(f"{'=' * 60}\n")

    persons_dir = vault_root / "Persons"
    existing: list[dict[str, str]] = []
    if persons_dir.is_dir():
        for note_path in sorted(persons_dir.glob("*.md")):
            if note_path.name.startswith("_"):
                continue
            try:
                person = load_person_from_note(note_path)
                existing.append(
                    {
                        "person_id": note_path.stem,
                        "display_name": str(person.get("display_name") or note_path.stem),
                    }
                )
            except (OSError, PersonSyncError):
                pass

    existing_person_ids = [p["person_id"] for p in existing]

    while True:
        if existing:
            print("Existing persons:")
            for i, p in enumerate(existing, start=1):
                print(f"  [{i}] {p['display_name']} ({p['person_id']})")
            next_num = len(existing) + 1
            print(f"  [{next_num}] Create new person")
            cancel_num = next_num + 1
            print(f"  [{cancel_num}] Cancel (quarantine)")

            try:
                choice = input(f"\nSelect [1-{cancel_num}]: ").strip()
            except EOFError:
                return None

            if choice.lower() == "q":
                return None

            try:
                n = int(choice)
            except ValueError:
                print("Enter a number.", file=sys.stderr)
                continue

            if 1 <= n <= len(existing):
                return existing[n - 1]["person_id"]
            if n == next_num:
                return _create_new_person_interactive(
                    root, vault_root, inbox_root, existing_person_ids, stdin
                )
            if n == cancel_num:
                return None
            print(f"Enter 1-{cancel_num}.", file=sys.stderr)
        else:
            print("No existing persons found.")
            try:
                choice = input("Create new person? [y/N]: ").strip().lower()
            except EOFError:
                return None
            if choice in {"y", "yes"}:
                return _create_new_person_interactive(root, vault_root, inbox_root, [], stdin)
            return None


def _create_new_person_interactive(
    root: Path,
    vault_root: Path,
    inbox_root: Path,
    existing_person_ids: list[str],
    stdin: object,
) -> str | None:
    import sqlite3

    db_path = root / "data" / "famed.db"
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return None

    try:
        person = create_person_interactive(
            vault_root,
            inbox_root,
            conn,
            existing_person_ids,
            _stdin=stdin,
        )
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if person is None:
        return None

    _apply_interactive_closure(vault_root, root)
    return person["person_id"]


def _apply_interactive_closure(vault_root: Path, root: Path) -> None:
    import sqlite3

    persons_by_id = _load_all_person_notes(vault_root)
    changed = apply_relationship_closure(persons_by_id)
    for warning in check_relationship_age_plausibility(persons_by_id):
        print(f"[relationship warning] {warning}")
    if not changed:
        return

    db_path = root / "data" / "famed.db"
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return

    try:
        for person_id in sorted(changed):
            person = persons_by_id.get(person_id)
            if person is None:
                continue
            note_path = vault_root / "Persons" / f"{person_id}.md"
            write_person_note_idempotent(note_path, person)
            replace_relationships(conn, person)
        conn.commit()
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _load_all_person_notes(
    vault_root: Path,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    persons_dir = vault_root / "Persons"
    if not persons_dir.is_dir():
        return result
    for note_path in sorted(persons_dir.glob("*.md")):
        if note_path.name.startswith("_"):
            continue
        try:
            person = load_person_from_note(note_path)
            result[note_path.stem] = person
        except (OSError, PersonSyncError):
            pass
    return result


def _extract_text_for_person_resolution(source_path: Path) -> str:
    """Extract readable text from a source file for person resolution display."""
    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(source_path))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages).strip()
        except Exception:
            return ""
    if suffix in {".png", ".jpg", ".jpeg"}:
        try:
            from famed.ocr import is_ocr_available, ocr_image_file

            if is_ocr_available():
                return ocr_image_file(source_path).strip()
        except Exception:
            pass
        return ""
    try:
        return source_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _attempt_interactive_person_resolution(
    route: InboxRoute, root: Path, inbox_root: Path
) -> str | None:
    text = _extract_text_for_person_resolution(route.source_path)
    return resolve_person_interactive(
        route.source_path,
        text,
        root=root,
        vault_root=root / "famed-vault",
        inbox_root=inbox_root,
    )


def _retry_with_interactive_resolution(
    route: InboxRoute,
    root: Path,
    inbox_root: Path,
    execute: bool,
    approve_pdf: bool,
) -> InboxExecution:
    """Attempt interactive person resolution; re-run the route if user resolves."""
    resolved_person_id = _attempt_interactive_person_resolution(route, root, inbox_root)
    if resolved_person_id is None:
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="interactive resolution cancelled; file quarantined",
        )

    resolved_route = InboxRoute(
        source_path=route.source_path,
        relative_path=route.relative_path,
        scope="person",
        person_id=resolved_person_id,
        category=route.category if route.category != "unknown" else "labs",
        status="routed",
        handler=(
            "history_review"
            if route.category == "history"
            else "import_labs"
            if route.category == "labs" and route.source_path.suffix.lower() in {".csv", ".pdf"}
            else "category_placeholder"
        ),
        reason="person resolved interactively",
    )
    return execute_route(
        resolved_route,
        root=root,
        inbox_root=inbox_root,
        execute=execute,
        approve_pdf=approve_pdf,
        interactive=True,
    )


def _match_filename_to_person(vault_root: Path, source_file: Path) -> str | None:
    normalized_name = source_file.name.casefold()
    matches: list[str] = []
    for note_path in (vault_root / "Persons").glob("*.md"):
        if note_path.name.startswith("_"):
            continue
        try:
            person = load_person_from_note(note_path)
        except (OSError, PersonSyncError):
            continue
        display_name = str(person.get("display_name") or "").casefold()
        if display_name and display_name in normalized_name:
            matches.append(str(person["person_id"]))
    return matches[0] if len(matches) == 1 else None


def _execute_history_review_route(
    route: InboxRoute, root: Path, *, skip_quarantine: bool = False
) -> InboxExecution:
    try:
        note_path = _process_history_review_source(route, root, skip_quarantine=skip_quarantine)
    except ImmunizationSyncError as exc:
        if skip_quarantine:
            return InboxExecution(
                route=route,
                exit_code=0,
                action="executed",
                final_state=STATE_AWAITING_USER_DECISION,
                message=f"immunization conflict awaiting user decision: {exc}",
            )
        try:
            quarantine_path = _quarantine_unresolved_route(
                route,
                root,
                extra_reason=f"immunization conflict: {exc}",
            )
        except (OSError, sqlite3.Error) as quarantine_exc:
            return InboxExecution(
                route=route,
                exit_code=1,
                action="executed",
                final_state="failed",
                message=f"immunization conflict quarantine failed: {quarantine_exc}",
            )
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message=f"immunization conflict quarantined: {quarantine_path}",
        )
    except (OSError, sqlite3.Error) as exc:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message=f"history review lifecycle failed: {exc}",
        )

    if note_path is None:
        return InboxExecution(
            route=route,
            exit_code=0,
            action="executed",
            final_state=STATE_QUARANTINED,
            message="history review quarantined: invalid source or ambiguous patient match",
        )

    return InboxExecution(
        route=route,
        exit_code=0,
        action="executed",
        final_state=STATE_PROCESSED,
        message=f"history review note: {note_path}",
    )


def _execute_history_archive_route(route: InboxRoute, root: Path) -> InboxExecution:
    if route.person_id is None:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message="history archive import failed: missing person_id",
        )

    storage_roots = configured_storage_roots(root)
    source_file = route.source_path
    checksum = source_file_checksum(source_file)
    archive_destination = (
        storage_roots.processed / route.person_id / "HealthSummary" / source_file.name
    )

    try:
        retained_archive = import_labs.retain_source_file(
            source_file, archive_destination, checksum
        )
        with sqlite3.connect(root / "data" / "famed.db") as conn:
            result = reprocess_retained_history_archive(
                root=root,
                vault_root=root / "famed-vault",
                processed_root=storage_roots.processed,
                conn=conn,
                archive_path=retained_archive,
            )
            accepted_notes, unresolved_notes = auto_accept_history_review_notes_for_source_prefix(
                conn=conn,
                vault_root=root / "famed-vault",
                person_id=route.person_id,
                source_path_prefix=f"{retained_archive.relative_to(root)}#",
                refresh_existing=True,
                processed_root=storage_roots.processed,
            )
            conn.commit()
    except (OSError, sqlite3.Error, HistorySyncError, import_labs.LabSyncError) as exc:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message=f"history archive import failed: {exc}",
        )

    return InboxExecution(
        route=route,
        exit_code=0,
        action="executed",
        final_state=STATE_PROCESSED,
        message=(
            "history archive processed: "
            f"{retained_archive.relative_to(root)} "
            f"({result.imported_rows} XML members, "
            f"{accepted_notes} notes auto-accepted, {unresolved_notes} still need review)"
        ),
    )


def _process_history_review_source(
    route: InboxRoute, root: Path, *, skip_quarantine: bool = False
) -> str | None:
    storage_roots = configured_storage_roots(root)
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    source_file = route.source_path
    source_type = "ccda_xml" if source_file.suffix.lower() == ".xml" else "history_pdf"
    document_kind = "C-CDA" if source_file.suffix.lower() == ".xml" else "PDF"
    is_ccda = source_file.suffix.lower() == ".xml"

    try:
        document = _parse_history_review_document(source_file)
        person_id = route.person_id or match_history_document_to_person(vault_root, document)
        if not person_id:
            raise HistorySyncError(
                "history document patient did not match exactly one known person"
            )
    except (HistorySyncError, import_labs.LabSyncError) as exc:
        if source_file.exists():
            if not skip_quarantine:
                _quarantine_unresolved_route(
                    route,
                    root,
                    extra_reason=f"history review processing failed: {exc}",
                )
            return None
        raise

    with sqlite3.connect(db_path) as conn:
        lab_rows: list[dict[str, Any]] = []
        immunization_rows: list[dict[str, Any]] = []
        medication_rows: list[dict[str, Any]] = []
        vital_rows: list[dict[str, Any]] = []
        rejected_labs: list[dict[str, Any]] = []
        if is_ccda:
            lab_rows = extract_ccda_lab_rows(source_file, person_id, rejected=rejected_labs)
            immunization_rows = extract_ccda_immunization_rows(source_file, person_id)
            medication_rows = extract_ccda_medication_rows(source_file, person_id)
            vital_rows = extract_ccda_vital_rows(source_file, person_id)
        checksum = source_file_checksum(source_file)
        processed_source = processed_history_source_path(
            storage_roots.processed,
            person_id,
            document.document_date,
            source_file,
        )
        processed_source_path = str(processed_source.relative_to(root))
        if rejected_labs:
            # An observation the source states must never disappear without a
            # trace; record it for review rather than discarding it (REQ-005).
            summary = "; ".join(
                f"{item['test_name']!r} ({item['unit']}): {item['reason']}"
                for item in rejected_labs[:10]
            )
            log_audit_event(
                conn,
                event_type=AUDIT_LAB_OBSERVATION_REJECTED,
                subject_type="lab_result",
                subject_id=person_id,
                source_path=processed_source_path,
                status=STATE_AWAITING_USER_DECISION,
                message=(
                    f"{len(rejected_labs)} C-CDA lab observation(s) failed validation "
                    f"and were not imported: {summary}"
                ),
            )
        if immunization_rows:
            validate_immunization_rows_for_persistence(
                conn, processed_source_path, immunization_rows
            )
        if is_ccda:
            if lab_rows:
                loinc_index = load_loinc_index(storage_roots.reference)
                import_labs.fill_loinc_codes(lab_rows, loinc_index)
                import_labs.persist_lab_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=source_file,
                    processed_source_path=processed_source_path,
                    checksum=checksum,
                    source_type=source_type,
                    rows=lab_rows,
                    refresh_reports=False,
                )
            if immunization_rows:
                persist_immunization_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=source_file,
                    processed_source_path=processed_source_path,
                    checksum=checksum,
                    source_type=source_type,
                    rows=immunization_rows,
                    document_date=immunization_document_date(
                        immunization_rows,
                        document.document_date,
                        str(route.relative_path),
                    ),
                )
            if medication_rows:
                fill_rxnorm_cuis(medication_rows, load_rxnorm_index(storage_roots.reference))
                persist_medication_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=source_file,
                    processed_source_path=processed_source_path,
                    checksum=checksum,
                    source_type=source_type,
                    rows=medication_rows,
                    document_date=medication_document_date(
                        medication_rows,
                        document.document_date,
                        str(route.relative_path),
                    ),
                )
            if vital_rows:
                persist_vital_rows(
                    conn=conn,
                    vault_root=vault_root,
                    person_id=person_id,
                    source_file=source_file,
                    processed_source_path=processed_source_path,
                    checksum=checksum,
                    source_type=source_type,
                    rows=vital_rows,
                    document_date=vital_document_date(
                        vital_rows,
                        document.document_date,
                        str(route.relative_path),
                    ),
                )
            remove_linked_category_placeholder_note(
                vault_root=vault_root,
                person_id=person_id,
                category_dir_name="Immunizations",
                source_path=processed_source_path,
            )
        note_path = persist_history_document_review_note(
            conn=conn,
            vault_root=vault_root,
            person_id=person_id,
            source_file=source_file,
            processed_source_path=processed_source_path,
            checksum=checksum,
            source_type=source_type,
            document=document,
            document_kind=document_kind,
        )
        if lab_rows:
            refresh_lab_dependent_reports(conn, vault_root, person_id)
        import_labs.retain_source_file(source_file, processed_source, checksum)
        conn.commit()
        return str(note_path.relative_to(vault_root))


def _parse_history_review_document(source_file: Path) -> HistoryReviewDocument:
    if source_file.suffix.lower() == ".xml":
        return parse_ccda_review_document(source_file)
    if source_file.suffix.lower() == ".pdf":
        text = import_labs.extract_pdf_text(source_file)
        return parse_text_history_review_document(source_file, text)
    if source_file.suffix.lower() in {".tif", ".tiff"}:
        from famed.ocr import is_ocr_available, ocr_image_file

        if not is_ocr_available():
            raise HistorySyncError(
                "OCR support is not available. Install with: "
                "brew install tesseract && pip install pytesseract"
            )
        text = ocr_image_file(source_file)
        if not text.strip():
            raise HistorySyncError(f"OCR produced no text from {source_file.name}")
        return parse_text_history_review_document(source_file, text)
    raise HistorySyncError(f"unsupported history review source extension: {source_file.suffix}")


def _execute_unresolved_route_quarantine(route: InboxRoute, root: Path) -> InboxExecution:
    try:
        quarantine_path = _quarantine_unresolved_route(route, root)
    except (OSError, sqlite3.Error) as exc:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message=f"unresolved route quarantine failed: {exc}",
        )

    return InboxExecution(
        route=route,
        exit_code=0,
        action="executed",
        final_state=STATE_QUARANTINED,
        message=f"unresolved route quarantined: {quarantine_path}",
    )


def _quarantine_unresolved_route(route: InboxRoute, root: Path, *, extra_reason: str = "") -> str:
    storage_roots = configured_storage_roots(root)
    db_path = root / "data" / "famed.db"
    checksum = source_file_checksum(route.source_path)
    quarantine_source = _unresolved_quarantine_source_path(
        storage_roots.quarantine,
        route.category,
        route.source_path,
    )
    retained_source = import_labs.retain_source_file(
        route.source_path,
        quarantine_source,
        checksum,
    )
    source_path = str(retained_source.relative_to(root))
    source_type = route.source_path.suffix.lower().lstrip(".") or "file"
    reason = f"{route.reason}; {extra_reason}" if extra_reason else route.reason
    message = f"non-interactive ambiguity quarantined: {reason}"

    with sqlite3.connect(db_path) as conn:
        log_ingestion_event(
            conn,
            source_type=source_type,
            source_path=source_path,
            source_checksum=checksum,
            status=STATE_QUARANTINED,
            message=message,
        )
        log_audit_event(
            conn,
            event_type=AUDIT_INBOX_ROUTE_QUARANTINED,
            subject_type="inbox_route",
            subject_id=route.person_id,
            source_path=source_path,
            status=STATE_QUARANTINED,
            message=message,
        )
        conn.commit()

    return source_path


def _process_medication_fact_extraction(
    route: InboxRoute,
    root: Path,
    extraction: InterpreterFactExtraction,
    *,
    source_type: str = "ai_extracted",
) -> str | None:
    source_label = str(route.relative_path)
    if route.person_id is not None and extraction.person_id != route.person_id:
        _quarantine_unresolved_route(
            route,
            root,
            extra_reason=(
                "medication extraction person mismatch: "
                f"route person_id={route.person_id!r}, "
                f"extraction person_id={extraction.person_id!r}"
            ),
        )
        return None
    try:
        rows = canonicalize_candidate_medication_facts(extraction, source_label)
    except MedicationSyncError as exc:
        _quarantine_unresolved_route(
            route, root, extra_reason=f"medication canonicalization failed: {exc}"
        )
        return None

    storage_roots = configured_storage_roots(root)
    fill_rxnorm_cuis(rows, load_rxnorm_index(storage_roots.reference))
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    source_file = route.source_path
    person_id = extraction.person_id
    try:
        document_date = medication_document_date(rows, extraction.document_date, source_label)
    except MedicationSyncError as exc:
        _quarantine_unresolved_route(
            route, root, extra_reason=f"medication date normalization failed: {exc}"
        )
        return None

    with sqlite3.connect(db_path) as conn:
        try:
            checksum, processed_source_path = retain_medication_source_for_import(
                root,
                storage_roots.processed,
                person_id,
                document_date,
                source_file,
            )
            note_path = persist_medication_rows(
                conn=conn,
                vault_root=vault_root,
                person_id=person_id,
                source_file=source_file,
                processed_source_path=processed_source_path,
                checksum=checksum,
                source_type=source_type,
                rows=rows,
                document_date=document_date,
            )
            _delete_superseded_placeholder(vault_root, person_id, "Medications", source_file)
            conn.commit()
            return str(note_path.relative_to(vault_root))
        except (OSError, sqlite3.Error, MedicationSyncError) as exc:
            if source_file.exists():
                quarantine_medication_import_error(
                    conn,
                    root,
                    storage_roots.quarantine,
                    person_id,
                    source_file,
                    source_type,
                    f"medication fact extraction persist failed: {exc}",
                )
                conn.commit()
                return None
            raise


def _unresolved_quarantine_source_path(
    quarantine_root: Path,
    category: str,
    source_file: Path,
) -> Path:
    safe_category = import_labs.slugify_filename(category or "unknown")
    stem = source_file.stem
    suffix = source_file.suffix.lower()
    return quarantine_root / "_unresolved" / safe_category / f"undated-{stem}{suffix}"


def _as_placeholder_route(route: InboxRoute, reason: str) -> InboxRoute:
    """Re-label a route so an unparseable source still reaches a documented outcome."""
    return InboxRoute(
        source_path=route.source_path,
        relative_path=route.relative_path,
        scope=route.scope,
        person_id=route.person_id,
        category=route.category,
        status=route.status,
        handler="category_placeholder",
        reason=reason,
    )


def _execute_placeholder_route(route: InboxRoute, root: Path) -> InboxExecution:
    try:
        note_path = _process_placeholder_source(route, root)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message=f"placeholder lifecycle failed: {exc}",
        )

    return InboxExecution(
        route=route,
        exit_code=0,
        action="executed",
        final_state=STATE_PROCESSED,
        message=f"placeholder note: {note_path}",
    )


def _execute_multi_category_placeholder_route(route: InboxRoute, root: Path) -> InboxExecution:
    try:
        note_paths = _process_multi_category_placeholder_source(route, root)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return InboxExecution(
            route=route,
            exit_code=1,
            action="executed",
            final_state="failed",
            message=f"multi-category placeholder lifecycle failed: {exc}",
        )

    return InboxExecution(
        route=route,
        exit_code=0,
        action="executed",
        final_state=STATE_PROCESSED,
        message=f"placeholder notes: {', '.join(note_paths)}",
    )


def _process_multi_category_placeholder_source(route: InboxRoute, root: Path) -> list[str]:
    if not route.person_id:
        raise ValueError("multi-category placeholder routing requires a resolved person_id")

    categories = _split_resolved_categories(route.category)
    if len(categories) < 2:
        raise ValueError("multi-category placeholder routing requires multiple categories")

    category_dir_names = []
    for category in categories:
        category_dir_name = PLACEHOLDER_CATEGORY_DIRS.get(category)
        if not category_dir_name:
            raise ValueError(f"unsupported multi-category placeholder category: {category}")
        category_dir_names.append(category_dir_name)

    storage_roots = configured_storage_roots(root)
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    checksum = source_file_checksum(route.source_path)
    processed_source = _processed_multi_category_source_path(
        storage_roots.processed,
        route.person_id,
        route.source_path,
    )
    retained_source = import_labs.retain_source_file(
        route.source_path,
        processed_source,
        checksum,
    )
    source_path = str(retained_source.relative_to(root))
    source_type = route.source_path.suffix.lower().lstrip(".") or "file"

    ensure_person_record_scaffold(vault_root, route.person_id)
    note_paths = []
    for category_dir_name in category_dir_names:
        category_dir = vault_root / "Records" / route.person_id / category_dir_name
        category_dir.mkdir(parents=True, exist_ok=True)
        note_path = category_dir / f"{retained_source.stem}-placeholder.md"
        _write_placeholder_note(
            note_path,
            person_id=route.person_id,
            category=category_dir_name,
            source_path=source_path,
            source_file_name=retained_source.name,
            source_type=source_type,
        )
        display_name = load_person_display_name(vault_root, route.person_id)
        write_category_index_note(
            category_dir, route.person_id, category_dir_name, display_name=display_name
        )
        note_paths.append(str(note_path.relative_to(vault_root)))

    message = f"placeholder notes: {', '.join(note_paths)}"
    with sqlite3.connect(db_path) as conn:
        log_ingestion_event(
            conn,
            source_type=source_type,
            source_path=source_path,
            source_checksum=checksum,
            status=STATE_PROCESSED,
            message=message,
        )
        log_audit_event(
            conn,
            event_type=AUDIT_PLACEHOLDER_IMPORT_ACCEPTED,
            subject_type="multi_category_placeholder",
            subject_id=route.person_id,
            source_path=source_path,
            status=STATE_PROCESSED,
            message=message,
        )
        conn.commit()

    return note_paths


def _processed_multi_category_source_path(
    processed_root: Path,
    person_id: str,
    source_file: Path,
) -> Path:
    stem = source_file.stem
    suffix = source_file.suffix.lower()
    return processed_root / person_id / "Mixed" / f"undated-{stem}{suffix}"


def _process_placeholder_source(route: InboxRoute, root: Path) -> str:
    if not route.person_id:
        raise ValueError("placeholder routing requires a resolved person_id")

    category_dir_name = PLACEHOLDER_CATEGORY_DIRS[route.category]
    storage_roots = configured_storage_roots(root)
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    checksum = source_file_checksum(route.source_path)
    processed_source = _processed_placeholder_source_path(
        storage_roots.processed,
        route.person_id,
        category_dir_name,
        route.source_path,
    )
    retained_source = import_labs.retain_source_file(
        route.source_path,
        processed_source,
        checksum,
    )
    source_path = str(retained_source.relative_to(root))
    source_type = route.source_path.suffix.lower().lstrip(".") or "file"

    ensure_person_record_scaffold(vault_root, route.person_id)
    category_dir = vault_root / "Records" / route.person_id / category_dir_name
    category_dir.mkdir(parents=True, exist_ok=True)
    note_path = category_dir / f"{retained_source.stem}-placeholder.md"
    _write_placeholder_note(
        note_path,
        person_id=route.person_id,
        category=category_dir_name,
        source_path=source_path,
        source_file_name=retained_source.name,
        source_type=source_type,
    )
    display_name = load_person_display_name(vault_root, route.person_id)
    write_category_index_note(
        category_dir, route.person_id, category_dir_name, display_name=display_name
    )
    note_source_path = str(note_path.relative_to(vault_root))

    with sqlite3.connect(db_path) as conn:
        log_ingestion_event(
            conn,
            source_type=source_type,
            source_path=source_path,
            source_checksum=checksum,
            status=STATE_PROCESSED,
            message=f"placeholder note: {note_source_path}",
        )
        log_audit_event(
            conn,
            event_type=AUDIT_PLACEHOLDER_IMPORT_ACCEPTED,
            subject_type="category_placeholder",
            subject_id=route.person_id,
            source_path=source_path,
            status=STATE_PROCESSED,
            message=f"placeholder note: {note_source_path}",
        )
        conn.commit()

    return note_source_path


def _processed_placeholder_source_path(
    processed_root: Path,
    person_id: str,
    category_dir_name: str,
    source_file: Path,
) -> Path:
    stem = source_file.stem
    suffix = source_file.suffix.lower()
    return processed_root / person_id / category_dir_name / f"undated-{stem}{suffix}"


def _delete_superseded_placeholder(
    vault_root: Path,
    person_id: str,
    category_dir: str,
    source_file: Path,
) -> None:
    """Delete any category_placeholder note whose source_path matches source_file.

    Called after canonical persistence succeeds so stale placeholders do not
    linger in the vault alongside the authoritative canonical note.
    """
    source_path_str = str(source_file)
    candidates = (vault_root / "Records" / person_id / category_dir).glob("*-placeholder.md")
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if "record_type: category_placeholder" in text and source_path_str in text:
            candidate.unlink(missing_ok=True)


def _write_placeholder_note(
    note_path: Path,
    *,
    person_id: str,
    category: str,
    source_path: str,
    source_file_name: str,
    source_type: str,
) -> None:
    preserved_review_notes = _preserved_review_notes(note_path)
    content = (
        "---\n"
        f"person_id: {person_id}\n"
        "record_type: category_placeholder\n"
        f"category: {category}\n"
        "review_status: needs_review\n"
        f"source_type: {source_type}\n"
        f"source_path: {source_path}\n"
        f"source_file_name: {source_file_name}\n"
        "---\n\n"
        f"# {category} Placeholder\n\n"
        "## Source\n\n"
        f"- Source path: {source_path}\n"
        f"- Source file: {source_file_name}\n\n"
        "## Captured Content\n\n"
        "This source is retained for review. No structured medical facts have been "
        "extracted or accepted yet.\n\n"
        f"{preserved_review_notes}"
    )
    note_path.write_text(content, encoding="utf-8")


def _preserved_review_notes(note_path: Path) -> str:
    if not note_path.exists():
        return "## Review Notes\n\n- Add reviewed summary or extraction notes here.\n"

    marker = "\n## Review Notes"
    content = note_path.read_text(encoding="utf-8")
    pos = content.rfind(marker)
    if pos == -1:
        return "## Review Notes\n\n- Add reviewed summary or extraction notes here.\n"
    return content[pos + 1 :]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect data/inbox and route files without writing canonical data."
    )
    parser.add_argument(
        "--inbox-dir",
        default="data/inbox",
        help="Inbox root to inspect (default: data/inbox).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Invoke supported routed handlers. Default is dry-run inspection only.",
    )
    parser.add_argument(
        "--approve-pdf",
        action="store_true",
        help="Allow PDF lab routes to run without an interactive review prompt.",
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="Do not append route/execution outcomes to data/inbox_state.tsv.",
    )
    parser.add_argument(
        "--resolve-person",
        help="Resolve shared/other inbox files to this person_id for routing.",
    )
    parser.add_argument(
        "--resolve-category",
        help="Resolve shared/other inbox files to this category for routing.",
    )
    parser.add_argument(
        "--resolution-file",
        help=(
            "TSV file with relative_path, person_id, and category columns for "
            "per-file shared/other inbox resolution."
        ),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Enable interactive person resolution when automatic person matching fails. "
            "Presents extracted text, lists existing persons, and offers new-person creation "
            "with relationship setup. Without this flag, unresolved files are quarantined."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    storage_roots = configured_storage_roots(root)
    inbox_root = (root / args.inbox_dir).resolve()
    if args.inbox_dir == "data/inbox":
        inbox_root = storage_roots.inbox.resolve()

    resolutions = None
    if args.resolution_file:
        resolution_file = Path(args.resolution_file)
        if not resolution_file.is_absolute():
            resolution_file = root / resolution_file
        try:
            resolutions = load_resolution_file(resolution_file.resolve())
        except (OSError, ValueError) as exc:
            print(f"Could not load resolution file: {exc}", file=sys.stderr)
            return 1

    routes = inspect_inbox(
        inbox_root,
        resolve_person=args.resolve_person,
        resolve_category=args.resolve_category,
        resolutions=resolutions,
    )
    if not routes:
        print(f"No inbox files found in: {inbox_root}")
        return 0

    executions = execute_routes(
        routes,
        root=root,
        inbox_root=inbox_root,
        execute=args.execute,
        approve_pdf=args.approve_pdf,
        interactive=args.interactive,
    )
    if not args.no_ledger:
        append_inbox_state_ledger(root, executions)
    for execution in executions:
        route = execution.route
        handler = route.handler or "-"
        person_id = route.person_id or "-"
        print(
            f"{route.status}\t{execution.action}\t{handler}\t{person_id}\t"
            f"{route.category}\t{route.relative_path}\t{execution.message}"
        )
    for line in format_batch_summary(executions):
        print(line)
    return 1 if any(execution.exit_code for execution in executions) else 0


if __name__ == "__main__":
    raise SystemExit(main())
