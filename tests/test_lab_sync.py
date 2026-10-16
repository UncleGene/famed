from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from famed import import_labs
from famed.import_labs import (
    clean_pdf_range_bar_artifacts,
    collect_loinc_misses,
    deterministic_panel_note_path,
    fill_loinc_codes,
    infer_person_id_from_inbox_path,
    infer_person_id_from_pdf_filename,
    infer_person_id_from_processed_lab_path,
    parse_labs_from_pdf_text,
    parse_labs_from_poct_general_text,
    parse_labs_from_poct_text,
    processed_lab_source_path,
    quarantine_lab_source_for_import,
    quarantined_lab_source_path,
    retain_processed_source,
    source_stem_without_redundant_date_prefix,
)
from famed.lab_sync import (
    _CBC_DIFFERENTIAL_COUNT_LOINC_CODES,
    _CBC_DIFFERENTIAL_MISMATCH_LOINC_CODES,
    CANONICAL_UNIT_BY_LOINC,
    LAB_CONFLICTS_TABLE_HEADER,
    LAB_PANEL_TABLE_HEADER,
    LOINC_CODE_ORIGIN_INFERRED,
    LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
    LabSyncError,
    SourceLoincOverride,
    _extract_coronary_calcium_findings,
    _format_reference_range,
    _is_approaching_reference_threshold,
    _is_out_of_range_result_stale,
    _is_stale_result,
    _lab_flag_label,
    _method_variant_note,
    _most_recent_panel_date,
    _render_lab_sparkline,
    _sparkline_axis,
    _two_most_recent_panel_dates,
    apply_reviewed_source_provided_loinc_overrides,
    assess_lab_panel_note_coverage,
    backfill_lab_units_in_db,
    backfill_reviewed_inferred_loinc_codes,
    build_lab_conflicts_from_db,
    build_lab_trends_from_db,
    build_latest_lab_results_from_notes,
    classify_loinc_code_origin,
    collapse_archive_restated_lab_rows,
    fill_unit,
    find_overlapping_panel_note,
    find_superseded_lab_panel_notes,
    interpretation_for_reading,
    is_non_result_lab_row,
    lab_payload_from_mapping,
    lab_payloads_from_mapping,
    load_labs_from_csv,
    load_labs_from_note,
    load_reviewed_source_loinc_overrides,
    log_ingestion_event,
    mark_lab_panel_note_superseded,
    merge_lab_panel_rows,
    normalize_test_name,
    normalize_unit,
    purge_non_result_lab_rows_in_db,
    reconcile_superseded_lab_panel_notes,
    render_flagged_for_discussion_report,
    render_lab_conflicts_report,
    render_lab_note,
    render_lab_panel_note,
    renormalize_lab_panel_note,
    repair_inferred_lab_loinc_codes_in_notes,
    repair_lab_loinc_codes,
    repair_lab_units,
    repair_reviewed_inferred_loinc_codes_in_notes,
    replace_lab_results_for_source_path,
    reviewed_unit_equivalence,
    source_file_checksum,
    source_interpretation_code,
    suspected_truncated_value,
    upsert_lab_result,
    write_category_index_note,
    write_flagged_for_discussion_report,
    write_lab_conflicts_report,
    write_lab_panel_note,
    write_labs_index_note,
)
from famed.person_sync import parse_front_matter
from famed.setup import SCHEMA_SQL, initialize_database
from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL


def test_load_labs_from_note_parses_single_test_front_matter(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-20-hemoglobin-a1c.md"
    note_path.write_text(
        """---
person_id: dummy_alice
record_type: lab_result
observed_at: 2023-04-20
loinc_code: 4548-4
test_name: Hemoglobin A1c
value: 5.8
unit: "%"
reference_range_low: 4.0
reference_range_high: 5.6
source_type: manual
---

# Hemoglobin A1c
""",
        encoding="utf-8",
    )

    payloads = load_labs_from_note(note_path)

    assert len(payloads) == 1
    payload = payloads[0]

    assert payload["person_id"] == "dummy_alice"
    assert payload["test_name"] == "Hemoglobin A1c"
    assert payload["value"] == 5.8
    assert payload["reference_range_high"] == 5.6


def test_load_labs_from_note_parses_multiple_tests(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-20-lab-panel.md"
    note_path.write_text(
        """---
person_id: dummy_alice
record_type: lab_panel
observed_at: 2023-04-20
source_type: manual
tests:
  - test_name: Hemoglobin A1c
    loinc_code: 4548-4
    value: 5.8
    unit: "%"
    reference_range_low: 4.0
    reference_range_high: 5.6
  - test_name: Fasting Glucose
    loinc_code: 1558-6
    value: 102
    unit: mg/dL
    reference_range_low: 70
    reference_range_high: 99
    observed_at: 2023-04-21
---

# Lab Panel
""",
        encoding="utf-8",
    )

    payloads = load_labs_from_note(note_path)

    assert len(payloads) == 2
    assert payloads[0]["test_name"] == "Hemoglobin A1c"
    assert payloads[0]["observed_at"] == "2023-04-20"
    assert payloads[1]["test_name"] == "Fasting Glucose"
    assert payloads[1]["observed_at"] == "2023-04-21"


def test_load_labs_from_note_parses_markdown_table_rows(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-20-lab-panel-table.md"
    note_path.write_text(
        """---
person_id: dummy_alice
record_type: lab_panel
observed_at: 2023-04-20
source_type: pdf:quest
---

# Lab Panel

## Results

| test_name | loinc_code | value | unit | reference_range_low | reference_range_high | observed_at |
| --- | --- | --- | --- | --- | --- | --- |
| Hemoglobin A1c | 4548-4 | 5.8 | % | 4.0 | 5.6 | 2023-04-20 |
| Fasting Glucose | 1558-6 | 102 | mg/dL | 70 | 99 | 2023-04-21 |
""",
        encoding="utf-8",
    )

    payloads = load_labs_from_note(note_path)

    assert len(payloads) == 2
    assert payloads[0]["source_type"] == "pdf:quest"
    assert payloads[0]["test_name"] == "Hemoglobin A1c"
    assert payloads[1]["test_name"] == "Fasting Glucose"
    assert payloads[1]["observed_at"] == "2023-04-21"


def test_load_labs_from_note_preserves_panel_source_path(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-20-lab-panel-table.md"
    note_path.write_text(
        """---
person_id: dummy_alice
record_type: lab_panel
observed_at: 2023-04-20
source_type: pdf:quest
source_path: data/processed/dummy_alice/Labs/2023-04-20-quest-report.pdf
---

# Lab Panel

## Results

| test_name | loinc_code | value | unit | reference_range_low | reference_range_high | observed_at |
| --- | --- | --- | --- | --- | --- | --- |
| Hemoglobin A1c | 4548-4 | 5.8 | % | 4.0 | 5.6 | 2023-04-20 |
| Fasting Glucose | 1558-6 | 102 | mg/dL | 70 | 99 | 2023-04-21 |
""",
        encoding="utf-8",
    )

    payloads = load_labs_from_note(note_path)

    assert {payload["source_path"] for payload in payloads} == {
        "data/processed/dummy_alice/Labs/2023-04-20-quest-report.pdf"
    }


def test_lab_payload_requires_test_name() -> None:
    try:
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "observed_at": "2023-04-20",
            },
            "test",
        )
    except LabSyncError as exc:
        assert "test_name is required" in str(exc)
    else:
        raise AssertionError("Expected LabSyncError for missing test_name")


def test_lab_payload_normalizes_canonical_test_names() -> None:
    cases = {
        "WHITE BLOOD CELL COUNT": "White blood cell count",
        "White Blood Cells": "White blood cell count",
        "RED BLOOD CELL COUNT": "Red blood cell count",
        "PLATELET COUNT": "Platelet count",
        "Platelets": "Platelet count",
        "AL T": "ALT",
        "VITAMIN D,25-OH,TOTAL,IA": "Vitamin D 25-hydroxy",
        "C-REACTIVE PROTEIN": "C reactive protein",
        "DHEA SULFATE": "Dehydroepiandrosterone sulfate",
        "GGT": "Gamma glutamyl transferase",
        "TESTOSTERONE, TOTAL, MALES (ADULT), IA": "Testosterone, total",
        "Triglycerides - LC": "Triglycerides",
        "C-Reactive Protein, Cardiac - LC": "C-Reactive Protein, Cardiac",
        "LDL-C (NIH Calc) - LC": "LDL Calculated",
        "LDL Chol Calc (NIH)": "LDL Calculated",
        "LDL, Calculated": "LDL Calculated",
        "CREATININE": "Creatinine",
        "BILIRUBIN,TOTAL": "Bilirubin, total",
        "Protein Total": "Protein, total",
        "Cholesterol, Total": "Cholesterol",
        "Eosinophils Absolute": "Eosinophils, absolute",
        "Baso (Absolute)": "Basophils, absolute",
        "Eos (Absolute)": "Eosinophils, absolute",
        "Lymphs (Absolute)": "Lymphocytes, absolute",
        "Monocytes(Absolute)": "Monocytes, absolute",
        "Neutrophils (Absolute)": "Neutrophils, absolute",
        "Carbon Dioxide, Total": "Carbon dioxide",
        "MONOCYTES, ABSOLUTE": "Monocytes, absolute",
        "Absolute Neutrophils": "Neutrophils, absolute",
        "PSA": "Prostate Specific Antigen",
    }

    for raw_name, canonical_name in cases.items():
        payload = lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": raw_name,
                "observed_at": "2023-04-20",
            },
            f"case:{raw_name}",
        )
        assert payload["test_name"] == canonical_name


def test_normalize_test_name_uses_loinc_specific_aliases() -> None:
    assert normalize_test_name("HDL", "2085-9") == "HDL Cholesterol"
    assert normalize_test_name("HDL", "111273-9") == "HDL Cholesterol"


@pytest.mark.parametrize(
    ("raw_name", "unit", "expected"),
    [
        ("Basos", "%", "% Basophils"),
        ("Eos", "%", "% Eosinophils"),
        ("Immature Granulocytes", "%", "% Immature Granulocytes"),
        ("Immature Granulocytes % (IG%)", "%", "% Immature Granulocytes"),
        (
            "Absolute Immature Granulocytes",
            "K/uL",
            "Immature Granulocytes, Absolute",
        ),
        ("Immature Granulocytes", "K/uL", "Immature Granulocytes, Absolute"),
        ("Immature Granulocytes Count", "K/uL", "Immature Granulocytes, Absolute"),
        ("Lymphs", "%", "% Lymphocytes"),
        ("Monocytes", "%", "% Monocytes"),
        ("Neutrophils", "%", "% Neutrophils"),
        ("Immature Grans (Abs)", "K/uL", "Immature Granulocytes, Absolute"),
        ("NRBC Auto", "/100 WBC", "% nRBC"),
        ("NRBC Auto", "%", "% nRBC"),
        ("NRBC Auto", "per 100 WBC(s)", "% nRBC"),
        ("nRBC(auto)%", "/100WBC", "% nRBC"),
        ("nRBC per 100 Cells", "%", "% nRBC"),
        ("nRBC#", "K/uL", "nRBC, Absolute"),
        ("NRBC% Auto", "K/uL", "nRBC, Absolute"),
        ("RDW", "%", "RDW-CV"),
        ("RDW-SD", "fL", "RDW-SD"),
    ],
)
def test_normalize_test_name_uses_unit_class_for_labcorp_cbc_aliases(
    raw_name: str,
    unit: str,
    expected: str,
) -> None:
    assert normalize_test_name(raw_name, unit=unit) == expected


def test_build_lab_trends_from_db_collapses_verified_alias_pairs() -> None:
    conn = _trends_conn(
        {
            "test_name": "Platelets",
            "loinc_code": "777-3",
            "value": 250.0,
            "unit": "K/uL",
            "ref_low": 150.0,
            "ref_high": 450.0,
            "observed_at": "2021-01-01",
            "source_path": "source-a",
            "source_type": "source_import",
        },
        {
            "test_name": "Platelet count",
            "loinc_code": "777-3",
            "value": 260.0,
            "unit": "K/uL",
            "ref_low": 150.0,
            "ref_high": 450.0,
            "observed_at": "2021-06-01",
            "source_path": "source-b",
            "source_type": "source_import",
        },
        {
            "test_name": "PSA",
            "loinc_code": "2857-1",
            "value": 3.2,
            "unit": "ng/mL",
            "observed_at": "2021-02-01",
            "source_path": "source-c",
            "source_type": "source_import",
        },
        {
            "test_name": "Prostate Specific Antigen",
            "loinc_code": "2857-1",
            "value": 3.9,
            "unit": "ng/mL",
            "observed_at": "2021-08-01",
            "source_path": "source-d",
            "source_type": "source_import",
        },
    )

    rows = build_lab_trends_from_db(conn, "dummy_alice")

    assert [row["test_name"] for row in rows] == [
        "Platelet count",
        "Prostate Specific Antigen",
    ]
    platelet_row = next(row for row in rows if row["test_name"] == "Platelet count")
    assert [reading["value"] for reading in platelet_row["readings"]] == [250.0, 260.0]
    psa_row = next(row for row in rows if row["test_name"] == "Prostate Specific Antigen")
    assert [reading["value"] for reading in psa_row["readings"]] == [3.2, 3.9]


def test_build_lab_trends_collapses_unit_sensitive_cbc_aliases() -> None:
    conn = _trends_conn(
        {
            "test_name": "% Immature Granulocytes",
            "value": 0.2,
            "unit": "%",
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Immature Granulocytes % (IG%)",
            "value": 0.3,
            "unit": "%",
            "observed_at": "2022-01-01",
        },
        {
            "test_name": "Absolute Immature Granulocytes",
            "value": 0.01,
            "unit": "K/uL",
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Immature Granulocytes Count",
            "value": 0.02,
            "unit": "K/uL",
            "observed_at": "2022-01-01",
        },
        {
            "test_name": "NRBC Auto",
            "value": 0.0,
            "unit": "/100 WBC",
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "nRBC per 100 Cells",
            "value": 0.1,
            "unit": "%",
            "observed_at": "2022-01-01",
        },
        {
            "test_name": "NRBC% Auto",
            "value": 0.0,
            "unit": "K/uL",
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "nRBC#",
            "value": 0.01,
            "unit": "K/uL",
            "observed_at": "2022-01-01",
        },
        {
            "test_name": "RDW",
            "value": 12.2,
            "unit": "%",
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "RDW-CV",
            "value": 12.4,
            "unit": "%",
            "observed_at": "2022-01-01",
        },
        {
            "test_name": "RDW-SD",
            "value": 44.0,
            "unit": "fL",
            "observed_at": "2022-01-01",
        },
    )

    rows = build_lab_trends_from_db(conn, "dummy_alice")

    assert {row["test_name"]: len(row["readings"]) for row in rows} == {
        "% Immature Granulocytes": 2,
        "% nRBC": 2,
        "Immature Granulocytes, Absolute": 2,
        "nRBC, Absolute": 2,
        "RDW-CV": 2,
        "RDW-SD": 1,
    }


def test_build_lab_trends_collapses_free_psa_but_keeps_percent_distinct() -> None:
    conn = _trends_conn(
        {
            "test_name": "% Free PSA",
            # Some source rows carry the Free PSA concentration code despite
            # reporting a percentage. The distinct name and unit must win.
            "loinc_code": "10886-0",
            "value": 20.0,
            "unit": "%",
            "observed_at": "2021-01-15",
        },
        {
            "test_name": "PSA, Free",
            "loinc_code": "10886-0",
            "value": 0.4,
            "unit": "ng/mL",
            "ref_high": 1.0,
            "observed_at": "2021-01-15",
        },
        {
            "test_name": "Free PSA",
            "loinc_code": "10886-0",
            "value": 0.6,
            "unit": "ng/mL",
            "ref_high": 1.2,
            "observed_at": "2022-01-15",
        },
    )

    rows = build_lab_trends_from_db(conn, "dummy_alice")

    assert [row["test_name"] for row in rows] == ["% Free PSA", "Free PSA"]
    percent_free_psa = rows[0]
    assert len(percent_free_psa["readings"]) == 1
    assert percent_free_psa["readings"][0]["unit"] == "%"
    free_psa = rows[1]
    assert [reading["date"] for reading in free_psa["readings"]] == [
        "2021-01-15",
        "2022-01-15",
    ]
    assert [reading["unit"] for reading in free_psa["readings"]] == ["ng/mL", "ng/mL"]
    assert [reading["ref_high"] for reading in free_psa["readings"]] == [1.0, 1.2]


def test_build_lab_trends_converges_tsh_narrow_ref_range_into_one_series() -> None:
    """ADR-037: TSH and TSH Narrow Ref Range are the same analyte and converge
    into one series; each reading keeps its own reference range."""
    conn = _trends_conn(
        {
            "test_name": "TSH",
            "loinc_code": "3016-3",
            "value": 2.1,
            "unit": "uIU/mL",
            "ref_low": 0.4,
            "ref_high": 4.5,
            "observed_at": "2021-02-15",
        },
        {
            "test_name": "TSH Narrow Ref Range",
            "value": 2.8,
            "unit": "uIU/mL",
            "ref_low": 0.45,
            "ref_high": 2.5,
            "observed_at": "2022-02-15",
        },
    )

    rows = build_lab_trends_from_db(conn, "dummy_alice")

    assert [row["test_name"] for row in rows] == ["TSH"]
    tsh = rows[0]
    assert tsh["loinc_code"] == "3016-3"
    assert [(reading["ref_low"], reading["ref_high"]) for reading in tsh["readings"]] == [
        (0.4, 4.5),
        (0.45, 2.5),
    ]


def test_is_approaching_reference_threshold_ignores_reference_range_change() -> None:
    """ADR-037: a reference-range-policy switch between the previous
    and latest reading (e.g. standard-range TSH followed by a narrow-range TSH
    reading) is a discontinuity, not a physiological trend, so it must not flag
    as approaching a threshold even though the latest value sits near its own
    (narrower) bound."""
    row = {
        "readings": [
            {"date": "2021-02-15", "value": 2.1, "ref_low": 0.4, "ref_high": 4.5},
            {"date": "2022-02-15", "value": 2.4, "ref_low": 0.45, "ref_high": 2.5},
        ]
    }

    assert _is_approaching_reference_threshold(row) is False


def test_is_approaching_reference_threshold_still_fires_within_same_reference_range() -> None:
    """Same-policy readings (no reference-range change) keep the existing
    approaching-threshold behavior."""
    row = {
        "readings": [
            {"date": "2021-02-15", "value": 2.0, "ref_low": 0.4, "ref_high": 4.5},
            {"date": "2022-02-15", "value": 4.3, "ref_low": 0.4, "ref_high": 4.5},
        ]
    }

    assert _is_approaching_reference_threshold(row) is True


def test_lab_payload_normalizes_units() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Glucose",
            "observed_at": "2023-04-20",
            "value": "102",
            "unit": "mg/dl",
        },
        "unit-case",
    )

    assert payload["unit"] == "mg/dL"


def test_lab_payload_raises_on_unit_mismatch_for_known_test() -> None:
    try:
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-20",
                "value": "5.8",
                "unit": "mg/dL",
            },
            "unit-mismatch",
        )
    except LabSyncError as exc:
        assert "potential unit mismatch" in str(exc)
    else:
        raise AssertionError("Expected LabSyncError for known unit mismatch")


def test_lab_payload_allows_known_unit_variant_for_tsh() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "TSH",
            "observed_at": "2023-04-20",
            "value": "1.2",
            "unit": "mIU/L",
        },
        "tsh-unit",
    )

    assert payload["unit"] == "mIU/L"


def test_lab_payload_normalizes_micro_sign_tsh_unit_to_canonical() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "TSH",
            "observed_at": "2023-04-20",
            "value": "6.39",
            "unit": "µIU/mL",
        },
        "tsh-micro-sign-unit",
    )

    assert payload["unit"] == "uIU/mL"


def test_lab_payload_normalizes_cbc_unit_aliases() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_bob",
            "test_name": "White blood cell count",
            "observed_at": "2023-04-20",
            "value": "5.5",
            "unit": "Thousand/uL",
        },
        "cbc-unit",
    )

    assert payload["unit"] == "K/uL"


def test_fill_unit_fills_reviewed_loinc_when_empty() -> None:
    assert fill_unit("2951-2", "") == "mmol/L"
    assert fill_unit("787-2", "") == "fL"
    assert fill_unit("100716-0", "") == "ng/mL"


def test_fill_unit_never_overrides_source_unit() -> None:
    assert fill_unit("2951-2", "mEq/L") == "mEq/L"


def test_fill_unit_ignores_unreviewed_or_missing_loinc() -> None:
    assert fill_unit("99999-9", "") == ""
    assert fill_unit("", "") == ""


def test_reviewed_unit_map_excludes_contaminated_loincs() -> None:
    # CBC differential count/mismatch LOINCs and the immature-granulocyte
    # count-vs-% LOINC must never be auto-filled by this lane (ADR-035).
    contaminated = (
        _CBC_DIFFERENTIAL_COUNT_LOINC_CODES | _CBC_DIFFERENTIAL_MISMATCH_LOINC_CODES | {"53115-2"}
    )
    assert not (set(CANONICAL_UNIT_BY_LOINC) & contaminated)


def test_lab_payload_fills_unit_for_reviewed_loinc_from_real_value_source() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "loinc_code": "2951-2",
            "test_name": "Sodium",
            "observed_at": "2023-04-20",
            "value": "140",
            "unit": "",
        },
        "ccda-real-sodium",
    )

    assert payload["unit"] == "mmol/L"


def test_lab_payload_leaves_unit_empty_for_unreviewed_loinc() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "loinc_code": "53115-2",
            "test_name": "% Immature Granulocytes",
            "observed_at": "2023-04-20",
            "value": "0.3",
            "unit": "",
        },
        "ambiguous-ig",
    )

    assert payload["unit"] == ""


def test_is_non_result_lab_row_detects_metadata_only_when_uncoded() -> None:
    assert is_non_result_lab_row("Urine Dipstick Lot Number", "") is True
    assert is_non_result_lab_row("POC Stool Occult Intrnl Cntrl Rslts OK", "") is True
    assert is_non_result_lab_row("Blood Expiration Date", "") is True
    assert is_non_result_lab_row("POCT Urinalysis Dipstick Exp Date", "") is True
    assert is_non_result_lab_row("POC Urine Dipstick Performing Location", "") is True
    # A coded result is never dropped on a name-substring coincidence.
    assert is_non_result_lab_row("Lot Number", "2951-2") is False
    assert is_non_result_lab_row("Performing Location", "2951-2") is False
    assert is_non_result_lab_row("Sodium", "2951-2") is False
    assert is_non_result_lab_row("Hemoglobin", "") is False


def _db_lab_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "person_id": "dummy_alice",
        "loinc_code": "",
        "test_name": "Sodium",
        "value": None,
        "unit": "",
        "reference_range_low": None,
        "reference_range_high": None,
        "observed_at": "2023-04-20",
        "source_type": "source_import",
        "source_path": "p#x",
    }
    row.update(overrides)
    return row


def test_backfill_lab_units_in_db_fills_only_reviewed_empties_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    upsert_lab_result(
        conn,
        _db_lab_row(loinc_code="2951-2", test_name="Sodium", value=140.0, source_path="p#a"),
    )
    upsert_lab_result(
        conn,
        _db_lab_row(
            loinc_code="2823-3",
            test_name="Potassium",
            value=4.0,
            unit="mmol/L",
            source_path="p#b",
        ),
    )
    upsert_lab_result(
        conn,
        _db_lab_row(
            loinc_code="53115-2",
            test_name="% Immature Granulocytes",
            value=0.3,
            source_path="p#c",
        ),
    )

    filled = backfill_lab_units_in_db(conn)
    assert filled == 1
    units = dict(conn.execute("SELECT loinc_code, unit FROM lab_results").fetchall())
    assert units["2951-2"] == "mmol/L"  # filled
    assert units["2823-3"] == "mmol/L"  # untouched (already present)
    assert units["53115-2"] == ""  # excluded (ambiguous)

    assert backfill_lab_units_in_db(conn) == 0  # idempotent


def test_purge_non_result_lab_rows_in_db_drops_only_uncoded_metadata() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    upsert_lab_result(
        conn,
        _db_lab_row(test_name="Urine Dipstick Lot Number", source_path="p#lot"),
    )
    upsert_lab_result(
        conn,
        _db_lab_row(
            loinc_code="2951-2",
            test_name="Sodium",
            value=140.0,
            unit="mmol/L",
            source_path="p#na",
        ),
    )

    deleted = purge_non_result_lab_rows_in_db(conn)
    assert deleted == 1
    remaining = [r[0] for r in conn.execute("SELECT test_name FROM lab_results").fetchall()]
    assert remaining == ["Sodium"]
    assert purge_non_result_lab_rows_in_db(conn) == 0  # idempotent


def test_repair_lab_units_fills_and_drops_in_panel_notes(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)
    note_path = labs_dir / "2023-04-20-panel.md"
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "loinc_code": "2951-2",
                "test_name": "Sodium",
                "observed_at": "2023-04-20",
                "value": "140",
                "unit": "",
            },
            "seed-sodium",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "loinc_code": "",
                "test_name": "Urine Dipstick Lot Number",
                "observed_at": "2023-04-20",
                "value_text": "ABC123",
                "unit": "",
            },
            "seed-lot",
        ),
    ]
    # Seed a note that still carries an empty unit and a non-result row by
    # bypassing the fill lane via direct render of pre-fill rows.
    seed_rows = [dict(rows[0]), dict(rows[1])]
    seed_rows[0]["unit"] = ""
    write_lab_panel_note(note_path, seed_rows, source_path="seed#src")

    changed = repair_lab_units(tmp_path)
    assert changed == 1

    repaired = load_labs_from_note(note_path)
    by_name = {r["test_name"]: r for r in repaired}
    assert "Urine Dipstick Lot Number" not in by_name
    assert by_name["Sodium"]["unit"] == "mmol/L"

    # Idempotent: a second pass changes nothing.
    assert repair_lab_units(tmp_path) == 0


# LOINC code provenance (ADR-034)


def test_classify_loinc_code_origin_rules() -> None:
    # Blank code → no origin.
    assert classify_loinc_code_origin("", "source_import") == ""
    # Structured imports and user authority are source-provided.
    assert (
        classify_loinc_code_origin("1751-7", "source_import") == LOINC_CODE_ORIGIN_SOURCE_PROVIDED
    )
    assert (
        classify_loinc_code_origin("1751-7", "user_correction") == LOINC_CODE_ORIGIN_SOURCE_PROVIDED
    )
    # Any other coded source is inferred.
    assert classify_loinc_code_origin("1751-7", "pdf") == LOINC_CODE_ORIGIN_INFERRED
    assert classify_loinc_code_origin("1751-7", "ai_extracted") == LOINC_CODE_ORIGIN_INFERRED
    # An explicit, recognized origin wins over the source_type heuristic.
    assert (
        classify_loinc_code_origin("1751-7", "pdf", "source_provided")
        == LOINC_CODE_ORIGIN_SOURCE_PROVIDED
    )
    # An unrecognized explicit value is ignored, falling back to the heuristic.
    assert classify_loinc_code_origin("1751-7", "pdf", "bogus") == LOINC_CODE_ORIGIN_INFERRED


def test_lab_payload_records_loinc_code_origin() -> None:
    source_provided = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "loinc_code": "1751-7",
            "test_name": "Albumin",
            "observed_at": "2023-04-20",
            "unit": "g/dL",
            "source_type": "source_import",
        },
        "seed",
    )
    assert source_provided["loinc_code_origin"] == LOINC_CODE_ORIGIN_SOURCE_PROVIDED
    blank = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "loinc_code": "",
            "test_name": "Albumin",
            "observed_at": "2023-04-20",
            "unit": "g/dL",
            "source_type": "pdf",
        },
        "seed",
    )
    assert blank["loinc_code_origin"] == ""


def test_fill_loinc_codes_marks_filled_codes_inferred() -> None:
    rows = [{"test_name": "Albumin", "unit": "g/dL", "loinc_code": ""}]
    import_labs.fill_loinc_codes(rows, {"placeholder": "x"})
    assert rows[0]["loinc_code"] == "1751-7"
    assert rows[0]["loinc_code_origin"] == LOINC_CODE_ORIGIN_INFERRED


def test_loinc_code_origin_migration_backfills_legacy_rows(tmp_path: Path) -> None:
    # Build a pre-migration lab_results table (no loinc_code_origin column).
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE lab_results (
            id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL,
            loinc_code TEXT,
            test_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            reference_range_low REAL,
            reference_range_high REAL,
            observed_at TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_path TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.executemany(
        "INSERT INTO lab_results (id, person_id, loinc_code, test_name, observed_at, source_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("r1", "dummy_alice", "1751-7", "Albumin", "2023-04-20", "source_import"),
            ("r2", "dummy_alice", "100158-5", "Albumin", "2023-04-20", "pdf"),
            ("r3", "dummy_alice", "", "pH", "2023-04-20", "pdf"),  # no code -> no origin
        ],
    )
    conn.commit()
    conn.close()

    initialize_database(db_path)

    conn = sqlite3.connect(db_path)
    origins = dict(conn.execute("SELECT id, loinc_code_origin FROM lab_results").fetchall())
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lab_results)")}
    assert "value_text" in columns
    assert origins["r1"] == LOINC_CODE_ORIGIN_SOURCE_PROVIDED  # structured import code
    assert origins["r2"] == LOINC_CODE_ORIGIN_INFERRED  # any other coded source
    assert origins["r3"] in (None, "")  # blank code carries no origin

    # Idempotent: re-running the migration leaves origins unchanged.
    conn.close()
    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    origins2 = dict(conn.execute("SELECT id, loinc_code_origin FROM lab_results").fetchall())
    assert origins2 == origins
    conn.close()


def test_renormalize_lab_panel_note_is_noop_for_clean_note(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)
    note_path = labs_dir / "2023-04-20-panel.md"
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "loinc_code": "2951-2",
                "test_name": "Sodium",
                "observed_at": "2023-04-20",
                "value": "140",
                "unit": "mmol/L",
            },
            "clean-sodium",
        )
    ]
    write_lab_panel_note(note_path, rows, source_path="clean#src")
    assert renormalize_lab_panel_note(note_path) is False


def test_lab_payload_normalizes_scientific_count_units_to_canonical() -> None:
    wbc_payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_bob",
            "test_name": "White blood cell count",
            "observed_at": "2023-04-20",
            "value": "5.5",
            "unit": "10E3/uL",
        },
        "cbc-unit-scientific-wbc",
    )
    rbc_payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_bob",
            "test_name": "RBC",
            "observed_at": "2023-04-20",
            "value": "4.8",
            "unit": "10E6/uL",
        },
        "cbc-unit-scientific-rbc",
    )

    assert wbc_payload["unit"] == "K/uL"
    assert rbc_payload["unit"] == "M/uL"


def test_lab_payload_raises_on_unit_mismatch_for_expanded_cbc_rules() -> None:
    try:
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "White blood cell count",
                "observed_at": "2023-04-20",
                "value": "5.5",
                "unit": "mg/dL",
            },
            "cbc-unit-mismatch",
        )
    except LabSyncError as exc:
        assert "potential unit mismatch" in str(exc)
    else:
        raise AssertionError("Expected LabSyncError for CBC unit mismatch")


def test_upsert_lab_result_inserts_and_updates() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    row: dict[str, Any] = {
        "person_id": "dummy_alice",
        "loinc_code": "4548-4",
        "test_name": "Hemoglobin A1c",
        "value": 6.1,
        "value_text": "6.1",
        "unit": "%",
        "reference_range_low": 4.0,
        "reference_range_high": 5.6,
        "observed_at": "2023-04-20",
        "source_type": "manual",
        "source_path": "Records/dummy_alice/Labs/2023-04-20-hemoglobin-a1c.md",
    }
    upsert_lab_result(conn, row)

    row["value"] = 5.9
    row["value_text"] = "<5.9"
    upsert_lab_result(conn, row)

    stored = conn.execute(
        "SELECT person_id, test_name, value, value_text, unit, source_path FROM lab_results"
    ).fetchone()
    assert stored == (
        "dummy_alice",
        "Hemoglobin A1c",
        5.9,
        "<5.9",
        "%",
        "Records/dummy_alice/Labs/2023-04-20-hemoglobin-a1c.md",
    )


def test_load_labs_from_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "labs.csv"
    csv_path.write_text(
        "person_id,test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "dummy_alice,Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )

    rows = load_labs_from_csv(csv_path)

    assert len(rows) == 1
    assert rows[0]["person_id"] == "dummy_alice"
    assert rows[0]["source_type"] == "csv"
    assert rows[0]["value"] == 5.8


def test_render_lab_note_contains_expected_fields() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-04-20",
            "value": "5.8",
            "unit": "%",
            "source_type": "manual",
        },
        "test",
    )

    content = render_lab_note(payload)
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])

    assert front_matter["person_id"] == "dummy_alice"
    assert front_matter["test_name"] == "Hemoglobin A1c"
    assert front_matter["value"] == 5.8


