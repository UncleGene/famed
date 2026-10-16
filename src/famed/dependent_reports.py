"""Refresh report outputs that depend on multiple canonical input categories.

Models the derived-report dependency graph from ADR-025: first-tier reports
(labs, the Genetics report) feed every composite refresh (medication safety
alerts, health advisory). Refreshing a first-tier report therefore cascades
through the full graph in one ordered operation — no composite is left
silently stale.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from famed.annotation_cache import REFERENCE_DB_FILENAME
from famed.fhir_export import write_fhir_export
from famed.findings_ledger import FINDINGS_LEDGER_PATH
from famed.genetics_report import write_genetics_report
from famed.health_advisory import load_heritable_dependents, write_health_advisory_unattended
from famed.query import list_person_ids
from famed.report_consistency import (
    ReportConsistencyError,
    find_genetics_contradictions,
)

_CompositeRefreshStep = Callable[..., Path]


def default_annotation_reference_db_path() -> Path:
    return Path("data") / "reference" / REFERENCE_DB_FILENAME


def _refresh_genetics_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    ref_db_path: Path,
    *,
    findings_ledger_path: Path | None,
) -> Path | None:
    """The one DNA report (ADR-031); also owns the PGx ledger findings."""
    return write_genetics_report(
        conn, vault_root, person_id, ref_db_path, findings_ledger_path=findings_ledger_path
    )


def _refresh_medication_safety_alerts(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    *,
    ref_db_path: Path,
    findings_ledger_path: Path | None,
) -> Path:
    # Deferred import: medication_safety_alerts -> medication_sync ->
    # dependent_reports would otherwise be a circular import at module load.
    from famed.medication_safety_alerts import write_medication_safety_alerts

    path, _count = write_medication_safety_alerts(
        vault_root,
        person_id,
        findings_ledger_path=findings_ledger_path,
        conn=conn,
    )
    return path


def _refresh_health_advisory(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    *,
    ref_db_path: Path,
    findings_ledger_path: Path | None,
) -> Path:
    path, _count = write_health_advisory_unattended(
        vault_root, person_id, conn=conn, ref_db_path=ref_db_path
    )
    return path


# Composites, in dependency order. Each reads first-tier reports (labs,
# genetics), so any first-tier refresh cascades through all of them.
_COMPOSITE_REFRESH_STEPS: tuple[_CompositeRefreshStep, ...] = (
    _refresh_medication_safety_alerts,
    _refresh_health_advisory,
)


_DATA_DEPENDENT_COMPOSITE_FILENAMES: tuple[str, ...] = ("medication-safety-alerts.md",)

# Report types retired by ADR-031; a composite refresh removes leftover notes so
# every vault converges without a separate migration.
RETIRED_REPORT_FILENAMES: tuple[str, ...] = (
    "prescriber-summary.md",
    "sbar-summary.md",
    "risk-landscape.md",
    "pgx-coverage.md",
    "pgx-wallet-card.md",
    "carrier-screening.md",
    "dna-profile-summary.md",
    "imputation-readiness.md",
    # Replaced by the combined lab-results.md.
    "latest-lab-results.md",
    "lab-trends.md",
)

LAB_DEPENDENT_COMPOSITE_FILENAMES: tuple[str, ...] = (
    *_DATA_DEPENDENT_COMPOSITE_FILENAMES,
    "health-advisory.md",
)

LAB_DEPENDENT_REPORT_FILENAMES: tuple[str, ...] = (
    "lab-results.md",
    "lab-conflicts.md",
    "flagged-for-discussion.md",
    "pending-review.md",
    "fhir-export.json",
    *LAB_DEPENDENT_COMPOSITE_FILENAMES,
)


def has_composite_report_surface(vault_root: Path, person_id: str) -> bool:
    """Return whether composites can be generated.

    Medication safety alerts and health advisory require the person note (they render
    identity context the self-scaffolding first-tier reports do not need) and
    raise if it is absent. Medication and lab data can arrive — and first-tier
    reports can be generated — for a person before onboarding writes their
    note, so composite generation is gated the same way FHIR export is gated
    by `has_fhir_export_surface`: skipped, not failed, until the surface
    exists. The next ingestion-triggered refresh (ADR-014) catches up once
    onboarding completes.
    """
    return (vault_root / "Persons" / f"{person_id}.md").is_file()


def _person_has_medical_data(conn: sqlite3.Connection, vault_root: Path, person_id: str) -> bool:
    """Return True if the person has any substantive medical data.

    Composites are skipped when this returns False — a person with no
    medications, labs, genotypes, immunizations, or history notes would
    produce only empty shells that carry no information.
    """
    for table, extra in [
        ("medications", "AND status = 'active'"),
        ("lab_results", ""),
        ("genotypes", ""),
        ("immunizations", ""),
    ]:
        try:
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE person_id = ? {extra} LIMIT 1",
                (person_id,),
            ).fetchone():
                return True
        except sqlite3.OperationalError:
            pass
    history_dir = vault_root / "Records" / person_id / "History"
    if history_dir.is_dir():
        return any(p.suffix == ".md" and not p.name.startswith("_") for p in history_dir.iterdir())
    return False


def _delete_stale_composite_reports(vault_root: Path, person_id: str) -> None:
    reports_dir = vault_root / "Records" / person_id / "Reports"
    for filename in LAB_DEPENDENT_COMPOSITE_FILENAMES:
        stale = reports_dir / filename
        if stale.exists():
            stale.unlink()


def _delete_reports(vault_root: Path, person_id: str, filenames: tuple[str, ...]) -> None:
    reports_dir = vault_root / "Records" / person_id / "Reports"
    for filename in filenames:
        stale = reports_dir / filename
        if stale.exists():
            stale.unlink()


def _delete_stale_data_dependent_composites(vault_root: Path, person_id: str) -> None:
    _delete_reports(vault_root, person_id, _DATA_DEPENDENT_COMPOSITE_FILENAMES)


def _refresh_composite_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    ref_db_path: Path,
    *,
    findings_ledger_path: Path | None,
) -> list[Path]:
    _delete_reports(vault_root, person_id, RETIRED_REPORT_FILENAMES)
    if not has_composite_report_surface(vault_root, person_id):
        return []

    if not _person_has_medical_data(conn, vault_root, person_id):
        # Health advisory runs even without personal medical data — family
        # history context is valuable independently of the person's own records.
        _delete_stale_data_dependent_composites(vault_root, person_id)
        return [
            _refresh_health_advisory(
                conn,
                vault_root,
                person_id,
                ref_db_path=ref_db_path,
                findings_ledger_path=findings_ledger_path,
            )
        ]

    return [
        step(
            conn,
            vault_root,
            person_id,
            ref_db_path=ref_db_path,
            findings_ledger_path=findings_ledger_path,
        )
        for step in _COMPOSITE_REFRESH_STEPS
    ]


def refresh_dna_dependent_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    *,
    ref_db_path: Path | None = None,
    findings_ledger_path: Path | None = FINDINGS_LEDGER_PATH,
) -> list[Path]:
    """Refresh every derived report whose inputs include a person's DNA data.

    A DNA change invalidates the Genetics report (ADR-031), and every composite
    reads it or another clinical context report, so the refresh cascades through
    the full dependency graph (ADR-025).
    """
    ref_db_path = ref_db_path or default_annotation_reference_db_path()
    genetics_path = _refresh_genetics_report(
        conn,
        vault_root,
        person_id,
        ref_db_path,
        findings_ledger_path=findings_ledger_path,
    )
    composite_paths = _refresh_composite_reports(
        conn,
        vault_root,
        person_id,
        ref_db_path,
        findings_ledger_path=findings_ledger_path,
    )
    return [*([genetics_path] if genetics_path else []), *composite_paths]


def refresh_medication_dependent_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    *,
    ref_db_path: Path | None = None,
    findings_ledger_path: Path | None = FINDINGS_LEDGER_PATH,
) -> list[Path]:
    """Refresh every derived report whose inputs include current medications.

    A medication change invalidates the Genetics report's medication column and
    PGx findings, and every composite reads current medications, so the refresh
    cascades through the full dependency graph (ADR-025).
    """
    ref_db_path = ref_db_path or default_annotation_reference_db_path()
    genetics_path = _refresh_genetics_report(
        conn,
        vault_root,
        person_id,
        ref_db_path,
        findings_ledger_path=findings_ledger_path,
    )
    composite_paths = _refresh_composite_reports(
        conn,
        vault_root,
        person_id,
        ref_db_path,
        findings_ledger_path=findings_ledger_path,
    )
    pending_path = _refresh_pending_review_report(conn, vault_root, person_id)
    first_tier = [genetics_path] if genetics_path is not None else []
    if pending_path is not None:
        first_tier.append(pending_path)
    return [*first_tier, *composite_paths]


def refresh_lab_dependent_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    *,
    ref_db_path: Path | None = None,
    findings_ledger_path: Path | None = FINDINGS_LEDGER_PATH,
) -> list[Path]:
    """Refresh every derived report whose inputs include a person's labs.

    Lab reports are first-tier inputs to every composite report. Regenerate
    those reports and the independent FHIR export before refreshing composites
    in dependency order so no consumer observes stale lab state (ADR-025).
    """
    # Deferred import keeps the dependency graph independent from the lab
    # persistence modules that call this orchestrator.
    from famed.lab_sync import (
        write_flagged_for_discussion_report,
        write_lab_conflicts_report,
        write_lab_results_report,
    )

    ref_db_path = ref_db_path or default_annotation_reference_db_path()
    results_path, _result_rows = write_lab_results_report(conn, vault_root, person_id)
    conflicts_path, _conflict_rows = write_lab_conflicts_report(conn, vault_root, person_id)
    flagged_path, _flagged_rows = write_flagged_for_discussion_report(conn, vault_root, person_id)
    fhir_path = refresh_fhir_dependent_export(conn, vault_root, person_id)
    pending_path = _refresh_pending_review_report(conn, vault_root, person_id)
    composite_paths = _refresh_composite_reports(
        conn,
        vault_root,
        person_id,
        ref_db_path,
        findings_ledger_path=findings_ledger_path,
    )
    first_tier = [path for path in (results_path, conflicts_path, flagged_path) if path]
    if pending_path is not None:
        first_tier.append(pending_path)
    if fhir_path is not None:
        first_tier.append(fhir_path)
    return [*first_tier, *composite_paths]


def refresh_history_dependent_reports(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> list[Path]:
    """Refresh every composite that reads a person's accepted history.

    Every composite reads the person's medical-history summary, and Health
    Advisory family-history context reads the accepted history of the blood
    relatives ADR-028 selects, so the cascade covers the person and each
    relative whose context names them (ADR-010). Reference and ledger paths
    resolve from the vault's project root, never the working directory.
    """
    root = vault_root.parent
    ref_db_path = root / default_annotation_reference_db_path()
    findings_ledger_path = root / FINDINGS_LEDGER_PATH
    return [
        path
        for affected_id in (person_id, *load_heritable_dependents(conn, person_id))
        for path in _refresh_composite_reports(
            conn,
            vault_root,
            affected_id,
            ref_db_path,
            findings_ledger_path=findings_ledger_path,
        )
    ]


def _refresh_pending_review_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> Path | None:
    """Refresh the current-review projection without creating an import cycle."""
    from famed.pending_review import refresh_pending_review_report_if_configured

    return refresh_pending_review_report_if_configured(
        conn,
        root=vault_root.parent,
        vault_root=vault_root,
        person_id=person_id,
    )


def refresh_derived_reports_for_household(
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    ref_db_path: Path | None = None,
    findings_ledger_path: Path | None = FINDINGS_LEDGER_PATH,
) -> dict[str, list[Path]]:
    """Refresh every derived report for every person in the vault (ADR-025).

    Runs the full DNA-dependent cascade — the superset that also covers every
    medication-dependent output — for each person in turn, in dependency
    order, so a household-wide refresh leaves no derived report stale.

    Raises ReportConsistencyError if any person's rendered genetics.md
    contradicts freshly-recomputed canonical state after the cascade completes
    (ADR-025 decision #4 consistency gate).
    """
    ref_db_path = ref_db_path or default_annotation_reference_db_path()
    person_ids = list_person_ids(vault_root)
    result = {
        person_id: refresh_dna_dependent_reports(
            conn,
            vault_root,
            person_id,
            ref_db_path=ref_db_path,
            findings_ledger_path=findings_ledger_path,
        )
        for person_id in person_ids
    }
    from famed.pending_review import refresh_pending_review_reports

    refresh_pending_review_reports(conn, root=vault_root.parent, vault_root=vault_root)
    for person_id in person_ids:
        result[person_id].append(
            vault_root / "Records" / person_id / "Reports" / "pending-review.md"
        )
    all_contradictions = []
    for person_id in person_ids:
        if has_composite_report_surface(vault_root, person_id):
            all_contradictions.extend(
                find_genetics_contradictions(conn, vault_root, person_id, ref_db_path)
            )
    if all_contradictions:
        details = "; ".join(str(c) for c in all_contradictions)
        raise ReportConsistencyError(
            f"{len(all_contradictions)} phenotype contradiction(s) after household refresh: "
            f"{details}"
        )
    return result


def refresh_fhir_dependent_export(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> Path | None:
    """Refresh the FHIR export when the person has complete local identity.

    Unit tests and partially scaffolded vaults may exercise lower-level
    persistence helpers before both the person note and DB row exist. A real
    export requires both, so absent identity scaffolding means there is no
    configured FHIR surface to refresh yet.
    """
    if not has_fhir_export_surface(conn, vault_root, person_id):
        return None
    export_path, *_ = write_fhir_export(conn, vault_root, person_id)
    return export_path


def has_fhir_export_surface(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> bool:
    """Return whether a FHIR export can be generated for this person."""
    person_note = vault_root / "Persons" / f"{person_id}.md"
    if not person_note.is_file():
        return False
    try:
        row = conn.execute("SELECT 1 FROM persons WHERE id = ? LIMIT 1", (person_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: persons" not in str(exc):
            raise
        return False
    return row is not None
