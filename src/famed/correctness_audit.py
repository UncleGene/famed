"""Append-only ledger for `fm-correctness-audit` discrepancy findings (ADR-033).

Each event records one disagreement between a persisted coding/normalization
choice (LOINC code, RxNorm CUI, unit, reference range, drug-name variant) and
the value retrieved for it from an authoritative public source via a
scrubbed-generic live web search. Recording an event here never modifies the
challenged local record — that stays with deterministic validation and human
resolution (ADR-018). A human resolves or dismisses a finding separately;
this module tracks that status so a re-run does not re-surface an
already-handled finding.

ADR-033 requires every query to be reducible to a generic
clinical/coding term with no person-identifying value. Judging that is the
agent's responsibility (semantic scrubbing cannot be fully automated without
violating ADR-018's ban on broad deterministic keyword inference), but this
module enforces one concrete, defensible safety net: the recorded
`query_issued` text must not contain the `person_id` verbatim.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

Category = Literal["labs", "medications", "dna", "history"]
Confidence = Literal["high", "medium", "low"]
FindingEventType = Literal["finding_recorded", "finding_resolved", "finding_dismissed"]
FindingStatus = Literal["open", "resolved", "dismissed"]

VALID_CATEGORIES: tuple[Category, ...] = ("labs", "medications", "dna", "history")
VALID_CONFIDENCE: tuple[Confidence, ...] = ("high", "medium", "low")


class CorrectnessAuditError(ValueError):
    """Raised when a correctness-audit ledger operation violates the ledger contract."""


@dataclass(frozen=True)
class FindingEvent:
    event_type: FindingEventType
    finding_id: str
    recorded_at: str
    category: Category | None = None
    person_id: str | None = None
    local_record: str | None = None
    local_field: str | None = None
    local_value: str | None = None
    retrieved_value: str | None = None
    source_url: str | None = None
    source_publisher: str | None = None
    query_issued: str | None = None
    confidence: Confidence | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CorrectnessFinding:
    finding_id: str
    category: Category
    person_id: str | None
    local_record: str
    local_field: str
    local_value: str
    retrieved_value: str
    source_url: str
    source_publisher: str
    query_issued: str
    confidence: Confidence
    status: FindingStatus
    reason: str | None = None


def findings_path(root: Path) -> Path:
    return root / "data" / "correctness-audit-findings.jsonl"


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_category(value: str) -> Category:
    if value not in VALID_CATEGORIES:
        allowed = ", ".join(VALID_CATEGORIES)
        raise CorrectnessAuditError(f"invalid category {value!r}; expected one of: {allowed}")
    return cast(Category, value)


def validate_confidence(value: str) -> Confidence:
    if value not in VALID_CONFIDENCE:
        allowed = ", ".join(VALID_CONFIDENCE)
        raise CorrectnessAuditError(f"invalid confidence {value!r}; expected one of: {allowed}")
    return cast(Confidence, value)


def validate_scrubbed_query(query_issued: str, *, person_id: str | None) -> str:
    """Reject a query that verbatim-contains the person_id it was audited for.

    This is a safety net, not a substitute for the agent's own scrubbing
    judgment (ADR-033): it can only catch verbatim leakage of the
    identifier itself, not paraphrased or otherwise identifying detail.
    """
    stripped = query_issued.strip()
    if not stripped:
        raise CorrectnessAuditError("query_issued is required")
    if person_id and person_id.strip() and person_id.strip().lower() in stripped.lower():
        raise CorrectnessAuditError("query_issued must not contain the person_id")
    return stripped


def _require_non_empty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise CorrectnessAuditError(f"{field_name} is required")
    return stripped


def record_finding(
    root: Path,
    *,
    category: str,
    local_record: str,
    local_field: str,
    local_value: str,
    retrieved_value: str,
    source_url: str,
    source_publisher: str,
    query_issued: str,
    confidence: str,
    person_id: str | None = None,
    finding_id: str | None = None,
) -> FindingEvent:
    validated_category = validate_category(category)
    validated_confidence = validate_confidence(confidence)
    validated_query = validate_scrubbed_query(query_issued, person_id=person_id)
    event = FindingEvent(
        event_type="finding_recorded",
        finding_id=finding_id or str(uuid.uuid4()),
        recorded_at=utc_timestamp(),
        category=validated_category,
        person_id=person_id.strip() if person_id else None,
        local_record=_require_non_empty(local_record, "local_record"),
        local_field=_require_non_empty(local_field, "local_field"),
        local_value=_require_non_empty(local_value, "local_value"),
        retrieved_value=_require_non_empty(retrieved_value, "retrieved_value"),
        source_url=_require_non_empty(source_url, "source_url"),
        source_publisher=_require_non_empty(source_publisher, "source_publisher"),
        query_issued=validated_query,
        confidence=validated_confidence,
    )
    return _append_event(root, event)


def resolve_finding(root: Path, *, finding_id: str, reason: str) -> FindingEvent:
    return _transition(root, finding_id=finding_id, reason=reason, event_type="finding_resolved")


def dismiss_finding(root: Path, *, finding_id: str, reason: str) -> FindingEvent:
    return _transition(root, finding_id=finding_id, reason=reason, event_type="finding_dismissed")


def _transition(
    root: Path, *, finding_id: str, reason: str, event_type: FindingEventType
) -> FindingEvent:
    stripped_id = _require_non_empty(finding_id, "finding_id")
    stripped_reason = _require_non_empty(reason, "reason")
    open_ids = {finding.finding_id for finding in open_findings(root)}
    if stripped_id not in open_ids:
        raise CorrectnessAuditError(f"finding is not open: {stripped_id}")
    return _append_event(
        root,
        FindingEvent(
            event_type=event_type,
            finding_id=stripped_id,
            recorded_at=utc_timestamp(),
            reason=stripped_reason,
        ),
    )


def _append_event(root: Path, event: FindingEvent) -> FindingEvent:
    path = findings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(event), sort_keys=True) + "\n")
    return event


def load_events(root: Path) -> list[FindingEvent]:
    path = findings_path(root)
    if not path.exists():
        return []
    events: list[FindingEvent] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorrectnessAuditError(
                    f"invalid JSON at {path}:{line_number}: {exc.msg}"
                ) from exc
            try:
                events.append(FindingEvent(**payload))
            except TypeError as exc:
                raise CorrectnessAuditError(
                    f"invalid event at {path}:{line_number}: {exc}"
                ) from exc
    return events


def current_findings(events: list[FindingEvent]) -> dict[str, CorrectnessFinding]:
    current: dict[str, CorrectnessFinding] = {}
    for event in events:
        if event.event_type == "finding_recorded":
            current[event.finding_id] = CorrectnessFinding(
                finding_id=event.finding_id,
                category=event.category or "labs",
                person_id=event.person_id,
                local_record=event.local_record or "",
                local_field=event.local_field or "",
                local_value=event.local_value or "",
                retrieved_value=event.retrieved_value or "",
                source_url=event.source_url or "",
                source_publisher=event.source_publisher or "",
                query_issued=event.query_issued or "",
                confidence=event.confidence or "low",
                status="open",
            )
        elif event.event_type in {"finding_resolved", "finding_dismissed"}:
            existing = current.get(event.finding_id)
            if existing is None:
                continue
            status: FindingStatus = (
                "resolved" if event.event_type == "finding_resolved" else "dismissed"
            )
            current[event.finding_id] = replace(existing, status=status, reason=event.reason)
    return current


def open_findings(root: Path, *, category: str | None = None) -> list[CorrectnessFinding]:
    findings = [
        finding
        for finding in current_findings(load_events(root)).values()
        if finding.status == "open"
    ]
    if category is not None:
        findings = [finding for finding in findings if finding.category == category]
    return findings


def all_findings(root: Path, *, status: str | None = None) -> list[CorrectnessFinding]:
    findings = list(current_findings(load_events(root)).values())
    if status is not None:
        findings = [finding for finding in findings if finding.status == status]
    return findings


@dataclass(frozen=True)
class LabCodeUsage:
    test_name: str
    loinc_code: str | None
    reading_count: int


@dataclass(frozen=True)
class MedicationCodeUsage:
    medication_name: str
    rxnorm_cui: str | None
    row_count: int


@dataclass(frozen=True)
class DnaVariantAnnotationUsage:
    rsid: str
    variation_id: str | None
    gene: str | None
    significance: str | None
    condition: str | None
    review_status: str | None


def distinct_lab_codes(db_path: Path) -> list[LabCodeUsage]:
    """Return distinct (test_name, loinc_code) pairs currently persisted, read-only."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cur = conn.execute(
            "SELECT test_name, loinc_code, COUNT(*) FROM lab_results "
            "GROUP BY test_name, loinc_code ORDER BY test_name"
        )
        return [
            LabCodeUsage(test_name=row[0], loinc_code=row[1], reading_count=row[2]) for row in cur
        ]