def test_render_lab_panel_note_contains_tests_list() -> None:
    labs = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-20",
                "value": "5.8",
                "unit": "%",
                "source_type": "manual",
            },
            "test-1",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Fasting Glucose",
                "observed_at": "2023-04-20",
                "value": "102",
                "unit": "mg/dL",
                "source_type": "manual",
            },
            "test-2",
        ),
    ]

    content = render_lab_panel_note(labs)
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])

    assert front_matter["person_id"] == "dummy_alice"
    assert front_matter["record_type"] == "lab_panel"
    assert "tests" not in front_matter
    assert LAB_PANEL_TABLE_HEADER in content
    assert "| Hemoglobin A1c |" in content
    assert "# Lab Panel — 2023-04-20" in content
    assert "[[_index|← Labs]]" in content


def test_render_lab_panel_note_includes_source_file_name_when_provided() -> None:
    labs = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-20",
                "value": "5.8",
                "unit": "%",
                "source_type": "manual",
            },
            "test-1",
        )
    ]

    content = render_lab_panel_note(labs, source_file_name="lab-report.pdf")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])

    assert front_matter["source_file_name"] == "lab-report.pdf"
    assert "- Source file: lab-report.pdf" in content


def test_render_lab_panel_note_includes_processed_source_path_when_provided() -> None:
    labs = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-20",
                "value": "5.8",
                "unit": "%",
                "source_type": "manual",
            },
            "test-1",
        )
    ]

    source_path = "data/processed/dummy_alice/Labs/2023-04-20-lab-report.pdf"
    content = render_lab_panel_note(
        labs,
        source_file_name="lab-report.pdf",
        source_path=source_path,
    )
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])

    assert front_matter["source_file_name"] == "lab-report.pdf"
    assert front_matter["source_path"] == source_path
    # source_path is in front matter for tools; it does not appear as a user-visible bullet
    assert f"- Source path: {source_path}" not in content


def test_replace_lab_results_for_source_path_handles_multiple_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    labs = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-20",
                "value": "5.8",
                "unit": "%",
                "source_type": "manual",
            },
            "test-1",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Fasting Glucose",
                "observed_at": "2023-04-20",
                "value": "102",
                "unit": "mg/dL",
                "source_type": "manual",
            },
            "test-2",
        ),
    ]

    source_path = "Records/dummy_alice/Labs/2023-04-20-intake-panel.md"
    replace_lab_results_for_source_path(conn, source_path, labs)

    count = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?", (source_path,)
    ).fetchone()
    assert count == (2,)

    # Replacing should keep row count stable for the same note source.
    replace_lab_results_for_source_path(conn, source_path, labs[:1])
    count = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?", (source_path,)
    ).fetchone()
    assert count == (1,)


def _make_lab(
    test_name: str = "Hemoglobin A1c",
    observed_at: str = "2023-04-20",
    value: str = "5.8",
    unit: str = "%",
    person_id: str = "dummy_alice",
) -> dict[str, Any]:
    return lab_payload_from_mapping(
        {
            "person_id": person_id,
            "test_name": test_name,
            "observed_at": observed_at,
            "value": value,
            "unit": unit,
            "source_type": "csv",
        },
        f"{person_id}-{test_name}",
    )


def test_replace_lab_results_skips_redundant_observation_from_different_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    lab = _make_lab()

    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"
    replace_lab_results_for_source_path(conn, csv_source, [lab])
    inserted, skipped = replace_lab_results_for_source_path(conn, pdf_source, [lab])

    total = conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]
    assert total == 1
    assert inserted == 0
    assert skipped == 1


def test_replace_lab_results_suppressed_observation_survives_owner_reimport() -> None:
    """A cross-source-redundant observation must not be lost when its owner drops it.

    Source A claims the observation, so source B's duplicate is skipped. When
    source A is later reprocessed and no longer yields it, the observation must
    remain canonically present under source B rather than disappearing while
    still rendering in both source-evidence notes (ADR-006).
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"
    shared = _make_lab()
    unrelated = _make_lab(test_name="Glucose", value="90", unit="mg/dL")

    replace_lab_results_for_source_path(conn, csv_source, [dict(shared), dict(unrelated)])
    inserted, skipped = replace_lab_results_for_source_path(conn, pdf_source, [dict(shared)])
    assert (inserted, skipped) == (0, 1)

    # The CSV is reprocessed and no longer reports that analyte.
    replace_lab_results_for_source_path(conn, csv_source, [dict(unrelated)])

    rows = conn.execute(
        "SELECT source_path FROM lab_results WHERE test_name = 'Hemoglobin A1c'"
    ).fetchall()
    assert [row[0] for row in rows] == [pdf_source]


def test_replace_lab_results_suppression_records_link_with_owner_provenance() -> None:
    """ADR-006: a dedup decision must be reversible and carry its provenance."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"

    replace_lab_results_for_source_path(conn, csv_source, [_make_lab()])
    replace_lab_results_for_source_path(conn, pdf_source, [_make_lab()])

    links = conn.execute(
        "SELECT subject_type, person_id, suppressed_source_path, owner_source_path FROM dedup_links"
    ).fetchall()
    assert links == [("lab_result", "dummy_alice", pdf_source, csv_source)]


def test_textual_suppression_links_remain_distinct_and_restore_all_facts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
    archive_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
    suppressed_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0002.XML"
    owner_rows = [
        _make_lab(test_name="Synthetic culture", value="", observed_at="2099-01-02"),
        _make_lab(test_name="Synthetic culture", value="", observed_at="2099-01-02"),
    ]
    owner_rows[0]["value_text"] = "Synthetic narrative alpha"
    owner_rows[1]["value_text"] = "Synthetic narrative beta"
    owner_rows[0]["loinc_code"] = owner_rows[1]["loinc_code"] = "630-4"
    suppressed_rows = [dict(row) for row in owner_rows]

    replace_lab_results_for_source_path(conn, archive_source, owner_rows)
    inserted, skipped = replace_lab_results_for_source_path(
        conn, suppressed_source, suppressed_rows
    )
    assert (inserted, skipped) == (0, 2)
    assert conn.execute("SELECT COUNT(*) FROM dedup_links").fetchone() == (2,)

    replace_lab_results_for_source_path(conn, archive_source, [])

    restored = conn.execute(
        """
        SELECT value_text, source_path
        FROM lab_results
        WHERE test_name = 'Synthetic culture'
        ORDER BY value_text
        """
    ).fetchall()
    assert restored == [
        ("Synthetic narrative alpha", suppressed_source),
        ("Synthetic narrative beta", suppressed_source),
    ]
    assert conn.execute("SELECT COUNT(*) FROM dedup_links").fetchone() == (0,)


def test_same_draw_archive_restatement_restores_when_owner_stops_reporting() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
    archive_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
    pdf_source = "data/processed/dummy_alice/Labs/2099-01-02-panel.pdf"
    archive_row = _make_lab(
        test_name="Synthetic measure",
        value="4.7",
        unit="g/dL",
        observed_at="2099-01-03T00:15:00",
    )
    archive_row["loinc_code"] = "1751-7"
    archive_row["source_type"] = "source_import"
    pdf_row = dict(archive_row)
    pdf_row["observed_at"] = "2099-01-02"
    pdf_row["source_type"] = "pdf:synthetic"

    replace_lab_results_for_source_path(conn, archive_source, [archive_row])
    assert replace_lab_results_for_source_path(conn, pdf_source, [pdf_row]) == (0, 1)

    replace_lab_results_for_source_path(conn, archive_source, [])

    assert conn.execute(
        "SELECT observed_at, source_path FROM lab_results WHERE test_name = 'Synthetic measure'"
    ).fetchall() == [("2099-01-02", pdf_source)]
    assert conn.execute(
        """
        SELECT COUNT(*) FROM audit_events
        WHERE event_type = 'lab_deduplicated_row_restored'
        """
    ).fetchone() == (1,)


def test_replace_lab_results_restore_does_not_override_user_correction() -> None:
    """A restored observation must never outrank a user correction (ADR-004)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"

    replace_lab_results_for_source_path(conn, csv_source, [_make_lab()])
    replace_lab_results_for_source_path(conn, pdf_source, [_make_lab()])

    corrected = _make_lab(value="6.4")
    corrected["source_path"] = "Records/dummy_alice/Labs/2023-05-01-corrections.md"
    corrected["row_index"] = 0
    corrected["source_type"] = "user_correction"
    upsert_lab_result(conn, corrected)
    conn.execute(
        "DELETE FROM lab_results WHERE source_path = ? AND source_type != 'user_correction'",
        (csv_source,),
    )
    replace_lab_results_for_source_path(
        conn, csv_source, [_make_lab(test_name="Glucose", value="90", unit="mg/dL")]
    )

    values = {
        row[0]
        for row in conn.execute("SELECT value FROM lab_results WHERE test_name = 'Hemoglobin A1c'")
    }
    assert values == {6.4}, "the user-corrected value must remain the only one"
    assert conn.execute("SELECT COUNT(*) FROM dedup_links").fetchone() == (1,)


def test_replace_lab_results_rejects_cbc_loinc_unit_mismatch() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    mismatched = _make_lab(test_name="Eosinophils, absolute", value="0.1", unit="K/uL")
    mismatched["loinc_code"] = "20472-7"

    with pytest.raises(LabSyncError, match="potential LOINC/unit mismatch"):
        replace_lab_results_for_source_path(
            conn,
            "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf",
            [mismatched],
        )


def test_replace_lab_results_preserves_conflicting_observation_from_different_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    csv_lab = _make_lab(value="5.8")
    pdf_lab = _make_lab(value="6.1")

    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"
    replace_lab_results_for_source_path(conn, csv_source, [csv_lab])
    inserted, skipped = replace_lab_results_for_source_path(conn, pdf_source, [pdf_lab])

    total = conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]
    assert total == 2
    assert inserted == 1
    assert skipped == 0
    values = {
        r[0]
        for r in conn.execute("SELECT value FROM lab_results WHERE test_name = 'Hemoglobin A1c'")
    }
    assert values == {5.8, 6.1}


def test_replace_lab_results_replaces_stale_same_value_row_with_more_complete_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    legacy_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    repaired_source = "Records/dummy_alice/Labs/2023-04-20-panel.md"
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-row",
            "dummy_alice",
            "",
            "Hemoglobin A1c",
            5.8,
            "%",
            4.0,
            5.6,
            "2023-04-20",
            "csv",
            legacy_source,
        ),
    )

    repaired_lab = _make_lab(value="5.8")
    repaired_lab["loinc_code"] = "4548-4"
    inserted, skipped = replace_lab_results_for_source_path(conn, repaired_source, [repaired_lab])

    assert inserted == 1
    assert skipped == 0
    rows = conn.execute(
        "SELECT loinc_code, source_path FROM lab_results WHERE test_name = 'Hemoglobin A1c'"
    ).fetchall()
    assert rows == [("4548-4", repaired_source)]


def test_replace_lab_results_replaces_same_draw_miscode_with_valid_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    archive_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
    pdf_source = "data/processed/dummy_alice/Labs/2021-10-09-CBC.pdf"
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "bad-eos",
            "dummy_alice",
            "20472-7",
            "Eosinophils, absolute",
            0.11,
            "K/uL",
            None,
            None,
            "2021-10-09",
            "csv",
            archive_source,
        ),
    )

    repaired = _make_lab(
        test_name="Eosinophils, absolute",
        value="0.11",
        unit="K/uL",
        observed_at="2021-10-09T22:29:18",
    )
    repaired["loinc_code"] = "711-2"
    inserted, skipped = replace_lab_results_for_source_path(conn, pdf_source, [repaired])

    assert inserted == 1
    assert skipped == 0
    rows = conn.execute(
        "SELECT loinc_code, source_path FROM lab_results WHERE test_name = 'Eosinophils, absolute'"
    ).fetchall()
    assert rows == [("711-2", pdf_source)]


def test_replace_lab_results_preserves_source_provided_archive_restatement_for_albumin() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    archive_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
    pdf_source = "data/processed/dummy_alice/Labs/2021-10-09-CMP.pdf"
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "archive-albumin",
            "dummy_alice",
            "1751-7",
            "Albumin",
            4.7,
            "g/dL",
            3.5,
            5.7,
            "2021-10-10T00:43:19",
            "source_import",
            archive_source,
        ),
    )

    repaired = _make_lab(
        test_name="Albumin",
        value="4.7",
        unit="g/dL",
        observed_at="2021-10-09",
    )
    repaired["loinc_code"] = "100158-5"
    repaired["reference_range_low"] = 3.5
    repaired["reference_range_high"] = 5.7
    inserted, skipped = replace_lab_results_for_source_path(conn, pdf_source, [repaired])

    assert inserted == 0
    assert skipped == 1
    rows = conn.execute(
        "SELECT loinc_code, source_path FROM lab_results WHERE test_name = 'Albumin'"
    ).fetchall()
    assert rows == [("1751-7", archive_source)]


@pytest.mark.parametrize("archive_first", [False, True])
def test_replace_lab_results_collapses_same_day_blank_unit_archive_restatement(
    archive_first: bool,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    archive_source = "data/processed/dummy_person/HealthSummary/archive.zip#DOC0001.XML"
    pdf_source = "data/processed/dummy_person/Labs/2099-01-02-CBC.pdf"
    archive_row = _make_lab(
        test_name="Synthetic cell measure",
        value="33.3",
        unit="pg",
        observed_at="2099-01-02T08:09:00",
    )
    archive_row["loinc_code"] = "785-6"
    archive_row["source_type"] = "source_import"
    archive_row["reference_range_high"] = "34.0"
    pdf_row = _make_lab(
        test_name="Synthetic cell measure",
        value="33.3",
        unit="",
        observed_at="2099-01-02",
    )
    pdf_row["loinc_code"] = "785-6"
    pdf_row["source_type"] = "pdf:mychart"

    ordered = (
        ((archive_source, archive_row), (pdf_source, pdf_row))
        if archive_first
        else ((pdf_source, pdf_row), (archive_source, archive_row))
    )
    for source_path, row in ordered:
        replace_lab_results_for_source_path(conn, source_path, [row])

    assert conn.execute(
        "SELECT loinc_code, value, unit, observed_at, source_path FROM lab_results"
    ).fetchall() == [("785-6", 33.3, "pg", "2099-01-02T08:09:00", archive_source)]


@pytest.mark.parametrize(
    ("loinc_code", "observed_at", "reference_range_high"),
    [
        ("99999-9", "2099-01-02", None),
        ("785-6", "2099-01-03", None),
        ("785-6", "2099-01-02", "35.0"),
    ],
)
def test_replace_lab_results_keeps_blank_unit_row_when_overlap_guards_fail(
    loinc_code: str,
    observed_at: str,
    reference_range_high: str | None,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    archive_source = "data/processed/dummy_person/HealthSummary/archive.zip#DOC0001.XML"
    pdf_source = "data/processed/dummy_person/Labs/synthetic-CBC.pdf"
    archive_row = _make_lab(
        test_name="Synthetic cell measure",
        value="33.3",
        unit="pg",
        observed_at="2099-01-02T08:09:00",
    )
    archive_row["loinc_code"] = "785-6"
    archive_row["source_type"] = "source_import"
    archive_row["reference_range_high"] = "34.0"
    replace_lab_results_for_source_path(conn, archive_source, [archive_row])

    pdf_row = _make_lab(
        test_name="Synthetic cell measure",
        value="33.3",
        unit="",
        observed_at=observed_at,
    )
    pdf_row["loinc_code"] = loinc_code
    pdf_row["source_type"] = "pdf:mychart"
    pdf_row["reference_range_high"] = reference_range_high
    replace_lab_results_for_source_path(conn, pdf_source, [pdf_row])

    assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (2,)


def test_replace_lab_results_replaces_inferred_pdf_row_with_source_provided_archive_code() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    pdf_source = "data/processed/dummy_alice/Labs/2021-10-09-CMP.pdf"
    archive_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "pdf-albumin",
            "dummy_alice",
            "100158-5",
            "Albumin",
            4.7,
            "g/dL",
            3.5,
            5.7,
            "2021-10-09",
            "pdf:mychart",
            pdf_source,
        ),
    )

    archive_row = _make_lab(
        test_name="Albumin",
        value="4.7",
        unit="g/dL",
        observed_at="2021-10-10T00:43:19",
    )
    archive_row["loinc_code"] = "1751-7"
    archive_row["reference_range_low"] = 3.5
    archive_row["reference_range_high"] = 5.7
    with_same_source_type = dict(archive_row)
    with_same_source_type["source_type"] = "source_import"
    inserted, skipped = replace_lab_results_for_source_path(
        conn, archive_source, [with_same_source_type]
    )

    assert inserted == 1
    assert skipped == 0
    rows = conn.execute(
        "SELECT loinc_code, source_path FROM lab_results WHERE test_name = 'Albumin'"
    ).fetchall()
    assert rows == [("1751-7", archive_source)]


def test_replace_lab_results_rejects_same_draw_miscode_before_persisting() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    good_source = "data/processed/dummy_alice/Labs/2021-10-09-CBC.pdf"
    archive_source = "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML"
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "good-eos",
            "dummy_alice",
            "711-2",
            "Eosinophils, absolute",
            0.11,
            "K/uL",
            None,
            None,
            "2021-10-09T22:29:18",
            "csv",
            good_source,
        ),
    )

    miscoded = _make_lab(
        test_name="Eosinophils, absolute",
        value="0.11",
        unit="K/uL",
        observed_at="2021-10-09",
    )
    miscoded["loinc_code"] = "20472-7"
    with pytest.raises(LabSyncError, match="potential LOINC/unit mismatch"):
        replace_lab_results_for_source_path(conn, archive_source, [miscoded])

    rows = conn.execute(
        "SELECT loinc_code, source_path FROM lab_results WHERE test_name = 'Eosinophils, absolute'"
    ).fetchall()
    assert rows == [("711-2", good_source)]


def test_replace_lab_results_preserves_user_correction_on_same_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    source_path = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"

    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "lab-corrected-id",
            "dummy_alice",
            "",
            "Hemoglobin A1c",
            6.2,
            "%",
            4.0,
            5.6,
            "2023-04-20",
            "user_correction",
            source_path,
        ),
    )

    inserted, skipped = replace_lab_results_for_source_path(
        conn, source_path, [_make_lab(value="5.8")]
    )

    assert (inserted, skipped) == (0, 1)
    row = conn.execute(
        "SELECT source_type, value FROM lab_results WHERE id = ?", ("lab-corrected-id",)
    ).fetchone()
    assert row == ("user_correction", 6.2)


def test_replace_lab_results_logs_skipped_user_correction_conflict() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    source_path = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at,
            source_type, source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "lab-corrected-id",
            "dummy_alice",
            "",
            "Hemoglobin A1c",
            6.2,
            "%",
            4.0,
            5.6,
            "2023-04-20",
            "user_correction",
            "data/processed/dummy_alice/Labs/original.csv",
        ),
    )

    inserted, skipped = replace_lab_results_for_source_path(
        conn, source_path, [_make_lab(value="5.8")]
    )

    assert (inserted, skipped) == (0, 1)
    event = conn.execute(
        "SELECT event_type, subject_type, subject_id, source_path, status FROM audit_events"
    ).fetchone()
    assert event == (
        "skipped_user_correction",
        "lab_result",
        "dummy_alice",
        source_path,
        "processed",
    )


def test_replace_lab_results_returns_correct_counts_for_mixed_panel() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    a1c = _make_lab(test_name="Hemoglobin A1c", value="5.8")
    glucose = _make_lab(test_name="Fasting Glucose", value="98")

    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"
    replace_lab_results_for_source_path(conn, csv_source, [a1c, glucose])
    # PDF has same A1c (redundant) and a conflicting glucose value
    glucose_conflict = _make_lab(test_name="Fasting Glucose", value="102")
    inserted, skipped = replace_lab_results_for_source_path(
        conn, pdf_source, [a1c, glucose_conflict]
    )

    assert inserted == 1
    assert skipped == 1
    total = conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]
    assert total == 3  # 2 from CSV + 1 conflicting glucose from PDF


def test_build_lab_conflicts_from_db_reports_same_test_date_different_values() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"

    replace_lab_results_for_source_path(conn, csv_source, [_make_lab(value="5.8")])
    replace_lab_results_for_source_path(conn, pdf_source, [_make_lab(value="6.1")])

    conflicts = build_lab_conflicts_from_db(conn, "dummy_alice")

    assert len(conflicts) == 2
    assert {row["value"] for row in conflicts} == {5.8, 6.1}
    assert {row["source_path"] for row in conflicts} == {csv_source, pdf_source}


def test_build_lab_conflicts_from_db_ignores_same_source_different_values() -> None:
    """Same-source different-value rows on the same date are multi-draw, not conflicts."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"

    for row_id, value in [("id-1", 5.8), ("id-2", 6.1)]:
        conn.execute(
            """
            INSERT INTO lab_results
                (id, person_id, test_name, value, unit, observed_at, source_type, source_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row_id, "dummy_alice", "Hemoglobin A1c", value, "%", "2023-04-20", "pdf", source),
        )

    assert build_lab_conflicts_from_db(conn, "dummy_alice") == []


def test_build_lab_conflicts_from_db_ignores_redundant_same_value_sources() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"

    replace_lab_results_for_source_path(conn, csv_source, [_make_lab(value="5.8")])
    replace_lab_results_for_source_path(conn, pdf_source, [_make_lab(value="5.8")])

    assert build_lab_conflicts_from_db(conn, "dummy_alice") == []


def test_build_lab_conflicts_from_db_ignores_null_vs_real_value() -> None:
    """A source with no parseable value (NULL) vs a source with a real value is not a conflict."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    note_source = "Records/dummy_alice/Labs/2023-04-20-mychart-panel.md"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"

    # Vault note source: test listed but value not extractable (NULL stored directly)
    conn.execute(
        """
        INSERT INTO lab_results
            (id, person_id, test_name, value, unit, observed_at, source_type, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("id-null", "dummy_alice", "Hemoglobin A1c", None, "%", "2023-04-20", "csv", note_source),
    )
    replace_lab_results_for_source_path(conn, pdf_source, [_make_lab(value="5.8")])

    assert build_lab_conflicts_from_db(conn, "dummy_alice") == []


def test_render_lab_conflicts_report_is_caregiver_visible() -> None:
    content = render_lab_conflicts_report(
        "dummy_alice",
        [
            {
                "observed_at": "2023-04-20",
                "test_name": "Hemoglobin A1c",
                "value": 5.8,
                "unit": "%",
                "source_path": "data/processed/dummy_alice/Labs/2023-04-20-panel.csv",
            },
            {
                "observed_at": "2023-04-20",
                "test_name": "Hemoglobin A1c",
                "value": 6.1,
                "unit": "%",
                "source_path": "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf",
            },
        ],
    )

    assert "report_type: lab_conflicts" in content
    assert LAB_CONFLICTS_TABLE_HEADER in content
    assert "Review the retained source files before resolving" in content
    assert (
        "| 2023-04-20 | Hemoglobin A1c | 5.8 | % | data/"
        "processed/dummy_alice/Labs/2023-04-20-panel.csv |"
    ) in content
    assert (
        "| 2023-04-20 | Hemoglobin A1c | 6.1 | % | data/"
        "processed/dummy_alice/Labs/2023-04-20-panel.pdf |"
    ) in content


def test_render_lab_conflicts_report_preserves_zero_values() -> None:
    content = render_lab_conflicts_report(
        "dummy_alice",
        [
            {
                "observed_at": "2023-04-20",
                "test_name": "Example",
                "value": 0.0,
                "unit": "mg/dL",
                "source_path": "data/processed/dummy_alice/Labs/a.csv",
            }
        ],
    )

    assert (
        "| 2023-04-20 | Example | 0.0 | mg/dL | data/processed/dummy_alice/Labs/a.csv |" in content
    )


def test_lab_payloads_from_mapping_single_and_multi_modes() -> None:
    single = lab_payloads_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-04-20",
            "value": "5.8",
        },
        "single",
    )
    assert len(single) == 1

    multi = lab_payloads_from_mapping(
        {
            "person_id": "dummy_alice",
            "observed_at": "2023-04-20",
            "tests": [
                {"test_name": "Hemoglobin A1c", "value": "5.8"},
                {"test_name": "Fasting Glucose", "value": "102"},
            ],
        },
        "multi",
    )
    assert len(multi) == 2


def test_parse_labs_from_pdf_text_extracts_rows() -> None:
    text = """
Quest Diagnostics
Collected: 04/20/2023
Hemoglobin A1c 5.8 % 4.0 - 5.6
Glucose 102 mg/dL 70 - 99
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_alice", source_label="sample.pdf")

    assert len(rows) == 2
    assert rows[0]["person_id"] == "dummy_alice"
    assert rows[0]["source_type"] == "pdf:quest"
    assert rows[0]["observed_at"] == "2023-04-20"
    assert rows[1]["test_name"] == "Glucose"
    assert rows[1]["value"] == 102.0


def test_parse_labs_from_pdf_text_extracts_structured_current_result_tables() -> None:
    text = """
Laboratory Corporation of America
Date Collected: 04/20/2023
Example Count Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Count 01 6.2 5.4 01/15/2022 x10E3/uL 3.0-11.0
Example Percent 01 42 48 01/15/2022 % Not Estab.
Example Metabolic Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Lower Bound 82 100 01/15/2022 mL/min/1.73 >60
Example Ratio 13 13* 01/15/2022 10-24
Example Chemistry 01 96 91 01/15/2022 mg/dL 70-100
Example Upper-Bound Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Upper Bound 138 High mg/dL 0-120
Reference Range:
For a subgroup, 97% of controls are <100.
Example Profile
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Wrapped,
Marker 01 64.0 nmol/L 20.0-80.0
Screening Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Screening Marker 03 7.95 ng/mL
Reference Range:
>=18y: 97% of controls are <8.0. Results may vary by method.
% Example Fraction 03 N/A %
Example Antibody Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Wrapped Antibody
Marker 01 150 High IU/mL 0-30
Example Comparator 02 <1.2 IU/mL 0.0-1.0
Some controls may have values up to 3 IU/mL.
Date Reported: 04/21/2023 1200 ET
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_alice",
        source_label="structured-table.pdf",
    )

    assert len(rows) == 11
    by_name = {row["test_name"]: row for row in rows}
    assert by_name["Example Count"]["value"] == 6.2
    assert by_name["Example Count"]["unit"] == "K/uL"
    assert by_name["Example Count"]["reference_range_low"] == 3.0
    assert by_name["Example Percent"]["reference_range_low"] is None
    assert by_name["Example Percent"]["reference_range_high"] is None
    assert by_name["Example Lower Bound"]["reference_range_low"] == 60.0
    assert by_name["Example Lower Bound"]["reference_range_high"] is None
    assert by_name["Example Ratio"]["unit"] == ""
    assert by_name["Example Chemistry"]["value"] == 96.0
    assert by_name["Example Upper Bound"]["value"] == 138.0
    assert by_name["Example Upper Bound"]["reference_range_high"] == 120.0
    assert by_name["Example Wrapped, Marker"]["value"] == 64.0
    assert by_name["Example Screening Marker"]["reference_range_low"] is None
    assert by_name["Example Screening Marker"]["reference_range_high"] == 8.0
    assert by_name["% Example Fraction"]["value"] is None
    assert by_name["% Example Fraction"]["value_text"] == "N/A"
    assert by_name["Example Wrapped Antibody Marker"]["value"] == 150.0
    assert by_name["Example Comparator"]["value"] is None
    assert by_name["Example Comparator"]["value_text"] == "<1.2"
    assert all(row["source_type"] == "pdf:labcorp" for row in rows)


@pytest.mark.parametrize(
    "malformed_line",
    [
        "Malformed Result 01 ??? High mg/dL 0-10",
        "Malformed Result 01 ??? mg/dL",
    ],
)
def test_parse_labs_from_pdf_text_rejects_incomplete_structured_result_table(
    malformed_line: str,
) -> None:
    text = f"""
Laboratory Corporation of America
Date Collected: 04/20/2023
Example Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
Example Marker 01 96 91 01/15/2022 mg/dL 70-100
{malformed_line}
"""

    with pytest.raises(LabSyncError, match="could not parse every structured result row"):
        parse_labs_from_pdf_text(
            text,
            person_id="dummy_alice",
            source_label="incomplete-table.pdf",
        )


def test_parse_labs_from_pdf_text_extracts_myquest_blocks() -> None:
    text = """
Quest Diagnostics
Collected:
03/15/2016 09:30
VITAMIN D,25-OH,TOTAL,IA
Analyte
Value
VITAMIN D,25-OH,TOTAL,IA
37
Reference Range:
30-100
ng/mL
C-REACTIVE PROTEIN
Analyte
Value
C-REACTIVE PROTEIN
<3.0
Reference Range:
<8.0
mg/L
ANA SCREEN, IFA, W/REFL TITER AND PATTERN
Analyte
Value
ANA SCREEN, IFA
NEGATIVE
Reference Range:
NEGATIVE
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="myquest.pdf")

    assert len(rows) == 3
    assert rows[0]["test_name"] == "Vitamin D 25-hydroxy"
    assert rows[0]["value"] == 37.0
    assert rows[0]["reference_range_low"] == 30.0
    assert rows[0]["reference_range_high"] == 100.0
    assert rows[0]["unit"] == "ng/mL"
    assert rows[0]["source_type"] == "pdf:quest"
    assert rows[0]["observed_at"] == "2016-03-15"

    assert rows[1]["test_name"] == "C reactive protein"
    assert rows[1]["value"] == 3.0
    assert rows[1]["reference_range_low"] is None
    assert rows[1]["reference_range_high"] == 8.0

    assert rows[2]["test_name"] == "ANA screen, IFA"
    assert rows[2]["value"] is None
    assert rows[2]["value_text"] == "NEGATIVE"


def test_parse_labs_from_pdf_text_extracts_mychart_blocks() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
Name: Test Subject | DOB: 01/01/1990 | MRN: 0000000 |
Lab PanelCollected on Jun 05, 2016 10:00 AM
Results
Bilirubin Total
Normal range: 0.1 - 1.2 mg/dL
0.10.1 1.21.2
1
Troponin I, POC
Normal value: See Ref Notes ng/mL
< 0.08 Negative; 0.08 - 0.20 Indeterminate; > 0.20 Positive
Value
0.01
% nRBC
Normal value: 0.0 per 100 WBCs
Value
0.0
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart.pdf")

    assert len(rows) == 3
    assert rows[0]["source_type"] == "pdf:mychart"
    assert rows[0]["observed_at"] == "2016-06-05"
    assert rows[0]["test_name"] == "Bilirubin, total"
    assert rows[0]["value"] == 1.0
    assert rows[0]["reference_range_low"] == 0.1
    assert rows[0]["reference_range_high"] == 1.2
    assert rows[0]["unit"] == "mg/dL"

    assert rows[1]["test_name"] == "Troponin I, POC"
    assert rows[1]["value"] == 0.01
    assert rows[1]["value_text"] == "0.01"

    assert rows[2]["test_name"] == "% nRBC"
    assert rows[2]["value"] == 0.0
    assert rows[2]["reference_range_low"] == 0.0
    assert rows[2]["reference_range_high"] == 0.0
    assert rows[2]["unit"] == "per 100 WBCs"


def test_parse_labs_from_pdf_text_handles_grouped_mychart_reference_bounds() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
SYNTHETIC PANEL
Collected on Jul 05, 2099 8:15 AM
Synthetic Marker
Normal range: 232 - 1,245 pg/mL
232232 1,2451,245
596
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_bob",
        source_label="mychart-grouped-range.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Synthetic Marker"
    assert rows[0]["value"] == 596.0
    assert rows[0]["unit"] == "pg/mL"
    assert rows[0]["reference_range_low"] == 232.0
    assert rows[0]["reference_range_high"] == 1245.0


def test_parse_labs_from_pdf_text_extracts_qualitative_mychart_value() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
SYNTHETIC MICROSCOPY
Collected on Jun 12, 2099 3:44 PM
Synthetic Cells
Normal range: 0 - 5 /hpf
Value
None seen
Synthetic Casts
Normal value: None seen /lpf
Value
None seen
Synthetic Bacteria
Normal value: None seen/Few
Value
None seen
Synthetic Appearance
Normal value: Clear
Value
Clear
Synthetic Glucose
Normal value: Negative mg/dL
Value
Negative
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_bob",
        source_label="mychart-qualitative.pdf",
    )

    assert len(rows) == 5
    assert rows[0]["test_name"] == "Synthetic Cells"
    assert rows[0]["value"] is None
    assert rows[0]["value_text"] == "None seen"
    assert rows[0]["unit"] == "/hpf"
    assert rows[1]["test_name"] == "Synthetic Casts"
    assert rows[1]["value_text"] == "None seen"
    assert rows[1]["unit"] == "/lpf"
    assert rows[2]["test_name"] == "Synthetic Bacteria"
    assert rows[2]["value_text"] == "None seen"
    assert rows[2]["unit"] == ""
    assert rows[3]["value_text"] == "Clear"
    assert rows[3]["unit"] == ""
    assert rows[4]["value_text"] == "Negative"
    assert rows[4]["unit"] == "mg/dL"


