"""Append-only findings ledger for derived insight events."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

EvidenceTier = Literal["E1", "E2", "E3", "E4", "E5"]
FindingEventType = Literal["finding_recorded", "finding_superseded", "finding_marked_stale"]
FindingStatus = Literal["active", "superseded", "stale"]
EvidenceCheck = Literal["verified", "lower_confidence_unverified"]

FINDINGS_LEDGER_PATH = Path("ledger") / "findings.jsonl"
VALID_EVIDENCE_TIERS: tuple[EvidenceTier, ...] = ("E1", "E2", "E3", "E4", "E5")
HIGH_EVIDENCE_TIERS = frozenset({"E1", "E2"})
VALID_EVIDENCE_CHECKS: tuple[EvidenceCheck, ...] = (
    "verified",
    "lower_confidence_unverified",
)
VALID_EVENT_TYPES: tuple[FindingEventType, ...] = (
    "finding_recorded",
    "finding_superseded",
    "finding_marked_stale",
)
VALID_STATUSES: tuple[FindingStatus, ...] = ("active", "superseded", "stale")
SCHEMA_VERSION = 1


class FindingsLedgerError(ValueError):
    """Raised when a findings-ledger operation violates the ledger contract."""


@dataclass(frozen=True)
class FindingEvent:
    event_type: FindingEventType
    finding_id: str
    occurred_at: str
    schema_version: int = SCHEMA_VERSION
    person_id: str | None = None
    domain: str | None = None
    statement: str | None = None
    evidence_tier: EvidenceTier | None = None
    confidence: float | None = None
    evidence_check: EvidenceCheck | None = None
    source_paths: list[str] = field(default_factory=list)
    status: FindingStatus | None = None
    superseded_by: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CurrentFinding:
    finding_id: str
    person_id: str
    domain: str
    statement: str
    evidence_tier: EvidenceTier
    confidence: float
    evidence_check: EvidenceCheck
    source_paths: tuple[str, ...]
    status: FindingStatus
    superseded_by: str | None = None
    reason: str | None = None


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_evidence_tier(value: str) -> EvidenceTier:
    if value not in VALID_EVIDENCE_TIERS:
        allowed = ", ".join(VALID_EVIDENCE_TIERS)
        raise FindingsLedgerError(f"invalid evidence tier {value!r}; expected one of: {allowed}")
    return cast(EvidenceTier, value)


def validate_evidence_check(value: str) -> EvidenceCheck:
    if value not in VALID_EVIDENCE_CHECKS:
        allowed = ", ".join(VALID_EVIDENCE_CHECKS)
        raise FindingsLedgerError(f"invalid evidence check {value!r}; expected one of: {allowed}")
    return cast(EvidenceCheck, value)


def validate_event_type(value: str) -> FindingEventType:
    if value not in VALID_EVENT_TYPES:
        allowed = ", ".join(VALID_EVENT_TYPES)
        raise FindingsLedgerError(f"invalid event type {value!r}; expected one of: {allowed}")
    return cast(FindingEventType, value)


def validate_status(value: str) -> FindingStatus:
    if value not in VALID_STATUSES:
        allowed = ", ".join(VALID_STATUSES)
        raise FindingsLedgerError(f"invalid finding status {value!r}; expected one of: {allowed}")
    return cast(FindingStatus, value)


def normalize_source_paths(source_paths: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in source_paths:
        source_path = " ".join(str(raw or "").split())
        if not source_path or source_path in seen:
            continue
        seen.add(source_path)
        normalized.append(source_path)
    if not normalized:
        raise FindingsLedgerError("at least one source path is required")
    return normalized


def validate_recorded_finding(
    *,
    person_id: str,
    domain: str,
    statement: str,
    evidence_tier: str,
    confidence: float,
    evidence_check: str,
    source_paths: list[str] | tuple[str, ...],
) -> tuple[EvidenceTier, EvidenceCheck, list[str]]:
    if not person_id.strip():
        raise FindingsLedgerError("person_id is required")
    if not domain.strip():
        raise FindingsLedgerError("domain is required")
    if not statement.strip():
        raise FindingsLedgerError("statement is required")
    tier = validate_evidence_tier(evidence_tier)
    check = validate_evidence_check(evidence_check)
    if not 0 <= confidence <= 1:
        raise FindingsLedgerError("confidence must be between 0 and 1")
    if tier in HIGH_EVIDENCE_TIERS and check != "verified":
        raise FindingsLedgerError("E1/E2 findings must have verified evidence checks")
    return tier, check, normalize_source_paths(source_paths)


def append_event(ledger_path: Path, event: FindingEvent) -> FindingEvent:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(event), sort_keys=True) + "\n")
    return event


def record_finding(
    ledger_path: Path,
    *,
    person_id: str,
    domain: str,
    statement: str,
    evidence_tier: str,
    confidence: float,
    source_paths: list[str] | tuple[str, ...],
    evidence_check: str = "lower_confidence_unverified",
    finding_id: str | None = None,
) -> FindingEvent:
    tier, check, normalized_sources = validate_recorded_finding(
        person_id=person_id,
        domain=domain,
        statement=statement,
        evidence_tier=evidence_tier,
        confidence=confidence,
        evidence_check=evidence_check,
        source_paths=source_paths,
    )
    return append_event(
        ledger_path,
        FindingEvent(
            event_type="finding_recorded",
            finding_id=finding_id or str(uuid.uuid4()),
            occurred_at=utc_timestamp(),
            person_id=person_id.strip(),
            domain=domain.strip(),
            statement=statement.strip(),
            evidence_tier=tier,
            confidence=confidence,
            evidence_check=check,
            source_paths=normalized_sources,
            status="active",
        ),
    )


def supersede_finding(
    ledger_path: Path,
    *,
    finding_id: str,
    superseded_by: str,
    reason: str,
) -> FindingEvent:
    if not finding_id.strip():
        raise FindingsLedgerError("finding_id is required")
    if not superseded_by.strip():
        raise FindingsLedgerError("superseded_by is required")
    if finding_id.strip() == superseded_by.strip():
        raise FindingsLedgerError("superseded_by must reference a different finding")
    if not reason.strip():
        raise FindingsLedgerError("reason is required")
    current = current_findings(load_events(ledger_path))
    _require_active_finding(current, finding_id.strip())
    _require_active_finding(current, superseded_by.strip())
    return append_event(
        ledger_path,
        FindingEvent(
            event_type="finding_superseded",
            finding_id=finding_id.strip(),
            occurred_at=utc_timestamp(),
            status="superseded",
            superseded_by=superseded_by.strip(),
            reason=reason.strip(),
        ),
    )


def mark_finding_stale(
    ledger_path: Path,
    *,
    finding_id: str,
    reason: str,
) -> FindingEvent:
    if not finding_id.strip():
        raise FindingsLedgerError("finding_id is required")
    if not reason.strip():
        raise FindingsLedgerError("reason is required")
    _require_active_finding(current_findings(load_events(ledger_path)), finding_id.strip())
    return append_event(
        ledger_path,
        FindingEvent(
            event_type="finding_marked_stale",
            finding_id=finding_id.strip(),
            occurred_at=utc_timestamp(),
            status="stale",
            reason=reason.strip(),
        ),
    )


def load_events(ledger_path: Path) -> list[FindingEvent]:
    if not ledger_path.exists():
        return []
    events: list[FindingEvent] = []
    with ledger_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FindingsLedgerError(
                    f"invalid JSON at {ledger_path}:{line_number}: {exc.msg}"
                ) from exc
            events.append(_event_from_payload(payload, ledger_path, line_number))
    return events


def _require_active_finding(current: dict[str, CurrentFinding], finding_id: str) -> CurrentFinding:
    finding = current.get(finding_id)
    if finding is None:
        raise FindingsLedgerError(f"finding not found: {finding_id}")
    if finding.status != "active":
        raise FindingsLedgerError(f"finding is not active: {finding_id}")
    return finding


def _event_from_payload(
    payload: dict[str, Any], ledger_path: Path, line_number: int
) -> FindingEvent:
    try:
        event = FindingEvent(**payload)
    except TypeError as exc:
        raise FindingsLedgerError(f"invalid event at {ledger_path}:{line_number}: {exc}") from exc
    if event.schema_version != SCHEMA_VERSION:
        raise FindingsLedgerError(
            f"unsupported schema_version at {ledger_path}:{line_number}: {event.schema_version}"
        )
    validate_event_type(event.event_type)
    if event.status is not None:
        validate_status(event.status)
    return event


def current_findings(events: list[FindingEvent]) -> dict[str, CurrentFinding]:
    current: dict[str, CurrentFinding] = {}
    for event in events:
        if event.event_type == "finding_recorded":
            _validate_loaded_record(event)
            current[event.finding_id] = CurrentFinding(
                finding_id=event.finding_id,
                person_id=event.person_id or "",
                domain=event.domain or "",
                statement=event.statement or "",
                evidence_tier=event.evidence_tier or "E5",
                confidence=event.confidence or 0,
                evidence_check=event.evidence_check or "lower_confidence_unverified",
                source_paths=tuple(event.source_paths),
                status="active",
            )
        elif event.event_type in {"finding_superseded", "finding_marked_stale"}:
            existing = current.get(event.finding_id)
            if existing is None:
                continue
            status: FindingStatus = (
                "superseded" if event.event_type == "finding_superseded" else "stale"
            )
            current[event.finding_id] = CurrentFinding(
                finding_id=existing.finding_id,
                person_id=existing.person_id,
                domain=existing.domain,
                statement=existing.statement,
                evidence_tier=existing.evidence_tier,
                confidence=existing.confidence,
                evidence_check=existing.evidence_check,
                source_paths=existing.source_paths,
                status=status,
                superseded_by=event.superseded_by,
                reason=event.reason,
            )
    return current


def _validate_loaded_record(event: FindingEvent) -> None:
    validate_recorded_finding(
        person_id=event.person_id or "",
        domain=event.domain or "",
        statement=event.statement or "",
        evidence_tier=event.evidence_tier or "",
        confidence=event.confidence if event.confidence is not None else -1,
        evidence_check=event.evidence_check or "",
        source_paths=event.source_paths,
    )


def active_findings(ledger_path: Path) -> list[CurrentFinding]:
    return [
        finding
        for finding in current_findings(load_events(ledger_path)).values()
        if finding.status == "active"
    ]


def supersede_stale_variants(
    ledger_path: Path,
    *,
    new_finding_id: str,
    stale_candidate_ids: list[str],
    reason: str,
    active_ids: set[str] | None = None,
) -> list[str]:
    """Supersede any of stale_candidate_ids that are currently active in the ledger.

    Returns the list of finding IDs that were superseded.  Callers may pass a
    pre-built active_ids set to avoid re-reading the ledger on each call; the
    set is mutated in-place when provided.
    """
    if active_ids is None:
        active_ids = {f.finding_id for f in active_findings(ledger_path)}
    superseded: list[str] = []
    for stale_id in stale_candidate_ids:
        if stale_id == new_finding_id:
            continue
        if stale_id in active_ids:
            supersede_finding(
                ledger_path,
                finding_id=stale_id,
                superseded_by=new_finding_id,
                reason=reason,
            )
            active_ids.discard(stale_id)
            superseded.append(stale_id)
    return superseded


def mark_orphaned_findings_stale(
    ledger_path: Path,
    *,
    person_id: str,
    domain: str,
    current_finding_ids: set[str],
    reason: str,
) -> list[FindingEvent]:
    """Mark active producer-owned findings stale when recomputation omits them."""
    if not person_id.strip():
        raise FindingsLedgerError("person_id is required")
    if not domain.strip():
        raise FindingsLedgerError("domain is required")
    if not reason.strip():
        raise FindingsLedgerError("reason is required")

    events: list[FindingEvent] = []
    for finding in active_findings(ledger_path):
        if finding.person_id != person_id or finding.domain != domain:
            continue
        if finding.finding_id in current_finding_ids:
            continue
        events.append(
            mark_finding_stale(
                ledger_path,
                finding_id=finding.finding_id,
                reason=reason,
            )
        )
    return events


def mark_findings_stale_for_source(
    ledger_path: Path,
    *,
    source_path: str,
    reason: str,
    person_id: str | None = None,
) -> list[FindingEvent]:
    if not source_path.strip():
        raise FindingsLedgerError("source_path is required")
    events: list[FindingEvent] = []
    for finding in active_findings(ledger_path):
        if person_id is not None and finding.person_id != person_id:
            continue
        if source_path in finding.source_paths:
            events.append(
                mark_finding_stale(ledger_path, finding_id=finding.finding_id, reason=reason)
            )
    return events


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Maintain the append-only findings ledger.")
    parser.add_argument(
        "--ledger-path",
        default=str(FINDINGS_LEDGER_PATH),
        help="Path to findings JSONL ledger (default: ledger/findings.jsonl).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Append a new finding.")
    record.add_argument("--person-id", required=True)
    record.add_argument("--domain", required=True)
    record.add_argument("--statement", required=True)
    record.add_argument("--evidence-tier", required=True, choices=VALID_EVIDENCE_TIERS)
    record.add_argument("--confidence", required=True, type=float)
    record.add_argument(
        "--evidence-check",
        choices=VALID_EVIDENCE_CHECKS,
        default="lower_confidence_unverified",
    )
    record.add_argument("--source-path", action="append", required=True)
    record.add_argument("--finding-id")

    supersede = subparsers.add_parser("supersede", help="Append a supersession event.")
    supersede.add_argument("--finding-id", required=True)
    supersede.add_argument("--superseded-by", required=True)
    supersede.add_argument("--reason", required=True)

    stale = subparsers.add_parser("stale-source", help="Mark active findings stale by source.")
    stale.add_argument("--source-path", required=True)
    stale.add_argument("--reason", required=True)
    stale.add_argument("--person-id")

    subparsers.add_parser("list-current", help="List non-superseded/stale findings.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ledger_path = Path(args.ledger_path)
    try:
        if args.command == "record":
            event = record_finding(
                ledger_path,
                person_id=args.person_id,
                domain=args.domain,
                statement=args.statement,
                evidence_tier=args.evidence_tier,
                confidence=args.confidence,
                evidence_check=args.evidence_check,
                source_paths=args.source_path,
                finding_id=args.finding_id,
            )
            print(f"recorded finding {event.finding_id}")
        elif args.command == "supersede":
            supersede_finding(
                ledger_path,
                finding_id=args.finding_id,
                superseded_by=args.superseded_by,
                reason=args.reason,
            )
            print(f"superseded finding {args.finding_id}")
        elif args.command == "stale-source":
            events = mark_findings_stale_for_source(
                ledger_path,
                source_path=args.source_path,
                reason=args.reason,
                person_id=args.person_id,
            )
            print(f"marked {len(events)} finding(s) stale")
        elif args.command == "list-current":
            for finding in active_findings(ledger_path):
                print(
                    f"{finding.finding_id}\t{finding.person_id}\t"
                    f"{finding.evidence_tier}\t{finding.statement}"
                )
    except FindingsLedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