def distinct_medication_codes(db_path: Path) -> list[MedicationCodeUsage]:
    """Return distinct (medication_name, rxnorm_cui) pairs currently persisted, read-only."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cur = conn.execute(
            "SELECT medication_name, rxnorm_cui, COUNT(*) FROM medications "
            "GROUP BY medication_name, rxnorm_cui ORDER BY medication_name"
        )
        return [
            MedicationCodeUsage(medication_name=row[0], rxnorm_cui=row[1], row_count=row[2])
            for row in cur
        ]


def distinct_dna_variant_annotations(
    db_path: Path, ref_db_path: Path
) -> list[DnaVariantAnnotationUsage]:
    """Return grouped genetic-risk candidate rsIDs with ClinVar annotation.

    The audit target is the generic reference annotation for pathogenic-text
    ClinVar rows whose alternate allele appears in at least one local genotype.
    It deliberately groups across people and excludes alleles, dates, source
    paths, and person identifiers so the live-agent verification query can stay
    within ADR-033's scrubbed-generic boundary.
    """
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("ATTACH DATABASE ? AS ref", (f"file:{ref_db_path}?mode=ro",))
            annotations = conn.execute(
                """
                SELECT
                    cv.rsid,
                    cv.variation_id,
                    cv.gene,
                    cv.significance,
                    cv.condition,
                    cv.review_status
                FROM genotypes AS g
                JOIN ref.clinvar_variants AS cv ON cv.rsid = g.rsid
                WHERE LOWER(COALESCE(cv.significance, '')) LIKE '%pathogenic%'
                  AND UPPER(COALESCE(cv.alternate_allele, '')) != ''
                  AND (
                    UPPER(g.allele1) = UPPER(cv.alternate_allele)
                    OR UPPER(g.allele2) = UPPER(cv.alternate_allele)
                  )
                GROUP BY
                    cv.rsid,
                    cv.variation_id,
                    cv.gene,
                    cv.significance,
                    cv.condition,
                    cv.review_status
                ORDER BY cv.rsid
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise CorrectnessAuditError(f"could not enumerate DNA audit facts: {exc}") from exc

    usages: list[DnaVariantAnnotationUsage] = []
    for annotation in annotations:
        usages.append(
            DnaVariantAnnotationUsage(
                rsid=str(annotation["rsid"]),
                variation_id=_optional_text(annotation, "variation_id"),
                gene=_optional_text(annotation, "gene"),
                significance=_optional_text(annotation, "significance"),
                condition=_optional_text(annotation, "condition"),
                review_status=_optional_text(annotation, "review_status"),
            )
        )
    return usages


