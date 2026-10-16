"""Deterministic current-review projections for person and household workflows."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from famed.lab_rejections import current_lab_rejection_gaps, retained_lab_archive_source
from famed.lab_sync import write_category_index_note
from famed.medication_sync import (
    build_medication_conflicts_from_db,
    medication_conflict_groups,
)
from famed.person_sync import load_person_display_name
from famed.query import list_person_ids

PENDING_REVIEW_ITEM_SCHEMA = "famed.pending-review-item.v1"
ReviewKind = Literal[
    "history_source",
    "lab_observation",
    "medication_conflict",
    "inbox_source",
    "quarantined_source",
]
_REVIEW_KINDS = frozenset(
    {
        "history_source",
        "lab_observation",
        "medication_conflict",
        "inbox_source",
        "quarantined_source",
    }
)


@dataclass(frozen=True)
class PendingReviewItem:
    """One current, actionable review item derived from category-owned evidence."""

    item_id: str
    kind: ReviewKind
    person_id: str | None
    subject: str
    reason: str
    source_paths: tuple[str, ...]
    resolution_skill: str
    resolution_action: str
    detail_report: str | None = None
    schema_version: str = PENDING_REVIEW_ITEM_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "kind": self.kind,
            "person_id": self.person_id,
            "subject": self.subject,
            "reason": self.reason,
            "source_paths": list(self.source_paths),
            "resolution_skill": self.resolution_skill,
            "resolution_action": self.resolution_action,
            "detail_report": self.detail_report,
        }


@dataclass(frozen=True)
class PendingReviewSnapshot:
    """Current household review state and explicitly unsupported audit coverage."""

    items: tuple[PendingReviewItem, ...]
    unsupported_event_counts: tuple[tuple[str, int], ...]

    def items_for_person(self, person_id: str) -> tuple[PendingReviewItem, ...]:
        return tuple(item for item in self.items if item.person_id == person_id)

    @property
    def unresolved_items(self) -> tuple[PendingReviewItem, ...]:
        return tuple(item for item in self.items if item.person_id is None)


def _stable_item_id(kind: ReviewKind, identity: object) -> str:
    payload = json.dumps(
        {"kind": kind, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _is_missing_table(exc: sqlite3.OperationalError, table_name: str) -> bool:
    return f"no such table: {table_name}" in str(exc)


def _clean_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"pending review {field_name} must be nonblank text")
    return value.strip()


def validate_pending_review_item(item: PendingReviewItem) -> PendingReviewItem:
    """Validate the current-review item contract before rendering."""
    if item.schema_version != PENDING_REVIEW_ITEM_SCHEMA:
        raise ValueError(f"unsupported pending review schema: {item.schema_version}")
    if item.kind not in _REVIEW_KINDS:
        raise ValueError(f"unsupported pending review kind: {item.kind}")
    if len(item.item_id) != 20 or any(ch not in "0123456789abcdef" for ch in item.item_id):
        raise ValueError("pending review item_id must be a 20-character lowercase hex digest")
    if item.person_id is not None:
        _clean_text(item.person_id, field_name="person_id")
    _clean_text(item.subject, field_name="subject")
    _clean_text(item.reason, field_name="reason")
    _clean_text(item.resolution_skill, field_name="resolution_skill")
    _clean_text(item.resolution_action, field_name="resolution_action")
    if not item.source_paths:
        raise ValueError("pending review source_paths must not be empty")
    for source_path in item.source_paths:
        _clean_text(source_path, field_name="source_path")
        path = Path(source_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("pending review source paths must be repository-relative")
    if item.detail_report is not None:
        _clean_text(item.detail_report, field_name="detail_report")
        detail_path = Path(item.detail_report)
        if detail_path.is_absolute() or ".." in detail_path.parts:
            raise ValueError("pending review detail_report must be vault-relative")
    return item


def _medication_conflict_items(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[PendingReviewItem]:
    try:
        rows = build_medication_conflicts_from_db(conn, person_id)
    except sqlite3.OperationalError as exc:
        if not _is_missing_table(exc, "medications"):
            raise
        rows = []
    groups = medication_conflict_groups(rows)

    items: list[PendingReviewItem] = []
    detail_report = f"Records/{person_id}/Reports/medication-conflicts.md"
    for key, group_rows in sorted(groups.items()):
        sources = tuple(sorted({str(row.get("source_path") or "") for row in group_rows}))
        candidates = tuple(
            sorted(
                (
                    str(row.get("source_path") or ""),
                    str(row.get(key[2]) or ""),
                )
                for row in group_rows
            )
        )
        medication_name = _clean_text(key[0], field_name="medication_name")
        axis = key[2]
        item = PendingReviewItem(
            item_id=_stable_item_id(
                "medication_conflict",
                {"person_id": person_id, "key": key, "candidates": candidates},
            ),
            kind="medication_conflict",
            person_id=person_id,
            subject=f"{medication_name} — conflicting {axis}",
            reason=f"Current retained sources disagree about the medication {axis}.",
            source_paths=sources,
            resolution_skill="fm-correct-fact",
            resolution_action=(
                f"Review the linked medication conflict report and record the user-selected {axis}."
            ),
            detail_report=detail_report,
        )
        items.append(validate_pending_review_item(item))
    return items


def _history_source_items(
    root: Path,
    vault_root: Path,
    person_id: str,
) -> list[PendingReviewItem]:
    """Project unresolved retained history notes into the current review queue."""
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return []

    items: list[PendingReviewItem] = []
    detail_report = f"Records/{person_id}/Reports/medical-history-summary.md"
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        try:
            text = note_path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            end = text.find("\n---", 3)
            if end == -1:
                continue
            front_matter = yaml.safe_load(text[3:end]) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(front_matter, dict):
            continue
        if front_matter.get("record_type") != "history":
            continue
        if front_matter.get("verification_status") != "needs_review":
            continue

        source_path = str(front_matter.get("source_path") or "").strip()
        missing_provenance = not source_path
        if missing_provenance:
            try:
                source_path = note_path.relative_to(root).as_posix()
            except ValueError:
                source_path = note_path.relative_to(vault_root.parent).as_posix()
        accepted_facts = front_matter.get("accepted_facts")
        has_accepted_facts = isinstance(accepted_facts, list) and bool(accepted_facts)
        subject = str(front_matter.get("source_file_name") or note_path.stem).strip()
        if missing_provenance:
            reason = "History review note lacks retained source provenance."
        elif has_accepted_facts:
            reason = "Structured history facts remain marked for review."
        else:
            reason = "Retained history source has not produced accepted structured facts."
        item = PendingReviewItem(
            item_id=_stable_item_id(
                "history_source",
                {
                    "person_id": person_id,
                    "note": note_path.name,
                    "source_path": source_path,
                },
            ),
            kind="history_source",
            person_id=person_id,
            subject=f"History: {subject}",
            reason=reason,
            source_paths=(source_path,),
            resolution_skill="fm-history-extract",
            resolution_action=(
                "Reconcile retained provenance before source-first history extraction."
                if missing_provenance
                else "Run source-first history extraction; reconcile retained provenance "
                "first if the source is unavailable."
            ),
            detail_report=detail_report,
        )
        items.append(validate_pending_review_item(item))
    return items


def _is_review_file(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return path.is_file() and all(not part.startswith(".") for part in relative.parts)


def _latest_audit_reason(conn: sqlite3.Connection, source_path: str) -> str | None:
    try:
        row = conn.execute(
            """
            SELECT message
            FROM audit_events
            WHERE source_path = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (source_path,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if not _is_missing_table(exc, "audit_events"):
            raise
        return None
    if row is None or not isinstance(row[0], str) or not row[0].strip():
        return None
    return row[0].strip()


def _file_review_items(
    conn: sqlite3.Connection,
    *,
    root: Path,
    known_person_ids: frozenset[str],
) -> list[PendingReviewItem]:
    items: list[PendingReviewItem] = []
    for lane, kind in (
        ("inbox", "inbox_source"),
        ("quarantine", "quarantined_source"),
    ):
        lane_root = root / "data" / lane
        if not lane_root.is_dir():
            continue
        for path in sorted(candidate for candidate in lane_root.rglob("*") if candidate.is_file()):
            if not _is_review_file(path, lane_root):
                continue
            repository_path = path.relative_to(root).as_posix()
            lane_relative = path.relative_to(lane_root)
            first_part = lane_relative.parts[0] if lane_relative.parts else ""
            person_id = first_part if first_part in known_person_ids else None
            if len(lane_relative.parts) < 2 or (
                person_id is not None and len(lane_relative.parts) == 2
            ):
                category = lane
            else:
                category = lane_relative.parts[-2]
            if kind == "inbox_source":
                reason = "Inbox source has not completed ingestion."
                resolution_skill = "fm-ingest-inbox"
                resolution_action = "Process the inbox source and resolve any person ambiguity."
            else:
                reason = (
                    _latest_audit_reason(conn, repository_path)
                    or "Quarantined source remains present on disk."
                )
                resolution_skill = "fm-ingest-inbox"
                resolution_action = (
                    "Re-process this quarantine source through the attended inbox workflow."
                    if person_id
                    else "Resolve the person and category before processing the source."
                )
            item = PendingReviewItem(
                item_id=_stable_item_id(kind, repository_path),
                kind=kind,
                person_id=person_id,
                subject=f"{category}: {path.name}",
                reason=reason,
                source_paths=(repository_path,),
                resolution_skill=resolution_skill,
                resolution_action=resolution_action,
            )
            items.append(validate_pending_review_item(item))
    return items


def _unsupported_event_counts(
    conn: sqlite3.Connection, person_ids: list[str]
) -> tuple[tuple[str, int], ...]:
    """Expose audit classes whose current state cannot yet be derived safely."""
    try:
        rows = conn.execute(
            """
            SELECT event_type, subject_id, source_path
            FROM audit_events
            WHERE status IN ('awaiting_user_decision', 'quarantined')
              AND event_type NOT IN (
                'medication_conflict_detected',
                'medication_conflict_resolution_recorded',
                'lab_import_quarantined',
                'inbox_route_quarantined'
              )
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _is_missing_table(exc, "audit_events"):
            raise
        return ()
    counts: dict[str, int] = {}
    for event_type, person_id, source_path in rows:
        if (
            event_type in {"lab_observation_rejected", "lab_observation_resolution_recorded"}
            and person_id in person_ids
            and retained_lab_archive_source(source_path, person_id) is not None
        ):
            continue
        counts[event_type] = counts.get(event_type, 0) + 1
    return tuple(sorted(counts.items()))


def derive_pending_review_snapshot(
    conn: sqlite3.Connection,
    *,
    root: Path,
    vault_root: Path,
) -> PendingReviewSnapshot:
    """Derive current review state without mutating canonical or audit storage."""
    person_ids = list_person_ids(vault_root)
    items: list[PendingReviewItem] = []
    for person_id in person_ids:
        items.extend(_history_source_items(root, vault_root, person_id))
        for gap in current_lab_rejection_gaps(conn, root=root, person_id=person_id):
            items.append(
                validate_pending_review_item(
                    PendingReviewItem(
                        item_id=_stable_item_id(
                            "lab_observation", {"person_id": person_id, "observation": gap.identity}
                        ),
                        kind="lab_observation",
                        person_id=person_id,
                        subject=gap.subject,
                        reason=gap.reason,
                        source_paths=gap.source_paths,
                        resolution_skill="fm-reprocess-source",
                        resolution_action=(
                            "Review the exact retained observation; replay with its validated "
                            "decisions, or reconcile inaccessible evidence first."
                        ),
                    )
                )
            )
        items.extend(_medication_conflict_items(conn, person_id))
    items.extend(
        _file_review_items(
            conn,
            root=root,
            known_person_ids=frozenset(person_ids),
        )
    )
    return PendingReviewSnapshot(
        items=tuple(
            sorted(
                items,
                key=lambda item: (
                    item.person_id is None,
                    item.person_id or "",
                    item.kind,
                    item.subject.casefold(),
                    item.item_id,
                ),
            )
        ),
        unsupported_event_counts=_unsupported_event_counts(conn, person_ids),
    )


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_pending_review_report(
    person_id: str,
    items: tuple[PendingReviewItem, ...],
    *,
    generated_on: str | None = None,
    display_name: str | None = None,
) -> str:
    """Render a compact Obsidian report that links detail-owning reports."""
    for item in items:
        validate_pending_review_item(item)
        if item.person_id != person_id:
            raise ValueError("pending review report item belongs to a different person")
    generated_on = generated_on or datetime.now(UTC).date().isoformat()
    front_matter = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "pending_review",
        "generated_at": generated_on,
        "verification_status": "needs_review" if items else "accepted",
        "pending_review_count": len(items),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    lines = [
        "---",
        yaml_block,
        "---",
        "",
        f"# Pending Review{f' — {display_name}' if display_name else ''}",
        "",
    ]
    if not items:
        lines.extend(
            [
                f"> [!success] No supported current review items as of {generated_on}",
                "> The current-state adapters found no person-scoped items requiring review.",
                "> Inbox or quarantine files added since then are not counted;",
                "> run `fm-review-pending` to refresh this report.",
                "",
                "_No pending review items._",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "> [!warning] Human Resolver review required",
            f"> {len(items)} current item(s) require an explicit review action.",
            "",
            "| subject | reason | source | resolving action |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in items:
        subject = _table_cell(item.subject)
        if item.detail_report:
            subject = f"[[{item.detail_report}|{subject}]]"
        sources = "<br>".join(_table_cell(source) for source in item.source_paths)
        action = f"`{item.resolution_skill}` — {_table_cell(item.resolution_action)}"
        lines.append(
            "| "
            + " | ".join(
                (
                    subject,
                    _table_cell(item.reason),
                    sources,
                    action,
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_pending_review_report(
    conn: sqlite3.Connection,
    *,
    root: Path,
    vault_root: Path,
    person_id: str,
    snapshot: PendingReviewSnapshot | None = None,
) -> tuple[Path, int]:
    """Regenerate one person's current-review report and Reports navigation."""
    if person_id not in list_person_ids(vault_root):
        raise ValueError(f"pending review person is not configured in the vault: {person_id}")
    snapshot = snapshot or derive_pending_review_snapshot(
        conn,
        root=root,
        vault_root=vault_root,
    )
    items = snapshot.items_for_person(person_id)
    reports_dir = vault_root / "Records" / person_id / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "pending-review.md"
    display_name = load_person_display_name(vault_root, person_id)
    report_path.write_text(
        render_pending_review_report(person_id, items, display_name=display_name),
        encoding="utf-8",
    )
    write_category_index_note(
        reports_dir,
        person_id,
        "Reports",
        note_label="report",
        display_name=display_name,
    )
    return report_path, len(items)


def refresh_pending_review_report_if_configured(
    conn: sqlite3.Connection,
    *,
    root: Path,
    vault_root: Path,
    person_id: str | None,
) -> Path | None:
    """Refresh one report when the source has a configured person."""
    if person_id is None or not (vault_root / "Persons" / f"{person_id}.md").is_file():
        return None
    path, _count = write_pending_review_report(
        conn,
        root=root,
        vault_root=vault_root,
        person_id=person_id,
    )
    return path


def refresh_pending_review_reports(
    conn: sqlite3.Connection,
    *,
    root: Path,
    vault_root: Path,
) -> PendingReviewSnapshot:
    """Regenerate every person report from one shared, deterministic snapshot."""
    snapshot = derive_pending_review_snapshot(conn, root=root, vault_root=vault_root)
    for person_id in list_person_ids(vault_root):
        write_pending_review_report(
            conn,
            root=root,
            vault_root=vault_root,
            person_id=person_id,
            snapshot=snapshot,
        )
    return snapshot