def test_parse_labs_from_pdf_text_extracts_unreferenced_mychart_value_block() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
SYNTHETIC CULTURE
Collected on Feb 20, 2099 2:56 PM
Synthetic Culture
Value
No growth of greater than or equal to 1000 CFU/mL at 18 hours
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_bob",
        source_label="mychart-unreferenced.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Synthetic Culture"
    assert rows[0]["value"] is None
    assert rows[0]["value_text"] == "No growth of greater than or equal to 1000 CFU/mL at 18 hours"
    assert rows[0]["unit"] == ""


@pytest.mark.parametrize(
    "block",
    [
        "Narrative sentence.\nValue\n100",
        "Resulted by system.\nValue\nNo",
        "Fasting?\nValue\nYes",
        "Synthetic Unit\nValue\n/HPF",
    ],
)
def test_parse_labs_from_pdf_text_rejects_unsafe_unreferenced_value_block(
    block: str,
) -> None:
    text = f"""
MyChart® licensed from Epic Systems Corporation
SYNTHETIC PANEL
Collected on Feb 20, 2099 2:56 PM
{block}
"""

    with pytest.raises(LabSyncError, match="no lab rows extracted"):
        parse_labs_from_pdf_text(
            text,
            person_id="dummy_bob",
            source_label="mychart-unsafe-unreferenced.pdf",
        )


def test_parse_labs_from_pdf_text_preserves_percent_unit_from_mychart_normal_value() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
CBC W/AUTO DIFFERENTIAL
Collected on Dec 31, 2017 1:15 AM
% Neutrophils
Normal value: Not Estab. %
Value
49
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_bob",
        source_label="mychart-cbc.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["test_name"] == "% Neutrophils"
    assert rows[0]["value"] == 49.0
    assert rows[0]["unit"] == "%"


def test_parse_labs_from_pdf_text_prefers_immediate_mychart_graph_value() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
HEMOGLOBIN A1C
Collected on Jun 24, 2023 9:53 AM
Hemoglobin A1C
Normal range: 4.0 - 5.6 %
44 5.65.6
5.8 High
eAG
mg/dL
Value
120
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-a1c.pdf")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Hemoglobin A1c"
    assert rows[0]["value"] == 5.8
    assert rows[0]["value_text"] == "5.8"
    assert rows[0]["unit"] == "%"


def test_parse_labs_from_pdf_text_does_not_steal_neighbor_value_block() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
CBC W/AUTO DIFFERENTIAL
Collected on Jun 24, 2023 9:53 AM
Basophils Absolute
Normal range: 0.01 - 0.08 10*3/uL
0.010.01 0.080.08
0.03
% Neutrophils
%
Value
53.8
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-cbc.pdf")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Basophils, absolute"
    assert rows[0]["value"] == 0.03
    # 10*3/uL canonicalises to K/uL like the sibling 10e3/uL alias; the point of
    # this test is that the value comes from its own block, not the neighbour's.
    assert rows[0]["unit"] == "K/uL"


def test_parse_labs_from_pdf_text_keeps_decimal_value_before_zip_code() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
LIPID PANEL
Collected on Jun 24, 2023 9:53 AM
CH/HD
Normal range: 0.00 - 4.97
00 4.974.97
2.74
Resulting lab:
EVERGREENHEALTH LABORATORY - REDMOND
Redmond WA 98052
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-lipid.pdf")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "CH/HD"
    assert rows[0]["value"] == 2.74


def test_parse_labs_from_pdf_text_prefers_explicit_value_over_mychart_risk_band() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
Name: Test Subject | DOB: 01/01/1990 | MRN: 0000000 |
HIGH SENSITIVITY CRP
Collected on Oct 09, 2021 8:52 AM
Results
C-Reactive Protein, Cardiac - LC
Normal value: 0.00 - 3.00 mg/L
Relative Risk for Future Cardiovascular Event
Low <1.00
Average 1.00 - 3.00
High >3.00
Value
7.09
High
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-crp.pdf")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "C-Reactive Protein, Cardiac"
    assert rows[0]["value"] == 7.09
    assert rows[0]["value_text"] == "7.09"
    assert rows[0]["unit"] == "mg/L"
    assert rows[0]["reference_range_low"] == 0.0
    assert rows[0]["reference_range_high"] == 3.0


def test_parse_labs_from_pdf_text_skips_labeled_interpretation_bands() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
ANTIBODY SCREEN
Collected on Dec 31, 2099 1:15 AM
Example Antibody
Normal range: 0.0 - 0.9 s/co ratio
Negative: < 0.8
Indeterminate: 0.8 - 0.9
Positive: > 0.9
Positive results require confirmation.
Follow-up testing may be appropriate.
Specimen received by the reference laboratory.
00 0.90.9
0.1
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_bob",
        source_label="mychart-antibody.pdf",
    )

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Example Antibody"
    assert rows[0]["value"] == 0.1
    assert rows[0]["value_text"] == "0.1"
    assert rows[0]["unit"] == "s/co ratio"


def test_parse_labs_from_pdf_text_resolves_mychart_followup_bullet_value() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
REFERENCE LABCollected on Jun 12, 2016 3:43 PM
- Example Screen, Urine Note: ng/mL 01
- See Final Results
- Reference Range: Cutoﬀ=20
Value
COMMENT
- Reference Range: Cutoﬀ=20
- Example Positive [A ] 01
- Reference Range: Cutoﬀ=20
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-bullets.pdf")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Example Screen, Urine"
    assert rows[0]["value"] is None
    assert rows[0]["value_text"] == "Positive"
    assert rows[0]["unit"] == "ng/mL"


def test_parse_labs_from_pdf_text_handles_mychart_prelabel_values_and_interval_units() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
Lab PanelCollected on Jun 05, 2016 10:00 AM
14
Alkaline Phosphatase
Normal range: 47 - 123 U/L
Please note. Reference range update to:
Age Male Female
White Blood Cells
Normal value: 3.40 - 10.80 K/uL
Value
8.15
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-ranges.pdf")

    assert len(rows) == 2
    assert rows[0]["test_name"] == "Alkaline phosphatase"
    assert rows[0]["value"] == 14.0
    assert rows[0]["value_text"] == "14"
    assert rows[0]["unit"] == "U/L"

    assert rows[1]["test_name"] == "White blood cell count"
    assert rows[1]["value"] == 8.15
    assert rows[1]["reference_range_low"] == 3.4
    assert rows[1]["reference_range_high"] == 10.8
    assert rows[1]["unit"] == "K/uL"


def test_parse_labs_from_pdf_text_strips_mychart_numeric_status_indicator() -> None:
    text = """
MyChart® licensed from Epic Systems Corporation
POC PanelCollected on Jun 05, 2016 10:00 AM
Sodium, POC
Normal range: 138 - 146 mmol/L
138138 146146
137Low
"""

    rows = parse_labs_from_pdf_text(text, person_id="dummy_bob", source_label="mychart-sodium.pdf")

    assert len(rows) == 1
    assert rows[0]["test_name"] == "Sodium, POC"
    assert rows[0]["value"] == 137.0
    assert rows[0]["value_text"] == "137"
    assert rows[0]["unit"] == "mmol/L"


def test_parse_labs_from_poct_extracts_qualitative_and_quantitative_rows() -> None:
    text = """
MyChart
POCT URINE DIPSTICK
Collected on Sep 13, 2015 2:26 AM
POCT Color, UA
Value
Yellow
POCT Glucose, UA
Value
Negative
POCT Spec Grav, UA
Value
1.025
POCT pH, UA
Value
6.0
POCT Urobilinogen, UA
Value
1.0 E.U./dL
"""
    rows = parse_labs_from_poct_text(text, person_id="dummy_alice", source_label="poct.pdf")

    assert len(rows) == 5

    assert rows[0]["test_name"] == "POCT Color, UA"
    assert rows[0]["value"] is None
    assert rows[0]["value_text"] == "Yellow"
    assert rows[0]["unit"] == ""

    assert rows[3]["test_name"] == "POCT pH, UA"
    assert rows[3]["value"] == 6.0
    assert rows[3]["unit"] == ""

    uro = rows[4]
    assert uro["test_name"] == "POCT Urobilinogen, UA"
    assert uro["value"] == 1.0
    assert uro["value_text"] == "1.0 E.U./dL"
    assert uro["unit"] == "E.U./dL"


def test_parse_labs_from_poct_skips_duplicate_template() -> None:
    text = """
MyChart
Collected on May 1, 2017
POCT Glucose, UA
Value
Negative
POCT Glucose, UA
Value
Normal
POCT Ketones, UA
Value
Negative
"""
    rows = parse_labs_from_poct_text(text, person_id="dummy_alice", source_label="poct.pdf")

    assert len(rows) == 2
    names = [r["test_name"] for r in rows]
    assert names.count("POCT Glucose, UA") == 1


def test_parse_labs_from_poct_requires_detectable_rows() -> None:
    text = """
MyChart
Collected on Sep 13, 2015
No POCT tests here.
"""
    try:
        parse_labs_from_poct_text(text, person_id="dummy_alice", source_label="no-tests.pdf")
    except LabSyncError as exc:
        assert "no POCT lab rows extracted" in str(exc)
    else:
        raise AssertionError("Expected LabSyncError")


def test_parse_labs_from_pdf_text_falls_back_to_poct_for_unrecognized_mychart() -> None:
    text = """
MyChart
POCT URINE DIPSTICK
Collected on Sep 13, 2015 2:26 AM
POCT Color, UA
Value
Yellow
POCT Glucose, UA
Value
Negative
POCT Spec Grav, UA
Value
1.025
"""
    rows = parse_labs_from_pdf_text(text, person_id="dummy_alice", source_label="sample.pdf")

    assert len(rows) == 3
    assert rows[0]["test_name"] == "POCT Color, UA"
    assert rows[0]["value_text"] == "Yellow"
    assert rows[2]["value"] == 1.025


def test_parse_labs_from_pdf_text_requires_detectable_rows() -> None:
    text = """
LabCorp
Collected: 04/20/2023
No structured rows here.
"""

    try:
        _ = parse_labs_from_pdf_text(text, person_id="dummy_alice", source_label="sample.pdf")
    except LabSyncError as exc:
        assert "no lab rows extracted" in str(exc)
    else:
        raise AssertionError("Expected LabSyncError when no structured lab rows can be parsed")


def test_infer_person_id_from_pdf_filename() -> None:
    assert infer_person_id_from_pdf_filename(Path("dummy_alice__quest-sample.pdf")) == "dummy_alice"
    assert infer_person_id_from_pdf_filename(Path("quest-sample.pdf")) is None


def test_infer_person_id_from_inbox_path() -> None:
    inbox_root = Path("/tmp/repo/data/inbox")
    path = Path("/tmp/repo/data/inbox/dummy_alice/labs/sample.csv")

    assert infer_person_id_from_inbox_path(path, inbox_root) == "dummy_alice"


def test_infer_person_id_from_processed_lab_path() -> None:
    processed_root = Path("/tmp/repo/data/processed")
    path = Path("/tmp/repo/data/processed/dummy_alice/Labs/2023-04-20-sample.csv")

    assert infer_person_id_from_processed_lab_path(path, processed_root) == "dummy_alice"


def test_deterministic_panel_note_path_is_stable() -> None:
    labs_dir = Path("/tmp/repo/famed-vault/Records/dummy_alice/Labs")

    first = deterministic_panel_note_path(labs_dir, "2023-04-20", "quest-sample")
    second = deterministic_panel_note_path(labs_dir, "2023-04-20", "quest-sample")

    assert first == second
    assert first.name == "2023-04-20-quest-sample-panel.md"


def test_deterministic_panel_note_path_does_not_duplicate_panel_suffix() -> None:
    labs_dir = Path("/tmp/repo/famed-vault/Records/dummy_alice/Labs")

    path = deterministic_panel_note_path(labs_dir, "2023-04-20", "Comprehensive Metabolic Panel")

    assert path.name == "2023-04-20-comprehensive-metabolic-panel.md"


def test_processed_lab_source_path_preserves_original_filename_stem() -> None:
    processed_root = Path("/tmp/repo/data/processed")
    source_file = Path("/tmp/repo/data/inbox/dummy_alice/labs/Example Report.PDF")

    path = processed_lab_source_path(
        processed_root, "dummy_alice", "2023-04-20T08:30:00", source_file
    )

    assert path == processed_root / "dummy_alice" / "Labs" / "2023-04-20-Example Report.pdf"


def test_processed_lab_source_path_strips_redundant_source_date_prefixes() -> None:
    processed_root = Path("/tmp/repo/data/processed")
    source_file = Path(
        "/tmp/repo/data/inbox/dummy_alice/labs/undated-210621 HEPATITIS C ANTIBODY.PDF"
    )

    path = processed_lab_source_path(
        processed_root, "dummy_alice", "2018-07-21T08:30:00", source_file
    )

    assert path == processed_root / "dummy_alice" / "Labs" / "2018-07-21-HEPATITIS C ANTIBODY.pdf"


def test_quarantined_lab_source_path_preserves_original_filename_stem() -> None:
    quarantine_root = Path("/tmp/repo/data/quarantine")
    source_file = Path("/tmp/repo/data/inbox/dummy_alice/labs/Example Report.PDF")

    path = quarantined_lab_source_path(quarantine_root, "dummy_alice", source_file)

    assert path == quarantine_root / "dummy_alice" / "Labs" / "undated-Example Report.pdf"


def test_source_path_preserves_clinically_meaningful_filename_components() -> None:
    """REQ-001: meaningful filename components survive relocation."""
    processed_root = Path("/tmp/repo/data/processed")
    quarantine_root = Path("/tmp/repo/data/quarantine")
    source_file = Path("/tmp/repo/data/inbox/dummy_alice/labs/2021-03-15_Example_CMP_Demo.pdf")

    processed = processed_lab_source_path(
        processed_root, "dummy_alice", "2023-04-20T08:30:00", source_file
    )
    assert processed.name == "2023-04-20-2021-03-15_Example_CMP_Demo.pdf"

    quarantined = quarantined_lab_source_path(quarantine_root, "dummy_alice", source_file)
    assert quarantined.name == "undated-2021-03-15_Example_CMP_Demo.pdf"


def test_source_stem_without_redundant_date_prefix_preserves_short_date_without_document_date() -> (
    None
):
    assert (
        source_stem_without_redundant_date_prefix("221031 visitSummary", None)
        == "221031 visitSummary"
    )


def test_source_stem_without_redundant_date_prefix_preserves_iso_date_without_document_date() -> (
    None
):
    assert (
        source_stem_without_redundant_date_prefix("2021-03-15 Example CMP Demo", None)
        == "2021-03-15 Example CMP Demo"
    )


def test_source_stem_without_redundant_date_prefix_strips_matching_iso_date() -> None:
    assert (
        source_stem_without_redundant_date_prefix("2021-03-15 Example CMP Demo", "2021-03-15")
        == "Example CMP Demo"
    )


def test_source_stem_without_redundant_date_prefix_strips_short_date_person_hint() -> None:
    assert (
        source_stem_without_redundant_date_prefix("251222m CBC W:DIFFERENTIAL", "2022-12-22")
        == "CBC W:DIFFERENTIAL"
    )


def test_retain_processed_source_avoids_overwriting_different_files(tmp_path: Path) -> None:
    source_file = tmp_path / "inbox" / "quest-report.csv"
    source_file.parent.mkdir()
    source_file.write_text("new content", encoding="utf-8")
    checksum = source_file_checksum(source_file)

    destination = tmp_path / "processed" / "dummy_alice" / "Labs" / "2023-04-20-quest-report.csv"
    destination.parent.mkdir(parents=True)
    destination.write_text("existing content", encoding="utf-8")
    destination.with_name(f"{destination.stem}-{checksum[:8]}{destination.suffix}").write_text(
        "another existing content",
        encoding="utf-8",
    )

    retained_path = retain_processed_source(source_file, destination, checksum)

    assert retained_path.name == f"2023-04-20-quest-report-{checksum[:8]}-2.csv"
    assert retained_path.read_text(encoding="utf-8") == "new content"
    assert destination.read_text(encoding="utf-8") == "existing content"
    assert not source_file.exists()


def test_quarantine_lab_source_for_import_avoids_overwriting_different_files(
    tmp_path: Path,
) -> None:
    root = tmp_path
    source_file = tmp_path / "data" / "inbox" / "dummy_alice" / "labs" / "quest-report.csv"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("new content", encoding="utf-8")
    checksum = source_file_checksum(source_file)

    destination = (
        tmp_path / "data" / "quarantine" / "dummy_alice" / "Labs" / "undated-quest-report.csv"
    )
    destination.parent.mkdir(parents=True)
    destination.write_text("existing content", encoding="utf-8")
    destination.with_name(f"{destination.stem}-{checksum[:8]}{destination.suffix}").write_text(
        "another existing content",
        encoding="utf-8",
    )

    _, quarantine_path = quarantine_lab_source_for_import(
        root,
        tmp_path / "data" / "quarantine",
        "dummy_alice",
        source_file,
    )

    retained_path = root / quarantine_path
    assert retained_path.name == f"undated-quest-report-{checksum[:8]}-2.csv"
    assert retained_path.read_text(encoding="utf-8") == "new content"
    assert destination.read_text(encoding="utf-8") == "existing content"
    assert not source_file.exists()


def test_load_labs_from_csv_uses_default_person_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "labs.csv"
    csv_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )

    rows = load_labs_from_csv(csv_path, default_person_id="dummy_alice")

    assert len(rows) == 1
    assert rows[0]["person_id"] == "dummy_alice"


def test_build_latest_lab_results_orders_by_latest_panel_table_order(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)

    older_rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-01-10",
                "value": "5.8",
                "unit": "%",
                "source_type": "csv",
            },
            "older-a1c",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Glucose",
                "observed_at": "2023-01-10",
                "value": "101",
                "unit": "mg/dL",
                "source_type": "csv",
            },
            "older-glucose",
        ),
    ]
    newer_rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Glucose",
                "observed_at": "2023-03-10",
                "value": "95",
                "unit": "mg/dL",
                "source_type": "csv",
            },
            "newer-glucose",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-03-10",
                "value": "5.6",
                "unit": "%",
                "source_type": "csv",
            },
            "newer-a1c",
        ),
    ]

    older_path = labs_dir / "2023-01-10-panel.md"
    newer_path = labs_dir / "2023-03-10-panel.md"
    older_path.write_text(render_lab_panel_note(older_rows), encoding="utf-8")
    newer_path.write_text(render_lab_panel_note(newer_rows), encoding="utf-8")

    latest_rows = build_latest_lab_results_from_notes([older_path, newer_path])

    # Newer panel's table lists Glucose before Hemoglobin A1c, so the latest
    # values are ordered to match that panel, not the order tests were ever
    # first seen historically (which would have put Hemoglobin A1c first).
    assert [row["test_name"] for row in latest_rows] == ["Glucose", "Hemoglobin A1c"]
    assert latest_rows[0]["value"] == 95.0
    assert latest_rows[0]["observed_at"] == "2023-03-10"
    assert latest_rows[1]["value"] == 5.6
    assert latest_rows[1]["observed_at"] == "2023-03-10"


def test_build_latest_lab_results_combines_same_day_multi_panel_in_panel_order(
    tmp_path: Path,
) -> None:
    labs_dir = tmp_path / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)

    morning_panel = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "WBC",
                "observed_at": "2023-04-01T08:00:00",
                "value": "6.2",
                "unit": "K/uL",
                "source_type": "csv",
            },
            "morning-wbc",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "RBC",
                "observed_at": "2023-04-01T08:00:00",
                "value": "4.8",
                "unit": "M/uL",
                "source_type": "csv",
            },
            "morning-rbc",
        ),
    ]
    afternoon_panel = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "TSH",
                "observed_at": "2023-04-01T14:00:00",
                "value": "2.1",
                "unit": "uIU/mL",
                "source_type": "csv",
            },
            "afternoon-tsh",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "T4",
                "observed_at": "2023-04-01T14:00:00",
                "value": "1.2",
                "unit": "ng/dL",
                "source_type": "csv",
            },
            "afternoon-t4",
        ),
    ]
    morning_path = labs_dir / "2023-04-01-cbc-panel.md"
    afternoon_path = labs_dir / "2023-04-01-thyroid-panel.md"
    morning_path.write_text(render_lab_panel_note(morning_panel), encoding="utf-8")
    afternoon_path.write_text(render_lab_panel_note(afternoon_panel), encoding="utf-8")

    latest_rows = build_latest_lab_results_from_notes([morning_path, afternoon_path])

    # Both panels share a calendar date but different draw times; each panel's
    # internal table order is preserved, with the earlier-drawn panel first.
    assert [row["test_name"] for row in latest_rows] == [
        "White blood cell count",
        "RBC",
        "TSH",
        "T4",
    ]


def test_write_lab_conflicts_report_writes_report_and_updates_index(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    reports_dir.mkdir(parents=True)

    csv_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.csv"
    pdf_source = "data/processed/dummy_alice/Labs/2023-04-20-panel.pdf"
    replace_lab_results_for_source_path(conn, csv_source, [_make_lab(value="5.8")])
    replace_lab_results_for_source_path(conn, pdf_source, [_make_lab(value="6.1")])

    report_path, conflict_count = write_lab_conflicts_report(conn, vault_root, "dummy_alice")
    report_content = report_path.read_text(encoding="utf-8")
    reports_index_content = (reports_dir / "_index.md").read_text(encoding="utf-8")

    assert report_path.name == "lab-conflicts.md"
    assert conflict_count == 2
    assert "report_type: lab_conflicts" in report_content
    assert "| Hemoglobin A1c | 5.8 |" in report_content
    assert "| Hemoglobin A1c | 6.1 |" in report_content
    assert "[[lab-conflicts|Lab conflicts]]" in reports_index_content


def test_write_lab_conflicts_report_writes_empty_state(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    report_path, conflict_count = write_lab_conflicts_report(conn, tmp_path, "dummy_alice")
    report_content = report_path.read_text(encoding="utf-8")

    assert conflict_count == 0
    assert "_No conflicting lab values found._" in report_content


# ---------------------------------------------------------------------------
# write_lab_panel_note — idempotent note writes
# ---------------------------------------------------------------------------


def _make_panel_labs(person_id: str = "dummy_alice") -> list[dict[str, Any]]:
    return [
        lab_payload_from_mapping(
            {
                "person_id": person_id,
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-01",
                "value": "5.5",
                "unit": "%",
                "source_type": "csv",
            },
            "test",
        )
    ]


def test_write_lab_panel_note_creates_note_when_absent(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-01-panel.md"
    write_lab_panel_note(note_path, _make_panel_labs())
    assert note_path.exists()
    content = note_path.read_text(encoding="utf-8")
    assert "Hemoglobin A1c" in content
    assert "## Notes" in content


def test_write_lab_panel_note_preserves_user_notes_on_rewrite(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-01-panel.md"
    # First write
    write_lab_panel_note(note_path, _make_panel_labs())

    # User adds a note
    original = note_path.read_text(encoding="utf-8")
    edited = original.replace("- Edit table values directly", "- MY CUSTOM NOTE")
    note_path.write_text(edited, encoding="utf-8")

    # Re-import with updated value
    updated_labs = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "Hemoglobin A1c",
                "observed_at": "2023-04-01",
                "value": "5.9",
                "unit": "%",
                "source_type": "csv",
            },
            "test",
        )
    ]
    write_lab_panel_note(note_path, updated_labs)

    result = note_path.read_text(encoding="utf-8")
    # Updated value is present in the Results table
    assert "5.9" in result
    # User note is preserved
    assert "MY CUSTOM NOTE" in result


def test_write_lab_panel_note_overwrites_when_no_notes_section(tmp_path: Path) -> None:
    note_path = tmp_path / "2023-04-01-panel.md"
    # Write a bare note with no ## Notes section
    note_path.write_text(
        "---\nperson_id: dummy_alice\n---\n\n# Lab Panel\n\n## Results\n\n", encoding="utf-8"
    )

    write_lab_panel_note(note_path, _make_panel_labs())

    content = note_path.read_text(encoding="utf-8")
    assert "Hemoglobin A1c" in content


# ---------------------------------------------------------------------------
# write_labs_index_note
# ---------------------------------------------------------------------------


def test_write_labs_index_note_creates_index_with_links(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    (labs_dir / "2023-01-10-panel-a.md").write_text(
        "---\nprojection_role: source_evidence\n---\n\n# A", encoding="utf-8"
    )
    (labs_dir / "2023-03-05-panel-b.md").write_text("# B", encoding="utf-8")
    (labs_dir / "_template.md").write_text("# template", encoding="utf-8")

    index_path = write_labs_index_note(labs_dir, "dummy_alice", display_name="Dummy Alice")

    content = index_path.read_text(encoding="utf-8")
    assert index_path.name == "_index.md"
    assert "[[2023-01-10-panel-a]]" in content
    assert "[[2023-01-10-panel-a]] — source evidence" in content
    assert "[[2023-03-05-panel-b]]" in content
    # Template file must not appear
    assert "_template" not in content
    # Links back to person hub using display name
    assert "[[dummy_alice|← Dummy Alice]]" in content
    assert "# Labs — Dummy Alice" in content
    # No lab-results report yet: the notice says so instead of a dangling link.
    assert "No current lab results are recorded." in content
    (tmp_path / "Reports").mkdir()
    (tmp_path / "Reports" / "lab-results.md").write_text("# Lab Results\n", encoding="utf-8")
    content = write_labs_index_note(labs_dir, "dummy_alice").read_text(encoding="utf-8")
    assert "[[Records/dummy_alice/Reports/lab-results|Lab Results]]" in content


def test_write_labs_index_note_regenerates_on_second_call(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    (labs_dir / "2023-01-10-panel-a.md").write_text("# A", encoding="utf-8")

    write_labs_index_note(labs_dir, "dummy_alice")
    # Add a second note, regenerate
    (labs_dir / "2023-04-20-panel-b.md").write_text("# B", encoding="utf-8")
    index_path = write_labs_index_note(labs_dir, "dummy_alice")

    content = index_path.read_text(encoding="utf-8")
    assert "[[2023-04-20-panel-b]]" in content


def test_write_labs_index_note_empty_labs_dir(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    index_path = write_labs_index_note(labs_dir, "dummy_alice")
    content = index_path.read_text(encoding="utf-8")
    assert "_No lab notes yet._" in content


def test_write_category_index_note_supports_non_lab_categories(tmp_path: Path) -> None:
    history_dir = tmp_path / "History"
    history_dir.mkdir()
    (history_dir / "2023-04-20-primary-care-follow-up.md").write_text("# Visit", encoding="utf-8")

    index_path = write_category_index_note(
        history_dir, "dummy_alice", "History", display_name="Dummy Alice"
    )

    content = index_path.read_text(encoding="utf-8")
    assert index_path.name == "_index.md"
    assert "record_type: history_index" in content
    assert "# History — Dummy Alice" in content
    assert "[[2023-04-20-primary-care-follow-up]]" in content
    assert "[[dummy_alice|← Dummy Alice]]" in content


def test_write_category_index_note_falls_back_to_person_id_when_no_display_name(
    tmp_path: Path,
) -> None:
    history_dir = tmp_path / "History"
    history_dir.mkdir()

    index_path = write_category_index_note(history_dir, "dummy_alice", "History")

    content = index_path.read_text(encoding="utf-8")
    assert "# History — dummy_alice" in content
    assert "[[dummy_alice|← dummy_alice]]" in content


def test_write_category_index_note_does_not_link_template_person_placeholder(
    tmp_path: Path,
) -> None:
    reports_dir = tmp_path / "Reports"
    reports_dir.mkdir()
    (reports_dir / "medical-history-summary.md").write_text("# Summary\n", encoding="utf-8")

    index_path = write_category_index_note(reports_dir, "_person_id", "Reports")

    content = index_path.read_text(encoding="utf-8")
    assert "# Reports — _person_id" in content
    assert "← _person_id" in content
    assert "[[_person_id" not in content
    assert "[[medical-history-summary|Medical history summary]]" in content


# ---------------------------------------------------------------------------
# find_overlapping_panel_note
# ---------------------------------------------------------------------------


def _write_panel_note(
    labs_dir: Path,
    filename: str,
    test_names: list[str],
    date: str = "2023-04-20",
) -> Path:
    note_path = labs_dir / filename
    # Use unit-free test names to avoid unit-mismatch validation errors in the helper.
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": t,
                "observed_at": date,
                "value": "1.0",
                "unit": "",
                "source_type": "csv",
            },
            "test",
        )
        for t in test_names
    ]
    write_lab_panel_note(note_path, rows)
    return note_path


def test_find_overlapping_panel_note_returns_existing_note(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    _write_panel_note(labs_dir, "2023-04-20-quest-panel.md", ["Hemoglobin A1c", "Glucose"])

    result = find_overlapping_panel_note(labs_dir, "2023-04-20", frozenset(["Hemoglobin A1c"]))

    assert result == labs_dir / "2023-04-20-quest-panel.md"


def test_find_overlapping_panel_note_returns_none_when_absent(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()

    result = find_overlapping_panel_note(labs_dir, "2023-04-20", frozenset(["Hemoglobin A1c"]))

    assert result is None


def test_find_overlapping_panel_note_returns_none_for_empty_test_names(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    _write_panel_note(labs_dir, "2023-04-20-quest-panel.md", ["Hemoglobin A1c"])

    result = find_overlapping_panel_note(labs_dir, "2023-04-20", frozenset())

    assert result is None


def test_find_overlapping_panel_note_requires_test_name_overlap(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    _write_panel_note(labs_dir, "2023-04-20-cbc-panel.md", ["Hemoglobin", "Platelet count"])

    result = find_overlapping_panel_note(labs_dir, "2023-04-20", frozenset(["Glucose", "Sodium"]))

    assert result is None


def test_find_overlapping_panel_note_ignores_index_notes(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    (labs_dir / "_index.md").write_text("# index", encoding="utf-8")

    result = find_overlapping_panel_note(labs_dir, "2023-04-20", frozenset(["Hemoglobin A1c"]))

    assert result is None


def test_find_overlapping_panel_note_skips_wrong_date(tmp_path: Path) -> None:
    labs_dir = tmp_path / "Labs"
    labs_dir.mkdir()
    _write_panel_note(labs_dir, "2023-03-01-quest-panel.md", ["Hemoglobin A1c"])

    result = find_overlapping_panel_note(labs_dir, "2023-04-20", frozenset(["Hemoglobin A1c"]))

    assert result is None


# ---------------------------------------------------------------------------
# merge_lab_panel_rows
# ---------------------------------------------------------------------------


def _make_row(test_name: str, value: float, date: str = "2023-04-20") -> dict[str, Any]:
    return lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": test_name,
            "observed_at": date,
            "value": str(value),
            "unit": "",
            "source_type": "csv",
        },
        "test",
    )


def test_merge_lab_panel_rows_combines_non_overlapping_rows() -> None:
    existing = [_make_row("Hemoglobin A1c", 5.5)]
    new = [_make_row("Glucose", 96.0)]

    result = merge_lab_panel_rows(existing, new)

    assert len(result) == 2
    names = [r["test_name"] for r in result]
    assert "Hemoglobin A1c" in names
    assert "Glucose" in names


def test_merge_lab_panel_rows_deduplicates_exact_duplicates() -> None:
    row = _make_row("Hemoglobin A1c", 5.5)
    result = merge_lab_panel_rows([row], [row])

    assert len(result) == 1


def test_merge_lab_panel_rows_prefers_more_complete_duplicate() -> None:
    existing = [
        {
            **_make_row("Triglycerides", 123.0),
            "loinc_code": "",
        }
    ]
    new = [
        {
            **_make_row("Triglycerides", 123.0),
            "loinc_code": "108346-8",
        }
    ]

    result = merge_lab_panel_rows(existing, new)

    assert len(result) == 1
    assert result[0]["loinc_code"] == "108346-8"


def test_merge_lab_panel_rows_prefers_source_provided_on_completeness_tie() -> None:
    """Regression: found while triaging the orphan lab-note
    backlog. A note-loaded existing row and an incoming source_provided row
    can tie on completeness (both have loinc_code/unit, same value) despite
    reporting different LOINC codes for the same fact -- e.g. an old PDF's
    inferred code vs a later C-CDA's own authoritative code. On a tie, the
    source_provided row must win; note-loaded existing rows never carry
    loinc_code_origin at all, so this only ever prefers a newly-arriving,
    explicitly authoritative row over an old row of unknown provenance.
    """
    existing = [
        {
            **_make_row("Sodium", 142.0),
            "loinc_code": "111263-0",
            "unit": "mmol/L",
        }
    ]
    new = [
        {
            **_make_row("Sodium", 142.0),
            "loinc_code": "2951-2",
            "unit": "mmol/L",
            "loinc_code_origin": "source_provided",
        }
    ]

    result = merge_lab_panel_rows(existing, new)

    assert len(result) == 1
    assert result[0]["loinc_code"] == "2951-2"


def test_merge_lab_panel_rows_tie_break_does_not_override_completeness() -> None:
    """The source_provided tie-break only applies to exact completeness ties
    -- it must not let a less-complete source_provided row beat a more
    complete existing row."""
    existing = [
        {
            **_make_row("Sodium", 142.0),
            "loinc_code": "111263-0",
            "unit": "mmol/L",
            "reference_range_low": 134.0,
            "reference_range_high": 144.0,
        }
    ]
    new = [
        {
            **_make_row("Sodium", 142.0),
            "loinc_code": "2951-2",
            "unit": "mmol/L",
            "loinc_code_origin": "source_provided",
        }
    ]

    result = merge_lab_panel_rows(existing, new)

    assert len(result) == 1
    assert result[0]["loinc_code"] == "111263-0"


def test_merge_lab_panel_rows_preserves_conflicting_values() -> None:
    row_a = _make_row("Hemoglobin A1c", 5.5)
    row_b = _make_row("Hemoglobin A1c", 5.9)

    result = merge_lab_panel_rows([row_a], [row_b])

    assert len(result) == 2
    values = {r["value"] for r in result}
    assert values == {5.5, 5.9}


def test_merge_lab_panel_rows_preserves_existing_order() -> None:
    existing = [_make_row("Hemoglobin A1c", 5.5), _make_row("Glucose", 96.0)]
    new = [_make_row("TSH", 2.1), _make_row("Glucose", 96.0)]

    result = merge_lab_panel_rows(existing, new)

    assert len(result) == 3
    assert result[0]["test_name"] == "Hemoglobin A1c"
    assert result[1]["test_name"] == "Glucose"
    assert result[2]["test_name"] == "TSH"


def test_merge_lab_panel_rows_drops_date_only_duplicate_of_timestamped_row() -> None:
    date_only = _make_row("AST", 20.0, date="2019-08-24")
    timestamped = {
        **_make_row("AST", 20.0, date="2019-08-24T04:33:00"),
        "unit": "U/L",
        "loinc_code": "1920-8",
    }

    result = merge_lab_panel_rows([date_only], [timestamped])

    assert len(result) == 1
    assert result[0]["observed_at"] == "2019-08-24T04:33:00"
    assert result[0]["unit"] == "U/L"


def test_merge_lab_panel_rows_keeps_distinct_full_timestamp_rows_same_day() -> None:
    morning = _make_row("Glucose", 95.0, date="2023-04-20T08:00:00")
    afternoon = _make_row("Glucose", 95.0, date="2023-04-20T16:00:00")

    result = merge_lab_panel_rows([morning], [afternoon])

    assert len(result) == 2


def test_persist_lab_rows_replaces_source_less_overlap_on_retained_reprocess(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    vault_root = tmp_path / "vault"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)
    archive_note_path = labs_dir / "2021-10-09-archive-panel.md"
    write_lab_panel_note(
        archive_note_path,
        [
            lab_payload_from_mapping(
                {
                    "person_id": "dummy_alice",
                    "test_name": "C-Reactive Protein, Cardiac - LC",
                    "loinc_code": "30522-7",
                    "value": "7.09",
                    "unit": "mg/L",
                    "reference_range_low": "0.0",
                    "reference_range_high": "3.0",
                    "observed_at": "2021-10-10",
                    "source_type": "source_import",
                },
                "archive",
            )
        ],
        source_file_name="HealthSummary.zip#DOC0011.XML",
        source_path=(
            "data/processed/dummy_alice/HealthSummary/"
            "HealthSummary.zip#IHE_XDM/Dummy Alice/DOC0011.XML"
        ),
    )
    note_path = labs_dir / "2021-10-09-mychart-test-details-12-panel.md"
    note_path.write_text(
        """---
person_id: dummy_alice
record_type: lab_panel
observed_at: '2021-10-09'
source_type: pdf:mychart
source_file_name: MyChart - Test Details 12.pdf
---

# Lab Panel — 2021-10-09

## Results

| test_name | loinc_code | value | unit | reference_range_low | reference_range_high | observed_at |
| --- | --- | --- | --- | --- | --- | --- |
| C-Reactive Protein, Cardiac - LC | 105126-7 | Low <1.00 | mg/L | 0.0 | 3.0 | 2021-10-09 |
""",
        encoding="utf-8",
    )
    replace_lab_results_for_source_path(
        conn,
        "Records/dummy_alice/Labs/2021-10-09-mychart-test-details-12-panel.md",
        load_labs_from_note(note_path),
    )

    source_file = tmp_path / "2021-10-09-HIGH SENSITIVITY CRP.pdf"
    source_file.write_text("stub", encoding="utf-8")
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "C-Reactive Protein, Cardiac - LC",
                "loinc_code": "30522-7",
                "value": "7.09",
                "value_text": "7.09",
                "unit": "mg/L",
                "reference_range_low": "0.0",
                "reference_range_high": "3.0",
                "observed_at": "2021-10-09",
                "source_type": "pdf:mychart",
            },
            "test",
        )
    ]

    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=source_file,
        processed_source_path="data/processed/dummy_alice/Labs/2021-10-09-HIGH SENSITIVITY CRP.pdf",
        checksum="abc123",
        source_type="pdf:mychart",
        rows=rows,
        retained_source=True,
        source_file_name="HIGH SENSITIVITY CRP.pdf",
        refresh_reports=False,
    )

    content = note_path.read_text(encoding="utf-8")
    assert "Low <1.00" not in content
    assert (
        "source_path: data/processed/dummy_alice/Labs/2021-10-09-HIGH SENSITIVITY CRP.pdf"
        in content
    )
    assert (
        "| C-Reactive Protein, Cardiac | 30522-7 | 7.09 | mg/L | 0.0 | 3.0 | 2021-10-09 |"
        in content
    )
    archive_content = archive_note_path.read_text(encoding="utf-8")
    assert (
        "| C-Reactive Protein, Cardiac | 30522-7 | 7.09 | mg/L | 0.0 | 3.0 | 2021-10-10 |"
        in archive_content
    )
    migrated_rows = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?",
        ("Records/dummy_alice/Labs/2021-10-09-mychart-test-details-12-panel.md",),
    ).fetchone()
    assert migrated_rows == (0,)


