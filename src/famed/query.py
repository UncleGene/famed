"""Vault context assembly and optional engine-backed question answering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

_EXPECTED_REPORTS: frozenset[str] = frozenset(
    {
        "lab-results.md",
        "current-medications.md",
        "medical-history-summary.md",
    }
)

_CATEGORY_ORDER = ("Reports", "Labs", "Medications", "History")


class QueryEngine(Protocol):
    def answer(self, context: str, question: str) -> str: ...


_CATEGORY_LABELS: dict[str, str] = {
    "Reports": "Reports (authoritative summaries)",
    "Labs": "Lab Panel Notes",
    "Medications": "Medication Notes",
    "History": "History Notes",
}


@dataclass
class VaultNote:
    path: Path
    relative_name: str
    content: str
    verified: bool


def _parse_front_matter(content: str) -> dict[str, Any]:
    match = _FRONT_MATTER_RE.match(content)
    if not match:
        return {}
    try:
        return dict(yaml.safe_load(match.group(1)) or {})
    except Exception:
        return {}


def _read_category_notes(category_dir: Path, category: str) -> list[VaultNote]:
    if not category_dir.is_dir():
        return []
    notes: list[VaultNote] = []
    for path in sorted(category_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_front_matter(content)
        verified = fm.get("verification_status") != "needs_review"
        notes.append(
            VaultNote(
                path=path,
                relative_name=f"{category}/{path.name}",
                content=content,
                verified=verified,
            )
        )
    return notes


def collect_person_vault_notes(vault_root: Path, person_id: str) -> list[VaultNote]:
    """Return all vault notes for *person_id* in display order.

    Order: Reports first, then Labs, Medications, History.
    Skips ``_index.md``, ``_template.md``, and other underscore-prefixed files.
    """
    records_dir = vault_root / "Records" / person_id
    notes: list[VaultNote] = []
    for category in _CATEGORY_ORDER:
        notes.extend(_read_category_notes(records_dir / category, category))
    return notes


def list_person_ids(vault_root: Path) -> list[str]:
    """Return sorted person IDs from the vault Persons directory."""
    persons_dir = vault_root / "Persons"
    if not persons_dir.is_dir():
        return []
    return sorted(p.stem for p in persons_dir.glob("*.md") if not p.name.startswith("_"))


def _missing_reports(notes: list[VaultNote]) -> list[str]:
    present = {Path(n.relative_name).name for n in notes if n.relative_name.startswith("Reports/")}
    return sorted(_EXPECTED_REPORTS - present)


def format_query_context(notes: list[VaultNote], person_id: str) -> str:
    """Render vault notes as a structured prompt context block."""
    missing = _missing_reports(notes)
    parts: list[str] = [f"=== VAULT NOTES FOR {person_id} ==="]
    if missing:
        parts.append(f"\nMissing expected reports (not yet generated): {', '.join(missing)}")

    current_category = ""
    for note in notes:
        category = note.relative_name.split("/")[0]
        if category != current_category:
            current_category = category
            label = _CATEGORY_LABELS.get(category, category)
            parts.append(f"\n--- {label} ---")
        header = f"\n## [{note.relative_name}]"
        if not note.verified:
            header += "\n[UNVERIFIED — not yet reviewed by caregiver]"
        parts.append(header)
        parts.append(note.content)

    return "\n".join(parts)


def answer_vault_question(
    vault_root: Path,
    person_id: str,
    question: str,
    engine: QueryEngine,
) -> str:
    """Assemble vault context for *person_id* and return the engine's answer."""
    notes = collect_person_vault_notes(vault_root, person_id)
    context = format_query_context(notes, person_id)
    return engine.answer(context, question)
