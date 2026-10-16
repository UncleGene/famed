"""Allergy status from accepted History facts, for medication-facing reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_NO_KNOWN_PREFIXES = ("no known", "nkda", "nka", "no allergies")


def _accepted_allergy_statements(vault_root: Path, person_id: str) -> list[tuple[str, str, str]]:
    """Return (date, description, note link target) for accepted allergy facts."""
    history_dir = vault_root / "Records" / person_id / "History"
    statements: list[tuple[str, str, str]] = []
    if not history_dir.is_dir():
        return statements
    for note_path in sorted(history_dir.glob("*.md")):
        text = note_path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        try:
            front_matter: dict[str, Any] = yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError:
            continue
        facts = front_matter.get("accepted_facts") or []
        for fact in facts:
            if str(fact.get("category") or "").casefold() != "allergy":
                continue
            description = " ".join(str(fact.get("description") or "").split())
            if not description:
                continue
            date = str(fact.get("recorded_date") or front_matter.get("event_date") or "")
            target = f"Records/{person_id}/History/{note_path.stem}"
            statements.append((date, description, target))
    return statements


def render_allergy_section(vault_root: Path, person_id: str) -> list[str]:
    """Render the allergy status block.

    A documented allergy is never hidden by a later "no known allergies"
    statement; that disagreement is shown for the clinician to confirm.
    """
    statements = _accepted_allergy_statements(vault_root, person_id)
    documented = [s for s in statements if not s[1].casefold().startswith(_NO_KNOWN_PREFIXES)]
    no_known = sorted(s for s in statements if s[1].casefold().startswith(_NO_KNOWN_PREFIXES))
    lines = ["## Allergies", ""]
    if not statements:
        lines.extend(["- No allergy information recorded in the vault.", ""])
        return lines
    if not documented:
        latest = no_known[-1]
        lines.append(
            f"- No known allergies documented (most recent record {latest[0] or 'undated'}, "
            f"[[{latest[2]}|source]])."
        )
        lines.append("")
        return lines
    latest_by_description: dict[str, tuple[str, str, str]] = {}
    for statement in sorted(documented):
        latest_by_description[statement[1].casefold()] = statement
    for date, description, target in sorted(latest_by_description.values()):
        lines.append(f"- **{description}** — recorded {date or 'undated'} ([[{target}|source]])")
    latest_documented = max(s[0] for s in documented)
    later_no_known = [s for s in no_known if s[0] > latest_documented]
    if later_no_known:
        latest = later_no_known[-1]
        lines.extend(
            [
                "",
                f"> [!warning] A later record ({latest[0]}) states no known allergies;"
                " confirm allergy status with the clinician.",
            ]
        )
    lines.append("")
    return lines