def test_persist_lab_rows_does_not_claim_source_less_note_with_foreign_rows(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    vault_root = tmp_path / "vault"
    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    labs_dir.mkdir(parents=True)
    note_path = labs_dir / "2021-10-09-mychart-test-details-12-panel.md"
    note_path.write_text(
        """---
person_id: dummy_alice
record_type: lab_panel
observed_at: '2021-10-09'
source_type: pdf:mychart
source_file_name: MyChart - Test Details 12.pdf
---

# Lab Panel — 2021-10-09

## Results

| test_name | loinc_code | value | unit | reference_range_low | reference_range_high | observed_at |
| --- | --- | --- | --- | --- | --- | --- |
| C-Reactive Protein, Cardiac - LC | 105126-7 | Low <1.00 | mg/L | 0.0 | 3.0 | 2021-10-09 |
| Hemoglobin A1c | 4548-4 | 5.4 | % | 4.0 | 5.6 | 2021-10-09 |
""",
        encoding="utf-8",
    )
    replace_lab_results_for_source_path(
        conn,
        "Records/dummy_alice/Labs/2021-10-09-mychart-test-details-12-panel.md",
        load_labs_from_note(note_path),
    )

    source_file = tmp_path / "2021-10-09-HIGH SENSITIVITY CRP.pdf"
    source_file.write_text("stub", encoding="utf-8")
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_alice",
                "test_name": "C-Reactive Protein, Cardiac - LC",
                "loinc_code": "30522-7",
                "value": "7.09",
                "value_text": "7.09",
                "unit": "mg/L",
                "reference_range_low": "0.0",
                "reference_range_high": "3.0",
                "observed_at": "2021-10-09",
                "source_type": "pdf:mychart",
            },
            "test",
        )
    ]

    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=source_file,
        processed_source_path="data/processed/dummy_alice/Labs/2021-10-09-HIGH SENSITIVITY CRP.pdf",
        checksum="abc123",
        source_type="pdf:mychart",
        rows=rows,
        retained_source=True,
        source_file_name="HIGH SENSITIVITY CRP.pdf",
        refresh_reports=False,
    )

    # The source-less note keeps its unrelated row and stays source-less:
    # the new source didn't produce that row, so it must not be reattributed.
    content = note_path.read_text(encoding="utf-8")
    assert "source_path:" not in content
    assert "Hemoglobin A1c" in content
    assert "Low <1.00" in content

    # The retained-source row lands in its own, separately-attributed note.
    new_note_path = labs_dir / "2021-10-09-high-sensitivity-crp-panel.md"
    assert new_note_path.exists()
    new_content = new_note_path.read_text(encoding="utf-8")
    assert (
        "source_path: data/processed/dummy_alice/Labs/2021-10-09-HIGH SENSITIVITY CRP.pdf"
        in new_content
    )
    assert (
        "| C-Reactive Protein, Cardiac | 30522-7 | 7.09 | mg/L | 0.0 | 3.0 | 2021-10-09 |"
        in new_content
    )

    # The old note's lab_results rows were not deleted or re-attributed.
    old_rows = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?",
        ("Records/dummy_alice/Labs/2021-10-09-mychart-test-details-12-panel.md",),
    ).fetchone()
    assert old_rows == (2,)


@pytest.mark.parametrize("reverse_order", [False, True])
def test_persist_lab_rows_keeps_overlapping_retained_sources_in_separate_evidence_notes(
    tmp_path: Path,
    reverse_order: bool,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    rows_by_source = (
        (
            tmp_path / "2023-04-20-source-a.pdf",
            "data/processed/dummy_alice/Labs/2023-04-20-source-a.pdf",
            "Source A.pdf",
            "5.4",
        ),
        (
            tmp_path / "2023-04-20-source-b.pdf",
            "data/processed/dummy_alice/Labs/2023-04-20-source-b.pdf",
            "Source B.pdf",
            "5.4",
        ),
    )

    ordered_sources = tuple(reversed(rows_by_source)) if reverse_order else rows_by_source
    for source_file, source_path, source_name, value in ordered_sources:
        source_file.write_text("synthetic", encoding="utf-8")
        rows = [
            lab_payload_from_mapping(
                {
                    "person_id": "dummy_alice",
                    "test_name": "Synthetic Analyte",
                    "loinc_code": "1234-5",
                    "value": value,
                    "unit": "%",
                    "reference_range_low": "4.0",
                    "reference_range_high": "5.6",
                    "observed_at": "2023-04-20",
                    "source_type": "pdf:synthetic",
                },
                source_name,
            )
        ]
        import_labs.persist_lab_rows(
            conn=conn,
            vault_root=vault_root,
            person_id="dummy_alice",
            source_file=source_file,
            processed_source_path=source_path,
            checksum=f"checksum-{source_file.stem}",
            source_type="pdf:synthetic",
            rows=rows,
            retained_source=True,
            source_file_name=source_name,
            refresh_reports=False,
        )

    labs_dir = vault_root / "Records" / "dummy_alice" / "Labs"
    source_notes = sorted(path for path in labs_dir.glob("*-panel.md"))
    assert [path.name for path in source_notes] == [
        "2023-04-20-source-a-panel.md",
        "2023-04-20-source-b-panel.md",
    ]
    for note_path, (_, source_path, _, _) in zip(source_notes, rows_by_source, strict=True):
        front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        assert front_matter["source_path"] == source_path
        assert front_matter["projection_role"] == "source_evidence"
        assert len(load_labs_from_note(note_path)) == 1


def test_persist_lab_rows_splits_multi_date_batch_into_per_date_notes(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    vault_root = tmp_path / "vault"
    labs_dir = vault_root / "Records" / "dummy_bob" / "Labs"

    source_file = tmp_path / "comprehensive-report.xml"
    source_file.write_text("stub", encoding="utf-8")
    processed_source_path = "data/processed/dummy_bob/HealthSummary/archive.zip#DOC0001.XML"
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Glucose",
                "value": "95",
                "value_text": "95",
                "unit": "mg/dL",
                "observed_at": "2020-01-01",
                "source_type": "xml",
            },
            "test",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Sodium",
                "value": "140",
                "value_text": "140",
                "unit": "mmol/L",
                "observed_at": "2020-06-15",
                "source_type": "xml",
            },
            "test",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Hemoglobin A1c",
                "value": "5.6",
                "value_text": "5.6",
                "unit": "%",
                "observed_at": "2021-02-20",
                "source_type": "xml",
            },
            "test",
        ),
    ]

    note_paths = import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_bob",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="abc123",
        source_type="xml",
        rows=rows,
        retained_source=True,
        source_file_name="archive.zip#DOC0001.XML",
        refresh_reports=False,
    )

    assert {p.name for p in note_paths} == {
        "2020-01-01-comprehensive-report-panel.md",
        "2020-06-15-comprehensive-report-panel.md",
        "2021-02-20-comprehensive-report-panel.md",
    }

    for note_name, expected_date, expected_test in [
        ("2020-01-01-comprehensive-report-panel.md", "2020-01-01", "Glucose"),
        ("2020-06-15-comprehensive-report-panel.md", "2020-06-15", "Sodium"),
        ("2021-02-20-comprehensive-report-panel.md", "2021-02-20", "Hemoglobin A1c"),
    ]:
        note_path = labs_dir / note_name
        note_rows = load_labs_from_note(note_path)
        assert [r["observed_at"] for r in note_rows] == [expected_date]
        assert note_rows[0]["test_name"] == expected_test
        front_matter = yaml.safe_load(note_path.read_text(encoding="utf-8").split("---")[1])
        assert front_matter["source_path"] == processed_source_path

    db_rows = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?",
        (processed_source_path,),
    ).fetchone()
    assert db_rows == (3,)


def test_persist_lab_rows_self_heals_existing_multi_date_catch_all_note(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    vault_root = tmp_path / "vault"
    labs_dir = vault_root / "Records" / "dummy_bob" / "Labs"
    labs_dir.mkdir(parents=True)

    processed_source_path = "data/processed/dummy_bob/HealthSummary/archive.zip#DOC0001.XML"
    catch_all_note_path = labs_dir / "2020-01-01-comprehensive-report-panel.md"
    write_lab_panel_note(
        catch_all_note_path,
        [
            lab_payload_from_mapping(
                {
                    "person_id": "dummy_bob",
                    "test_name": "Glucose",
                    "value": "95",
                    "value_text": "95",
                    "unit": "mg/dL",
                    "observed_at": "2020-01-01",
                    "source_type": "xml",
                },
                "test",
            ),
            lab_payload_from_mapping(
                {
                    "person_id": "dummy_bob",
                    "test_name": "Sodium",
                    "value": "140",
                    "value_text": "140",
                    "unit": "mmol/L",
                    "observed_at": "2020-06-15",
                    "source_type": "xml",
                },
                "test",
            ),
        ],
        source_file_name="archive.zip#DOC0001.XML",
        source_path=processed_source_path,
    )
    replace_lab_results_for_source_path(
        conn, processed_source_path, load_labs_from_note(catch_all_note_path)
    )

    # Reprocessing the same retained source with the fixed grouping should
    # leave the 2020-01-01 note containing only its own date's row and move
    # the 2020-06-15 row into its own per-date note.
    source_file = tmp_path / "comprehensive-report.xml"
    source_file.write_text("stub", encoding="utf-8")
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Glucose",
                "value": "95",
                "value_text": "95",
                "unit": "mg/dL",
                "observed_at": "2020-01-01",
                "source_type": "xml",
            },
            "test",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Sodium",
                "value": "140",
                "value_text": "140",
                "unit": "mmol/L",
                "observed_at": "2020-06-15",
                "source_type": "xml",
            },
            "test",
        ),
    ]

    note_paths = import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_bob",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="abc123",
        source_type="xml",
        rows=rows,
        retained_source=True,
        source_file_name="archive.zip#DOC0001.XML",
        refresh_reports=False,
    )

    assert len(note_paths) == 2

    catch_all_rows = load_labs_from_note(catch_all_note_path)
    assert [r["observed_at"] for r in catch_all_rows] == ["2020-01-01"]
    assert catch_all_rows[0]["test_name"] == "Glucose"

    split_note_path = labs_dir / "2020-06-15-comprehensive-report-panel.md"
    assert split_note_path.exists()
    split_rows = load_labs_from_note(split_note_path)
    assert [r["observed_at"] for r in split_rows] == ["2020-06-15"]
    assert split_rows[0]["test_name"] == "Sodium"


def test_persist_lab_rows_migrates_source_less_multi_date_note_per_date_group(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    vault_root = tmp_path / "vault"
    labs_dir = vault_root / "Records" / "dummy_bob" / "Labs"
    labs_dir.mkdir(parents=True)

    old_note_path = labs_dir / "2020-01-01-archive-panel.md"
    write_lab_panel_note(
        old_note_path,
        [
            lab_payload_from_mapping(
                {
                    "person_id": "dummy_bob",
                    "test_name": "Glucose",
                    "value": "95",
                    "value_text": "95",
                    "unit": "mg/dL",
                    "observed_at": "2020-01-01",
                    "source_type": "xml",
                },
                "test",
            ),
            lab_payload_from_mapping(
                {
                    "person_id": "dummy_bob",
                    "test_name": "Sodium",
                    "value": "140",
                    "value_text": "140",
                    "unit": "mmol/L",
                    "observed_at": "2020-06-15",
                    "source_type": "xml",
                },
                "test",
            ),
        ],
        source_file_name="archive.zip#DOC0001.XML",
        # No source_path: this note is source-less, as legacy catch-all notes are.
    )
    old_note_source_path = str(old_note_path.relative_to(vault_root))
    replace_lab_results_for_source_path(
        conn, old_note_source_path, load_labs_from_note(old_note_path)
    )

    source_file = tmp_path / "archive-export.xml"
    source_file.write_text("stub", encoding="utf-8")
    processed_source_path = "data/processed/dummy_bob/HealthSummary/archive.zip#DOC0001.XML"
    rows = [
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Glucose",
                "value": "95",
                "value_text": "95",
                "unit": "mg/dL",
                "observed_at": "2020-01-01",
                "source_type": "xml",
            },
            "test",
        ),
        lab_payload_from_mapping(
            {
                "person_id": "dummy_bob",
                "test_name": "Sodium",
                "value": "140",
                "value_text": "140",
                "unit": "mmol/L",
                "observed_at": "2020-06-15",
                "source_type": "xml",
            },
            "test",
        ),
    ]

    note_paths = import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_bob",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="abc123",
        source_type="xml",
        rows=rows,
        retained_source=True,
        source_file_name="archive.zip#DOC0001.XML",
        refresh_reports=False,
    )

    assert len(note_paths) == 2

    old_note_rows = load_labs_from_note(old_note_path)
    assert [r["observed_at"] for r in old_note_rows] == ["2020-01-01"]
    old_front_matter = yaml.safe_load(old_note_path.read_text(encoding="utf-8").split("---")[1])
    assert old_front_matter["source_path"] == processed_source_path

    new_note_path = labs_dir / "2020-06-15-archive-export-panel.md"
    assert new_note_path.exists()
    new_note_rows = load_labs_from_note(new_note_path)
    assert [r["observed_at"] for r in new_note_rows] == ["2020-06-15"]
    new_front_matter = yaml.safe_load(new_note_path.read_text(encoding="utf-8").split("---")[1])
    assert new_front_matter["source_path"] == processed_source_path

    # The old source-less note's rows were re-attributed to processed_source_path,
    # not left orphaned under its own note path.
    old_path_rows = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?",
        (old_note_source_path,),
    ).fetchone()
    assert old_path_rows == (0,)
    new_path_rows = conn.execute(
        "SELECT COUNT(*) FROM lab_results WHERE source_path = ?",
        (processed_source_path,),
    ).fetchone()
    assert new_path_rows == (2,)


def test_source_provided_code_beats_inferred_direct_restatement() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    archive_lab = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "C-Reactive Protein, Cardiac - LC",
            "loinc_code": "30522-7",
            "value": "7.09",
            "unit": "mg/L",
            "reference_range_low": "0.0",
            "reference_range_high": "3.0",
            "observed_at": "2021-10-09",
            "source_type": "source_import",
            "source_path": (
                "data/processed/dummy_alice/HealthSummary/"
                "HealthSummary.zip#IHE_XDM/Dummy Alice/DOC0011.XML"
            ),
        },
        "archive",
    )
    upsert_lab_result(conn, archive_lab)

    direct_lab = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "C-Reactive Protein, Cardiac - LC",
            "value": "7.09",
            "unit": "mg/L",
            "reference_range_low": "0.0",
            "reference_range_high": "3.0",
            "observed_at": "2021-10-09",
            "source_type": "pdf:mychart",
        },
        "direct",
    )
    inserted, skipped = replace_lab_results_for_source_path(
        conn,
        "data/processed/dummy_alice/Labs/2021-10-09-HIGH SENSITIVITY CRP.pdf",
        [direct_lab],
    )

    assert inserted == 0
    assert skipped == 1
    rows = conn.execute(
        "SELECT source_path, value FROM lab_results "
        "WHERE person_id = 'dummy_alice' AND test_name = 'C-Reactive Protein, Cardiac' "
        "ORDER BY source_path"
    ).fetchall()
    assert rows == [
        (
            (
                "data/processed/dummy_alice/HealthSummary/"
                "HealthSummary.zip#IHE_XDM/Dummy Alice/DOC0011.XML"
            ),
            7.09,
        )
    ]


def test_reprocess_retained_pdf_fills_loinc_codes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    processed_root = data_dir / "processed"
    reference_dir = data_dir / "reference"
    processed_labs_dir = processed_root / "dummy_alice" / "Labs"
    processed_labs_dir.mkdir(parents=True)
    reference_dir.mkdir(parents=True)

    reference_dir.joinpath("loinc").mkdir()
    reference_dir.joinpath("loinc", "loinc_concepts.jsonl").write_text(
        '{"loinc_num":"108346-8","component":"Triglycerides","long_common_name":"Triglycerides"}\n',
        encoding="utf-8",
    )

    pdf_path = processed_labs_dir / "2021-10-09-NMR LIPO.pdf"
    pdf_path.write_text("stub", encoding="utf-8")
    vault_root = tmp_path / "vault"
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    original_extract = import_labs.extract_pdf_text
    original_parse = import_labs.parse_labs_from_pdf_text
    original_review = import_labs.review_pdf_extraction

    def fake_extract_pdf_text(path: Path) -> str:
        assert path == pdf_path
        return "stub pdf text"

    def fake_parse_labs_from_pdf_text(
        text: str,
        person_id: str,
        source_label: str,
    ) -> list[dict[str, Any]]:
        assert text == "stub pdf text"
        assert source_label == str(pdf_path)
        return [
            lab_payload_from_mapping(
                {
                    "person_id": person_id,
                    "test_name": "Triglycerides - LC",
                    "value": "123",
                    "unit": "mg/dL",
                    "observed_at": "2021-10-09",
                    "source_type": "pdf:mychart",
                },
                "stub",
            )
        ]

    def fake_review_pdf_extraction(*_args: Any, **_kwargs: Any) -> bool:
        return True

    try:
        import_labs.extract_pdf_text = fake_extract_pdf_text  # type: ignore[assignment]
        import_labs.parse_labs_from_pdf_text = fake_parse_labs_from_pdf_text  # type: ignore[assignment]
        import_labs.review_pdf_extraction = fake_review_pdf_extraction  # type: ignore[assignment]

        result = import_labs.reprocess_retained_lab_source(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=pdf_path,
            requested_person_id=None,
            approve_pdf=True,
            review_only=False,
        )
    finally:
        import_labs.extract_pdf_text = original_extract  # type: ignore[assignment]
        import_labs.parse_labs_from_pdf_text = original_parse  # type: ignore[assignment]
        import_labs.review_pdf_extraction = original_review  # type: ignore[assignment]

    assert result.imported_rows == 1
    stored = conn.execute(
        "SELECT test_name, loinc_code FROM lab_results WHERE person_id = 'dummy_alice'"
    ).fetchone()
    assert stored == ("Triglycerides", "2571-8")


def test_reprocess_retained_pdf_reports_zero_when_facts_already_recorded(
    tmp_path: Path,
) -> None:
    """``imported_rows`` must reflect actual lab_results inserts, not the raw
    extracted-row count. Reprocessing a PDF whose every fact is already
    recorded under a different source_path with ``source_type='source_import'``
    (same person/test/date/value) inserts nothing -- a non-``source_import``
    PDF restatement never displaces an already-recorded structured import,
    by design. The caller must be able to see that zero rows were inserted,
    not read a misleadingly nonzero "rows imported" count for a source that
    persisted zero rows.
    """
    data_dir = tmp_path / "data"
    processed_root = data_dir / "processed"
    processed_labs_dir = processed_root / "dummy_alice" / "Labs"
    processed_labs_dir.mkdir(parents=True)

    pdf_path = processed_labs_dir / "2021-10-09-NMR LIPO.pdf"
    pdf_path.write_text("stub", encoding="utf-8")
    vault_root = tmp_path / "vault"
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    existing_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Triglycerides - LC",
            "value": "123",
            "unit": "mg/dL",
            "observed_at": "2021-10-09",
            "source_type": "source_import",
        },
        "archive",
    )
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=tmp_path / "bundle.zip",
        processed_source_path="data/processed/dummy_alice/HealthSummary/bundle.zip#DOC0001.XML",
        checksum="archive-checksum",
        source_type="zip_xml",
        rows=[existing_row],
        retained_source=True,
        refresh_reports=False,
    )

    original_extract = import_labs.extract_pdf_text
    original_parse = import_labs.parse_labs_from_pdf_text
    original_review = import_labs.review_pdf_extraction

    def fake_extract_pdf_text(path: Path) -> str:
        return "stub pdf text"

    def fake_parse_labs_from_pdf_text(
        text: str,
        person_id: str,
        source_label: str,
    ) -> list[dict[str, Any]]:
        return [
            lab_payload_from_mapping(
                {
                    "person_id": person_id,
                    "test_name": "Triglycerides - LC",
                    "value": "123",
                    "unit": "mg/dL",
                    "observed_at": "2021-10-09",
                    "source_type": "pdf:mychart",
                },
                "stub",
            )
        ]

    def fake_review_pdf_extraction(*_args: Any, **_kwargs: Any) -> bool:
        return True

    try:
        import_labs.extract_pdf_text = fake_extract_pdf_text  # type: ignore[assignment]
        import_labs.parse_labs_from_pdf_text = fake_parse_labs_from_pdf_text  # type: ignore[assignment]
        import_labs.review_pdf_extraction = fake_review_pdf_extraction  # type: ignore[assignment]

        result = import_labs.reprocess_retained_lab_source(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=pdf_path,
            requested_person_id=None,
            approve_pdf=True,
            review_only=False,
        )
    finally:
        import_labs.extract_pdf_text = original_extract  # type: ignore[assignment]
        import_labs.parse_labs_from_pdf_text = original_parse  # type: ignore[assignment]
        import_labs.review_pdf_extraction = original_review  # type: ignore[assignment]

    assert result.imported_rows == 0
    rows = conn.execute(
        "SELECT source_path FROM lab_results WHERE person_id = 'dummy_alice' "
        # "Triglycerides - LC" normalizes to the canonical alias "Triglycerides".
        "AND test_name = 'Triglycerides'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "data/processed/dummy_alice/HealthSummary/bundle.zip#DOC0001.XML"


def test_retained_source_reprocess_replaces_source_specific_note_rows(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    source_file = (
        tmp_path / "data" / "processed" / "dummy_alice" / "Labs" / "2023-06-24-A1C PANEL.pdf"
    )
    source_file.parent.mkdir(parents=True)
    source_file.write_text("stub", encoding="utf-8")
    processed_source_path = "data/processed/dummy_alice/Labs/2023-06-24-A1C PANEL.pdf"
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    stale_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-06-24",
            "value": "120",
            "unit": "%",
            "source_type": "pdf:mychart",
        },
        "stale",
    )
    corrected_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-06-24",
            "value": "5.8",
            "unit": "%",
            "source_type": "pdf:mychart",
        },
        "corrected",
    )

    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="old",
        source_type="pdf",
        rows=[stale_row],
        retained_source=True,
        refresh_reports=False,
    )
    overlapping_note = (
        vault_root / "Records" / "dummy_alice" / "Labs" / "2023-06-24-aardvark-panel.md"
    )
    write_lab_panel_note(
        overlapping_note,
        [stale_row],
        source_file_name="Other.pdf",
        source_path=processed_source_path,
    )
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="new",
        source_type="pdf",
        rows=[corrected_row],
        retained_source=True,
        refresh_reports=False,
    )

    note_path = vault_root / "Records" / "dummy_alice" / "Labs" / "2023-06-24-a1c-panel.md"
    note_rows = load_labs_from_note(note_path)
    assert [(row["test_name"], row["value"]) for row in note_rows] == [("Hemoglobin A1c", 5.8)]
    assert load_labs_from_note(overlapping_note)[0]["value"] == 120.0


def test_retained_source_reprocess_keeps_other_source_in_its_own_evidence_note(
    tmp_path: Path,
) -> None:
    # ADR-038: even an initial (non-reprocess) import writes a source-scoped
    # evidence note. Reprocessing another source cannot absorb or discard it.
    vault_root = tmp_path / "vault"
    source_file = (
        tmp_path / "data" / "processed" / "dummy_alice" / "Labs" / "2023-06-24-A1C PANEL.pdf"
    )
    source_file.parent.mkdir(parents=True)
    source_file.write_text("stub", encoding="utf-8")
    processed_source_path = "data/processed/dummy_alice/Labs/2023-06-24-A1C PANEL.pdf"
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    stale_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-06-24",
            "value": "120",
            "unit": "%",
            "source_type": "pdf:mychart",
        },
        "stale",
    )
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="old",
        source_type="pdf",
        rows=[stale_row],
        retained_source=True,
        refresh_reports=False,
    )

    # A later non-retained import overlaps on "Hemoglobin A1c" and also
    # contributes an unrelated "TSH" row. Both remain under their own source.
    overlap_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-06-24",
            "value": "120",
            "unit": "%",
            "source_type": "csv",
        },
        "merged",
    )
    other_test_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "TSH",
            "observed_at": "2023-06-24",
            "value": "2.1",
            "unit": "uIU/mL",
            "source_type": "csv",
        },
        "merged",
    )
    other_source_file = tmp_path / "data" / "processed" / "dummy_alice" / "Labs" / "bundle.csv"
    other_source_file.parent.mkdir(parents=True, exist_ok=True)
    other_source_file.write_text("stub", encoding="utf-8")
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=other_source_file,
        processed_source_path="data/processed/dummy_alice/Labs/bundle.csv",
        checksum="other",
        source_type="csv",
        rows=[overlap_row, other_test_row],
        refresh_reports=False,
    )

    corrected_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hemoglobin A1c",
            "observed_at": "2023-06-24",
            "value": "5.8",
            "unit": "%",
            "source_type": "pdf:mychart",
        },
        "corrected",
    )
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=source_file,
        processed_source_path=processed_source_path,
        checksum="new",
        source_type="pdf",
        rows=[corrected_row],
        retained_source=True,
        refresh_reports=False,
    )

    note_path = vault_root / "Records" / "dummy_alice" / "Labs" / "2023-06-24-a1c-panel.md"
    note_rows = load_labs_from_note(note_path)
    by_test = {row["test_name"]: row["value"] for row in note_rows}
    assert by_test == {"Hemoglobin A1c": 5.8}
    other_note = vault_root / "Records" / "dummy_alice" / "Labs" / "2023-06-24-bundle-panel.md"
    assert {row["test_name"]: row["value"] for row in load_labs_from_note(other_note)} == {
        "Hemoglobin A1c": 120.0,
        "TSH": 2.1,
    }


