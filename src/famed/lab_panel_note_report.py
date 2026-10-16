"""Read-only lab_panel note coverage report.

Diagnoses the backlog of lab_panel notes whose source has no current
lab_results DB rows, categorizing each by how much of its content is covered
by current data elsewhere. Never writes anything; the goal is to make manual
triage of that backlog tractable, since automatic
reconciliation (``reconcile_superseded_lab_panel_notes``) only handles the
narrow single-replacement case safely.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from famed.lab_sync import LabPanelNoteCoverage, assess_lab_panel_note_coverage
from famed.query import list_person_ids

CATEGORY_ORDER = (
    "single_replacement",
    "multiple_replacements",
    "covered_no_note",
    "partial",
    "no_replacement",
)
CATEGORY_LABELS = {
    "single_replacement": "Fully covered by one note (auto-reconcile should have handled this)",
    "multiple_replacements": "Fully covered, but scattered across multiple notes",
    "covered_no_note": "Fully covered by current data, but no note represents it",
    "partial": "Partially covered (likely a merged note)",
    "no_replacement": "Not covered anywhere (quarantine/deletion candidate)",
}


def collect_report(
    conn: sqlite3.Connection, vault_root: Path, person_ids: list[str]
) -> dict[str, list[LabPanelNoteCoverage]]:
    by_category: dict[str, list[LabPanelNoteCoverage]] = {name: [] for name in CATEGORY_ORDER}
    for person_id in person_ids:
        for entry in assess_lab_panel_note_coverage(conn, vault_root, person_id):
            by_category[entry.category].append(entry)
    return by_category


def render_report(vault_root: Path, by_category: dict[str, list[LabPanelNoteCoverage]]) -> str:
    lines: list[str] = []
    total = sum(len(entries) for entries in by_category.values())
    lines.append(f"Lab panel note coverage report: {total} stale-source note(s)\n")
    for category in CATEGORY_ORDER:
        entries = by_category[category]
        lines.append(f"## {CATEGORY_LABELS[category]} ({len(entries)})")
        for entry in entries:
            rel_note = entry.note_path.relative_to(vault_root)
            covering = ", ".join(str(p.relative_to(vault_root)) for p in entry.covering_notes)
            lines.append(
                f"- {rel_note} [{entry.person_id}, {entry.date_str}] "
                f"{entry.covered_facts}/{entry.total_facts} facts covered"
            )
            if covering:
                lines.append(f"    covering notes: {covering}")
            if entry.uncovered_test_names:
                lines.append(f"    uncovered: {', '.join(entry.uncovered_test_names)}")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report stale-source lab_panel notes for human triage."
    )
    parser.add_argument("--root", default=".", help="Repo root (default: current directory).")
    parser.add_argument("--person-id", help="Limit the report to one person.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    person_ids = [args.person_id] if args.person_id else list_person_ids(vault_root)
    by_category = collect_report(conn, vault_root, person_ids)
    print(render_report(vault_root, by_category))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
