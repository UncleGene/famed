"""ADR-041: a prescription authorization window is not a therapy end.

These are regression tests for a live defect. An expired refill window caused the
*current* prescription to be dropped from `current-medications.md` and a four-year-old
row at double the dose to be rendered in its place.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from famed.history_sync import (
    _ccda_date,
    _ccda_document_local_offsets,
    _ccda_medication_mapping,
)
from famed.medication_sync import (
    _annotate_source_list_currency,
    _collapse_dose_changes_over_time,
    _render_authorized_through_cell,
    write_current_medications_report,
)
from famed.setup import initialize_database

_HL7 = "urn:hl7-org:v3"


def _administration(mood: str, *, low: str, high: str | None) -> tuple[ET.Element, ET.Element]:
    high_xml = f'<high value="{high}" />' if high else ""
    section = ET.fromstring(
        f"""
        <section xmlns="{_HL7}">
          <text><list><item ID="sig1">Take 1 tablet by mouth once daily</item></list></text>
          <entry>
            <substanceAdministration classCode="SBADM" moodCode="{mood}">
              <text><reference value="#sig1" /></text>
              <statusCode code="active" />
              <effectiveTime><low value="{low}" />{high_xml}</effectiveTime>
              <consumable><manufacturedProduct><manufacturedMaterial>
                <code code="000000" codeSystem="2.16.840.1.113883.6.88"
                      displayName="dummy-drug 10 MG tablet" />
              </manufacturedMaterial></manufacturedProduct></consumable>
            </substanceAdministration>
          </entry>
        </section>
        """
    )
    administration = section.find(f".//{{{_HL7}}}substanceAdministration")
    assert administration is not None
    return administration, section


class TestIngestionMoodCode:
    def test_int_mood_high_is_an_authorization_window_not_a_therapy_end(self) -> None:
        """A prescription intent's high boundary is when refills lapse, not when
        the patient stopped — the same entry still says statusCode=active."""
        administration, section = _administration("INT", low="20220627", high="20230628")

        mapping = _ccda_medication_mapping(administration, section, person_id="dummy_alice")

        assert mapping is not None
        assert mapping["end_date"] is None
        assert mapping["authorized_through"] == "2023-06-28"
        assert mapping["status"] == "active"

    def test_evn_mood_high_still_ends_the_therapy(self) -> None:
        """An administration event genuinely records when a course finished."""
        administration, section = _administration("EVN", low="20220627", high="20230628")

        mapping = _ccda_medication_mapping(administration, section, person_id="dummy_alice")

        assert mapping is not None
        assert mapping["end_date"] == "2023-06-28"
        assert mapping["authorized_through"] is None

    def test_int_mood_without_a_high_boundary_sets_neither_date(self) -> None:
        administration, section = _administration("INT", low="20220627", high=None)

        mapping = _ccda_medication_mapping(administration, section, person_id="dummy_alice")

        assert mapping is not None
        assert mapping["end_date"] is None
        assert mapping["authorized_through"] is None


def _contradicting_rows(*, undated_source: str) -> list[dict[str, object]]:
    """Two documents contradicting each other on one drug, plus a second drug that
    dates each document's list. `undated_source` chooses which side lacks a start date.
    """
    rows: list[dict[str, object]] = [
        {
            "medication_name": "dummy-drug 10 MG capsule",
            "medication_type": "prescription",
            "dose": "10 mg",
            "frequency": "Take 10 mg by mouth once daily",
            "start_date": "2022-06-27",
            "end_date": None,
            "source_path": "current-list.xml",
        },
        {
            "medication_name": "dummy-drug 10 mg capsule",
            "medication_type": "prescription",
            "dose": "10 mg",
            "frequency": "Take 1 capsule (10 mg) by mouth 2 times daily",
            "start_date": "2016-04-17",
            "end_date": None,
            "source_path": "legacy-export.xml",
        },
        # A second drug on the same two documents: this is what dates each list.
        {
            "medication_name": "dummy-other 5 MG tablet",
            "medication_type": "prescription",
            "dose": "5 mg",
            "frequency": "once daily",
            "start_date": "2022-06-27",
            "end_date": None,
            "source_path": "current-list.xml",
        },
        {
            "medication_name": "dummy-other 5 mg tablet",
            "medication_type": "prescription",
            "dose": "5 mg",
            "frequency": "once daily",
            "start_date": "2016-12-18",
            "end_date": None,
            "source_path": "legacy-export.xml",
        },
    ]
    for row in rows:
        if row["source_path"] == undated_source and "dummy-drug" in str(row["medication_name"]):
            row["start_date"] = None
    return rows


class TestTimezoneResolution:
    """A UTC-stamped instant must not be truncated to the wrong calendar date."""

    def test_end_of_local_day_in_utc_resolves_to_the_local_date(self) -> None:
        # 06:59Z is 23:59 the previous day at -0700, and 22:59 at -0800 — both 06-27.
        assert _ccda_date("20230628065900+0000", local_offsets=("-0700", "-0800")) == "2023-06-27"

    def test_a_local_offset_timestamp_is_already_local_and_is_not_shifted(self) -> None:
        """Digits alongside a non-UTC offset are local wall-clock time already."""
        assert _ccda_date("20230530104625-0700", local_offsets=("-0700", "-0800")) == "2023-05-30"

    def test_a_date_only_value_is_untouched(self) -> None:
        assert _ccda_date("20220627", local_offsets=("-0700",)) == "2022-06-27"

    def test_without_document_offsets_the_utc_date_is_kept(self) -> None:
        assert _ccda_date("20230628065900+0000") == "2023-06-28"

    def test_offsets_that_disagree_on_the_date_fail_closed(self) -> None:
        """A midday instant lands on the same date everywhere; an ambiguous one must not
        be guessed. 03:30Z is 2023-06-27 at -0700 but 2023-06-28 at +0530, so the
        conversion declines rather than picking one."""
        assert _ccda_date("20230628033000+0000", local_offsets=("-0700", "+0530")) == "2023-06-28"

    def test_offsets_are_read_from_the_document(self) -> None:
        root = ET.fromstring(
            f"""
            <ClinicalDocument xmlns="{_HL7}">
              <effectiveTime value="20230530104625-0700" />
              <a value="20230115080000-0800" />
              <b value="20230628065900+0000" />
              <c value="20220627" />
            </ClinicalDocument>
            """
        )
        assert _ccda_document_local_offsets(root) == ("-0700", "-0800")


class TestCollapseRanking:
    @pytest.mark.parametrize("undated_source", ["current-list.xml", "legacy-export.xml"])
    def test_current_list_wins_regardless_of_which_side_lacks_a_start_date(
        self, undated_source: str
    ) -> None:
        """A missing start_date used to decide the winner.

        The old rule sorted a NULL start_date as "9999-99-99", so the undated row won
        outright. That produced the right answer only when the *current* document was
        the undated one; when the stale document was undated it promoted a six-year-old
        regimen. Absence of a date is not evidence of recency — rank on how current the
        stating document's list is.
        """
        rows = _contradicting_rows(undated_source=undated_source)
        _annotate_source_list_currency(rows)

        collapsed = _collapse_dose_changes_over_time(rows)

        winner = next(r for r in collapsed if "dummy-drug" in str(r["medication_name"]))
        assert winner["source_path"] == "current-list.xml"
        assert "once daily" in str(winner["frequency"])

    def test_an_undated_row_on_an_undated_list_is_still_treated_as_current(self) -> None:
        """Guards a regression caught during ADR-041 implementation.

        Hand-entered lists often record no start dates at all. Ranking such a row by its
        document's currency scores it empty and drops it below any dated row — which
        silently discarded the only rows carrying real dose and frequency text. With no
        dating evidence anywhere, keep treating the row as current.
        """
        rows = [
            {
                "medication_name": "dummy-supplement",
                "medication_type": "supplement",
                "dose": "1000 mcg",
                "frequency": "once daily",
                "start_date": None,
                "end_date": None,
                "source_path": "hand-entered-list.md",
            },
            {
                "medication_name": "dummy-supplement",
                "medication_type": "supplement",
                "dose": "1000 mcg",
                "frequency": "",
                "start_date": "2017-01-01",
                "end_date": None,
                "source_path": "old-export.xml",
            },
        ]
        _annotate_source_list_currency(rows)

        collapsed = _collapse_dose_changes_over_time(rows)

        winner = next(r for r in collapsed if r["medication_name"] == "dummy-supplement")
        assert winner["frequency"] == "once daily"


class TestReportRendering:
    def test_lapsed_authorization_keeps_the_current_dose_and_flags_a_renewal(
        self, tmp_path: Path
    ) -> None:
        """The exact live failure: an expired refill window must not delete the
        current prescription in favour of an older, higher-dose row."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        db_path = tmp_path / "famed.db"
        initialize_database(db_path)
        lapsed = (datetime.now(UTC).date() - timedelta(days=33)).isoformat()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO persons (id, display_name) VALUES ('dummy_alice', 'Dummy Alice')"
            )
            conn.executemany(
                """
                INSERT INTO medications (
                    id, person_id, medication_name, medication_type, dose, frequency,
                    start_date, end_date, authorized_through, status, source_type, source_path
                ) VALUES (?, 'dummy_alice', ?, 'prescription', ?, 'once daily',
                          ?, NULL, ?, 'active', 'zip_xml', ?)
                """,
                [
                    (
                        "current",
                        "dummy-drug 20 MG tablet",
                        "20 mg",
                        "2022-06-27",
                        lapsed,
                        "current-list.xml",
                    ),
                    (
                        "legacy",
                        "dummy-drug 40 mg tablet",
                        "40 mg",
                        "2018-08-23",
                        None,
                        "legacy-export.xml",
                    ),
                ],
            )
            conn.commit()
            report_path = write_current_medications_report(vault_root, "dummy_alice", conn)

        assert report_path is not None
        content = report_path.read_text(encoding="utf-8")
        assert "| 20 mg |" in content, "the current dose must survive a lapsed refill window"
        assert "| 40 mg |" not in content, "the stale higher dose must not be promoted"
        assert "renewal due" in content

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            ("", ""),
            ("2999-01-01", "2999-01-01"),
        ],
    )
    def test_authorization_cell_renders_plainly_when_not_lapsed(
        self, value: str | None, expected: str
    ) -> None:
        assert _render_authorized_through_cell(value) == expected

    def test_authorization_cell_flags_a_past_date(self) -> None:
        past = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        assert _render_authorized_through_cell(past) == f"{past} — renewal due"