def test_persist_lab_rows_surfaces_dedup_stats_when_facts_already_recorded(
    tmp_path: Path,
) -> None:
    """A retained source whose facts are already recorded under a different
    source_path with ``source_type='source_import'`` (a structured C-CDA/XML
    extraction) has every row skipped as a redundant duplicate rather than
    re-inserted: ``_incoming_lab_replaces_redundant_rows`` never lets a
    non-``source_import`` incoming fact (e.g. a PDF-scraped ``pdf:mychart``
    restatement) replace an already-recorded ``source_import`` fact, by
    design. The vault note is still written from the full row list, so it can
    legitimately cite a source_path backed by zero ``lab_results`` rows.
    Regression test for a real repro: reprocessing a cumulative HealthSummary
    archive (whose lab facts persist with ``source_type='source_import'``)
    before a standalone same-fact lab PDF made the PDF's reprocess silently
    report "N rows" while inserting none, with no way to tell that outcome
    apart from an actual persistence failure. ``dedup_stats`` must make this
    explicit.
    """
    vault_root = tmp_path / "vault"
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    archive_source = "data/processed/dummy_alice/HealthSummary/bundle.zip#DOC0001.XML"
    archive_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hematocrit",
            "observed_at": "2023-06-24",
            "value": "40.0",
            "unit": "%",
            "source_type": "source_import",
        },
        "archive",
    )
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=tmp_path / "bundle.zip",
        processed_source_path=archive_source,
        checksum="archive-checksum",
        source_type="zip_xml",
        rows=[archive_row],
        retained_source=True,
        refresh_reports=False,
    )

    pdf_source_file = (
        tmp_path / "data" / "processed" / "dummy_alice" / "Labs" / "2023-06-24-CBC.pdf"
    )
    pdf_source_file.parent.mkdir(parents=True)
    pdf_source_file.write_text("stub", encoding="utf-8")
    pdf_source_path = "data/processed/dummy_alice/Labs/2023-06-24-CBC.pdf"
    pdf_row = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Hematocrit",
            "observed_at": "2023-06-24",
            "value": "40.0",
            "unit": "%",
            "source_type": "pdf:mychart",
        },
        "pdf",
    )

    dedup_stats: dict[str, int] = {}
    import_labs.persist_lab_rows(
        conn=conn,
        vault_root=vault_root,
        person_id="dummy_alice",
        source_file=pdf_source_file,
        processed_source_path=pdf_source_path,
        checksum="pdf-checksum",
        source_type="pdf",
        rows=[pdf_row],
        retained_source=True,
        refresh_reports=False,
        dedup_stats=dedup_stats,
    )

    # The fact was skipped as a redundant duplicate of the archive's row, not
    # inserted under the PDF's own source_path.
    assert dedup_stats == {"inserted": 0, "skipped_redundant": 1}
    db_rows = conn.execute(
        "SELECT source_path FROM lab_results WHERE person_id = 'dummy_alice' "
        "AND test_name = 'Hematocrit'"
    ).fetchall()
    assert [row[0] for row in db_rows] == [archive_source]

    # Same test_name + date makes this an overlapping panel note, so the PDF's
    # rows merge into the archive's existing note rather than starting a new
    # one -- the merged note still renders the value even though it has zero
    # backing lab_results rows under the PDF's own source_path. That gap is
    # made discoverable via dedup_stats and the ingestion_log/audit_events
    # message rather than silent.
    note_path = vault_root / "Records" / "dummy_alice" / "Labs" / "2023-06-24-bundle-panel.md"
    note_rows = load_labs_from_note(note_path)
    assert [(row["test_name"], row["value"]) for row in note_rows] == [("Hematocrit", 40.0)]

    ingestion_message = conn.execute(
        "SELECT message FROM ingestion_log WHERE source_path = ? ORDER BY imported_at DESC LIMIT 1",
        (pdf_source_path,),
    ).fetchone()[0]
    assert "1 of 1 fact(s) already recorded via another source_path" in ingestion_message


# ---------------------------------------------------------------------------
# log_ingestion_event and source_file_checksum
# ---------------------------------------------------------------------------


def test_log_ingestion_event_writes_row(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    from famed.setup import SCHEMA_SQL

    conn.executescript(SCHEMA_SQL)

    log_ingestion_event(
        conn,
        "csv",
        "/some/path.csv",
        "abc123",
        "processed",
        "note: Records/dummy_alice/Labs/x.md",
    )
    conn.commit()

    rows = conn.execute(
        "SELECT source_type, source_path, source_checksum, status, message FROM ingestion_log"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == (
        "csv",
        "/some/path.csv",
        "abc123",
        "processed",
        "note: Records/dummy_alice/Labs/x.md",
    )


def test_log_ingestion_event_rejects_invalid_processing_state() -> None:
    conn = sqlite3.connect(":memory:")
    from famed.setup import SCHEMA_SQL

    conn.executescript(SCHEMA_SQL)

    try:
        log_ingestion_event(conn, "csv", "/some/path.csv", "abc123", "imported")
    except ValueError as exc:
        assert "invalid processing state" in str(exc)
    else:
        raise AssertionError("Expected invalid ingestion status to be rejected")


def test_source_file_checksum_returns_hex_digest(tmp_path: Path) -> None:
    f = tmp_path / "sample.csv"
    f.write_bytes(b"person_id,test_name\ndummy_alice,HbA1c\n")
    digest = source_file_checksum(f)
    assert len(digest) == 64  # SHA-256 hex
    assert digest == source_file_checksum(f)  # deterministic


def test_import_labs_moves_accepted_csv_to_processed_storage(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    csv_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])

    assert import_labs.main() == 0

    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-quest-report.csv"
    note_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Labs"
        / "2023-04-20-quest-report-panel.md"
    )
    assert not csv_path.exists()
    assert processed_path.exists()

    note_content = note_path.read_text(encoding="utf-8")
    assert (
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv" in note_content
    )
    assert "- Source path:" not in note_content

    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute("SELECT source_path, status FROM ingestion_log").fetchone()
        lab_row = conn.execute("SELECT source_path FROM lab_results").fetchone()
        audit_row = conn.execute(
            "SELECT event_type, subject_type, subject_id, source_path, status FROM audit_events"
        ).fetchone()

    assert log_row == ("data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv", "processed")
    assert lab_row == ("data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",)
    assert audit_row == (
        "lab_import_accepted",
        "lab_panel",
        "dummy_alice",
        "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
        "processed",
    )


def test_import_labs_reimporting_same_csv_does_not_duplicate_lab_rows(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    csv_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.csv"
    csv_path.parent.mkdir(parents=True)
    csv_content = (
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n"
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])

    csv_path.write_text(csv_content, encoding="utf-8")
    assert import_labs.main() == 0
    csv_path.write_text(csv_content, encoding="utf-8")
    assert import_labs.main() == 0

    with sqlite3.connect(db_path) as conn:
        lab_count = conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]
        log_count = conn.execute("SELECT COUNT(*) FROM ingestion_log").fetchone()[0]
        lab_row = conn.execute("SELECT source_path FROM lab_results").fetchone()

    assert lab_count == 1
    assert log_count == 2
    assert lab_row == ("data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",)


def test_import_labs_refreshes_lab_conflict_report_for_conflicting_sources(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox = data_dir / "inbox" / "dummy_alice" / "labs"
    inbox.mkdir(parents=True)
    csv_a = inbox / "panel-a.csv"
    csv_a.write_text(
        "test_name,observed_at,value,unit\nHemoglobin A1c,2023-04-20,5.8,%\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])
    assert import_labs.main() == 0

    csv_b = inbox / "panel-b.csv"
    csv_b.write_text(
        "test_name,observed_at,value,unit\nHemoglobin A1c,2023-04-20,6.1,%\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["import_labs"])
    assert import_labs.main() == 0

    report_path = (
        tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Reports" / "lab-conflicts.md"
    )
    report_content = report_path.read_text(encoding="utf-8")

    assert "report_type: lab_conflicts" in report_content
    assert "data/processed/dummy_alice/Labs/2023-04-20-panel-a.csv" in report_content
    assert "data/processed/dummy_alice/Labs/2023-04-20-panel-b.csv" in report_content
    assert "| 2023-04-20 | Hemoglobin A1c | 5.8 | % |" in report_content
    assert "| 2023-04-20 | Hemoglobin A1c | 6.1 | % |" in report_content


def test_import_labs_keeps_overlapping_panels_as_source_evidence_notes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Overlapping source panels stay separate while the current view deduplicates."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox = data_dir / "inbox" / "dummy_alice" / "labs"
    inbox.mkdir(parents=True)

    csv_a = inbox / "panel-a.csv"
    csv_a.write_text(
        "test_name,observed_at,value,unit\nHemoglobin A1c,2023-04-20,5.8,%\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])
    assert import_labs.main() == 0

    csv_b = inbox / "panel-b.csv"
    csv_b.write_text(
        "test_name,observed_at,value,unit\n"
        "Hemoglobin A1c,2023-04-20,5.8,%\n"
        "Glucose,2023-04-20,96.0,mg/dL\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["import_labs"])
    assert import_labs.main() == 0

    labs_dir = tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Labs"
    panel_notes = sorted(labs_dir.glob("2023-04-20-*-panel.md"))
    assert [path.name for path in panel_notes] == [
        "2023-04-20-panel-a-panel.md",
        "2023-04-20-panel-b-panel.md",
    ]
    for note_path in panel_notes:
        front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        assert front_matter["projection_role"] == "source_evidence"
    latest_report = (
        tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Reports" / "lab-results.md"
    ).read_text(encoding="utf-8")
    assert latest_report.count("| Hemoglobin A1c | 5.8 % |") == 1
    assert "| Glucose | 96.0 mg/dL |" in latest_report
    assert "[[Records/dummy_alice/Labs/_index|Lab Source Evidence]]" in latest_report


def test_import_labs_creates_separate_notes_for_non_overlapping_panels(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Two CSVs with disjoint tests on the same date produce two separate notes."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    inbox = data_dir / "inbox" / "dummy_alice" / "labs"
    inbox.mkdir(parents=True)

    csv_a = inbox / "cbc.csv"
    csv_a.write_text(
        "test_name,observed_at,value,unit\nHemoglobin,2023-04-20,14.0,g/dL\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])
    assert import_labs.main() == 0

    csv_b = inbox / "cmp.csv"
    csv_b.write_text(
        "test_name,observed_at,value,unit\nGlucose,2023-04-20,96.0,mg/dL\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["import_labs"])
    assert import_labs.main() == 0

    labs_dir = tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Labs"
    panel_notes = [p for p in labs_dir.glob("2023-04-20-*-panel.md")]
    assert len(panel_notes) == 2, f"expected 2 panel notes, got {[p.name for p in panel_notes]}"


def test_import_labs_reprocesses_retained_csv_without_moving_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    csv_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])

    assert import_labs.main() == 0

    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-quest-report.csv"
    note_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Labs"
        / "2023-04-20-quest-report-panel.md"
    )
    processed_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,6.1,%,4.0,5.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_labs", "--reprocess-source", str(processed_path.relative_to(tmp_path))],
    )

    assert import_labs.main() == 0
    assert processed_path.exists()
    assert not csv_path.exists()

    note_content = note_path.read_text(encoding="utf-8")
    assert (
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv" in note_content
    )
    assert "| Hemoglobin A1c |  | 6.1 | % | 4.0 | 5.6 | 2023-04-20 |" in note_content

    with sqlite3.connect(db_path) as conn:
        lab_rows = conn.execute("SELECT value, source_path FROM lab_results").fetchall()
        log_rows = conn.execute(
            "SELECT source_path, status, message FROM ingestion_log ORDER BY rowid"
        ).fetchall()
        audit_rows = conn.execute(
            """
            SELECT event_type, source_path, status, message
            FROM audit_events
            ORDER BY rowid
            """
        ).fetchall()

    assert lab_rows == [(6.1, "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv")]
    assert log_rows == [
        (
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "processed",
            "note: Records/dummy_alice/Labs/2023-04-20-quest-report-panel.md",
        ),
        (
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "processed",
            "reprocessed note: Records/dummy_alice/Labs/2023-04-20-quest-report-panel.md",
        ),
    ]
    assert audit_rows == [
        (
            "lab_import_accepted",
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "processed",
            "note: Records/dummy_alice/Labs/2023-04-20-quest-report-panel.md",
        ),
        (
            "dependent_output_marked_stale",
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "stale",
            "Records/dummy_alice/Reports/lab-results.md: retained lab source reprocessed",
        ),
        (
            "dependent_output_marked_stale",
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "stale",
            "Records/dummy_alice/Reports/lab-conflicts.md: retained lab source reprocessed",
        ),
        (
            "dependent_output_marked_stale",
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "stale",
            "Records/dummy_alice/Reports/flagged-for-discussion.md: "
            "retained lab source reprocessed",
        ),
        (
            "dependent_output_marked_stale",
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "stale",
            "Records/dummy_alice/Reports/pending-review.md: retained lab source reprocessed",
        ),
        (
            "lab_source_reprocessed",
            "data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv",
            "processed",
            "reprocessed note: Records/dummy_alice/Labs/2023-04-20-quest-report-panel.md",
        ),
    ]


def test_import_labs_reprocess_source_requires_processed_lab_path(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    csv_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,%,4.0,5.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_labs",
            "--reprocess-source",
            str(csv_path.relative_to(tmp_path)),
            "--person-id",
            "dummy_alice",
        ],
    )

    assert import_labs.main() == 1

    with sqlite3.connect(db_path) as conn:
        lab_count = conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]

    assert lab_count == 0
    assert csv_path.exists()


def test_import_labs_quarantines_invalid_csv_without_canonical_writes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    csv_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,5.8,mg/dL,4.0,5.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["import_labs"])

    assert import_labs.main() == 1

    quarantine_path = data_dir / "quarantine" / "dummy_alice" / "Labs" / "undated-quest-report.csv"
    note_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Labs"
        / "2023-04-20-quest-report-panel.md"
    )
    assert not csv_path.exists()
    assert quarantine_path.exists()
    assert not note_path.exists()

    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute("SELECT source_path, status, message FROM ingestion_log").fetchone()
        audit_row = conn.execute(
            "SELECT event_type, source_path, status, message FROM audit_events"
        ).fetchone()
        lab_count = conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]

    assert log_row is not None
    assert log_row[0] == "data/quarantine/dummy_alice/Labs/undated-quest-report.csv"
    assert log_row[1] == "quarantined"
    assert "potential unit mismatch" in log_row[2]
    assert audit_row is not None
    assert audit_row[0] == "lab_import_quarantined"
    assert audit_row[1] == "data/quarantine/dummy_alice/Labs/undated-quest-report.csv"
    assert audit_row[2] == "quarantined"
    assert "potential unit mismatch" in audit_row[3]
    assert lab_count == 0


def test_import_labs_cancelled_pdf_review_leaves_source_in_inbox(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    pdf_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_labs", "--pdf-file", str(pdf_path), "--person-id", "dummy_alice"],
    )
    monkeypatch.setattr(import_labs, "extract_pdf_text", lambda _path: "pdf text")
    monkeypatch.setattr(
        import_labs,
        "parse_labs_from_pdf_text",
        lambda _text, person_id, _source_label: [
            lab_payload_from_mapping(
                {
                    "person_id": person_id,
                    "test_name": "Hemoglobin A1c",
                    "observed_at": "2023-04-20",
                    "value": "5.8",
                    "unit": "%",
                    "source_type": "pdf:quest",
                },
                "pdf",
            )
        ],
    )
    monkeypatch.setattr(import_labs, "review_pdf_extraction", lambda *_args, **_kwargs: False)

    assert import_labs.main() == 1

    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-quest-report.pdf"
    assert pdf_path.exists()
    assert not processed_path.exists()


def test_import_labs_moves_accepted_pdf_to_processed_storage(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    pdf_path = data_dir / "inbox" / "dummy_alice" / "labs" / "quest-report.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(import_labs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_labs", "--pdf-file", str(pdf_path), "--person-id", "dummy_alice", "--approve-pdf"],
    )
    monkeypatch.setattr(import_labs, "extract_pdf_text", lambda _path: "pdf text")
    monkeypatch.setattr(
        import_labs,
        "parse_labs_from_pdf_text",
        lambda _text, person_id, _source_label: [
            lab_payload_from_mapping(
                {
                    "person_id": person_id,
                    "test_name": "Hemoglobin A1c",
                    "observed_at": "2023-04-20",
                    "value": "5.8",
                    "unit": "%",
                    "source_type": "pdf:quest",
                },
                "pdf",
            )
        ],
    )

    assert import_labs.main() == 0

    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-quest-report.pdf"
    note_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Labs"
        / "2023-04-20-quest-report-panel.md"
    )
    assert not pdf_path.exists()
    assert processed_path.exists()

    note_content = note_path.read_text(encoding="utf-8")
    assert (
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-quest-report.pdf" in note_content
    )
    assert "- Source path:" not in note_content

    with sqlite3.connect(db_path) as conn:
        log_row = conn.execute("SELECT source_path, status FROM ingestion_log").fetchone()
        lab_row = conn.execute("SELECT source_path FROM lab_results").fetchone()

    assert log_row == ("data/processed/dummy_alice/Labs/2023-04-20-quest-report.pdf", "processed")
    assert lab_row == ("data/processed/dummy_alice/Labs/2023-04-20-quest-report.pdf",)


def _make_panel_note(
    tmp_path: Path,
    person_id: str,
    observed_at: str,
    rows: list[tuple[str, str, str, str]],
    *,
    source_path: str = "source.pdf",
    filename: str = "panel.md",
) -> Path:
    """Write a minimal lab panel note and return its path.

    *rows* is a list of ``(test_name, loinc_code, value, unit)`` tuples.
    """
    labs_dir = tmp_path / "Records" / person_id / "Labs"
    labs_dir.mkdir(parents=True, exist_ok=True)
    note_path = labs_dir / filename

    table_lines = [
        LAB_PANEL_TABLE_HEADER,
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
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


def test_repair_lab_loinc_codes_fills_blank_cells(tmp_path: Path) -> None:
    loinc_index = {"glucose": "2345-7", "hemoglobin a1c": "4548-4"}

    note_path = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2021-01-15",
        [
            ("Glucose", "", "95.0", "mg/dL"),
            ("Hemoglobin A1c", "", "5.8", "%"),
        ],
    )

    notes_updated, cells_filled = repair_lab_loinc_codes(tmp_path, loinc_index)

    assert notes_updated == 1
    assert cells_filled == 2

    content = note_path.read_text(encoding="utf-8")
    assert "| Glucose | 2345-7 |" in content
    assert "| Hemoglobin A1c | 4548-4 |" in content


def test_repair_lab_loinc_codes_does_not_overwrite_existing_code(tmp_path: Path) -> None:
    loinc_index = {"glucose": "2345-7"}

    note_path = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2021-01-15",
        [("Glucose", "EXISTING-CODE", "95.0", "mg/dL")],
    )

    notes_updated, cells_filled = repair_lab_loinc_codes(tmp_path, loinc_index)

    assert notes_updated == 0
    assert cells_filled == 0

    content = note_path.read_text(encoding="utf-8")
    assert "EXISTING-CODE" in content


def test_repair_lab_loinc_codes_skips_unmapped_tests(tmp_path: Path) -> None:
    loinc_index = {"glucose": "2345-7"}

    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2021-01-15",
        [("Unknown Test XYZ", "", "1.0", "unit")],
    )

    notes_updated, cells_filled = repair_lab_loinc_codes(tmp_path, loinc_index)

    assert notes_updated == 0
    assert cells_filled == 0


def test_repair_lab_loinc_codes_returns_zero_for_empty_index(tmp_path: Path) -> None:
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2021-01-15",
        [("Glucose", "", "95.0", "mg/dL")],
    )

    notes_updated, cells_filled = repair_lab_loinc_codes(tmp_path, {})

    assert notes_updated == 0
    assert cells_filled == 0


def test_backfill_reviewed_inferred_loinc_codes_repairs_db_notes_and_audit(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    rows = [
        (
            "ig-inferred",
            "dummy_alice",
            "53115-2",
            LOINC_CODE_ORIGIN_INFERRED,
            "% Immature Granulocytes",
            0.2,
            "%",
            "source-a.pdf",
        ),
        (
            "rdw-inferred",
            "dummy_alice",
            "21000-5",
            LOINC_CODE_ORIGIN_INFERRED,
            "RDW",
            12.2,
            "%",
            "source-a.pdf",
        ),
        (
            "ig-source",
            "dummy_bob",
            "53115-2",
            LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
            "% Immature Granulocytes",
            0.3,
            "%",
            "source-b.pdf",
        ),
        (
            "rdw-source",
            "dummy_bob",
            "30385-9",
            LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
            "RDW-SD",
            44.0,
            "fL",
            "source-b.pdf",
        ),
        (
            "ig-source-same-identity",
            "dummy_alice",
            "53115-2",
            LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
            "% Immature Granulocytes",
            0.2,
            "%",
            "source-c.pdf",
        ),
        (
            "freepsa-inferred",
            "dummy_alice",
            "10886-0",
            LOINC_CODE_ORIGIN_INFERRED,
            "% Free PSA",
            20.4,
            "%",
            "source-a.pdf",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, '2022-01-15', 'pdf', ?)
        """,
        rows,
    )
    inferred_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [
            ("% Immature Granulocytes", "53115-2", "0.2", "%"),
            ("RDW", "21000-5", "12.2", "%"),
            ("% Free PSA", "10886-0", "20.4", "%"),
        ],
        source_path="source-a.pdf",
    )
    source_note = _make_panel_note(
        tmp_path,
        "dummy_bob",
        "2022-01-15",
        [
            ("% Immature Granulocytes", "53115-2", "0.3", "%"),
            ("RDW-SD", "30385-9", "44.0", "fL"),
        ],
        source_path="source-b.pdf",
    )
    duplicate_inferred_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("RDW", "21000-5", "12.2", "%")],
        source_path="overlapping-source.pdf",
        filename="duplicate.md",
    )
    source_same_identity_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("% Immature Granulocytes", "53115-2", "0.2", "%")],
        source_path="source-c.pdf",
        filename="source-conflict.md",
    )

    repairs = backfill_reviewed_inferred_loinc_codes(conn)
    notes_updated, cells_updated = repair_inferred_lab_loinc_codes_in_notes(conn, tmp_path, repairs)

    assert len(repairs) == 3
    assert dict(conn.execute("SELECT id, loinc_code FROM lab_results")) == {
        "ig-inferred": "71695-1",
        "rdw-inferred": "788-0",
        "ig-source": "53115-2",
        "rdw-source": "30385-9",
        "ig-source-same-identity": "53115-2",
        "freepsa-inferred": "12841-3",
    }
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            ("lab_inferred_loinc_repaired",),
        ).fetchone()[0]
        == 3
    )
    assert notes_updated == 2
    assert cells_updated == 4
    inferred_text = inferred_note.read_text(encoding="utf-8")
    assert "| % Immature Granulocytes | 71695-1 |" in inferred_text
    assert "| RDW | 788-0 |" in inferred_text
    assert "| % Free PSA | 12841-3 |" in inferred_text
    source_text = source_note.read_text(encoding="utf-8")
    assert "| % Immature Granulocytes | 53115-2 |" in source_text
    assert "| RDW-SD | 30385-9 |" in source_text
    assert "| RDW | 788-0 |" in duplicate_inferred_note.read_text(encoding="utf-8")
    assert "| % Immature Granulocytes | 53115-2 |" in (
        source_same_identity_note.read_text(encoding="utf-8")
    )

    assert backfill_reviewed_inferred_loinc_codes(conn) == []
    assert repair_inferred_lab_loinc_codes_in_notes(conn, tmp_path, repairs) == (0, 0)


_RDW_SD_OVERRIDES = [
    SourceLoincOverride("reviewed-rdw-sd-1", "30385-9", "21000-5", "fL property"),
    SourceLoincOverride("reviewed-rdw-sd-2", "30385-9", "21000-5", "fL property"),
]


def test_apply_reviewed_source_provided_loinc_overrides_is_row_scoped(
    tmp_path: Path,
) -> None:
    """ADR-035: overrides apply only to the exact reviewed row ids.

    A different row sharing the same test-name/unit/code pattern -- and a row
    with the same id but a non-source-provided origin -- must not be touched,
    proving this is a row-id lane, not a pattern-match lane like
    ``_REVIEWED_INFERRED_LOINC_REPAIRS``.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    rows = [
        (
            "reviewed-rdw-sd-1",
            "dummy_alice",
            "30385-9",
            LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
            "RDW-SD",
            43.9,
            "fL",
        ),
        (
            "reviewed-rdw-sd-2",
            "dummy_bob",
            "30385-9",
            LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
            "RDW-SD",
            46.3,
            "fL",
        ),
        (
            "not-a-reviewed-row-id",
            "dummy_alice",
            "30385-9",
            LOINC_CODE_ORIGIN_SOURCE_PROVIDED,
            "RDW-SD",
            44.0,
            "fL",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, '2022-01-15', 'source_import', 'ccda.xml')
        """,
        rows,
    )

    repairs = apply_reviewed_source_provided_loinc_overrides(conn, _RDW_SD_OVERRIDES)

    assert len(repairs) == 2
    assert {r.row_id for r in repairs} == {
        "reviewed-rdw-sd-1",
        "reviewed-rdw-sd-2",
    }
    assert dict(conn.execute("SELECT id, loinc_code FROM lab_results")) == {
        "reviewed-rdw-sd-1": "21000-5",
        "reviewed-rdw-sd-2": "21000-5",
        "not-a-reviewed-row-id": "30385-9",
    }
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            ("lab_source_provided_loinc_overridden",),
        ).fetchone()[0]
        == 2
    )

    # Idempotent: already-applied rows are skipped, not reapplied.
    assert apply_reviewed_source_provided_loinc_overrides(conn, _RDW_SD_OVERRIDES) == []


def test_apply_reviewed_source_provided_loinc_overrides_ignores_inferred_origin(
    tmp_path: Path,
) -> None:
    """A row id in the override table with ``inferred`` origin is untouched.

    The override lane requires ``source_provided`` origin even for a listed
    row id -- it never reaches into inferred data (that stays with the
    separate ADR-035 lane).
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES (?, 'dummy_alice', '30385-9', ?, 'RDW-SD', 43.9, 'fL', NULL, NULL,
                  '2022-01-15', 'pdf', 'source.pdf')
        """,
        ("reviewed-rdw-sd-1", LOINC_CODE_ORIGIN_INFERRED),
    )

    assert apply_reviewed_source_provided_loinc_overrides(conn, _RDW_SD_OVERRIDES) == []
    assert (
        conn.execute(
            "SELECT loinc_code FROM lab_results WHERE id = ?",
            ("reviewed-rdw-sd-1",),
        ).fetchone()[0]
        == "30385-9"
    )


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


def test_find_superseded_lab_panel_notes_detects_note_with_replacement(
    tmp_path: Path,
) -> None:
    """A note whose source has no current rows, superseded by a note that
    shares a test name and whose own source is current (REQ-006)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_current_row(conn, "dummy_alice", "new.xml")

    old_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    new_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "200-2", "5.0", "K/uL")],
        source_path="new.xml",
        filename="2022-01-15-new-panel.md",
    )

    superseded = find_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice")

    assert len(superseded) == 1
    assert superseded[0].note_path == old_note
    assert superseded[0].replacement_path == new_note
    assert superseded[0].source_path == "old.pdf"


def test_find_superseded_lab_panel_notes_leaves_note_without_replacement(
    tmp_path: Path,
) -> None:
    """A note whose source has no current rows and no sibling note shares a
    test name for that date is left alone -- that's a quarantine/deletion
    case for human triage, not an automatic convergence."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )

    assert find_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice") == []


def test_find_superseded_lab_panel_notes_leaves_partially_covered_note_alone(
    tmp_path: Path,
) -> None:
    """Regression: a note is not marked superseded just because it shares
    ONE test name with a current note -- every fact it contains must be
    covered by current data, or it's left alone.

    This reproduces a real false positive found in the vault: a merged
    "CBC" panel note that had absorbed many unrelated tests (CMP, lipids,
    PSA, TSH...) over past reprocessing runs also happened to contain a
    couple of POCT urine-dipstick rows. The old (overlap-based) algorithm
    would have marked the whole CBC-titled note as superseded by a small,
    unrelated urine-dipstick note just because those two rows matched --
    misrepresenting a note that is still the only record of most of its
    content.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_current_row(conn, "dummy_alice", "urine-dipstick.pdf")
    conn.execute(
        """
        UPDATE lab_results SET test_name = 'POCT Color, UA' WHERE source_path = ?
        """,
        ("urine-dipstick.pdf",),
    )

    merged_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [
            ("White blood cell count", "100-1", "5.0", "K/uL"),
            ("Hemoglobin", "101-1", "14.0", "g/dL"),
            ("POCT Color, UA", "5778-6", "Yellow", ""),
        ],
        source_path="merged-cbc.pdf",
        filename="2022-01-15-merged-cbc-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("POCT Color, UA", "5778-6", "Yellow", "")],
        source_path="urine-dipstick.pdf",
        filename="2022-01-15-urine-dipstick-panel.md",
    )

    superseded = find_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice")

    assert superseded == []
    assert "superseded_by" not in merged_note.read_text(encoding="utf-8")


def test_find_superseded_lab_panel_notes_requires_single_covering_note(
    tmp_path: Path,
) -> None:
    """A note fully covered by current data, but scattered across two
    different current notes, is left alone -- there's no single unambiguous
    replacement to point at."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('row-a', 'dummy_alice', '100-1', 'source_provided',
                  'White blood cell count', 5.0, 'K/uL', NULL, NULL,
                  '2022-01-15', 'source_import', 'a.xml')
        """
    )
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('row-b', 'dummy_alice', '101-1', 'source_provided', 'Hemoglobin', 14.0, 'g/dL',
                  NULL, NULL, '2022-01-15', 'source_import', 'b.xml')
        """
    )

    old_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [
            ("White blood cell count", "100-1", "5.0", "K/uL"),
            ("Hemoglobin", "101-1", "14.0", "g/dL"),
        ],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="a.xml",
        filename="2022-01-15-a-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("Hemoglobin", "101-1", "14.0", "g/dL")],
        source_path="b.xml",
        filename="2022-01-15-b-panel.md",
    )

    assert find_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice") == []
    assert "superseded_by" not in old_note.read_text(encoding="utf-8")


def test_find_superseded_lab_panel_notes_skips_already_marked(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_current_row(conn, "dummy_alice", "new.xml")

    old_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    new_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "200-2", "5.0", "K/uL")],
        source_path="new.xml",
        filename="2022-01-15-new-panel.md",
    )
    mark_lab_panel_note_superseded(old_note, new_note)

    assert find_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice") == []


def test_mark_lab_panel_note_superseded_is_idempotent_and_preserves_body(
    tmp_path: Path,
) -> None:
    old_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    new_note = tmp_path / "Records" / "dummy_alice" / "Labs" / "2022-01-15-new-panel.md"

    assert mark_lab_panel_note_superseded(old_note, new_note) is True
    content_after_first = old_note.read_text(encoding="utf-8")
    assert "superseded_by: 2022-01-15-new-panel" in content_after_first
    assert "Edit table values directly if corrections are needed" in content_after_first

    assert mark_lab_panel_note_superseded(old_note, new_note) is False
    assert old_note.read_text(encoding="utf-8") == content_after_first


def test_reconcile_superseded_lab_panel_notes_marks_and_audits(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_current_row(conn, "dummy_alice", "new.xml")

    old_note = _make_panel_note(
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

    applied = reconcile_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice")

    assert len(applied) == 1
    assert "superseded_by:" in old_note.read_text(encoding="utf-8")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            ("lab_panel_note_marked_superseded",),
        ).fetchone()[0]
        == 1
    )

    # Idempotent: nothing left to reconcile on a second pass.
    assert reconcile_superseded_lab_panel_notes(conn, tmp_path, "dummy_alice") == []


def test_write_category_index_note_renders_superseded_annotation(tmp_path: Path) -> None:
    old_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    new_note = tmp_path / "Records" / "dummy_alice" / "Labs" / "2022-01-15-new-panel.md"
    mark_lab_panel_note_superseded(old_note, new_note)

    index_path = write_category_index_note(old_note.parent, "dummy_alice", "Labs")

    content = index_path.read_text(encoding="utf-8")
    assert "[[2022-01-15-old-panel]] — superseded by [[2022-01-15-new-panel]]" in content


def test_assess_lab_panel_note_coverage_normalizes_db_test_name_for_matching(
    tmp_path: Path,
) -> None:
    """Regression: found via real-vault triage. load_labs_from_note normalizes
    a note's raw row (e.g. "RDW" + unit "%" -> "RDW-CV"), but the DB's own
    persisted test_name column is not always normalized to the same alias.
    A perfectly current, matching fact must not be reported as uncovered
    just because the two sides used different (but equivalent) spellings.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('rdw-row', 'dummy_alice', '788-0', 'source_provided', 'RDW', 12.9, '%',
                  NULL, NULL, '2022-01-15', 'source_import', 'doc0001.xml')
        """
    )
    note_path = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("RDW", "788-0", "12.9", "%")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")

    assert len(coverage) == 1
    entry = coverage[0]
    assert entry.note_path == note_path
    assert entry.uncovered_test_names == ()
    assert entry.category == "covered_no_note"


def test_assess_lab_panel_note_coverage_treats_mixed_coverage_as_no_note(
    tmp_path: Path,
) -> None:
    """Regression: a real false positive found 2023-07-21. A note whose facts
    are fully covered, but by a MIX of sources -- some mapping to a real
    current note, some not -- must not be reported as single_replacement
    just because the *mapped* subset happens to resolve to exactly one note.
    Silently dropping the unmapped source and pointing only at the mapped
    remainder mislabeled a 62-fact note (mostly covered by an unlinkable
    C-CDA source) as fully explained by an unrelated small note that only
    covered 2 of its facts -- and was auto-applied to the real vault before
    being caught by manual verification.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    # Covers "White blood cell count" -- source has no note of its own.
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('wbc-row', 'dummy_alice', '100-1', 'source_provided',
                  'White blood cell count', 5.0, 'K/uL', NULL, NULL,
                  '2022-01-15', 'source_import', 'doc0001-no-note.xml')
        """
    )
    # Covers "POCT Color, UA" -- source DOES have a current note.
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('poct-row', 'dummy_alice', '5778-6', 'source_provided',
                  'POCT Color, UA', 5.0, '', NULL, NULL,
                  '2022-01-15', 'source_import', 'poct.pdf')
        """
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("POCT Color, UA", "5778-6", "5.0", "")],
        source_path="poct.pdf",
        filename="2022-01-15-poct-panel.md",
    )
    mixed_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [
            ("White blood cell count", "100-1", "5.0", "K/uL"),
            ("POCT Color, UA", "5778-6", "5.0", ""),
        ],
        source_path="old-mixed.pdf",
        filename="2022-01-15-old-mixed-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")
    by_note = {entry.note_path: entry for entry in coverage}

    entry = by_note[mixed_note]
    assert entry.covered_facts == entry.total_facts  # fully covered
    assert entry.category == "covered_no_note"
    assert entry.covering_notes == ()


