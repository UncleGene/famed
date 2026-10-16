from __future__ import annotations

import sqlite3
from pathlib import Path

from famed.lab_panel_note_report import CATEGORY_ORDER, collect_report, render_report
from famed.lab_sync import LAB_PANEL_TABLE_HEADER, LabPanelNoteCoverage
from famed.setup import SCHEMA_SQL


def _make_panel_note(
    tmp_path: Path,
    person_id: str,
    observed_at: str,
    rows: list[tuple[str, str, str, str]],
    *,
    source_path: str,
    filename: str,
) -> Path:
    """Write a minimal lab panel note and return its path.

    *rows* is a list of ``(test_name, loinc_code, value, unit)`` tuples.
    """
    labs_dir = tmp_path / "Records" / person_id / "Labs"
    labs_dir.mkdir(parents=True, exist_ok=True)
    note_path = labs_dir / filename

    table_lines = [LAB_PANEL_TABLE_HEADER, "| --- | --- | --- | --- | --- | --- | --- |"]
    for test_name, loinc_code, value, unit in rows:
        table_lines.append(
            f"| {test_name} | {loinc_code} | {value} | {unit} |  |  | {observed_at} |"
        )

    note_path.write_text(
        f"---\nperson_id: {person_id}\nrecord_type: lab_panel\nobserved_at: {observed_at}\n"
        f"source_type: pdf\nsource_path: {source_path}\n---\n\n"
        f"# Lab Panel — {observed_at[:10]}\n\n"
        "[[_index|← Labs]]\n\n## Results\n\n" + "\n".join(table_lines) + "\n\n## Notes\n\n"
        "- Edit table values directly if corrections are needed before syncing.\n",
        encoding="utf-8",
    )
    return note_path


def _insert_current_row(conn: sqlite3.Connection, person_id: str, source_path: str) -> None:
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES (?, ?, '200-2', 'source_provided', 'White blood cell count', 5.0, 'K/uL',
                  NULL, NULL, '2022-01-15', 'source_import', ?)
        """,
        (f"row-{source_path}", person_id, source_path),
    )


def test_collect_report_groups_by_category_across_people(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_current_row(conn, "dummy_alice", "new.xml")

    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "200-2", "5.0", "K/uL")],
        source_path="new.xml",
        filename="2022-01-15-new-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_bob",
        "2022-02-01",
        [("Hemoglobin", "101-1", "14.0", "g/dL")],
        source_path="bob-orphan.pdf",
        filename="2022-02-01-bob-orphan-panel.md",
    )

    by_category = collect_report(conn, tmp_path, ["dummy_alice", "dummy_bob"])

    assert {entry.person_id for entry in by_category["single_replacement"]} == {"dummy_alice"}
    assert {entry.person_id for entry in by_category["no_replacement"]} == {"dummy_bob"}
    assert by_category["multiple_replacements"] == []
    assert by_category["partial"] == []


def test_render_report_shows_categories_paths_and_uncovered_facts(tmp_path: Path) -> None:
    note_path = tmp_path / "Records" / "dummy_alice" / "Labs" / "2022-01-15-old-panel.md"
    covering_path = tmp_path / "Records" / "dummy_alice" / "Labs" / "2022-01-15-new-panel.md"
    entry = LabPanelNoteCoverage(
        note_path=note_path,
        person_id="dummy_alice",
        source_path="old.pdf",
        date_str="2022-01-15",
        total_facts=2,
        covered_facts=1,
        covering_notes=(covering_path,),
        uncovered_test_names=("Hemoglobin",),
    )
    by_category = {name: [] for name in CATEGORY_ORDER}
    by_category["partial"] = [entry]

    report = render_report(tmp_path, by_category)

    assert "1 stale-source note(s)" in report
    assert "Partially covered (likely a merged note) (1)" in report
    assert "Records/dummy_alice/Labs/2022-01-15-old-panel.md" in report
    assert "1/2 facts covered" in report
    assert "covering notes: Records/dummy_alice/Labs/2022-01-15-new-panel.md" in report
    assert "uncovered: Hemoglobin" in report