def _optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain the append-only correctness-audit findings ledger."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repo root containing data/ (default: current directory).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Append a new discrepancy finding.")
    record.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    record.add_argument("--local-record", required=True)
    record.add_argument("--local-field", required=True)
    record.add_argument("--local-value", required=True)
    record.add_argument("--retrieved-value", required=True)
    record.add_argument("--source-url", required=True)
    record.add_argument("--source-publisher", required=True)
    record.add_argument("--query-issued", required=True)
    record.add_argument("--confidence", required=True, choices=VALID_CONFIDENCE)
    record.add_argument("--person-id")
    record.add_argument("--finding-id")

    resolve = subparsers.add_parser("resolve", help="Mark an open finding resolved.")
    resolve.add_argument("--finding-id", required=True)
    resolve.add_argument("--reason", required=True)

    dismiss = subparsers.add_parser("dismiss", help="Mark an open finding dismissed.")
    dismiss.add_argument("--finding-id", required=True)
    dismiss.add_argument("--reason", required=True)

    list_open = subparsers.add_parser("list-open", help="List open findings.")
    list_open.add_argument("--category", choices=VALID_CATEGORIES)

    list_facts = subparsers.add_parser(
        "list-facts", help="Enumerate current coding facts in scope for audit."
    )
    list_facts.add_argument("--category", required=True, choices=("labs", "medications", "dna"))
    list_facts.add_argument(
        "--db-path", default=str(Path("data") / "famed.db"), help="Path to famed.db."
    )
    list_facts.add_argument(
        "--ref-db-path",
        default=str(Path("data") / "reference" / "famed-reference.db"),
        help="Path to famed-reference.db for DNA annotation facts.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "record":
            event = record_finding(
                root,
                category=args.category,
                local_record=args.local_record,
                local_field=args.local_field,
                local_value=args.local_value,
                retrieved_value=args.retrieved_value,
                source_url=args.source_url,
                source_publisher=args.source_publisher,
                query_issued=args.query_issued,
                confidence=args.confidence,
                person_id=args.person_id,
                finding_id=args.finding_id,
            )
            print(f"recorded finding {event.finding_id}")
        elif args.command == "resolve":
            resolve_finding(root, finding_id=args.finding_id, reason=args.reason)
            print(f"resolved finding {args.finding_id}")
        elif args.command == "dismiss":
            dismiss_finding(root, finding_id=args.finding_id, reason=args.reason)
            print(f"dismissed finding {args.finding_id}")
        elif args.command == "list-open":
            for finding in open_findings(root, category=args.category):
                print(
                    f"{finding.finding_id}\t{finding.category}\t{finding.local_record}"
                    f"\t{finding.local_value} -> {finding.retrieved_value}"
                    f"\t{finding.confidence}\t{finding.source_url}"
                )
        elif args.command == "list-facts":
            db_path = Path(args.db_path)
            if args.category == "labs":
                for usage in distinct_lab_codes(db_path):
                    print(f"{usage.test_name}\t{usage.loinc_code}\t{usage.reading_count}")
            elif args.category == "medications":
                for med_usage in distinct_medication_codes(db_path):
                    print(
                        f"{med_usage.medication_name}\t{med_usage.rxnorm_cui}"
                        f"\t{med_usage.row_count}"
                    )
            else:
                ref_db_path = Path(args.ref_db_path)
                for dna_usage in distinct_dna_variant_annotations(db_path, ref_db_path):
                    print(
                        f"{dna_usage.rsid}\t{dna_usage.variation_id}\t{dna_usage.gene}"
                        f"\t{dna_usage.significance}\t{dna_usage.condition}"
                        f"\t{dna_usage.review_status}"
                    )
    except CorrectnessAuditError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
