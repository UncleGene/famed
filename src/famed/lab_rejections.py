"""Source-derived Lab rejection coverage and durable exact review decisions (ADR-039)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from famed.lab_sync import normalize_unit, reviewed_unit_equivalence, source_file_checksum

if TYPE_CHECKING:
    from famed.history_sync import LabObservationResolution

LAB_DECISION_SCHEMA = "famed.lab-observation-decision.v1"


def retained_lab_archive_source(source: object, person_id: str) -> str | None:
    """Recognize only the person-scoped archive family covered by this scanner."""
    if not isinstance(source, str):
        return None
    archive, separator, member = source.partition("#")
    path = Path(archive)
    prefix = ("data", "processed", person_id, "HealthSummary")
    if (
        not separator
        or not member
        or path.parts[:4] != prefix
        or ".." in path.parts
        or path.suffix.lower() != ".zip"
    ):
        return None
    return archive


def lab_decision_message(
    resolution: LabObservationResolution,
    *,
    person_id: str,
    source_checksum: str,
    summary: str,
) -> str:
    """Retain exact validated decision fields alongside the readable audit summary."""
    return json.dumps(
        {
            "schema_version": LAB_DECISION_SCHEMA,
            "summary": summary,
            "source_sha256": source_checksum,
            "resolution": {"category": "labs", "person_id": person_id, **asdict(resolution)},
        },
        sort_keys=True,
    )


def recorded_lab_decisions(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    person_id: str,
) -> tuple[tuple[tuple[str, LabObservationResolution], ...], bool]:
    """Read latest exact decisions; legacy summaries cannot supply source fields."""
    from famed.history_sync import HistorySyncError
    from famed.reprocess_sources import ReprocessError
    from famed.retained_replay import validate_lab_observation_resolution

    try:
        rows = conn.execute(
            """SELECT message FROM audit_events
               WHERE event_type = 'lab_observation_resolution_recorded'
                 AND source_path = ? AND subject_id = ? ORDER BY rowid""",
            (source_path, person_id),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: audit_events" not in str(exc):
            raise
        rows = []
    latest: dict[str, tuple[str, LabObservationResolution]] = {}
    current_checksum: str | None = None
    legacy = False
    for index, (message,) in enumerate(rows):
        if not isinstance(message, str) or not message.lstrip().startswith("{"):
            legacy = True
            continue
        try:
            payload = json.loads(message)
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != LAB_DECISION_SCHEMA
            ):
                raise ValueError("unsupported structured Lab decision")
            checksum = payload.get("source_sha256")
            if (
                not isinstance(checksum, str)
                or len(checksum) != 64
                or any(char not in "0123456789abcdef" for char in checksum)
            ):
                raise ValueError("invalid Lab decision checksum")
            raw_resolution = payload.get("resolution")
            if not isinstance(raw_resolution, dict):
                raise ValueError("missing structured Lab resolution")
            resolution = validate_lab_observation_resolution(
                raw_resolution, decision_index=index, person_id=person_id
            )
        except (ValueError, ReprocessError) as exc:
            raise HistorySyncError(
                f"invalid recorded Lab decision for {source_path}: {exc}"
            ) from exc
        identity = json.dumps(asdict(resolution.selector), sort_keys=True)
        if current_checksum != checksum:
            latest.clear()
            current_checksum = checksum
        latest[identity] = (checksum, resolution)
    return tuple(latest.values()), legacy


def lab_resolutions_for_replay(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    source_path: str,
    source_checksum: str,
    rejected: list[dict[str, Any]],
    supplied: Sequence[LabObservationResolution] = (),
) -> tuple[LabObservationResolution, ...]:
    """Fail before mutation if prior review cannot be replayed exactly."""
    from famed.history_sync import HistorySyncError, validate_rejected_lab_observation_resolutions

    stored, legacy = recorded_lab_decisions(conn, source_path=source_path, person_id=person_id)
    validate_rejected_lab_observation_resolutions(
        rejected=rejected, resolutions=supplied, source_label=source_path
    )
    supplied_by_selector = {resolution.selector: resolution for resolution in supplied}
    stale = any(checksum != source_checksum for checksum, _ in stored)
    if stale and not supplied:
        raise HistorySyncError(f"recorded Lab decision checksum is stale for {source_path}")
    effective = {
        resolution.selector: resolution
        for checksum, resolution in stored
        if checksum == source_checksum
    }
    effective.update(supplied_by_selector)
    resolutions = tuple(effective.values())
    matched = validate_rejected_lab_observation_resolutions(
        rejected=rejected, resolutions=resolutions, source_label=source_path
    )
    covered = {index for _, indexes in matched for index in indexes}
    if (legacy or stale) and (not resolutions or len(covered) != len(rejected)):
        raise HistorySyncError(
            f"previously reviewed Lab observations in {source_path} require complete "
            "source-bound retained decisions before replay"
        )
    return resolutions


def validate_recorded_lab_archive_members(
    conn: sqlite3.Connection, *, archive_source: str, person_id: str, members: set[str]
) -> None:
    """A removed reviewed member must not evade archive decision preflight."""
    from famed.history_sync import HistorySyncError

    try:
        rows = conn.execute(
            """SELECT DISTINCT source_path FROM audit_events
               WHERE event_type = 'lab_observation_resolution_recorded' AND subject_id = ?""",
            (person_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: audit_events" not in str(exc):
            raise
        return
    prefix = f"{archive_source}#"
    for (source_path,) in rows:
        if isinstance(source_path, str) and source_path.startswith(prefix):
            if source_path[len(prefix) :] not in members:
                raise HistorySyncError(
                    f"previously reviewed Lab archive member is missing: {source_path}"
                )


@dataclass(frozen=True)
class LabRejectionGap:
    identity: str
    subject: str
    reason: str
    source_paths: tuple[str, ...]


def _canonical_text_coverage(
    observation: dict[str, Any], canonical: list[dict[str, Any]], *, source_path: str
) -> bool:
    """Match the same source text and measurement, allowing the existing one-day draw window."""
    raw_unit = normalize_unit(observation.get("unit"))
    unit = reviewed_unit_equivalence(observation.get("test_name"), raw_unit) or raw_unit
    observed_date = date.fromisoformat(str(observation["observed_at"])[:10])
    for row in canonical:
        exact_identity = (
            row["test_name"] == observation["test_name"]
            and row["observed_at"] == observation["observed_at"]
        )
        if exact_identity and (
            row.get("source_type") == "user_correction"
            or (row.get("source_type") == "user_update" and row.get("source_path") == source_path)
        ):
            return True
        if observation.get("loinc_code") and row.get("loinc_code"):
            same_test = observation["loinc_code"] == row["loinc_code"]
        else:
            same_test = str(observation["test_name"]).casefold() == row["test_name"].casefold()
        if not same_test or normalize_unit(row.get("unit")) != unit:
            continue
        if row.get("value") is not None or row.get("value_text") != observation.get("value_text"):
            continue
        bounds_agree = True
        for field in ("reference_range_low", "reference_range_high"):
            source_bound = observation.get(field)
            if source_bound is not None:
                try:
                    if row.get(field) != float(source_bound):
                        bounds_agree = False
                except (ValueError, TypeError):
                    bounds_agree = False
        if not bounds_agree:
            continue
        if abs((date.fromisoformat(row["observed_at"][:10]) - observed_date).days) <= 1:
            return True
    return False


def current_lab_rejection_gaps(
    conn: sqlite3.Connection, *, root: Path, person_id: str
) -> list[LabRejectionGap]:
    """Re-read retained evidence; never treat historical event counts as a queue."""
    from famed.history_sync import (
        HistorySyncError,
        _iter_history_archive_members,
        _rejected_lab_matches_selector,
        extract_ccda_lab_rows,
        validate_rejected_lab_observation_resolutions,
    )

    archive_dir = root / "data" / "processed" / person_id / "HealthSummary"
    archives = {path for path in archive_dir.rglob("*") if path.suffix.lower() == ".zip"}
    try:
        source_rows = conn.execute(
            """SELECT DISTINCT source_path FROM audit_events WHERE subject_id = ?
               AND event_type IN (
                   'lab_observation_rejected', 'lab_observation_resolution_recorded'
               )""",
            (person_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: audit_events" not in str(exc):
            raise
        source_rows = []
    for (source,) in source_rows:
        archive_source = retained_lab_archive_source(source, person_id)
        if archive_source is not None:
            archives.add(root / archive_source)
    try:
        cursor = conn.execute("SELECT * FROM lab_results WHERE person_id = ?", (person_id,))
        columns = [column[0] for column in cursor.description or ()]
        canonical = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table: lab_results" not in str(exc):
            raise
        canonical = []
    grouped: dict[str, tuple[str, str, set[str]]] = {}
    errors: list[LabRejectionGap] = []
    for archive_path in sorted(archives):
        archive_source = archive_path.relative_to(root).as_posix()
        source_path = archive_source
        try:
            checksum = source_file_checksum(archive_path)
            members = list(_iter_history_archive_members(archive_path))
            validate_recorded_lab_archive_members(
                conn,
                archive_source=archive_source,
                person_id=person_id,
                members={name for name, _ in members},
            )
            with tempfile.TemporaryDirectory(prefix="famed-lab-review-") as td:
                for member, content in members:
                    source_path = f"{archive_source}#{member}"
                    temp_path = Path(td) / Path(member).name
                    temp_path.write_bytes(content)
                    rejected: list[dict[str, Any]] = []
                    extract_ccda_lab_rows(temp_path, person_id, rejected=rejected)
                    stored, legacy = recorded_lab_decisions(
                        conn, source_path=source_path, person_id=person_id
                    )
                    if any(sha != checksum for sha, _ in stored):
                        raise HistorySyncError("recorded Lab decision checksum is stale")
                    resolutions = tuple(resolution for _, resolution in stored)
                    validate_rejected_lab_observation_resolutions(
                        rejected=rejected, resolutions=resolutions, source_label=source_path
                    )
                    for observation in rejected:
                        resolution = next(
                            (
                                item
                                for item in resolutions
                                if _rejected_lab_matches_selector(observation, item.selector)
                            ),
                            None,
                        )
                        if resolution is not None and resolution.action == "reject":
                            continue
                        quarantined = resolution is not None and resolution.action == "quarantine"
                        if not quarantined and _canonical_text_coverage(
                            observation, canonical, source_path=source_path
                        ):
                            continue
                        selector = {
                            k: v
                            for k, v in observation.items()
                            if k not in {"reason", "raw_mapping"}
                        }
                        identity = json.dumps(selector, sort_keys=True)
                        subject = f"Lab: {observation['test_name']} — {observation['observed_at']}"
                        if quarantined:
                            reason = (
                                "Exact retained Lab observation remains quarantined "
                                "by reviewed decision."
                            )
                        elif resolution is not None:
                            reason = (
                                "Previously accepted Lab observation is missing "
                                "from canonical data."
                            )
                        elif legacy:
                            reason = (
                                "Rejected Lab observation lacks canonical coverage; "
                                "replay the exact legacy reviewed bundle."
                            )
                        else:
                            reason = (
                                "Retained Lab observation lacks canonical coverage: "
                                f"{observation['reason']}"
                            )
                        if identity in grouped:
                            old_subject, _, sources = grouped[identity]
                            sources.add(source_path)
                            if quarantined:
                                grouped[identity] = (old_subject, reason, sources)
                        else:
                            grouped[identity] = (subject, reason, {source_path})
        except (OSError, zipfile.BadZipFile, HistorySyncError, ValueError) as exc:
            errors.append(
                LabRejectionGap(
                    identity=source_path,
                    subject=f"Lab coverage: {archive_path.name}",
                    reason=f"Retained Lab evidence or decisions could not be validated: {exc}",
                    source_paths=(source_path,),
                )
            )
    return [
        LabRejectionGap(identity, subject, reason, tuple(sorted(sources)))
        for identity, (subject, reason, sources) in sorted(grouped.items())
    ] + errors