def test_assess_lab_panel_note_coverage_categorizes_all_five_cases(tmp_path: Path) -> None:
    """The diagnostic report must categorize every stale-source note
    correctly, including the four cases the strict auto-reconcile function
    intentionally declines to act on.

    Regression: covered_no_note (fully covered, but the covering source has
    no note of its own -- e.g. a C-CDA summary document whose lab facts
    never got a lab_panel note written) must not be conflated with
    multiple_replacements (fully covered, 2+ real notes to choose between).
    A real vault triage 2023-07-21 found the category property collapsing
    "0 covering notes" and "2+ covering notes" into one bucket."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)

    # single_replacement: fully covered by exactly one current note.
    single_old = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="single-old.pdf",
        filename="2022-01-15-single-old-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "200-2", "5.0", "K/uL")],
        source_path="single-new.xml",
        filename="2022-01-15-single-new-panel.md",
    )
    _insert_current_row(conn, "dummy_alice", "single-new.xml")

    # multiple_replacements: fully covered, scattered across two current notes.
    multi_old = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-02-01",
        [
            ("White blood cell count", "100-1", "5.0", "K/uL"),
            ("Hemoglobin", "101-1", "14.0", "g/dL"),
        ],
        source_path="multi-old.pdf",
        filename="2022-02-01-multi-old-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-02-01",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="multi-a.xml",
        filename="2022-02-01-multi-a-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-02-01",
        [("Hemoglobin", "101-1", "14.0", "g/dL")],
        source_path="multi-b.xml",
        filename="2022-02-01-multi-b-panel.md",
    )
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('multi-a-row', 'dummy_alice', '100-1', 'source_provided',
                  'White blood cell count', 5.0, 'K/uL', NULL, NULL,
                  '2022-02-01', 'source_import', 'multi-a.xml')
        """
    )
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('multi-b-row', 'dummy_alice', '101-1', 'source_provided',
                  'Hemoglobin', 14.0, 'g/dL', NULL, NULL,
                  '2022-02-01', 'source_import', 'multi-b.xml')
        """
    )

    # partial: only some facts covered.
    partial_old = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-03-01",
        [
            ("White blood cell count", "100-1", "5.0", "K/uL"),
            ("Hemoglobin", "101-1", "14.0", "g/dL"),
        ],
        source_path="partial-old.pdf",
        filename="2022-03-01-partial-old-panel.md",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-03-01",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="partial-new.xml",
        filename="2022-03-01-partial-new-panel.md",
    )
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('partial-row', 'dummy_alice', '100-1', 'source_provided',
                  'White blood cell count', 5.0, 'K/uL', NULL, NULL,
                  '2022-03-01', 'source_import', 'partial-new.xml')
        """
    )

    # no_replacement: source gone, nothing covers it anywhere.
    orphan_old = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-04-01",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="orphan-old.pdf",
        filename="2022-04-01-orphan-old-panel.md",
    )

    # covered_no_note: fully covered by current DB data, but that data's
    # source_path has no corresponding lab_panel note (e.g. a C-CDA summary
    # document that never got its own note written).
    no_note_old = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-05-01",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="no-note-old.pdf",
        filename="2022-05-01-no-note-old-panel.md",
    )
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type, source_path
        ) VALUES ('no-note-row', 'dummy_alice', '100-1', 'source_provided',
                  'White blood cell count', 5.0, 'K/uL', NULL, NULL,
                  '2022-05-01', 'source_import', 'doc0001-no-note.xml')
        """
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")
    by_note = {entry.note_path: entry for entry in coverage}

    assert by_note[single_old].category == "single_replacement"
    assert by_note[multi_old].category == "multiple_replacements"
    assert len(by_note[multi_old].covering_notes) == 2
    assert by_note[partial_old].category == "partial"
    assert by_note[partial_old].uncovered_test_names == ("Hemoglobin",)
    assert by_note[orphan_old].category == "no_replacement"
    assert by_note[orphan_old].covering_notes == ()
    assert by_note[no_note_old].category == "covered_no_note"
    assert by_note[no_note_old].covering_notes == ()
    assert by_note[no_note_old].uncovered_test_names == ()


def test_assess_lab_panel_note_coverage_excludes_already_marked_notes(
    tmp_path: Path,
) -> None:
    """Regression: a note already labeled superseded_by must not keep
    reappearing in the diagnostic report as an open single_replacement
    candidate once reconciled."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_current_row(conn, "dummy_alice", "new.xml")

    old_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "100-1", "5.0", "K/uL")],
        source_path="old.pdf",
        filename="2022-01-15-old-panel.md",
    )
    new_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2022-01-15",
        [("White blood cell count", "200-2", "5.0", "K/uL")],
        source_path="new.xml",
        filename="2022-01-15-new-panel.md",
    )

    assert len(assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")) == 1

    mark_lab_panel_note_superseded(old_note, new_note)

    assert assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice") == []


def test_reprocess_source_script_reprocesses_retained_lab_csv(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """scripts/reprocess_source.py routes a retained lab CSV through the labs handler."""
    import importlib

    reprocess_source_module = importlib.import_module("scripts.reprocess_source")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

    # Place an already-retained lab CSV (simulates a source that was previously imported)
    processed_path = data_dir / "processed" / "dummy_alice" / "Labs" / "2023-04-20-quest-report.csv"
    processed_path.parent.mkdir(parents=True)
    processed_path.write_text(
        "test_name,observed_at,value,unit,reference_range_low,reference_range_high\n"
        "Hemoglobin A1c,2023-04-20,6.1,%,4.0,5.6\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(reprocess_source_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["reprocess_source", str(processed_path.relative_to(tmp_path))],
    )

    reprocess_source_module.main()

    note_path = (
        tmp_path
        / "famed-vault"
        / "Records"
        / "dummy_alice"
        / "Labs"
        / "2023-04-20-quest-report-panel.md"
    )
    assert note_path.exists()
    note_content = note_path.read_text(encoding="utf-8")
    assert (
        "source_path: data/processed/dummy_alice/Labs/2023-04-20-quest-report.csv" in note_content
    )
    assert "| Hemoglobin A1c |  | 6.1 | % | 4.0 | 5.6 | 2023-04-20 |" in note_content

    with sqlite3.connect(db_path) as conn:
        audit_types = [
            row[0]
            for row in conn.execute("SELECT event_type FROM audit_events ORDER BY rowid").fetchall()
        ]

    assert "lab_source_reprocessed" in audit_types
    assert "dependent_output_marked_stale" in audit_types


# --- collect_loinc_misses ---


def _loinc_row(test_name: str, loinc_code: str = "") -> dict[str, Any]:
    return {"test_name": test_name, "loinc_code": loinc_code}


def test_collect_loinc_misses_empty_index_returns_empty() -> None:
    rows = [_loinc_row("Glucose"), _loinc_row("TSH")]
    assert collect_loinc_misses(rows, {}) == []


def test_collect_loinc_misses_all_mapped_returns_empty() -> None:
    rows = [_loinc_row("Glucose", "2345-7"), _loinc_row("TSH", "3016-3")]
    assert collect_loinc_misses(rows, {"glucose": "2345-7"}) == []


def test_collect_loinc_misses_partial_map_returns_unmapped_names() -> None:
    rows = [_loinc_row("Glucose", "2345-7"), _loinc_row("Unknown Panel XYZ")]
    result = collect_loinc_misses(rows, {"glucose": "2345-7"})
    assert result == ["Unknown Panel XYZ"]


def test_collect_loinc_misses_deduplicates_names() -> None:
    rows = [_loinc_row("Unknown Test"), _loinc_row("Unknown Test"), _loinc_row("Another Miss")]
    result = collect_loinc_misses(rows, {"glucose": "2345-7"})
    assert result == ["Another Miss", "Unknown Test"]


def test_collect_loinc_misses_is_sorted() -> None:
    rows = [_loinc_row("Zinc"), _loinc_row("Albumin"), _loinc_row("Ferritin")]
    result = collect_loinc_misses(rows, {"glucose": "2345-7"})
    assert result == ["Albumin", "Ferritin", "Zinc"]


def test_fill_then_collect_misses_reflects_post_fill_state() -> None:
    loinc_index = {"glucose": "2345-7"}
    rows = [_loinc_row("Glucose"), _loinc_row("Unknown Analyte")]
    fill_loinc_codes(rows, loinc_index)
    misses = collect_loinc_misses(rows, loinc_index)
    assert rows[0]["loinc_code"] == "2345-7"
    assert rows[1]["loinc_code"] == ""
    assert misses == ["Unknown Analyte"]


# ---------------------------------------------------------------------------
# build_lab_trends_from_db
# ---------------------------------------------------------------------------


def _trends_conn(*rows: dict[str, Any]) -> sqlite3.Connection:
    """In-memory DB pre-loaded with lab_results rows for trend tests."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for row in rows:
        conn.execute(
            """INSERT INTO lab_results
               (id, person_id, loinc_code, test_name, value, value_text, unit,
                reference_range_low, reference_range_high, observed_at, source_type, source_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("id", f"id-{hash(str(row))}"),
                row.get("person_id", "dummy_alice"),
                row.get("loinc_code", ""),
                row["test_name"],
                row.get("value"),
                row.get("value_text"),
                row.get("unit", ""),
                row.get("ref_low"),
                row.get("ref_high"),
                row["observed_at"],
                row.get("source_type", "csv"),
                row.get("source_path", "src.csv"),
            ),
        )
    conn.commit()
    return conn


def test_build_trends_groups_by_test_name() -> None:
    conn = _trends_conn(
        {"test_name": "Glucose", "value": 90.0, "observed_at": "2021-01-01"},
        {"test_name": "Glucose", "value": 95.0, "observed_at": "2021-06-01"},
        {"test_name": "TSH", "value": 2.0, "observed_at": "2021-01-01"},
        {"test_name": "TSH", "value": 2.5, "observed_at": "2021-06-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    names = {r["test_name"] for r in result}
    assert names == {"Glucose", "TSH"}
    glucose = next(r for r in result if r["test_name"] == "Glucose")
    assert len(glucose["readings"]) == 2


def test_format_reference_range_renders_single_sided_bounds() -> None:
    assert _format_reference_range(100.0, 200.0) == "100.0–200.0"
    assert _format_reference_range(None, 200.0) == "≤200.0"
    assert _format_reference_range(40.0, None) == "≥40.0"
    assert _format_reference_range(None, None) == "—"


def test_build_trends_in_range_latest_uses_single_bound_when_only_one_present() -> None:
    # Cholesterol/LDL are commonly reported with only an upper reference bound
    # (no clinical lower-bound concern); in_range_latest must classify these
    # using whichever bound is present rather than falling back to None just
    # because the other bound is absent (this previously hid genuinely
    # out-of-range cholesterol/LDL results from the Out of Range section).
    conn = _trends_conn(
        {
            "test_name": "Cholesterol",
            "value": 255.0,
            "ref_low": None,
            "ref_high": 200.0,
            "observed_at": "2023-02-13",
        },
        {
            "test_name": "Triglycerides",
            "value": 89.0,
            "ref_low": None,
            "ref_high": 150.0,
            "observed_at": "2023-02-13",
        },
        {
            "test_name": "HDL Cholesterol",
            "value": 62.0,
            "ref_low": 40.0,
            "ref_high": None,
            "observed_at": "2023-02-13",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    by_name = {r["test_name"]: r["in_range_latest"] for r in result}
    assert by_name == {
        "Cholesterol": False,
        "Triglycerides": True,
        "HDL Cholesterol": True,
    }


def test_build_trends_merges_ldl_calculated_aliases() -> None:
    """LDL Calculated, LDL-C (NIH Calc), and LDL, Calculated are the same
    calculated-LDL result under different lab-vendor display names and must
    converge into one trend."""
    conn = _trends_conn(
        {"test_name": "LDL Calculated", "value": 160.0, "observed_at": "2019-08-23"},
        {"test_name": "LDL-C (NIH Calc)", "value": 188.0, "observed_at": "2021-10-09"},
        {"test_name": "LDL Calculated", "value": 175.0, "observed_at": "2023-02-12"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert len(result) == 1
    ldl = result[0]
    assert ldl["test_name"] == "LDL Calculated"
    assert [r["value"] for r in ldl["readings"]] == [160.0, 188.0, 175.0]


def test_build_trends_merges_case_and_punctuation_aliases_for_same_loinc() -> None:
    conn = _trends_conn(
        {
            "test_name": "Eosinophils Absolute",
            "loinc_code": "711-2",
            "value": 0.2,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Eosinophils, Absolute",
            "loinc_code": "711-2",
            "value": 0.3,
            "observed_at": "2021-06-01",
        },
        {
            "test_name": "CREATININE",
            "loinc_code": "2160-0",
            "value": 0.9,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Creatinine",
            "loinc_code": "2160-0",
            "value": 1.0,
            "observed_at": "2021-06-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")

    assert {row["test_name"] for row in result} == {"Creatinine", "Eosinophils, absolute"}
    eosinophils = next(row for row in result if row["test_name"] == "Eosinophils, absolute")
    assert eosinophils["loinc_code"] == "711-2"
    assert [reading["value"] for reading in eosinophils["readings"]] == [0.2, 0.3]


def test_build_trends_merges_hdl_loinc_variants() -> None:
    # HDL (111273-9) and HDL Cholesterol (2085-9) are the same measurement;
    # both normalize to "HDL Cholesterol" and collapse into one trend section.
    conn = _trends_conn(
        {
            "test_name": "HDL",
            "loinc_code": "2085-9",
            "value": 45.0,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "HDL CHOLESTEROL",
            "loinc_code": "2085-9",
            "value": 50.0,
            "observed_at": "2021-06-01",
        },
        {
            "test_name": "HDL",
            "loinc_code": "111273-9",
            "value": 55.0,
            "observed_at": "2021-09-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")

    assert len(result) == 1
    assert result[0]["test_name"] == "HDL Cholesterol"
    assert [r["value"] for r in result[0]["readings"]] == [45.0, 50.0, 55.0]


def test_build_trends_merges_same_name_different_loinc_codes() -> None:
    # Same normalized test name from two lab systems using different LOINC codes —
    # e.g. % Basophils reported as 706-2 by one lab and 26444-0 by another.
    # Readings should form one trend section, not two.
    conn = _trends_conn(
        {
            "test_name": "% Basophils",
            "loinc_code": "706-2",
            "value": 0.5,
            "observed_at": "2020-01-01",
        },
        {
            "test_name": "% Basophils",
            "loinc_code": "26444-0",
            "value": 0.6,
            "observed_at": "2021-01-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")

    assert len(result) == 1
    assert result[0]["test_name"] == "% Basophils"
    assert [r["value"] for r in result[0]["readings"]] == [0.5, 0.6]


def test_build_trends_collapses_cross_loinc_archive_restatement() -> None:
    conn = _trends_conn(
        {
            "test_name": "Albumin",
            "loinc_code": "100158-5",
            "value": 4.7,
            "unit": "g/dL",
            "ref_low": 3.5,
            "ref_high": 5.7,
            "observed_at": "2023-02-12",
            "source_path": "data/processed/dummy_alice/Labs/2023-02-12-CMP.pdf",
            "source_type": "pdf:mychart",
        },
        {
            "test_name": "Albumin",
            "loinc_code": "1751-7",
            "value": 4.7,
            "unit": "g/dL",
            "ref_low": 3.5,
            "ref_high": 5.7,
            "observed_at": "2023-02-13T03:32:42",
            "source_path": "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML",
            "source_type": "source_import",
        },
    )

    result = build_lab_trends_from_db(conn, "dummy_alice")

    assert len(result) == 1
    assert result[0]["test_name"] == "Albumin"
    assert result[0]["loinc_code"] == "1751-7"
    assert [reading["value"] for reading in result[0]["readings"]] == [4.7]


def test_collapse_archive_restated_lab_rows_collapses_cross_loinc_duplicate() -> None:
    pdf_row = {
        "id": "lab-pdf",
        "test_name": "Albumin",
        "loinc_code": "100158-5",
        "value": 4.7,
        "unit": "g/dL",
        "reference_range_low": 3.5,
        "reference_range_high": 5.7,
        "observed_at": "2023-02-12",
        "source_path": "data/processed/dummy_alice/Labs/2023-02-12-CMP.pdf",
        "source_type": "pdf:mychart",
    }
    archive_row = {
        "id": "lab-archive",
        "test_name": "Albumin",
        "loinc_code": "1751-7",
        "value": 4.7,
        "unit": "g/dL",
        "reference_range_low": 3.5,
        "reference_range_high": 5.7,
        "observed_at": "2023-02-13T03:32:42",
        "source_path": "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML",
        "source_type": "source_import",
    }

    result = collapse_archive_restated_lab_rows([pdf_row, archive_row])

    assert len(result) == 1
    assert result[0]["id"] == "lab-archive"
    assert result[0]["loinc_code"] == "1751-7"


def test_collapse_archive_restated_lab_rows_keeps_distinct_same_value_readings() -> None:
    earlier_row = {
        "id": "lab-1",
        "test_name": "Albumin",
        "loinc_code": "1751-7",
        "value": 4.7,
        "unit": "g/dL",
        "reference_range_low": 3.5,
        "reference_range_high": 5.7,
        "observed_at": "2023-02-12",
        "source_path": "data/processed/dummy_alice/Labs/2023-02-12-CMP.pdf",
        "source_type": "pdf:mychart",
    }
    later_row = {
        "id": "lab-2",
        "test_name": "Albumin",
        "loinc_code": "1751-7",
        "value": 4.7,
        "unit": "g/dL",
        "reference_range_low": 3.5,
        "reference_range_high": 5.7,
        "observed_at": "2023-08-01",
        "source_path": "data/processed/dummy_alice/HealthSummary/archive.zip#DOC0002.XML",
        "source_type": "source_import",
    }

    result = collapse_archive_restated_lab_rows([earlier_row, later_row])

    assert {row["id"] for row in result} == {"lab-1", "lab-2"}


def test_build_trends_merges_blank_loinc_into_unambiguous_coded_series() -> None:
    conn = _trends_conn(
        {
            "test_name": "Creatinine",
            "loinc_code": "",
            "value": 0.9,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "CREATININE",
            "loinc_code": "2160-0",
            "value": 1.0,
            "observed_at": "2021-06-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")

    assert len(result) == 1
    assert result[0]["test_name"] == "Creatinine"
    assert result[0]["loinc_code"] == "2160-0"
    assert [reading["value"] for reading in result[0]["readings"]] == [0.9, 1.0]


def test_build_trends_excludes_specimen_metadata_rows() -> None:
    conn = _trends_conn(
        {"test_name": "Urine Dipstick Lot Number", "value": 912031.0, "observed_at": "2021-01-01"},
        {"test_name": "POC Kit Lot Number", "value": 503039.0, "observed_at": "2021-01-01"},
        {
            "test_name": "POCT URINALYSIS DIPSTICK LOT #",
            "value": 802037.0,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Blood Expiration Date",
            "value": 20251102235900.0,
            "observed_at": "2021-01-01",
        },
        {"test_name": "Glucose", "value": 90.0, "observed_at": "2021-01-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    names = {r["test_name"] for r in result}
    assert names == {"Glucose"}


def test_build_trends_trend_up() -> None:
    conn = _trends_conn(
        {"test_name": "ALT", "value": 20.0, "observed_at": "2020-01-01"},
        {"test_name": "ALT", "value": 30.0, "observed_at": "2021-01-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["trend"] == "up"


def test_build_trends_trend_down() -> None:
    conn = _trends_conn(
        {"test_name": "ALT", "value": 30.0, "observed_at": "2020-01-01"},
        {"test_name": "ALT", "value": 20.0, "observed_at": "2021-01-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["trend"] == "down"


def test_build_trends_trend_stable() -> None:
    conn = _trends_conn(
        {"test_name": "ALT", "value": 25.0, "observed_at": "2020-01-01"},
        {"test_name": "ALT", "value": 25.0, "observed_at": "2021-01-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["trend"] == "stable"


def test_build_trends_qualitative_when_all_null() -> None:
    conn = _trends_conn(
        {
            "test_name": "Color",
            "value": None,
            "value_text": "Yellow",
            "observed_at": "2020-01-01",
        },
        {
            "test_name": "Color",
            "value": None,
            "value_text": "Clear",
            "observed_at": "2021-01-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["trend"] == "qualitative"
    assert [reading["value_text"] for reading in result[0]["readings"]] == ["Yellow", "Clear"]


def test_build_trends_does_not_treat_comparators_as_exact_values() -> None:
    conn = _trends_conn(
        {
            "test_name": "Dummy bounded result",
            "value": None,
            "value_text": "<0.1",
            "observed_at": "2098-01-01",
        },
        {
            "test_name": "Dummy bounded result",
            "value": None,
            "value_text": "above 60",
            "observed_at": "2099-01-01",
        },
    )

    result = build_lab_trends_from_db(conn, "dummy_alice")

    assert result[0]["trend"] == "qualitative"
    assert [reading["value"] for reading in result[0]["readings"]] == [None, None]
    assert [reading["value_text"] for reading in result[0]["readings"]] == [
        "<0.1",
        "above 60",
    ]


def test_build_trends_single_reading_trend_is_none() -> None:
    conn = _trends_conn(
        {"test_name": "PSA", "value": 3.2, "observed_at": "2021-01-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["trend"] is None


def test_build_trends_in_range_true() -> None:
    conn = _trends_conn(
        {
            "test_name": "Glucose",
            "value": 90.0,
            "ref_low": 70.0,
            "ref_high": 100.0,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Glucose",
            "value": 95.0,
            "ref_low": 70.0,
            "ref_high": 100.0,
            "observed_at": "2021-06-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["in_range_latest"] is True


def test_build_trends_in_range_false() -> None:
    conn = _trends_conn(
        {
            "test_name": "Glucose",
            "value": 80.0,
            "ref_low": 70.0,
            "ref_high": 100.0,
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Glucose",
            "value": 115.0,
            "ref_low": 70.0,
            "ref_high": 100.0,
            "observed_at": "2021-06-01",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["in_range_latest"] is False


def test_build_trends_in_range_none_when_no_bounds() -> None:
    conn = _trends_conn(
        {"test_name": "ALT", "value": 20.0, "observed_at": "2021-01-01"},
        {"test_name": "ALT", "value": 25.0, "observed_at": "2021-06-01"},
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result[0]["in_range_latest"] is None


def test_build_trends_deduplicates_same_date_keeps_first() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for i, val in enumerate([10.0, 20.0]):
        conn.execute(
            """
            INSERT INTO lab_results
                (id, person_id, loinc_code, test_name, value, unit,
                 reference_range_low, reference_range_high, observed_at, source_type,
                 source_path)
            VALUES (?, 'dummy_alice', '', 'Glucose', ?, '', NULL, NULL, '2021-01-01',
                    'csv', 'src.csv')
            """,
            (f"id-{i}", val),
        )
    conn.commit()
    result = build_lab_trends_from_db(conn, "dummy_alice")
    glucose = next(r for r in result if r["test_name"] == "Glucose")
    # Only one reading per date after deduplication
    assert len(glucose["readings"]) == 1
    assert glucose["readings"][0]["value"] == 10.0


def test_build_trends_collapses_healthsummary_restatement_of_same_fact() -> None:
    conn = _trends_conn(
        {
            "test_name": "C-Reactive Protein, Cardiac",
            "value": 7.09,
            "unit": "mg/L",
            "ref_low": 0.0,
            "ref_high": 3.0,
            "observed_at": "2021-10-09",
            "source_type": "pdf:mychart",
            "source_path": "data/processed/dummy_alice/Labs/2021-10-09-HIGH SENSITIVITY CRP.pdf",
        },
        {
            "test_name": "C-Reactive Protein, Cardiac - LC",
            "value": 7.09,
            "unit": "mg/L",
            "ref_low": 0.0,
            "ref_high": 3.0,
            "observed_at": "2021-10-10T11:08:00",
            "source_type": "source_import",
            "source_path": (
                "data/processed/dummy_alice/HealthSummary/"
                "HealthSummary_Jun_03_2026.zip#IHE_XDM/Dummy Alice/DOC0001.XML"
            ),
        },
    )

    result = build_lab_trends_from_db(conn, "dummy_alice")
    crp = next(r for r in result if r["test_name"] == "C-Reactive Protein, Cardiac")
    assert len(crp["readings"]) == 1
    assert crp["readings"][0]["date"] == "2021-10-10T11:08:00"
    assert crp["readings"][0]["value"] == 7.09


def test_build_trends_groups_lc_suffix_with_base_test_name() -> None:
    conn = _trends_conn(
        {
            "test_name": "Triglycerides",
            "value": 110.0,
            "unit": "mg/dL",
            "observed_at": "2021-01-01",
        },
        {
            "test_name": "Triglycerides - LC",
            "value": 123.0,
            "unit": "mg/dL",
            "observed_at": "2021-06-01",
        },
    )

    result = build_lab_trends_from_db(conn, "dummy_alice")
    triglycerides = next(r for r in result if r["test_name"] == "Triglycerides")
    assert len(triglycerides["readings"]) == 2
    assert [reading["date"] for reading in triglycerides["readings"]] == [
        "2021-01-01",
        "2021-06-01",
    ]


def test_build_trends_empty_person_returns_empty() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    assert build_lab_trends_from_db(conn, "nobody") == []


def test_build_trends_other_person_excluded() -> None:
    conn = _trends_conn(
        {
            "test_name": "Glucose",
            "value": 90.0,
            "observed_at": "2021-01-01",
            "person_id": "dummy_bob",
        },
    )
    result = build_lab_trends_from_db(conn, "dummy_alice")
    assert result == []


# ---------------------------------------------------------------------------
# render_lab_trends_report
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# write_lab_trends_report
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_approaching_reference_threshold (ADR-030 Exclusion 1)
# ---------------------------------------------------------------------------


def _row_approaching_threshold(
    previous: float | None,
    latest: float | None,
    ref_low: float | None,
    ref_high: float | None,
) -> dict:
    return {
        "readings": [
            {
                "date": "2021-01-01",
                "value": previous,
                "ref_low": ref_low,
                "ref_high": ref_high,
            },
            {
                "date": "2021-07-01",
                "value": latest,
                "ref_low": ref_low,
                "ref_high": ref_high,
            },
        ],
    }


def test_is_approaching_reference_threshold_false_when_well_inside_range() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(45.0, 55.0, 0.0, 100.0))
        is False
    )


def test_is_approaching_reference_threshold_true_near_lower_bound_moving_down() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(25.0, 5.0, 0.0, 100.0))
        is True
    )


def test_is_approaching_reference_threshold_true_near_upper_bound_moving_up() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(75.0, 95.0, 0.0, 100.0))
        is True
    )


def test_is_approaching_reference_threshold_false_when_moving_away_from_bound() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(95.0, 75.0, 0.0, 100.0))
        is False
    )


def test_is_approaching_reference_threshold_false_when_no_ref_bounds() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(40.0, 50.0, None, None))
        is False
    )


def test_is_approaching_reference_threshold_uses_single_bound_magnitude() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(75.0, 90.0, None, 100.0))
        is True
    )
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(50.0, 60.0, None, 100.0))
        is False
    )


def test_is_approaching_reference_threshold_false_for_invalid_range() -> None:
    assert (
        _is_approaching_reference_threshold(_row_approaching_threshold(50.0, 50.0, 50.0, 50.0))
        is False
    )


def test_is_approaching_reference_threshold_false_without_two_numeric_readings() -> None:
    row = {
        "readings": [
            {"date": "2021-01-01", "value": None, "ref_low": 0.0, "ref_high": 100.0},
            {"date": "2021-07-01", "value": 95.0, "ref_low": 0.0, "ref_high": 100.0},
        ]
    }
    assert _is_approaching_reference_threshold(row) is False


# ---------------------------------------------------------------------------
# _two_most_recent_panel_dates and _is_stale_result (ADR-030 Exclusion 2)
# ---------------------------------------------------------------------------


def _trend_row(dates: list[str]) -> dict:
    return {
        "readings": [{"date": d, "value": 1.0, "ref_low": None, "ref_high": None} for d in dates]
    }


def test_two_most_recent_panel_dates_returns_top_2() -> None:
    rows = [
        _trend_row(["2019-01-01", "2020-01-01"]),
        _trend_row(["2021-01-01"]),
    ]
    result = _two_most_recent_panel_dates(rows)
    assert result == frozenset({"2021-01-01", "2020-01-01"})


def test_two_most_recent_panel_dates_single_date() -> None:
    rows = [_trend_row(["2021-01-01"])]
    assert _two_most_recent_panel_dates(rows) == frozenset({"2021-01-01"})


def test_two_most_recent_panel_dates_empty() -> None:
    assert _two_most_recent_panel_dates([]) == frozenset()


def test_is_stale_result_false_when_recent() -> None:
    row = _trend_row(["2020-01-01", "2021-01-01"])
    assert _is_stale_result(row, frozenset({"2021-01-01", "2020-06-01"})) is False


def test_is_stale_result_true_when_old() -> None:
    row = _trend_row(["2018-01-01", "2019-01-01"])
    assert _is_stale_result(row, frozenset({"2021-01-01", "2020-06-01"})) is True


def test_is_stale_result_false_when_empty_recent_dates() -> None:
    row = _trend_row(["2021-01-01"])
    assert _is_stale_result(row, frozenset()) is False


def test_is_stale_result_false_when_empty_readings() -> None:
    row = {"readings": []}
    assert _is_stale_result(row, frozenset({"2021-01-01"})) is False


def test_most_recent_panel_date_returns_latest_date() -> None:
    rows = [_trend_row(["2021-01-01"]), _trend_row(["2021-03-15", "2021-06-01"])]
    assert _most_recent_panel_date(rows) == date(2021, 6, 1)


def test_most_recent_panel_date_ignores_point_of_care_test_name() -> None:
    # A standalone POCT dipstick visit is newer than the last comprehensive panel,
    # but must not become the anchor — it would shrink the Out-of-Range window for
    # unrelated tests that were never re-checked at that visit.
    comprehensive_panel = _trend_row(["2021-01-01"])
    poct_visit = _trend_row(["2021-06-01"])
    poct_visit["test_name"] = "POCT Glucose iSTAT"
    rows = [comprehensive_panel, poct_visit]
    assert _most_recent_panel_date(rows) == date(2021, 1, 1)


def test_is_out_of_range_result_stale_uses_window_from_latest_panel_date() -> None:
    row = _trend_row(["2020-11-01", "2021-01-15"])
    assert _is_out_of_range_result_stale(row, date(2021, 7, 1), window_days=180) is False


def test_is_out_of_range_result_stale_true_outside_window() -> None:
    row = _trend_row(["2020-01-01"])
    assert _is_out_of_range_result_stale(row, date(2021, 7, 1), window_days=180) is True


# ---------------------------------------------------------------------------
# render_flagged_for_discussion_report
# ---------------------------------------------------------------------------


def _flagged_row(
    test_name: str,
    readings: list[dict],
    *,
    trend: str | None,
    in_range_latest: bool | None,
) -> dict:
    return {
        "test_name": test_name,
        "loinc_code": "",
        "readings": readings,
        "trend": trend,
        "in_range_latest": in_range_latest,
    }


def test_render_flagged_out_of_range_appears_in_report() -> None:
    rows = [
        _flagged_row(
            "Glucose",
            [
                {
                    "date": "2021-01-01",
                    "value": 95.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-01-01",
                    "value": 115.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
            ],
            trend=None,
            in_range_latest=False,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "## Out of Range" in report
    assert "Glucose" in report
    out_of_range_section = report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]
    assert "<svg " in out_of_range_section  # visual chart rendered
    assert "115" in out_of_range_section  # latest value shown


def test_render_flagged_out_of_range_splits_newly_flagged_results_to_top() -> None:
    rows = [
        _flagged_row(
            "Glucose",
            [
                {
                    "date": "2021-01-01",
                    "value": 95.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 115.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
            ],
            trend=None,
            in_range_latest=False,
        ),
        _flagged_row(
            "LDL",
            [
                {
                    "date": "2021-01-01",
                    "value": 150.0,
                    "unit": "mg/dL",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 155.0,
                    "unit": "mg/dL",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
            ],
            trend=None,
            in_range_latest=False,
        ),
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    out_of_range_section = report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]
    newly_index = out_of_range_section.index("### Newly Out of Range")
    other_index = out_of_range_section.index("### Other Out of Range")
    assert newly_index < other_index
    newly_block = out_of_range_section[newly_index:other_index]
    other_block = out_of_range_section[other_index:]
    assert "Glucose" in newly_block and "LDL" not in newly_block
    assert "LDL" in other_block and "Glucose" not in other_block


def test_render_flagged_out_of_range_no_split_without_a_previous_reading() -> None:
    rows = [
        _flagged_row(
            "Glucose",
            [
                {
                    "date": "2021-07-01",
                    "value": 115.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
            ],
            trend=None,
            in_range_latest=False,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    out_of_range_section = report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]
    assert "### Newly Out of Range" not in out_of_range_section
    assert "Glucose" in out_of_range_section


def test_render_flagged_trending_appears_in_report() -> None:
    # Value 38 in [0, 40]: margin = 4. From upper: 40-38=2 < 4, so it is near.
    rows = [
        _flagged_row(
            "ALT",
            [
                {
                    "date": "2020-01-01",
                    "value": 30.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 40.0,
                },
                {
                    "date": "2021-01-01",
                    "value": 38.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 40.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "## Trending" in report
    assert "ALT" in report
    trending_section = report.split("## Trending", 1)[1]
    assert "<svg " in trending_section  # visual chart rendered
    assert "38" in trending_section  # latest value shown


def test_render_flagged_trending_excludes_rows_already_out_of_range() -> None:
    rows = [
        _flagged_row(
            "Glucose",
            [
                {
                    "date": "2021-01-01",
                    "value": 95.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 115.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                },
            ],
            trend="up",
            in_range_latest=False,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    out_of_range = report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]
    trending = report.split("## Trending", 1)[1]
    assert "Glucose" in out_of_range
    assert "Glucose" not in trending


def test_render_flagged_trending_excludes_near_bound_result_moving_away() -> None:
    rows = [
        _flagged_row(
            "ALT",
            [
                {
                    "date": "2021-01-01",
                    "value": 39.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 40.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 32.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 40.0,
                },
            ],
            trend="down",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "ALT" not in report.split("## Trending", 1)[1]


@pytest.mark.parametrize(
    ("test_name", "previous", "latest", "ref_low", "ref_high"),
    [
        ("Lower-bound marker", 24.5, 20.0, 14.0, 70.0),
        ("Upper-bound marker", 89.0, 132.0, 0.0, 149.0),
    ],
)
def test_render_flagged_trending_excludes_results_outside_ten_percent_margin(
    test_name: str,
    previous: float,
    latest: float,
    ref_low: float,
    ref_high: float,
) -> None:
    rows = [
        _flagged_row(
            test_name,
            [
                {
                    "date": "2023-02-13",
                    "value": previous,
                    "unit": "mg/dL",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                },
                {
                    "date": "2023-07-07",
                    "value": latest,
                    "unit": "mg/dL",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                },
            ],
            trend="up" if latest > previous else "down",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert test_name not in report.split("## Trending", 1)[1]


def test_render_flagged_trending_includes_near_bound_small_directional_change() -> None:
    rows = [
        _flagged_row(
            "Screening marker",
            [
                {
                    "date": "2023-02-13",
                    "value": 7.9,
                    "unit": "units",
                    "ref_low": 0.0,
                    "ref_high": 8.0,
                },
                {
                    "date": "2023-07-07",
                    "value": 7.95,
                    "unit": "units",
                    "ref_low": 0.0,
                    "ref_high": 8.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "Screening marker" in report.split("## Trending", 1)[1]


@pytest.mark.parametrize(
    ("test_name", "previous", "latest", "ref_low", "ref_high"),
    [
        ("Upper-only marker", 75.0, 90.0, None, 100.0),
        ("Lower-only marker", 80.0, 64.0, 59.0, None),
    ],
)
def test_render_flagged_trending_includes_single_bound_result_approaching_threshold(
    test_name: str,
    previous: float,
    latest: float,
    ref_low: float | None,
    ref_high: float | None,
) -> None:
    rows = [
        _flagged_row(
            test_name,
            [
                {
                    "date": "2021-01-01",
                    "value": previous,
                    "unit": "mg/dL",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                },
                {
                    "date": "2021-07-01",
                    "value": latest,
                    "unit": "mg/dL",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                },
            ],
            trend="up" if latest > previous else "down",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert test_name in report.split("## Trending", 1)[1]


@pytest.mark.parametrize(
    ("test_name", "previous", "latest", "ref_low", "ref_high"),
    [
        ("Upper-only marker", 50.0, 60.0, None, 100.0),
        ("Lower-only marker", 100.0, 80.0, 59.0, None),
    ],
)
def test_render_flagged_trending_excludes_single_bound_result_far_from_threshold(
    test_name: str,
    previous: float,
    latest: float,
    ref_low: float | None,
    ref_high: float | None,
) -> None:
    rows = [
        _flagged_row(
            test_name,
            [
                {
                    "date": "2021-01-01",
                    "value": previous,
                    "unit": "mg/dL",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                },
                {
                    "date": "2021-07-01",
                    "value": latest,
                    "unit": "mg/dL",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                },
            ],
            trend="up" if latest > previous else "down",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert test_name not in report.split("## Trending", 1)[1]


def test_render_flagged_exclusion1_removes_far_from_thresholds_trend() -> None:
    # Value 25 in [0,100] is more than the 10-point margin from both bounds.
    rows = [
        _flagged_row(
            "Sodium",
            [
                {
                    "date": "2020-01-01",
                    "value": 20.0,
                    "unit": "mEq/L",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-01-01",
                    "value": 25.0,
                    "unit": "mEq/L",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    trending_section = report.split("## Trending", 1)[1] if "## Trending" in report else ""
    assert "Sodium" not in trending_section


def test_render_flagged_exclusion1_keeps_near_threshold_trend() -> None:
    # Value 92 in [0,100] is 8 from the upper bound, within the 10-point margin.
    rows = [
        _flagged_row(
            "Potassium",
            [
                {
                    "date": "2020-01-01",
                    "value": 80.0,
                    "unit": "mEq/L",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-01-01",
                    "value": 92.0,
                    "unit": "mEq/L",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "Potassium" in report.split("## Trending", 1)[1]


def test_render_flagged_exclusion2_removes_stale_result() -> None:
    # Out-of-range rows now use the anchored day window while Trending still uses the
    # stricter 2-most-recent-panel-date filter.
    rows = [
        _flagged_row(
            "Older but still recent lipid result",
            [
                {
                    "date": "2021-01-15",
                    "value": 200.0,
                    "unit": "mg/dL",
                    "ref_low": 0.0,
                    "ref_high": 99.0,
                }
            ],
            trend=None,
            in_range_latest=False,
        ),
        _flagged_row(
            "Recent CMP marker",
            [
                {
                    "date": "2021-06-15",
                    "value": 90.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 110.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 100.0,
                },
            ],
            trend=None,
            in_range_latest=False,
        ),
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "Recent CMP marker" in report
    assert "Older but still recent lipid result" in report


def test_render_flagged_out_of_range_anchor_ignores_unrelated_poct_visit() -> None:
    # Comprehensive lipid panel 187 days before an unrelated POCT dipstick visit.
    # The POCT visit must not become the anchor: anchoring on it would push the
    # Out-of-Range cutoff past the lipid panel and hide a clinically recent
    # abnormal result by a few days, the failure mode ADR-030 targets.
    rows = [
        _flagged_row(
            "Cholesterol",
            [
                {
                    "date": "2021-01-01",
                    "value": 250.0,
                    "unit": "mg/dL",
                    "ref_low": 0.0,
                    "ref_high": 199.0,
                }
            ],
            trend=None,
            in_range_latest=False,
        ),
        _flagged_row(
            "POCT Spec Grav, UA",
            [
                {
                    "date": "2021-07-06",
                    "value": 1.02,
                    "unit": "",
                    "ref_low": 1.01,
                    "ref_high": 1.03,
                }
            ],
            trend=None,
            in_range_latest=True,
        ),
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "Cholesterol" in report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]


def test_render_flagged_trending_still_uses_two_most_recent_panel_dates() -> None:
    rows = [
        _flagged_row(
            "Older Trend",
            [
                {
                    "date": "2021-01-15",
                    "value": 20.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 30.0,
                },
                {
                    "date": "2021-01-15",
                    "value": 29.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 30.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        ),
        _flagged_row(
            "Newest Trend A",
            [
                {
                    "date": "2021-06-15",
                    "value": 20.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 30.0,
                },
                {
                    "date": "2021-06-15",
                    "value": 29.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 30.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        ),
        _flagged_row(
            "Newest Trend B",
            [
                {
                    "date": "2021-07-01",
                    "value": 20.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 30.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 29.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 30.0,
                },
            ],
            trend="up",
            in_range_latest=True,
        ),
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    trending_section = report.split("## Trending", 1)[1]
    assert "Newest Trend A" in trending_section
    assert "Newest Trend B" in trending_section
    assert "Older Trend" not in trending_section


def test_render_flagged_recently_normalized_appears_when_previous_was_out_of_range() -> None:
    rows = [
        _flagged_row(
            "TSH",
            [
                {
                    "date": "2021-01-01",
                    "value": 6.8,
                    "unit": "uIU/mL",
                    "ref_low": 0.4,
                    "ref_high": 5.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 4.2,
                    "unit": "uIU/mL",
                    "ref_low": 0.4,
                    "ref_high": 5.0,
                },
            ],
            trend="stable",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    normalized_section = report.split("## Recently Normalized", 1)[1]
    assert "<svg " in normalized_section  # visual chart rendered
    assert "TSH" in normalized_section


def test_render_flagged_recently_normalized_appears_for_single_bound_test() -> None:
    # build_lab_trends_from_db's in_range_latest is computed via the permissive
    # (at-least-one-bound) _reading_in_range check, so a single-bound test (one
    # reference bound present, the other absent — common for analytes like
    # cholesterol/LDL that only carry an upper threshold) still gets a real
    # True/False in_range_latest rather than the indeterminate None a stricter
    # both-bounds check would produce.
    rows = [
        _flagged_row(
            "Ferritin",
            [
                {
                    "date": "2021-01-01",
                    "value": 410.0,
                    "unit": "ng/mL",
                    "ref_low": None,
                    "ref_high": 300.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 180.0,
                    "unit": "ng/mL",
                    "ref_low": None,
                    "ref_high": 300.0,
                },
            ],
            trend="stable",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    normalized_section = report.split("## Recently Normalized", 1)[1]
    assert "Ferritin" in normalized_section


def test_render_flagged_recently_normalized_excludes_row_from_trending() -> None:
    rows = [
        _flagged_row(
            "ALT",
            [
                {
                    "date": "2021-01-01",
                    "value": 50.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 40.0,
                },
                {
                    "date": "2021-07-01",
                    "value": 35.0,
                    "unit": "U/L",
                    "ref_low": 0.0,
                    "ref_high": 40.0,
                },
            ],
            trend="down",
            in_range_latest=True,
        )
    ]

    report = render_flagged_for_discussion_report("dummy_alice", rows)
    trending_section = report.split("## Trending", 1)[1].split("## Recently Normalized", 1)[0]
    normalized_section = report.split("## Recently Normalized", 1)[1]
    assert "ALT" not in trending_section
    assert "ALT" in normalized_section


def test_render_flagged_trending_excludes_result_without_a_reference_bound() -> None:
    rows = [
        _flagged_row(
            "Mystery Marker",
            [
                {
                    "date": "2020-01-01",
                    "value": 10.0,
                    "unit": "—",
                    "ref_low": None,
                    "ref_high": None,
                },
                {
                    "date": "2021-01-01",
                    "value": 15.0,
                    "unit": "—",
                    "ref_low": None,
                    "ref_high": None,
                },
            ],
            trend="up",
            in_range_latest=None,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    assert "Mystery Marker" not in report.split("## Trending", 1)[1]


def test_render_flagged_empty_both_sections_message() -> None:
    report = render_flagged_for_discussion_report("dummy_alice", [])
    assert "_No out-of-range lab results in recent panels._" in report
    assert "_No notably trending lab results in recent panels._" in report


def test_render_flagged_front_matter_has_report_type() -> None:
    report = render_flagged_for_discussion_report("dummy_alice", [])
    assert "report_type: flagged_for_discussion" in report


def test_render_flagged_includes_coronary_calcium_section_when_present() -> None:
    report = render_flagged_for_discussion_report(
        "dummy_alice",
        [],
        coronary_calcium_findings=[
            "- 2022-03-10 — CAC score 51.9 Agatston."
            " Source: [[Records/dummy_alice/History/example|example]]"
        ],
    )
    assert "## Coronary Calcium" in report
    assert "CAC score 51.9 Agatston" in report


def test_render_flagged_tables_show_latest_date(tmp_path: Path) -> None:
    rows = [
        _flagged_row(
            "Glucose",
            [
                {
                    "date": "2021-07-31T10:20:58",
                    "value": 115.0,
                    "unit": "mg/dL",
                    "ref_low": 70.0,
                    "ref_high": 100.0,
                }
            ],
            trend=None,
            in_range_latest=False,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    out_of_range_section = report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]
    assert "<svg " in out_of_range_section
    # SVG title carries dates; verify date is trimmed to date-only
    assert "2021-07-31" in out_of_range_section
    assert "10:20:58" not in out_of_range_section  # datetime trimmed to date


def test_extract_coronary_calcium_appends_value_and_unit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    history_dir = vault / "Records" / "dummy_alice" / "History"
    history_dir.mkdir(parents=True)
    front_matter = {
        "person_id": "dummy_alice",
        "record_type": "history",
        "verification_status": "accepted",
        "accepted_facts": [
            {
                "description": "Observed Agatston coronary calcium score",
                "category": "imaging_finding",
                "recorded_date": "2022-03-10",
                "value": "51.9",
                "unit": "Agatston score",
            },
            {
                "description": "Coronary calcium scoring ordered",
                "category": "imaging_finding",
                "recorded_date": "2022-03-10",
                "value": None,
            },
        ],
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    (history_dir / "2022-03-10-history-review.md").write_text(
        f"---\n{yaml_block}\n---\n\n# History Review\n",
        encoding="utf-8",
    )

    findings = _extract_coronary_calcium_findings(vault, "dummy_alice")
    joined = "\n".join(findings)
    # Value-bearing fact renders the number and unit.
    assert "Observed Agatston coronary calcium score: 51.9 Agatston score." in joined
    # Null-value fact renders no measure, just the description.
    assert "Coronary calcium scoring ordered. Source:" in joined


# ---------------------------------------------------------------------------
# write_flagged_for_discussion_report
# ---------------------------------------------------------------------------


def test_write_flagged_creates_file(tmp_path: Path) -> None:
    conn = _trends_conn(
        {
            "test_name": "Glucose",
            "value": 115.0,
            "observed_at": "2021-01-01",
            "ref_low": 70.0,
            "ref_high": 100.0,
        },
    )
    vault = tmp_path / "vault"
    (vault / "Records" / "dummy_alice" / "Reports").mkdir(parents=True)
    report_path, count = write_flagged_for_discussion_report(conn, vault, "dummy_alice")
    assert report_path is not None
    assert report_path.exists()
    assert report_path.name == "flagged-for-discussion.md"
    assert count == 1


# ---------------------------------------------------------------------------
# SVG sparkline rendering
# ---------------------------------------------------------------------------


def _sparkline_row(
    test_name: str,
    readings: list[dict],
    *,
    in_range: bool | None = None,
    trend: str | None = "stable",
) -> dict:
    return {
        "test_name": test_name,
        "loinc_code": "",
        "readings": readings,
        "trend": trend,
        "in_range_latest": in_range,
    }


def _reading(
    date: str, value: float, unit: str, ref_low: float | None, ref_high: float | None
) -> dict:
    return {"date": date, "value": value, "unit": unit, "ref_low": ref_low, "ref_high": ref_high}


def test_render_lab_sparkline_returns_svg_string() -> None:
    row = _sparkline_row(
        "Glucose",
        [
            _reading("2021-01-01", 95.0, "mg/dL", 70.0, 100.0),
            _reading("2021-07-01", 115.0, "mg/dL", 70.0, 100.0),
        ],
        in_range=False,
    )
    svg = _render_lab_sparkline(row, in_range=False)
    assert svg is not None
    assert svg.startswith("<svg ")
    assert svg.endswith("</svg>")
    assert "\n" not in svg  # must be single line for Obsidian block rendering
    assert "Glucose" in svg
    assert "mg/dL" in svg
    assert "2021-01-01" in svg
    assert "2021-07-01" in svg


def test_render_lab_sparkline_dates_match_previous_and_latest_numeric_readings() -> None:
    row = _sparkline_row(
        "Synthetic marker",
        [
            _reading("2020-01-01", 1.0, "units", 0.0, 5.0),
            _reading("2021-01-01", 2.0, "units", 0.0, 5.0),
            _reading("2021-07-01", 3.0, "units", 0.0, 5.0),
        ],
        in_range=True,
    )

    svg = _render_lab_sparkline(row, in_range=True)

    assert svg is not None
    assert "2021-01-01 → 2021-07-01" in svg
    assert "2020-01-01" not in svg


def test_render_lab_sparkline_out_of_range_uses_red_dot() -> None:
    row = _sparkline_row(
        "LDL",
        [_reading("2021-07-01", 180.0, "mg/dL", None, 100.0)],
        in_range=False,
    )
    svg = _render_lab_sparkline(row, in_range=False)
    assert svg is not None
    assert "#c0392b" in svg  # red dot for out-of-range


def test_render_lab_sparkline_in_range_uses_green_dot() -> None:
    row = _sparkline_row(
        "TSH",
        [{"date": "2021-07-01", "value": 2.5, "unit": "uIU/mL", "ref_low": 0.4, "ref_high": 5.0}],
        in_range=True,
    )
    svg = _render_lab_sparkline(row, in_range=True)
    assert svg is not None
    assert "#3b9a57" in svg  # green dot for in-range
    assert "#c0392b" not in svg
    assert "2021-07-01 → 2021-07-01" in svg


def test_render_lab_sparkline_qualitative_only_returns_none() -> None:
    row = _sparkline_row(
        "Urine Color",
        [{"date": "2021-07-01", "value": None, "unit": "", "ref_low": None, "ref_high": None}],
        in_range=None,
    )
    svg = _render_lab_sparkline(row, in_range=None)
    assert svg is None


def test_render_lab_sparkline_single_sided_ref_high_only() -> None:
    row = _sparkline_row(
        "Cholesterol",
        [_reading("2021-07-01", 255.0, "mg/dL", None, 200.0)],
        in_range=False,
    )
    svg = _render_lab_sparkline(row, in_range=False)
    assert svg is not None
    assert "200" in svg  # ref_high label
    # ref_high tick should be present; rect should start from left edge (no ref_low tick)
    assert "#3b9a57" in svg  # reference range green
    assert "255" in svg  # latest value


def test_render_lab_sparkline_no_reference_range() -> None:
    row = _sparkline_row(
        "Immature Granulocytes",
        [
            {"date": "2021-01-01", "value": 0.02, "unit": "", "ref_low": None, "ref_high": None},
            {"date": "2021-07-01", "value": 0.01, "unit": "", "ref_low": None, "ref_high": None},
        ],
        in_range=None,
    )
    svg = _render_lab_sparkline(row, in_range=None)
    assert svg is not None
    assert "<rect" not in svg  # no reference range rectangle


def test_sparkline_axis_symmetric_margin() -> None:
    axis_min, axis_max = _sparkline_axis(70.0, 100.0, 95.0, 115.0)
    assert axis_min < 70.0
    assert axis_max > 115.0
    # Both dots and ref range must fit inside
    assert axis_min < 95.0
    assert axis_max > 115.0


def test_sparkline_axis_single_sided_high_only() -> None:
    axis_min, axis_max = _sparkline_axis(None, 200.0, None, 255.0)
    assert axis_min < 200.0
    assert axis_max > 255.0


def test_render_flagged_report_contains_svgs_not_tables() -> None:
    rows = [
        _flagged_row(
            "Glucose",
            [
                _reading("2021-01-01", 95.0, "mg/dL", 70.0, 100.0),
                _reading("2021-07-01", 115.0, "mg/dL", 70.0, 100.0),
            ],
            trend=None,
            in_range_latest=False,
        )
    ]
    report = render_flagged_for_discussion_report("dummy_alice", rows)
    # SVG present in Out of Range section; no pipe table or HTML comment
    out_section = report.split("## Out of Range", 1)[1].split("## Trending", 1)[0]
    assert "<svg " in out_section
    assert "Glucose" in out_section
    assert "<!--" not in report
    assert "| Glucose |" not in report


def test_render_flagged_report_build_lab_observations_for_advisory() -> None:
    """Verify build_lab_observations_for_advisory surfaces the right data from rows."""
    from famed.lab_sync import build_lab_observations_for_advisory

    rows = [
        _flagged_row(
            "LDL",
            [
                _reading("2021-01-01", 120.0, "mg/dL", None, 100.0),
                _reading("2021-07-01", 145.0, "mg/dL", None, 100.0),
            ],
            trend="up",
            in_range_latest=False,
        )
    ]
    obs = build_lab_observations_for_advisory(rows)
    assert any("LDL" in line for line in obs)
    assert any("145" in line for line in obs)


def test_write_flagged_idempotent(tmp_path: Path) -> None:
    conn = _trends_conn({"test_name": "Glucose", "value": 90.0, "observed_at": "2021-01-01"})
    vault = tmp_path / "vault"
    (vault / "Records" / "dummy_alice" / "Reports").mkdir(parents=True)
    path1, _ = write_flagged_for_discussion_report(conn, vault, "dummy_alice")
    assert path1 is not None
    path2, _ = write_flagged_for_discussion_report(conn, vault, "dummy_alice")
    assert path2 is not None
    assert path1 == path2
    assert path1.read_text(encoding="utf-8") == path2.read_text(encoding="utf-8")


def test_write_flagged_includes_coronary_calcium_findings_from_history(tmp_path: Path) -> None:
    conn = _trends_conn({"test_name": "Glucose", "value": 90.0, "observed_at": "2021-07-01"})
    vault = tmp_path / "vault"
    history_dir = vault / "Records" / "dummy_alice" / "History"
    (vault / "Records" / "dummy_alice" / "Reports").mkdir(parents=True)
    history_dir.mkdir(parents=True)
    front_matter = {
        "person_id": "dummy_alice",
        "record_type": "history",
        "verification_status": "accepted",
        "accepted_facts": [
            {
                "description": "CAC score 51.9 Agatston",
                "category": "imaging_finding",
                "recorded_date": "2022-03-10",
            },
            {
                "description": "Coronary calcium score percentile 57",
                "category": "imaging_finding",
                "recorded_date": "2022-03-10",
            },
        ],
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    (history_dir / "2022-03-10-history-review.md").write_text(
        f"---\n{yaml_block}\n---\n\n# History Review\n",
        encoding="utf-8",
    )

    report_path, _ = write_flagged_for_discussion_report(conn, vault, "dummy_alice")
    assert report_path is not None
    report = report_path.read_text(encoding="utf-8")
    assert "## Coronary Calcium" in report
    assert "CAC score 51.9 Agatston" in report
    assert "Coronary calcium score percentile 57" in report


# ---------------------------------------------------------------------------
# parse_labs_from_poct_general_text (occult blood stool and similar formats)
# ---------------------------------------------------------------------------

_POCT_STOOL_SAMPLE = """\
Name: Patient Name | DOB: 01/01/1970
POCT OCCULT BLOOD STOOL
Collected on Sep 13, 2015 9:14 AM
Results
Authorizing provider: Dr Smith
Collection date: Sep 13, 2015
Result status: Final
Resulting lab:
LEGACY KIRKLAND
New
Fecal Occult Blood
Value
Negative
POC Stool Occult Intrnl Cntrl Rslts OK
Value
Yes
Stool Occult Card / Strip Lot #
Value
7X014M
Test Strip Expiration Date
Value
03/12/2016
"""


def test_poct_general_extracts_clinical_results() -> None:
    rows = parse_labs_from_poct_general_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    names = [r["test_name"] for r in rows]
    assert "Fecal Occult Blood" in names


def test_poct_general_excludes_lot_numbers() -> None:
    rows = parse_labs_from_poct_general_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    names = [r["test_name"] for r in rows]
    assert not any("Lot" in n for n in names)


def test_poct_general_excludes_expiration_dates() -> None:
    rows = parse_labs_from_poct_general_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    names = [r["test_name"] for r in rows]
    assert not any("Expiration" in n for n in names)


def test_poct_general_excludes_internal_control_rows() -> None:
    rows = parse_labs_from_poct_general_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    names = [r["test_name"] for r in rows]
    assert not any("Cntrl" in n or "Control" in n for n in names)


def test_poct_general_qualitative_value_stored() -> None:
    rows = parse_labs_from_poct_general_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    stool = next(r for r in rows if r["test_name"] == "Fecal Occult Blood")
    assert stool["value_text"] == "Negative"
    assert stool["value"] is None  # qualitative → no numeric value


def test_poct_general_observed_at_extracted() -> None:
    rows = parse_labs_from_poct_general_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    assert all(r["observed_at"] == "2015-09-13" for r in rows)


def test_poct_general_raises_when_no_clinical_rows() -> None:
    text = (
        "POCT OCCULT BLOOD STOOL\n"
        "Collected on Sep 13, 2015\n"
        "Stool Occult Card / Strip Lot #\n"
        "Value\n"
        "7X014M\n"
    )
    try:
        parse_labs_from_poct_general_text(text, "dummy_alice", "stool.pdf")
    except LabSyncError:
        pass
    else:
        raise AssertionError("Expected LabSyncError")


def test_pdf_text_routes_to_poct_general_for_stool() -> None:
    """parse_labs_from_pdf_text falls through to the general POCT parser for stool format."""
    rows = parse_labs_from_pdf_text(_POCT_STOOL_SAMPLE, "dummy_alice", "stool.pdf")
    names = [r["test_name"] for r in rows]
    assert "Fecal Occult Blood" in names
    assert not any("Lot" in n for n in names)


def _insert_lab_row(
    conn: sqlite3.Connection,
    row_id: str,
    *,
    test_name: str,
    value: str,
    observed_at: str,
    source_path: str,
    unit: str = "mmol/L",
    loinc_code: str = "2951-2",
    value_text: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, unit,
            reference_range_low, reference_range_high, observed_at, source_type,
            source_path, value_text
        ) VALUES (?, 'dummy_alice', ?, 'source_provided', ?, ?, ?, NULL, NULL, ?,
                  'source_import', ?, ?)
        """,
        (
            row_id,
            loinc_code,
            test_name,
            float(value) if value else None,
            unit,
            observed_at,
            source_path,
            value_text,
        ),
    )


def test_assess_coverage_matches_identical_value_across_midnight(tmp_path: Path) -> None:
    """Regression: the same draw can carry different calendar dates across
    sources -- a provider PDF states the local collection date while a C-CDA
    export timestamps the same reading in UTC, landing just past midnight.
    Matching on the date string alone reported 26 real notes as having zero
    coverage when every fact was canonical under the other source.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_lab_row(
        conn,
        "ccda-row",
        test_name="Sodium",
        value="139",
        observed_at="2018-01-01T04:53:00",
        source_path="archive.zip#DOC0001.XML",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-12-31",
        [("Sodium", "2951-2", "139", "mmol/L")],
        source_path="2017-12-31-panel.pdf",
        filename="2017-12-31-old-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")

    assert len(coverage) == 1
    assert coverage[0].uncovered_test_names == ()
    assert coverage[0].category == "covered_no_note"


def test_assess_coverage_keeps_distinct_adjacent_draws_uncovered(tmp_path: Path) -> None:
    """The date tolerance is anchored on the value: two genuine draws a day
    apart carry different values and must stay uncovered. Without this, a
    real next-day result would silently absorb the previous day's note.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_lab_row(
        conn,
        "next-day-row",
        test_name="Sodium",
        value="145",
        observed_at="2018-01-01T04:53:00",
        source_path="archive.zip#DOC0001.XML",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-12-31",
        [("Sodium", "2951-2", "139", "mmol/L")],
        source_path="2017-12-31-panel.pdf",
        filename="2017-12-31-old-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")

    assert len(coverage) == 1
    assert coverage[0].uncovered_test_names == ("Sodium",)
    assert coverage[0].category == "no_replacement"


def test_assess_coverage_does_not_match_beyond_one_day(tmp_path: Path) -> None:
    """The tolerance is one day, not an open window: a matching value two days
    away is a different draw, not a timezone artifact.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_lab_row(
        conn,
        "two-days-later",
        test_name="Sodium",
        value="139",
        observed_at="2018-01-02T04:53:00",
        source_path="archive.zip#DOC0001.XML",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-12-31",
        [("Sodium", "2951-2", "139", "mmol/L")],
        source_path="2017-12-31-panel.pdf",
        filename="2017-12-31-old-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")

    assert coverage[0].uncovered_test_names == ("Sodium",)


def test_assess_coverage_matches_qualitative_value_across_midnight(tmp_path: Path) -> None:
    """Qualitative readings carry no numeric value, so they anchor on
    value_text instead. They shift across midnight for the same reason.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_lab_row(
        conn,
        "qual-row",
        test_name="Urine Culture",
        value="",
        observed_at="2018-01-01T04:53:00",
        source_path="archive.zip#DOC0001.XML",
        unit="",
        loinc_code="630-4",
        value_text="No growth",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-12-31",
        [("Urine Culture", "630-4", "No growth", "")],
        source_path="2017-12-31-panel.pdf",
        filename="2017-12-31-old-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")

    assert coverage[0].uncovered_test_names == ()


def test_assess_coverage_survives_non_numeric_value_cell(tmp_path: Path) -> None:
    """A hand-edited note can render text in the value column. The read-only
    diagnostic must degrade to text matching rather than aborting the scan.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_lab_row(
        conn,
        "other-row",
        test_name="Sodium",
        value="139",
        observed_at="2018-01-01",
        source_path="archive.zip#DOC0001.XML",
    )
    _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-12-31",
        [("Sodium", "2951-2", "pending", "mmol/L")],
        source_path="2017-12-31-panel.pdf",
        filename="2017-12-31-old-panel.md",
    )

    coverage = assess_lab_panel_note_coverage(conn, tmp_path, "dummy_alice")

    assert coverage[0].uncovered_test_names == ("Sodium",)


def test_parse_labs_from_pdf_text_keeps_area_qualified_unit() -> None:
    """Regression: a unit carrying a body-surface-area qualifier separated by a
    space, as in eGFR's "mL/min/1.73 m2", was cut to its final token by the
    space-delimited scan and persisted as "m2". Nine live rows carried a
    truncated unit before this fix.
    """
    text = """
Laboratory Corporation of America
Date Collected: 04/20/2023
Renal Panel
Test Current Result and Flag Previous Result and Date Units Reference Interval
eGFR 85 82 01/15/2022 mL/min/1.73 m2 >59
"""

    rows = parse_labs_from_pdf_text(
        text,
        person_id="dummy_alice",
        source_label="renal.pdf",
    )

    egfr = [row for row in rows if row["test_name"] == "eGFR"]
    assert len(egfr) == 1
    assert egfr[0]["unit"] == "mL/min/1.73 m2"


def test_reviewed_repair_replaces_pediatric_egfr_equation_code(tmp_path: Path) -> None:
    """The CKiD equation (102097-3) is pediatric and combines creatinine,
    cystatin C and urea. Inference assigned it to adults because the source
    PDFs state only "eGFR" with no equation. 69405-9 is the honest
    equation-unspecified code. Source-provided equation codes must be
    untouched (REQ-003).
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for row_id, code, origin, unit in [
        ("inferred-trunc", "102097-3", "inferred", "mL/min/1"),
        ("inferred-area", "102097-3", "inferred", "mL/min/1.73 m2"),
        ("source-ckdepi", "62238-1", "source_provided", "mL/min/1.73 m2"),
        ("source-2021", "98979-8", "source_provided", "mL/min/1.73"),
    ]:
        conn.execute(
            """
            INSERT INTO lab_results (
                id, person_id, loinc_code, loinc_code_origin, test_name, value,
                unit, reference_range_low, reference_range_high, observed_at,
                source_type, source_path
            ) VALUES (?, 'dummy_alice', ?, ?, 'eGFR', 85.0, ?, NULL, NULL,
                      '2023-04-20', 'source_import', 'renal.pdf')
            """,
            (row_id, code, origin, unit),
        )

    repairs = backfill_reviewed_inferred_loinc_codes(conn)

    assert {r.row_id for r in repairs} == {"inferred-trunc", "inferred-area"}
    codes = dict(conn.execute("SELECT id, loinc_code FROM lab_results").fetchall())
    assert codes["inferred-trunc"] == "69405-9"
    assert codes["inferred-area"] == "69405-9"
    assert codes["source-ckdepi"] == "62238-1"
    assert codes["source-2021"] == "98979-8"

    assert backfill_reviewed_inferred_loinc_codes(conn) == []


def test_mpv_reviewed_repair_preserves_source_codes_and_is_idempotent(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for row_id, origin, unit in [
        ("inferred", "inferred", "fL"),
        ("source", "source_provided", "fL"),
        ("different-unit", "inferred", "%"),
    ]:
        conn.execute(
            """
            INSERT INTO lab_results (
                id, person_id, loinc_code, loinc_code_origin, test_name, value,
                unit, observed_at, source_type, source_path
            ) VALUES (?, 'dummy_alice', '32711-4', ?, 'MPV', 9.5, ?,
                      '2017-02-03', 'source_import', ?)
            """,
            (row_id, origin, unit, f"{row_id}.pdf"),
        )
    inferred_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-02-03",
        [("MPV", "32711-4", "9.5", "fL")],
        source_path="inferred.pdf",
        filename="inferred.md",
    )
    source_note = _make_panel_note(
        tmp_path,
        "dummy_alice",
        "2017-02-03",
        [("MPV", "32711-4", "9.5", "fL")],
        source_path="source.pdf",
        filename="source.md",
    )
    source_before = source_note.read_bytes()

    repairs = backfill_reviewed_inferred_loinc_codes(conn)

    assert [r.row_id for r in repairs] == ["inferred"]
    assert repair_inferred_lab_loinc_codes_in_notes(conn, tmp_path, repairs) == (1, 1)
    assert "| MPV | 32623-1 |" in inferred_note.read_text()
    assert source_note.read_bytes() == source_before
    assert dict(conn.execute("SELECT id, loinc_code FROM lab_results")) == {
        "inferred": "32623-1",
        "source": "32711-4",
        "different-unit": "32711-4",
    }
    assert backfill_reviewed_inferred_loinc_codes(conn) == []
    assert repair_inferred_lab_loinc_codes_in_notes(conn, tmp_path, repairs) == (0, 0)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='lab_inferred_loinc_repaired'"
        ).fetchone()[0]
        == 1
    )


def test_normalize_test_name_collapses_egfr_case_only_split() -> None:
    """Trend series identity is the normalized test name, so "eGFR" and "EGFR"
    rendered as two separate kidney-function series for the same person. Race
    reported variants stay distinct: they are different measurements.
    """
    assert normalize_test_name("eGFR", "62238-1", "mL/min/1.73 m2") == "eGFR"
    assert normalize_test_name("EGFR", "98979-8", "mL/min/1.73") == "eGFR"
    assert normalize_test_name("eGFR If Afr Am", "88293-6", "mL/min/1.73 m2") == "eGFR If Afr Am"
    assert (
        normalize_test_name("GFR -- AFRICAN AMERICAN", "33914-3", "ML/MIN")
        == "GFR -- AFRICAN AMERICAN"
    )


def test_repair_reviewed_note_codes_reaches_rows_no_db_repair_matched(tmp_path: Path) -> None:
    """The DB-driven note repair only reaches rows matching a repaired DB row by
    source or clinical identity. Two real classes escaped it: a note whose fact
    is canonical under another source on an adjacent date, and a stale
    projection whose DB row already holds the correct source-provided code.
    A source-provided code is never a key in the reviewed table, so an
    authoritative cell must stay untouched.
    """
    labs = tmp_path / "Records" / "dummy_alice" / "Labs"
    labs.mkdir(parents=True)
    note = labs / "2017-12-31-panel.md"
    note.write_text(
        "| test_name | loinc_code | value | unit | low | high | observed_at |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| eGFR | 102097-3 | 99 | mL/min/1.73 m2 |  |  | 2017-12-31 |\n"
        "| eGFR | 62238-1 | 88 | mL/min/1.73 m2 |  |  | 2017-12-31 |\n"
        "| Sodium | 2951-2 | 140 | mmol/L |  |  | 2017-12-31 |\n",
        encoding="utf-8",
    )

    notes_updated, cells_updated = repair_reviewed_inferred_loinc_codes_in_notes(tmp_path)

    assert (notes_updated, cells_updated) == (1, 1)
    body = note.read_text(encoding="utf-8")
    assert "| 69405-9 |" in body
    assert "102097-3" not in body
    assert "| 62238-1 |" in body
    assert "| 2951-2 |" in body

    assert repair_reviewed_inferred_loinc_codes_in_notes(tmp_path) == (0, 0)


def test_reviewed_override_splits_gfr_race_variants(tmp_path: Path) -> None:
    """The source reports two distinct MDRD results per draw but coded both with
    the generic, LOINC-DISCOURAGED 33914-3. Each row gets the exact ACTIVE code
    its own result label names, row-scoped per ADR-035, and the two variants
    stay distinct.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for row_id, name in [
        ("reviewed-gfr-aa", "GFR -- AFRICAN AMERICAN"),
        ("reviewed-gfr-non-aa", "GFR -- NON-AFRICAN AMERICAN"),
    ]:
        conn.execute(
            """
            INSERT INTO lab_results (
                id, person_id, loinc_code, loinc_code_origin, test_name, value,
                unit, reference_range_low, reference_range_high, observed_at,
                source_type, source_path
            ) VALUES (?, 'dummy_alice', '33914-3', 'source_provided', ?, 60.0,
                      'ML/MIN', NULL, NULL, '2015-04-21', 'source_import', 'panel.pdf')
            """,
            (row_id, name),
        )

    overrides = [
        SourceLoincOverride("reviewed-gfr-aa", "33914-3", "48643-1", "race-reported MDRD"),
        SourceLoincOverride("reviewed-gfr-non-aa", "33914-3", "48642-3", "race-reported MDRD"),
    ]
    repairs = apply_reviewed_source_provided_loinc_overrides(conn, overrides)

    assert len(repairs) == 2
    codes = dict(conn.execute("SELECT test_name, loinc_code FROM lab_results").fetchall())
    assert codes["GFR -- AFRICAN AMERICAN"] == "48643-1"
    assert codes["GFR -- NON-AFRICAN AMERICAN"] == "48642-3"

    assert apply_reviewed_source_provided_loinc_overrides(conn, overrides) == []


def test_load_reviewed_source_loinc_overrides_reads_local_file(tmp_path: Path) -> None:
    """Row-scoped overrides come from an untracked local file; absent means none."""
    path = tmp_path / "reviewed_source_loinc_overrides.jsonl"
    assert load_reviewed_source_loinc_overrides(path) == []

    path.write_text(
        '{"row_id": "r1", "expected_old_code": "30385-9", "new_code": "21000-5", '
        '"reason": "fL property"}\n\n',
        encoding="utf-8",
    )
    assert load_reviewed_source_loinc_overrides(path) == [
        SourceLoincOverride("r1", "30385-9", "21000-5", "fL property")
    ]

    path.write_text('{"row_id": "r1", "new_code": "21000-5", "reason": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing field"):
        load_reviewed_source_loinc_overrides(path)


def test_method_variant_note_explains_coexisting_gfr_series() -> None:
    """Kidney-function series are kept separate because different estimating
    formulas produce step changes that are methodology, not physiology. The
    report must say so where more than one such series exists, and stay silent
    when only one does.
    """
    two_series = [
        {"test_name": "eGFR", "loinc_code": "62238-1", "readings": []},
        {"test_name": "GFR -- AFRICAN AMERICAN", "loinc_code": "48643-1", "readings": []},
    ]
    note = _method_variant_note(two_series)
    assert "Kidney function" in note
    assert "`eGFR`" in note
    assert "`GFR -- AFRICAN AMERICAN`" in note
    assert "methodology change" in note

    assert _method_variant_note([two_series[0]]) == ""
    assert _method_variant_note([{"test_name": "Sodium", "loinc_code": "2951-2"}]) == ""


def test_source_interpretation_flag_wins_over_range_derivation() -> None:
    """The lab's own flag can mark a result abnormal while its value sits inside
    the stated reference range. 75 real panels are abnormal for exactly that
    reason, so a source-stated flag must never be overwritten by derivation.
    """
    assert interpretation_for_reading("AA", 5.0, 1.0, 10.0) == "AA"
    assert interpretation_for_reading("a", 5.0, 1.0, 10.0) == "A"


def test_interpretation_derived_from_bounds_when_source_is_silent() -> None:
    assert interpretation_for_reading("", 12.0, 1.0, 10.0) == "H"
    assert interpretation_for_reading(None, 0.5, 1.0, 10.0) == "L"
    assert interpretation_for_reading("", 5.0, 1.0, 10.0) == "N"
    assert interpretation_for_reading("", 12.0, None, 10.0) == "H"


def test_interpretation_absent_rather_than_assumed_normal() -> None:
    """With no stated flag and no bound there is nothing to conclude. Asserting
    'normal' by default would manufacture reassurance the source never gave.
    """
    assert interpretation_for_reading("", 5.0, None, None) is None
    assert interpretation_for_reading("", None, 1.0, 10.0) is None


def test_unrecognised_source_flag_falls_back_to_derivation() -> None:
    """An unknown code is not trusted as an assertion, but the reading still has
    bounds, so derivation still applies rather than losing the signal.
    """
    assert interpretation_for_reading("ZZ", 12.0, 1.0, 10.0) == "H"


def test_lab_payload_persists_only_source_stated_interpretation() -> None:
    payload = lab_payload_from_mapping(
        {
            "person_id": "dummy_alice",
            "test_name": "Example Marker",
            "value": "12.0",
            "unit": "mg/dL",
            "reference_range_low": "1.0",
            "reference_range_high": "10.0",
            "observed_at": "2023-04-20",
            "source_type": "source_import",
            "source_path": "panel.pdf",
        },
        "panel.pdf",
    )

    assert payload["interpretation_code"] is None
    assert source_interpretation_code("AA") == "AA"
    assert source_interpretation_code("ZZ") is None


def test_lab_flag_column_shows_only_source_stated_flags() -> None:
    """A derived flag would merely restate the In Range column. Only the lab's
    own assertion adds information -- it can mark a result abnormal that sits
    inside its own stated bounds.
    """
    assert _lab_flag_label({"interpretation_code_stated": "AA"}) == "**Critically abnormal**"
    assert _lab_flag_label({"interpretation_code_stated": "H"}) == "High"
    assert _lab_flag_label({"interpretation_code_stated": None}) == "—"
    assert _lab_flag_label({}) == "—"


def test_panel_verdict_is_excluded_from_trends(tmp_path: Path) -> None:
    """56850-1 is the lab's overall verdict for a panel, not a measurement. It
    cannot trend, and per-test flags now carry the same information per result.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for row_id, code, name, value in [
        ("verdict-1", "56850-1", "Lab Interpretation", None),
        ("verdict-2", "56850-1", "Lab Interpretation", None),
        ("real-1", "2951-2", "Sodium", 140.0),
    ]:
        conn.execute(
            """
            INSERT INTO lab_results (
                id, person_id, loinc_code, loinc_code_origin, test_name, value,
                value_text, unit, reference_range_low, reference_range_high,
                observed_at, source_type, source_path
            ) VALUES (?, 'dummy_alice', ?, 'source_provided', ?, ?, 'Abnormal',
                      'mmol/L', NULL, NULL, '2023-04-20', 'source_import', 'p.xml')
            """,
            (row_id, code, name, value),
        )

    trends = build_lab_trends_from_db(conn, "dummy_alice")

    assert [t["test_name"] for t in trends] == ["Sodium"]


def test_dimensionless_units_normalise_to_empty() -> None:
    """A ratio has no unit by definition and "pH" names the quantity rather than
    a unit. Asserting either into a medical record would fabricate a unit that
    does not exist; the reference range still conveys scale.
    """
    assert normalize_unit("Ratio") == ""
    assert normalize_unit("ratio") == ""
    assert normalize_unit("pH") == ""
    assert normalize_unit("mg/dL") == "mg/dL"


def test_case_only_unit_variants_converge() -> None:
    assert normalize_unit("PG") == normalize_unit("pg") == "pg"
    assert normalize_unit("/HPF") == normalize_unit("/hpf") == "/hpf"


def test_reviewed_equivalence_covers_bicarbonate_but_not_divalent_ions() -> None:
    """Bicarbonate is monovalent, so mEq/L equals mmol/L exactly. Calcium is
    divalent and must stay rejected -- converting it would silently halve or
    double a real result (ADR-035).
    """
    assert reviewed_unit_equivalence("Carbon dioxide", "MEQ/L") == "mmol/L"
    assert reviewed_unit_equivalence("Calcium", "MEQ/L") is None
    assert reviewed_unit_equivalence("Magnesium", "MEQ/L") is None


def test_reviewed_name_fill_reaches_uncoded_analytes() -> None:
    """A C-CDA `xsi:type="REAL"` value carries no unit attribute, and when the
    observation also carries no LOINC code the LOINC-keyed map has no key. The
    name fill covers those, and still never overrides a stated unit.
    """
    assert fill_unit("", "", "POCT Anion Gap (AGAP)") == "mmol/L"
    assert fill_unit("", "", "Average RNFL Today (OS)") == "µm"
    assert fill_unit("", "mg/dL", "POCT Anion Gap (AGAP)") == "mg/dL"
    assert fill_unit("", "", "Some Unreviewed Analyte") == ""


def test_immature_granulocyte_code_stays_unfilled() -> None:
    """53115-2 is used for both the percentage and the absolute count, so any
    auto-fill from it would be wrong half the time. The affected row keeps
    an empty unit rather than guessing.
    """
    assert "53115-2" not in CANONICAL_UNIT_BY_LOINC
    assert fill_unit("53115-2", "", "Immature Granulocytes, Absolute") == ""


def test_suspected_truncation_detects_lost_leading_digit() -> None:
    """PDF text extraction can drop a value's leading digit, so a reading arrives
    as 3.5 under a stated range of 31.5-35.7.
    """
    assert suspected_truncated_value(3.5, 31.5, 35.7, None) == 33.5
    assert suspected_truncated_value(1.0, 5.0, 16.0, None) == 11.0


def test_suspected_truncation_ignores_source_corroborated_critical_values() -> None:
    """This is the condition that makes the heuristic safe. A real severe anemia
    of 5.9 g/dL under a 13.0-18.0 range looks exactly like a truncation, but the
    lab flagged it AA and that corroboration is decisive. Treating it as a
    truncation would erase a documented critical result.
    """
    assert suspected_truncated_value(5.9, 13.0, 18.0, "AA") is None
    assert suspected_truncated_value(7.0, 14.0, 40.0, "L") is None
    assert suspected_truncated_value(5.9, 13.0, 18.0, None) is not None


def test_suspected_truncation_ignores_plausible_and_unrestorable_values() -> None:
    assert suspected_truncated_value(140.0, 135.0, 145.0, None) is None
    # A genuinely high PSA: no prepended digit brings it back into range.
    assert suspected_truncated_value(134.0, 0.0, 5.6, None) is None
    assert suspected_truncated_value(None, 1.0, 10.0, None) is None
    assert suspected_truncated_value(5.0, None, None, None) is None


def test_suspected_truncation_tolerates_unnormalised_input() -> None:
    """Callers reach the check on paths that have not normalised the payload."""
    assert suspected_truncated_value("3.5", "31.5", "35.7", None) == 33.5
    assert suspected_truncated_value("not a number", 1.0, 10.0, None) is None


def test_implausible_value_is_persisted_and_flagged_for_review(tmp_path: Path) -> None:
    """The reading may be real, so it is never discarded on a heuristic. It is
    persisted unchanged and routed to human review (REQ-005).
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_alice', 'Dummy Alice')")
    upsert_lab_result(
        conn,
        {
            "person_id": "dummy_alice",
            "loinc_code": "",
            "test_name": "Example Marker",
            "value": 3.5,
            "value_text": "3.5",
            "unit": "g/dL",
            "reference_range_low": 31.5,
            "reference_range_high": 35.7,
            "observed_at": "2023-04-20",
            "source_type": "source_import",
            "source_path": "panel.pdf",
        },
    )

    stored = conn.execute("SELECT value FROM lab_results").fetchone()
    assert stored[0] == 3.5  # persisted unchanged, never rewritten

    events = conn.execute(
        "SELECT event_type, status, message FROM audit_events WHERE event_type = ?",
        ("lab_value_implausible",),
    ).fetchall()
    assert len(events) == 1
    assert events[0][1] == "awaiting_user_decision"
    assert "33.5" in events[0][2]


def test_range_bar_cleanup_preserves_lone_value_lines() -> None:
    """The doubled-number pattern also matches a repeated leading digit inside a
    genuine number, so applying it to a lone value line turned real readings
    into wrong ones: 33.5 -> 3.5, 88.8 -> 8.8, 44.3 -> 4.3, 22 -> 2. A range bar
    always carries both bounds, so it has at least two tokens; a lone number on
    its own line is the measured value.
    """
    assert clean_pdf_range_bar_artifacts("33.5") == "33.5"
    assert clean_pdf_range_bar_artifacts("88.8") == "88.8"
    assert clean_pdf_range_bar_artifacts("44.3") == "44.3"
    assert clean_pdf_range_bar_artifacts("22") == "22"


def test_range_bar_cleanup_still_collapses_real_range_bars() -> None:
    assert clean_pdf_range_bar_artifacts("31.531.5 35.735.7") == "31.5 35.7"
    assert clean_pdf_range_bar_artifacts("3.43.4 10.810.8") == "3.4 10.8"
    assert clean_pdf_range_bar_artifacts("1010 2020") == "10 20"
    assert clean_pdf_range_bar_artifacts("79 97") == "79 97"


def test_mychart_value_found_past_long_reference_table() -> None:
    """A liver panel repeats its age-banded reference table between the
    "Normal range:" line and the measurement, pushing the value out of any
    fixed-size window. The parser previously fell back to the line before the
    test name -- which in this layout is the *previous* analyte's value, so
    alkaline phosphatase was recorded as ALT's 14 instead of its own 65.
    """
    text = "\n".join(
        [
            "MyChart - Test Details",
            "Collected: 12/22/2022",
            "AL T",
            "Normal range: 0 - 50 U/L",
            "0 50",
            "14",
            "Alkaline Phosphatase",
            "Normal range: 47 - 123 U/L",
            "Please note. Reference range update to:",
            *[f"{n} years - {n + 1} years 47 - 123 U/L 41 - 116 U/L" for n in range(20, 40)],
            "47 123",
            "65",
        ]
    )

    rows = parse_labs_from_pdf_text(text, person_id="dummy_alice", source_label="liver.pdf")

    by_name = {str(r["test_name"]): r["value"] for r in rows}
    assert by_name["Alkaline phosphatase"] == 65.0
    assert by_name["ALT"] == 14.0


def test_lab_range_distance_is_fraction_beyond_range() -> None:
    from famed.lab_sync import lab_range_distance

    assert lab_range_distance(5.0, 4.0, 6.0) == 0.0
    assert lab_range_distance(7.0, 4.0, 6.0) == pytest.approx(0.5)
    assert lab_range_distance(3.0, 4.0, 6.0) == pytest.approx(0.5)
    assert lab_range_distance(134.0, None, 5.6) == pytest.approx((134.0 - 5.6) / 5.6)
    assert lab_range_distance(50.0, 60.0, None) == pytest.approx(10.0 / 60.0)
    assert lab_range_distance(None, 4.0, 6.0) == 0.0


def _ranked_rows() -> list[dict]:
    return [
        _flagged_row(
            "Analyte Mild",
            [
                _reading("2021-01-01", 6.0, "u", 4.0, 6.0),
                _reading("2021-07-01", 6.2, "u", 4.0, 6.0),
            ],
            trend="up",
            in_range_latest=False,
        ),
        _flagged_row(
            "Analyte Severe",
            [
                _reading("2021-01-01", 80.0, "u", 0.0, 5.6),
                _reading("2021-07-01", 134.0, "u", 0.0, 5.6),
            ],
            trend="up",
            in_range_latest=False,
        ),
    ]


def test_advisory_lab_observations_rank_out_of_range_by_distance() -> None:
    """The furthest-from-range result leads, not the alphabetically first."""
    from famed.lab_sync import build_lab_observations_for_advisory

    obs = build_lab_observations_for_advisory(_ranked_rows())

    assert obs[0].startswith("- **Analyte Severe**")
    assert obs[1].startswith("- **Analyte Mild**")


def test_lab_discussion_prompts_follow_severity() -> None:
    from famed.lab_sync import build_lab_discussion_prompts

    prompts = build_lab_discussion_prompts(_ranked_rows(), limit=1)

    assert len(prompts) == 1
    assert "Analyte Severe" in prompts[0]
    assert "134" in prompts[0]
    assert "previously 80" in prompts[0]


# code -> (LOINC CLASS, COMPONENT, SYSTEM)
_CLASSES = {
    "6690-2": ("HEM/BC", "Leukocytes", "Bld"),
    "2345-7": ("CHEM", "Glucose", "Ser/Plas"),
    "5767-9": ("SPEC", "Clarity", "Urine"),
    "11523-1": ("PANEL.CHEM", "Albumin", "Ser/Plas"),
    "1558-6": ("CHAL.ROUTINE", "Glucose^post CFst", "Ser/Plas"),
    "2093-3": ("CHEM", "Cholesterol", "Ser/Plas"),
    "2085-9": ("CHEM", "Cholesterol.in HDL", "Ser/Plas"),
    "3016-3": ("CHEM", "Thyrotropin", "Ser/Plas"),
    "8099-4": ("SERO", "Thyroperoxidase Ab", "Ser/Plas"),
    "2350-7": ("CHEM", "Glucose", "Urine"),
    "29771-3": ("CHEM", "Hemoglobin", "Stool^Patient"),
    "1975-2": ("CHEM", "Bilirubin", "Ser/Plas"),
    "1914-3": ("CHEM", "Calcidiol+ercalcidiol", "Ser/Plas"),
    "2692-2": ("CHEM", "Osmolality", "Ser/Plas"),
    "10839-9": ("CHEM", "Troponin I.cardiac", "Ser/Plas"),
    "19295-5": ("DRUG/TOX", "Opiates", "Urine"),
    "31843-6": ("MICRO", "Helicobacter pylori Ag", "Stool"),
}


@pytest.mark.parametrize(
    ("code", "section"),
    [
        ("6690-2", "Blood counts"),
        ("2345-7", "Glucose & diabetes"),
        ("1558-6", "Glucose & diabetes"),
        ("11523-1", "Liver"),
        ("1975-2", "Liver"),
        ("2093-3", "Lipids"),
        ("2085-9", "Lipids"),
        ("3016-3", "Thyroid"),
        ("8099-4", "Thyroid"),
        ("1914-3", "Iron & vitamins"),
        ("10839-9", "Heart"),
        ("2350-7", "Urine"),
        ("5767-9", "Urine"),
        ("29771-3", "Stool (colon screening)"),
        ("19295-5", "Drug levels & toxicology"),
        ("31843-6", "Infection & immunology"),
        ("2692-2", "Other chemistry"),
        ("99999-9", "Other"),
        ("", "No standard code"),
    ],
)
def test_lab_report_section_uses_loinc_component_and_specimen(code: str, section: str) -> None:
    """Panels like lipids and thyroid come from the LOINC component; urine and stool
    specimens go to their own sections regardless of class."""
    from famed.lab_sync import lab_report_section

    assert lab_report_section(code, _CLASSES) == section


def _latest_row(name: str, code: str, value: float, low: float, high: float) -> dict:
    return {
        "test_name": name,
        "loinc_code": code,
        "value": value,
        "unit": "u",
        "reference_range_low": low,
        "reference_range_high": high,
        "observed_at": "2023-04-01T08:00:00",
    }


def test_reports_index_groups_by_audience_and_names_absent_reports(tmp_path: Path) -> None:
    reports = tmp_path / "Records" / "dummy_alice" / "Reports"
    (reports / "doctor-visit-packets").mkdir(parents=True)
    (reports / "doctor-visit-packets" / "_index.md").write_text("# Packets\n", encoding="utf-8")
    for name in ("pending-review", "current-medications", "health-advisory", "lab-conflicts"):
        (reports / f"{name}.md").write_text(f"---\nperson_id: dummy_alice\n---\n# {name}\n")

    index = write_category_index_note(reports, "dummy_alice", "Reports").read_text(encoding="utf-8")

    headings = [line for line in index.splitlines() if line.startswith("## ")]
    assert headings == [
        "## For the doctor",
        "## Needs attention",
        "## Records",
        "## Genetics",
        "## Data review",
    ]
    assert index.index("[[doctor-visit-packets/_index|Doctor visit packets]]") < index.index(
        "## Needs attention"
    )
    assert "- Lab results — _not applicable: no lab results_" in index
    assert "- Genetics — _not applicable: no DNA imported_" in index
    assert "[[pending-review|Pending review]]" in index


def test_backfill_repairs_qtc_and_clears_comment_codes() -> None:
    """Reviewed repairs (ADR-035): QTc gets its own code; a comment loses a wrong code."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    conn.executemany(
        """
        INSERT INTO lab_results (
            id, person_id, loinc_code, loinc_code_origin, test_name, value, value_text,
            unit, observed_at, source_type, source_path
        ) VALUES (?, 'dummy_alice', ?, ?, ?, ?, ?, ?, '2022-01-15', 'pdf', 'src.xml')
        """,
        [
            (
                "qtc",
                "8634-8",
                LOINC_CODE_ORIGIN_INFERRED,
                "Q-T INTERVAL (CORRECTED)",
                466.0,
                None,
                "ms",
            ),
            ("qt", "8634-8", LOINC_CODE_ORIGIN_INFERRED, "Q-T INTERVAL", 392.0, None, "ms"),
            ("note", "28816-7", LOINC_CODE_ORIGIN_INFERRED, "Comment:,", None, "Possible", ""),
        ],
    )

    repairs = backfill_reviewed_inferred_loinc_codes(conn)

    codes = dict(conn.execute("SELECT id, loinc_code FROM lab_results").fetchall())
    origins = dict(conn.execute("SELECT id, loinc_code_origin FROM lab_results").fetchall())
    assert codes == {"qtc": "8636-3", "qt": "8634-8", "note": None}
    assert origins["note"] is None
    assert {(r.row_id, r.new_code) for r in repairs} == {("qtc", "8636-3"), ("note", "")}
    assert (
        conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type = 'lab_inferred_loinc_repaired'"
        ).fetchone()[0]
        == 2
    )


def _history_rows() -> list[dict]:
    hdl = _flagged_row(
        "HDL Cholesterol",
        [
            _reading("2019-03-01", 39.0, "mg/dL", 40.0, 59.0),
            _reading("2021-03-01", 41.0, "mg/dL", 40.0, 59.0),
            _reading("2023-06-24", 42.0, "mg/dL", 40.0, 59.0),
        ],
        trend="up",
        in_range_latest=True,
    )
    hdl["loinc_code"] = "2085-9"
    vldl = _flagged_row(
        "VLDL Cholesterol",
        [_reading("2020-07-06", 22.0, "mg/dL", 5.0, 40.0)],
        trend=None,
        in_range_latest=True,
    )
    vldl["loinc_code"] = "2093-3"
    tsh = _flagged_row(
        "TSH",
        [
            _reading("2022-07-01", 6.58, "uIU/mL", 0.4, 5.0),
            _reading("2023-06-24", 5.86, "uIU/mL", 0.4, 5.0),
        ],
        trend="down",
        in_range_latest=False,
    )
    tsh["loinc_code"] = "3016-3"
    return [hdl, vldl, tsh]


def test_lab_results_report_combines_latest_and_history_by_section() -> None:
    """One lab report: newest draw first, older rows grey, charts and readings collapsed."""
    from famed.lab_sync import render_lab_results_report

    report = render_lab_results_report("dummy_alice", _history_rows(), loinc_classes=_CLASSES)

    assert [line for line in report.splitlines() if line.startswith("## ")] == [
        "## Lipids",
        "## Thyroid",
    ]
    lipids = report.split("## Lipids", 1)[1].split("## Thyroid", 1)[0]
    assert "| Test | Latest | Date | Range | Flag | Previous | Trend |" in lipids
    # Newest-draw row first and plain; older-than-newest row grey.
    assert lipids.index("| HDL Cholesterol | 42.0 mg/dL | 2023-06-24 |") < lipids.index(
        "VLDL Cholesterol"
    )
    assert '<span style="color:#888">VLDL Cholesterol</span>' in lipids
    assert "> [!example]- Trend charts (1)" in lipids
    assert lipids.count("<svg") == 1
    assert ">Mar 2019<" in lipids and ">Jun 2023<" in lipids  # date anchors per reading
    assert "> [!info]- All readings" in lipids
    assert "> | 2019-03-01 | 39.0 mg/dL | 40.0–59.0 | L |" in lipids
    thyroid = report.split("## Thyroid", 1)[1]
    assert "| TSH | 5.86 uIU/mL | 2023-06-24 | 0.4–5.0 | **H** | 6.58 | ↓ |" in thyroid


def test_write_lab_results_report_skips_people_without_labs(tmp_path: Path) -> None:
    from famed.lab_sync import write_lab_results_report

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    reports = tmp_path / "famed-vault" / "Records" / "dummy_alice" / "Reports"
    reports.mkdir(parents=True)
    (reports / "lab-results.md").write_text("stale\n", encoding="utf-8")

    assert write_lab_results_report(conn, tmp_path / "famed-vault", "dummy_alice") == (None, 0)
    assert not (reports / "lab-results.md").exists()


def test_lab_results_report_text_values_fallback_and_front_matter() -> None:
    from famed.lab_sync import render_lab_results_report

    culture = _flagged_row(
        "Urine Culture",
        [
            {
                "date": "2023-06-24",
                "value": None,
                "value_text": "No growth",
                "unit": "",
                "ref_low": None,
                "ref_high": None,
            }
        ],
        trend=None,
        in_range_latest=None,
    )
    report = render_lab_results_report("dummy_alice", [culture], loinc_classes={})

    assert "report_type: lab_results" in report
    assert [line for line in report.splitlines() if line.startswith("## ")] == ["## All tests"]
    assert "| Urine Culture | No growth | 2023-06-24 |" in report
    assert "<svg" not in report


def test_no_lab_reports_without_lab_results(tmp_path: Path) -> None:
    """Reports exist only when their inputs do (ADR-031 section 4)."""
    from famed.lab_sync import write_lab_results_report

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    vault = tmp_path / "famed-vault"
    reports = vault / "Records" / "dummy_alice" / "Reports"
    reports.mkdir(parents=True)
    for name in ("lab-results.md", "flagged-for-discussion.md"):
        (reports / name).write_text("stale\n", encoding="utf-8")

    assert write_lab_results_report(conn, vault, "dummy_alice") == (None, 0)
    assert write_flagged_for_discussion_report(conn, vault, "dummy_alice") == (None, 0)
    assert not [p for p in reports.iterdir() if p.name != "_index.md"]


def test_backfill_repairs_unit_contradicting_inferred_codes_only() -> None:
    """ADR-034: a molar glucose code inferred onto a mg/dL row is repaired; a
    source-provided row carrying the same code is never touched (REQ-003)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    for row_id, name, code, origin, unit in [
        ("inferred-glucose", "Glucose", "14749-6", "inferred", "mg/dL"),
        ("inferred-a1c", "Hemoglobin A1c", "41995-2", "inferred", "%"),
        ("source-glucose", "Glucose", "14749-6", "source_provided", "mg/dL"),
    ]:
        conn.execute(
            """
            INSERT INTO lab_results (
                id, person_id, loinc_code, loinc_code_origin, test_name, value,
                unit, reference_range_low, reference_range_high, observed_at,
                source_type, source_path
            ) VALUES (?, 'dummy_alice', ?, ?, ?, 1.0, ?, NULL, NULL,
                      '2099-01-01', 'source_import', 'panel.pdf')
            """,
            (row_id, code, origin, name, unit),
        )

    repairs = backfill_reviewed_inferred_loinc_codes(conn)

    assert {r.row_id for r in repairs} == {"inferred-glucose", "inferred-a1c"}
    codes = dict(conn.execute("SELECT id, loinc_code FROM lab_results").fetchall())
    assert codes == {
        "inferred-glucose": "2345-7",
        "inferred-a1c": "4548-4",
        "source-glucose": "14749-6",
    }
    assert backfill_reviewed_inferred_loinc_codes(conn) == []
