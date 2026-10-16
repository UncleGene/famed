#!/usr/bin/env python3
"""Shared helpers for lab import and vault sync workflows."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

from famed.person_sync import (
    PersonSyncError,
    load_person_display_name,
    normalize_person_id,
    parse_front_matter,
)
from famed.reference_cache import load_loinc_classes, lookup_loinc_code
from famed.storage_contract import (
    AUDIT_LAB_DEDUPLICATED_ROW_RESTORED,
    AUDIT_LAB_INFERRED_LOINC_REPAIRED,
    AUDIT_LAB_PANEL_NOTE_MARKED_SUPERSEDED,
    AUDIT_LAB_SOURCE_PROVIDED_LOINC_OVERRIDDEN,
    AUDIT_LAB_VALUE_IMPLAUSIBLE,
    AUDIT_SKIPPED_USER_CORRECTION,
    AUDIT_USER_CORRECTION_REVOKED,
    DEDUP_SUBJECT_LAB_RESULT,
    STATE_AWAITING_USER_DECISION,
    STATE_PROCESSED,
    ConflictChoice,
    ProcessingState,
    clear_dedup_links_for_source,
    delete_dedup_link,
    ensure_dedup_links_table,
    load_dedup_links,
    log_audit_event,
    record_dedup_link,
    validate_processing_state,
)


class LabSyncError(ValueError):
    """Raised when lab payloads are invalid."""


@dataclass(frozen=True)
class InferredLoincRepair:
    """One reviewed LOINC code correction: either the ADR-035 inferred-only
    pattern-matching lane, or an ADR-035 row-scoped source-provided override.
    The shape is identical; only the producing function and audit event type
    differ."""

    row_id: str
    person_id: str
    source_path: str
    test_name: str
    observed_at: str
    unit: str
    old_code: str
    new_code: str


_COMPACT_TEST_NAME_RE = re.compile(r"[\s,]+")
_FLAGGED_OUT_OF_RANGE_WINDOW_DAYS = 180
_CAC_DESCRIPTION_TERMS: tuple[str, ...] = ("agatston", "coronary calcium", "calcium score", "cac ")

# Canonical display labels are normalized against current NLM Clinical Tables / LOINC naming
# for the imported test variants already present in this repository.
CANONICAL_TEST_NAME_ALIASES = {
    "% nrbc": "% nRBC",
    "al t": "ALT",
    "albumin": "Albumin",
    "alk phos": "Alkaline phosphatase",
    "alk phosphatase": "Alkaline phosphatase",
    "alkaline phosphatase": "Alkaline phosphatase",
    "alt (sgpt)": "ALT",
    "amphetamines screen, urine": "Amphetamines screen, urine",
    "ana screen, ifa": "ANA screen, IFA",
    "anion gap, poc": "Anion gap, POC",
    "ast (sgot)": "AST",
    # "eGFR" and "EGFR" would otherwise render as two separate trend series for
    # the same person. Series identity is the normalized test name, so a case difference alone
    # split a kidney-function trend. Race-reported variants ("eGFR If Afr Am",
    # "GFR -- AFRICAN AMERICAN") are genuinely different reported measurements
    # and are deliberately not aliased here.
    "egfr": "eGFR",
    "auto wbc": "White blood cell count",
    "absolute basophils": "Basophils, absolute",
    "absolute eosinophils": "Eosinophils, absolute",
    "absolute lymphocytes": "Lymphocytes, absolute",
    "absolute monocytes": "Monocytes, absolute",
    "absolute neutrophils": "Neutrophils, absolute",
    "absolute neutrophils, automated": "Neutrophils, absolute",
    "baso abs": "Basophils, absolute",
    "baso (absolute)": "Basophils, absolute",
    "basophils %": "% Basophils",
    "basophils absolute": "Basophils, absolute",
    "barbiturates screen, urine": "Barbiturates screen, urine",
    "benzodiazepines screen, urine": "Benzodiazepines screen, urine",
    "bili total": "Bilirubin, total",
    "bilirubin direct": "Bilirubin, direct",
    "bilirubin total": "Bilirubin, total",
    "bilirubin ur": "Bilirubin, Urine",
    "bun, poc": "BUN, POC",
    "bun/creat ratio": "BUN/Creatinine Ratio",
    "buprenorphine, urine": "Buprenorphine, urine",
    "c-reactive protein": "C reactive protein",
    "cannabinoid screen, urine": "Cannabinoid screen, urine",
    "calcium": "Calcium",
    "carbon dioxide": "Carbon dioxide",
    "carbon dioxide, total": "Carbon dioxide",
    "carboxy thc conf, ms, ur": "Carboxy-THC confirmatory, urine",
    "chloride": "Chloride",
    "chloride, poc": "Chloride, POC",
    "chol/hdl ratio": "Chol/HDL Ratio",
    "cholesterol": "Cholesterol",
    "cholesterol total": "Cholesterol",
    "cholesterol, total": "Cholesterol",
    "co2": "Carbon dioxide",
    "cocaine (metab.) screen, urine": "Cocaine metabolites screen, urine",
    "creat": "Creatinine",
    "creatinine": "Creatinine",
    "creatinine, poc": "Creatinine, POC",
    "dhea sulfate": "Dehydroepiandrosterone sulfate",
    "eag": "Estimated Average Glucose (eAG)",
    "eos abs": "Eosinophils, absolute",
    "eos (absolute)": "Eosinophils, absolute",
    "eosinophils %": "% Eosinophils",
    "eosinophils absolute": "Eosinophils, absolute",
    "fentanyl, urine": "Fentanyl, urine",
    "ggt": "Gamma glutamyl transferase",
    "globulin total": "Globulin, total",
    "glucose": "Glucose",
    "glucose, poc": "Glucose, POC",
    "glucose, random": "Glucose",
    "glucose ur": "Glucose, Urine",
    "hct": "Hematocrit",
    "hematocrit": "Hematocrit",
    "hematocrit, poc": "Hematocrit, POC",
    "hgb": "Hemoglobin",
    "hgba1c": "Hemoglobin A1c",
    "hemoglobin": "Hemoglobin",
    "hemoglobin a1c": "Hemoglobin A1c",
    "hemoglobin, poc": "Hemoglobin, POC",
    "hdl": "HDL Cholesterol",
    "hdl cholesterol": "HDL Cholesterol",
    "homocysteine": "Homocysteine",
    "ionized calcium, poc": "Ionized calcium, POC",
    # All three are LDL cholesterol calculated from the lipid panel (mg/dL,
    # reference <100); the display name varies by lab vendor (LabCorp uses
    # "NIH Calc", others use a plain comma or "CHOLESTEROL").
    "ldl chol calc (nih)": "LDL Calculated",
    "ldl, calculated": "LDL Calculated",
    "ldl cholesterol": "LDL Calculated",
    "ldl-c (nih calc)": "LDL Calculated",
    "lipase": "Lipase",
    "lymphocytes %": "% Lymphocytes",
    "lymphocytes absolute": "Lymphocytes, absolute",
    "lymphs (absolute)": "Lymphocytes, absolute",
    "lymphs abs": "Lymphocytes, absolute",
    "magnesium": "Magnesium",
    "meperidine screen, urine": "Meperidine screen, urine",
    "ketones ur": "Ketones, Urine",
    "methadone screen, urine": "Methadone screen, urine",
    "monocytes % (auto)": "% Monocytes",
    "monocytes absolute": "Monocytes, absolute",
    "monocytes(absolute)": "Monocytes, absolute",
    "monocytesabs": "Monocytes, absolute",
    "neutrophils % (auto)": "% Neutrophils",
    "neutrophils abs": "Neutrophils, absolute",
    "neutrophils (absolute)": "Neutrophils, absolute",
    "neutrophils absolute": "Neutrophils, absolute",
    "nitrite ur": "Nitrite, Urine",
    "opiate screen, urine": "Opiates screen, urine",
    "oxycodone/oxymorphone, urine": "Oxycodone/Oxymorphone screen, urine",
    "ph ur": "pH, Urine",
    "ph-ua": "pH, Urine",
    "phencyclidine screen, urine": "Phencyclidine screen, urine",
    "platelet count": "Platelet count",
    "platelets": "Platelet count",
    "poc prostate specific antigen": "Prostate Specific Antigen",
    "potassium": "Potassium",
    "potassium, poc": "Potassium, POC",
    "protein total": "Protein, total",
    "protein ur": "Protein, Urine",
    "propoxyphene screen, urine": "Propoxyphene screen, urine",
    "prostate specific ag, serum": "Prostate Specific Antigen",
    "prostate specific antigen": "Prostate Specific Antigen",
    "psa": "Prostate Specific Antigen",
    "psa, free": "Free PSA",
    "rbc urine": "RBC urine",
    "rdw-cv": "RDW-CV",
    "red blood cell count": "Red blood cell count",
    "red blood cells": "Red blood cell count",
    "sodium": "Sodium",
    "specific gravity ur": "Specific Gravity, Urine",
    "specific gravity, ua, poc": "Specific Gravity, Urine",
    "sodium, poc": "Sodium, POC",
    "tco2, poc": "Total carbon dioxide, POC",
    "testosterone, total, males (adult), ia": "Testosterone, total",
    "total bilirubin": "Bilirubin, total",
    "total protein": "Protein, total",
    "tramadol screen, urine": "Tramadol screen, urine",
    "triglycerides": "Triglycerides",
    "troponin i, poc": "Troponin I, POC",
    # Same analyte as "TSH"; the lab reports a narrower reference range under a
    # distinct label rather than a different test. Series converge under ADR-037;
    # each reading keeps its own ref_low/ref_high (see _reference_range_changed).
    "tsh narrow ref range": "TSH",
    "vitamin b-12": "Vitamin B12",
    "vitamin d,25-oh,total,ia": "Vitamin D 25-hydroxy",
    "wbc": "White blood cell count",
    "wbc urine": "WBC urine",
    "white blood cell count": "White blood cell count",
    "white blood cells": "White blood cell count",
}

LOINC_TEST_NAME_ALIASES = {
    # HDL: both LOINC codes map to the same measurement (same analyte, same unit in practice)
    ("hdl", "2085-9"): "HDL Cholesterol",
    ("hdl", "111273-9"): "HDL Cholesterol",
    ("hdl-c", "2085-9"): "HDL Cholesterol",
    ("hdl-c", "111273-9"): "HDL Cholesterol",
    # CBC % abbreviations: bare names are ambiguous (could be abs or %) without LOINC
    ("basos", "706-2"): "% Basophils",
    ("eos", "713-8"): "% Eosinophils",
    ("lymphs", "736-9"): "% Lymphocytes",
    ("monocytes", "5905-5"): "% Monocytes",
    ("neutrophils", "770-8"): "% Neutrophils",
    # POC measurements sharing LOINC with non-POC counterpart in imported data
    ("hematocrit,poc", "4544-3"): "Hematocrit",
    ("hemoglobin,poc", "103750-6"): "Hemoglobin",
}

UNIT_CLASS_TEST_NAME_ALIASES: dict[tuple[str, str], str] = {
    ("basos", "percent"): "% Basophils",
    ("eos", "percent"): "% Eosinophils",
    ("immaturegranulocytes", "percent"): "% Immature Granulocytes",
    ("immaturegranulocytes%(ig%)", "percent"): "% Immature Granulocytes",
    ("lymphs", "percent"): "% Lymphocytes",
    ("monocytes", "percent"): "% Monocytes",
    ("neutrophils", "percent"): "% Neutrophils",
    ("immaturegrans(abs)", "count"): "Immature Granulocytes, Absolute",
    ("absoluteimmaturegranulocytes", "count"): "Immature Granulocytes, Absolute",
    ("immaturegranulocytes", "count"): "Immature Granulocytes, Absolute",
    ("immaturegranulocytescount", "count"): "Immature Granulocytes, Absolute",
    ("nrbcauto", "ratio"): "% nRBC",
    ("nrbcauto", "percent"): "% nRBC",
    ("nrbc(auto)%", "ratio"): "% nRBC",
    ("nrbc(auto)%", "percent"): "% nRBC",
    ("nrbcper100cells", "percent"): "% nRBC",
    ("nrbcper100cells", "ratio"): "% nRBC",
    ("nrbc#", "count"): "nRBC, Absolute",
    ("nrbc%auto", "count"): "nRBC, Absolute",
    ("rdw", "percent"): "RDW-CV",
}


CANONICAL_TEST_NAME_COMPACT_ALIASES = {
    _COMPACT_TEST_NAME_RE.sub("", key): value for key, value in CANONICAL_TEST_NAME_ALIASES.items()
}


CANONICAL_UNIT_ALIASES = {
    "mg/dl": "mg/dL",
    "ng/ml": "ng/mL",
    "pg/ml": "pg/mL",
    "mcg/dl": "mcg/dL",
    "g/dl": "g/dL",
    "mmol/l": "mmol/L",
    "umol/l": "umol/L",
    # Case-only vendor variants of one unit.
    "pg": "pg",
    "/hpf": "/hpf",
    "/lpf": "/lpf",
    # Dimensionless quantities carry no unit. A ratio has none by definition and
    # "pH" names the quantity rather than a unit, so both normalise to empty
    # instead of asserting a unit that does not exist. The reference range still
    # conveys scale.
    "ratio": "",
    "ph": "",
    # GFR unit spellings. These strings only ever denote a glomerular filtration
    # rate, so the mapping is analyte-independent notation (ADR-035).
    "ml/min/1.73": "mL/min/1.73 m2",
    "ml/min/1.73 m2": "mL/min/1.73 m2",
    "ml/min/1.73m2": "mL/min/1.73 m2",
    "ml/min/1.73m*2": "mL/min/1.73 m2",
    "ml/min/1.73 sq m": "mL/min/1.73 m2",
    "10e3/ul": "K/uL",
    "10e6/ul": "M/uL",
    "x10e3/ul": "K/uL",
    "x10e6/ul": "M/uL",
    "iu/l": "U/L",
    "uiu/ml": "uIU/mL",
    "μiu/ml": "uIU/mL",  # casefold("µIU/mL") == "μiu/ml" (micro sign vs Greek mu)
    "iu/ml": "IU/mL",
    "k/ul": "K/uL",
    "m/ul": "M/uL",
    "thousand/ul": "K/uL",
    "million/ul": "M/uL",
    "/100wbc": "/100WBC",
    "/100 wbc": "/100WBC",
    "10*3/ul": "K/uL",
    "10*6/ul": "M/uL",
    "gm/dl": "g/dL",
    "fl": "fL",
    "%pcv": "%PCV",
}


# Reviewed unit equivalences that are only valid for a named analyte, keyed by
# (casefolded test name, casefolded source unit). Closed lane, exact match only:
# a unit spelling is never generalised across analytes from here (ADR-035).
#
# Two kinds of entry live here, and both are deliberately analyte-scoped:
#   * Milliequivalent equivalence, valid only at valence 1. mEq/L equals mmol/L
#     for monovalent ions; a divalent ion such as calcium or magnesium differs
#     by its valence, so those analytes must never be added to this table.
#   * Reviewed reporting conventions, where the source omits a denominator or
#     uses a percentage that the analyte's own convention fixes.
REVIEWED_UNIT_EQUIVALENCES: dict[tuple[str, str], str] = {
    # Monovalent ions: mEq/L == mmol/L exactly.
    ("sodium", "meq/l"): "mmol/L",
    ("potassium", "meq/l"): "mmol/L",
    ("chloride", "meq/l"): "mmol/L",
    # Bicarbonate is monovalent, so mEq/L and mmol/L are numerically identical.
    ("carbon dioxide", "meq/l"): "mmol/L",
    ("co2", "meq/l"): "mmol/L",
    # Source omits the /uL denominator; the analyte is always reported per uL.
    ("platelet count", "10*3"): "K/uL",
    ("platelets", "10*3"): "K/uL",
    ("rbc", "10*6"): "M/uL",
    # MCHC as a percentage is mass concentration in g/100 mL, i.e. g/dL.
    ("mchc", "%"): "g/dL",
    # Nucleated RBC percentage is defined per 100 white cells.
    ("nrbc per 100 cells", "%"): "/100WBC",
    # Truncated GFR units. Unlike the spellings above these are not valid unit
    # strings on their own -- "m2" is an area and "mL/min/1" is a clearance --
    # so they are scoped to the analyte rather than aliased globally.
    ("egfr", "m2"): "mL/min/1.73 m2",
    ("egfr", "ml/min/1"): "mL/min/1.73 m2",
}


def reviewed_unit_equivalence(test_name: str | None, unit: str | None) -> str | None:
    """Return the reviewed canonical unit for this analyte/unit pair, if any."""
    if not test_name or not unit:
        return None
    return REVIEWED_UNIT_EQUIVALENCES.get(
        (" ".join(str(test_name).split()).casefold(), str(unit).strip().casefold())
    )


_REVIEWED_INFERRED_LOINC_REPAIRS: dict[tuple[str, str, str], str] = {
    # https://loinc.org/32711-4: deprecated duplicate, explicit Map-To (ADR-035).
    ("mpv", "fl", "32711-4"): "32623-1",
    ("%immaturegranulocytes", "%", "53115-2"): "71695-1",
    ("immaturegranulocytes", "%", "53115-2"): "71695-1",
    ("immaturegranulocytes%(ig%)", "%", "53115-2"): "71695-1",
    ("rdw", "%", "21000-5"): "788-0",
    # Correctness-audit finding: a "% Free PSA" row (unit %)
    # was inferred to the Free-PSA mass-concentration LOINC (10886-0, MCnc,
    # ng/mL) instead of the Free/Total mass-fraction ratio LOINC (12841-3,
    # MFr, %) that its own unit implies.
    ("%freepsa", "%", "10886-0"): "12841-3",
    # eGFR: PDF sources often state only "eGFR" with no
    # estimating equation, but inference resolved the bare component name to
    # 102097-3 -- the CKiD equation, which is pediatric and combines
    # creatinine, cystatin C and urea, and must not be assigned to adults. The
    # honest code for an equation-unspecified eGFR is 69405-9 ("based on
    # 1.73 sq M"). Every listed unit is a spelling of the same unit; the
    # truncated forms come from the structured-table unit scan fixed in
    # import_labs._STRUCTURED_UNIT_AT_END. Source-provided equation codes
    # (62238-1 CKD-EPI, 98979-8 CKD-EPI 2021, 33914-3 MDRD, 88293-6
    # race-based) are never touched by this lane -- REQ-003 keeps them for
    # review.
    ("egfr", "m2", "102097-3"): "69405-9",
    ("egfr", "ml/min/1", "102097-3"): "69405-9",
    ("egfr", "ml/min/1.73", "102097-3"): "69405-9",
    ("egfr", "ml/min/1.73 m2", "102097-3"): "69405-9",
    ("egfr", "ml/min/1.73m2", "102097-3"): "69405-9",
    ("egfr", "ml/min/1.73m*2", "102097-3"): "69405-9",
    # Same inference, race-reported label. LOINC publishes no "race-reported
    # eGFR, method unspecified" code for serum, so 69405-9 under-specifies
    # rather than misstates -- unlike 102097-3, which asserts a pediatric
    # multi-marker equation. The reported variant stays visible in the test
    # name, and the series stays separate from plain eGFR.
    ("egfrifafram", "ml/min/1.73 m2", "102097-3"): "69405-9",
    # QTc must not carry the uncorrected QT code.
    ("q-tinterval(corrected)", "ms", "8634-8"): "8636-3",
    # A bare "Comment" label inferred an eyeglasses comment code. An empty
    # target clears the code: the row stays as text with no analyte identity.
    ("comment:", "", "28816-7"): "",
    ("comment", "", "28816-7"): "",
    # Correctness audit (ADR-034): codes inferred before unit-aware
    # selection. Molar codes on mass-unit results and the reverse, a urinary
    # stone code on serum bilirubin, a mass/volume A1c on % results, and
    # method-specific variants the reports do not state. Each target was
    # confirmed against NLM's LOINC table.
    ("bilirubintotal", "mg/dl", "74434-2"): "1975-2",
    ("bun", "mg/dl", "14937-7"): "3094-0",
    ("creatinine", "mg/dl", "103616-9"): "2160-0",
    ("dehydroepiandrosteronesulfate", "mcg/dl", "107494-7"): "2191-5",
    ("ferritin", "ng/ml", "14723-1"): "2276-4",
    ("glucose", "mg/dl", "14749-6"): "2345-7",
    ("glucosefasting", "mg/dl", "14749-6"): "2345-7",
    ("hemoglobina1c", "%", "41995-2"): "4548-4",
    ("lipoprotein(a)", "nmol/l", "10835-7"): "43583-4",
    ("t4free(direct)", "ng/dl", "14920-3"): "3024-7",
    ("testosteronetotal", "ng/dl", "105121-8"): "2986-8",
    ("triiodothyronine(t3)free", "pg/ml", "14928-6"): "3051-0",
    ("vitamind25-hydroxy", "ng/ml", "14635-7"): "62292-8",
    # Point-of-care labels: same targets as current inference. The specimen
    # (whole blood vs serum) is not stated, so the mass code is a property fix.
    ("bunpoc", "mg/dl", "14937-7"): "3094-0",
    ("creatininepoc", "mg/dl", "103616-9"): "2160-0",
    ("glucosepoc", "mg/dl", "14749-6"): "2345-7",
}

_CBC_DIFFERENTIAL_PERCENT_LOINC_CODES: frozenset[str] = frozenset(
    {"30180-4", "5905-5", "713-8", "736-9", "770-8", "26450-7", "71695-1"}
)
_CBC_DIFFERENTIAL_COUNT_LOINC_CODES: frozenset[str] = frozenset(
    {"32200-8", "704-7", "711-2", "731-0", "742-7", "751-8", "26444-0", "53115-2"}
)
_CBC_DIFFERENTIAL_MISMATCH_LOINC_CODES: frozenset[str] = frozenset(
    {"100850-7", "104548-3", "105870-0", "20472-7"}
)
_COUNT_UNIT_KEYS: frozenset[str] = frozenset({"10*3/ul", "k/ul", "thousand/ul"})

# Reviewed LOINC -> canonical unit lane (ADR-035).
#
# Some CCDA sources encode standard lab results as ``xsi:type="REAL"`` (which
# carries no unit attribute), leaving the unit only in human-readable narrative
# prose that is not reliably parseable. For this closed, reviewed set of
# analytes the unit is clinically determined by the LOINC identity, so we fill
# the canonical unit when -- and only when -- the source provides none. This is
# never used to override a source-provided unit.
#
# Deliberately excluded: LOINC codes with known assignment ambiguity. The CBC
# differential percent/count LOINCs are already governed by
# ``_CBC_DIFFERENTIAL_*`` validation, and ``53115-2`` (immature granulocytes,
# count vs %) is a deferred clinical-review case. A missing unit on
# those codes signals a LOINC-data-quality problem, not a unit gap, and must not
# be papered over here.
CANONICAL_UNIT_BY_LOINC: dict[str, str] = {
    "2951-2": "mmol/L",  # Sodium
    "2823-3": "mmol/L",  # Potassium
    "2075-0": "mmol/L",  # Chloride
    "718-7": "g/dL",  # Hemoglobin
    "787-2": "fL",  # MCV
    "785-6": "pg",  # MCH
    "788-0": "%",  # RDW (RDW-CV)
    "100716-0": "ng/mL",  # Prostate Specific Antigen
    "62292-8": "ng/mL",  # Vitamin D, 25-Hydroxy Total
    "19195-7": "ng/mL",  # Prostate Specific Antigen (second reported code)
    # Deliberately absent: 53115-2. It is used for both the immature-granulocyte
    # percentage and the absolute count, so filling either unit from it would be
    # wrong half the time. This analyte is fenced and a test enforces it; the
    # one affected row keeps its empty unit rather than guessing.
}

# Non-result narrative rows that some CCDA sources emit into result tables
# (instrument lot numbers, kit expiration dates, internal-control flags). These
# carry no LOINC and no clinical value; they are filtered before persistence and
# purged from existing data.
_NON_RESULT_LAB_NAME_MARKERS: tuple[str, ...] = (
    "lot number",
    "lot #",
    "expiration date",
    "exp date",
    "intrnl cntrl",
    "internal control",
    "control results ok",
    "cntrl rslts",
    "performing location",
)

_TRAILING_LC_SUFFIX_RE = re.compile(r"\s*-\s*LC\s*$", re.IGNORECASE)


LAB_PANEL_TABLE_HEADER = (
    "| test_name | loinc_code | value | unit | reference_range_low | "
    "reference_range_high | observed_at |"
)
LAB_CONFLICTS_TABLE_HEADER = "| observed_at | test_name | value | unit | source_path |"


EXPECTED_UNITS_BY_TEST: dict[str, tuple[str, ...]] = {
    "hemoglobin a1c": ("%",),
    "tsh": ("uIU/mL", "mIU/L"),
    "glucose": ("mg/dL",),
    "glucose, poc": ("mg/dL",),
    "estimated average glucose (eag)": ("mg/dL",),
    "bun": ("mg/dL",),
    "bun, poc": ("mg/dL",),
    "creatinine": ("mg/dL",),
    "creatinine, poc": ("mg/dL",),
    "sodium": ("mmol/L",),
    "sodium, poc": ("mmol/L",),
    "potassium": ("mmol/L",),
    "potassium, poc": ("mmol/L",),
    "chloride": ("mmol/L",),
    "chloride, poc": ("mmol/L",),
    "anion gap": ("mmol/L",),
    "anion gap, poc": ("mmol/L",),
    "co2": ("mmol/L",),
    "total carbon dioxide, poc": ("mmol/L",),
    "calcium": ("mg/dL",),
    "bilirubin, total": ("mg/dL",),
    "bilirubin, direct": ("mg/dL",),
    "alkaline phosphatase": ("U/L",),
    "ast": ("U/L",),
    "alt": ("U/L",),
    "lipase": ("U/L",),
    "cholesterol": ("mg/dL",),
    "cholesterol, total - lc": ("mg/dL",),
    "triglycerides": ("mg/dL",),
    "triglycerides - lc": ("mg/dL",),
    "ldl calculated": ("mg/dL",),
    "hdl": ("mg/dL",),
    "hdl-c - lc": ("mg/dL",),
    "prostate specific antigen": ("ng/mL",),
    "prostate specific ag, serum - lc": ("ng/mL",),
    "psa, free - lc": ("ng/mL",),
    "vitamin b12": ("pg/mL",),
    "folate": ("ng/mL",),
    "white blood cell count": ("K/uL",),
    "red blood cell count": ("M/uL",),
    "rbc": ("M/uL",),
    "hemoglobin": ("g/dL",),
    "hemoglobin, poc": ("g/dL",),
    "hematocrit": ("%",),
    "hematocrit, poc": ("%PCV", "%"),
    "mcv": ("fL",),
    "mch": ("pg",),
    "mchc": ("g/dL",),
    "rdw-sd": ("fL",),
    "rdw-cv": ("%",),
    "rdw": ("%",),
    "platelet count": ("K/uL",),
    "mpv": ("fL",),
    "nrbc(auto)%": ("/100WBC", "%"),
    "% nrbc": ("per 100 WBCs", "/100WBC"),
    "nrbc#": ("K/uL",),
    "absolute neutrophils": ("K/uL",),
    "absolute lymphocytes": ("K/uL",),
    "absolute monocytes": ("K/uL",),
    "absolute eosinophils": ("K/uL",),
    "absolute basophils": ("K/uL",),
}


def normalize_optional_float(raw_value: Any, field_name: str) -> float | None:
    if raw_value in (None, "", "null"):
        return None

    if isinstance(raw_value, float):
        return raw_value

    if isinstance(raw_value, int):
        return float(raw_value)

    value = str(raw_value).strip()
    if not value:
        return None

    try:
        return float(value)
    except ValueError as exc:
        raise LabSyncError(
            f"{field_name} must be numeric when provided, got: {raw_value!r}"
        ) from exc


def can_parse_optional_float(raw_value: Any) -> bool:
    try:
        normalize_optional_float(raw_value, "value")
    except LabSyncError:
        return False
    return True


def normalize_observed_at(raw_observed_at: Any, source_label: str) -> str:
    if raw_observed_at in (None, ""):
        raise LabSyncError(f"observed_at is required in {source_label}")

    if isinstance(raw_observed_at, (datetime, date)):
        return raw_observed_at.isoformat()

    observed_at = str(raw_observed_at).strip()
    if not observed_at:
        raise LabSyncError(f"observed_at is required in {source_label}")

    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LabSyncError(
            f"observed_at must be an ISO date or datetime in {source_label}, "
            f"got: {raw_observed_at!r}"
        ) from exc

    return observed_at


def normalize_source_type(raw_source_type: Any) -> str:
    source_type = str(raw_source_type or "manual").strip().lower()
    return source_type or "manual"


# LOINC code provenance markers (ADR-034). Explicit code-origin metadata so a
# source-authored code can be distinguished from one filled by lookup_loinc_code
# inference, making the source-vs-inferred distinction queryable (REQ-003).
LOINC_CODE_ORIGIN_SOURCE_PROVIDED = "source_provided"
LOINC_CODE_ORIGIN_INFERRED = "inferred"
_LOINC_CODE_ORIGINS = frozenset({LOINC_CODE_ORIGIN_SOURCE_PROVIDED, LOINC_CODE_ORIGIN_INFERRED})
# Source types whose non-blank LOINC code came with the data rather than from
# inference: structured imports (CCDA) and user-authored corrections/updates.
_SOURCE_PROVIDED_SOURCE_TYPES = frozenset({"source_import", "user_correction", "user_update"})


def classify_loinc_code_origin(loinc_code: Any, source_type: Any, explicit: Any = None) -> str:
    """Return the provenance marker for a row's ``loinc_code`` (ADR-034).

    A blank code has no origin (empty string). An explicit, recognized origin is
    honored as-is. Otherwise a non-blank code is ``source_provided`` when it
    arrives from a structured import or user correction, and ``inferred``
    otherwise (the inference step ``fill_loinc_codes`` also marks codes it fills).
    """
    if not str(loinc_code or "").strip():
        return ""
    explicit_origin = str(explicit or "").strip().lower()
    if explicit_origin in _LOINC_CODE_ORIGINS:
        return explicit_origin
    if normalize_source_type(source_type) in _SOURCE_PROVIDED_SOURCE_TYPES:
        return LOINC_CODE_ORIGIN_SOURCE_PROVIDED
    return LOINC_CODE_ORIGIN_INFERRED


def _compact_test_name_key(test_name: str) -> str:
    return _COMPACT_TEST_NAME_RE.sub("", test_name.casefold())


def normalize_test_name(raw_test_name: Any, loinc_code: Any = "", unit: Any = "") -> str:
    test_name = " ".join(str(raw_test_name or "").split()).strip()
    if not test_name:
        return ""
    test_name = _TRAILING_LC_SUFFIX_RE.sub("", test_name).strip()

    loinc = str(loinc_code or "").strip()
    compact_key = _compact_test_name_key(test_name)
    if loinc:
        loinc_alias = LOINC_TEST_NAME_ALIASES.get((compact_key, loinc))
        if loinc_alias:
            return loinc_alias

    unit_class = _unit_class(str(unit or ""))
    if unit_class:
        unit_alias = UNIT_CLASS_TEST_NAME_ALIASES.get((compact_key, unit_class))
        if unit_alias:
            return unit_alias

    folded_key = test_name.casefold()
    return CANONICAL_TEST_NAME_ALIASES.get(
        folded_key,
        CANONICAL_TEST_NAME_COMPACT_ALIASES.get(compact_key, test_name),
    )


def normalize_unit(raw_unit: Any) -> str:
    unit = " ".join(str(raw_unit or "").split()).strip()
    if not unit:
        return ""

    return CANONICAL_UNIT_ALIASES.get(unit.casefold(), unit)


# ADR-035: a reviewed test-name fill for analytes the
# LOINC-keyed map cannot reach. A C-CDA `xsi:type="REAL"` value carries no unit
# attribute, and when such an observation also carries no LOINC code there is no
# key for `CANONICAL_UNIT_BY_LOINC`. Each entry below is a reviewed analyte whose
# sibling readings state the unit unambiguously in the same archives. A name key
# is weaker than a LOINC key, so this table stays closed and small, and like the
# LOINC map it only ever fills an empty unit -- it can never override the source.
CANONICAL_UNIT_BY_TEST_NAME: dict[str, str] = {
    "poct anion gap (agap)": "mmol/L",
    "average rnfl today (os)": "\u00b5m",
    "average rnfl today (od)": "\u00b5m",
}


def fill_unit(loinc_code: Any, unit: str, test_name: Any = "") -> str:
    """Fill a canonical unit from the reviewed maps only when empty.

    Returns *unit* unchanged when it is already present (never overrides a
    source-provided unit). When *unit* is empty, the reviewed
    ``CANONICAL_UNIT_BY_LOINC`` map is consulted first, then the reviewed
    ``CANONICAL_UNIT_BY_TEST_NAME`` map for analytes that carry no LOINC code;
    otherwise returns the empty string. See ADR-035.
    """
    if unit:
        return unit
    loinc = str(loinc_code or "").strip()
    by_loinc = CANONICAL_UNIT_BY_LOINC.get(loinc, "")
    if by_loinc:
        return by_loinc
    name_key = " ".join(str(test_name or "").split()).casefold()
    return CANONICAL_UNIT_BY_TEST_NAME.get(name_key, "")


def is_non_result_lab_row(test_name: Any, loinc_code: Any) -> bool:
    """Return True for non-result narrative rows (lot numbers, control flags).

    Only rows with no LOINC are eligible, so a real coded result is never
    dropped on a name-substring coincidence. See ADR-035.
    """
    if str(loinc_code or "").strip():
        return False
    folded = " ".join(str(test_name or "").split()).casefold()
    if not folded:
        return False
    return any(marker in folded for marker in _NON_RESULT_LAB_NAME_MARKERS)


def _unit_class(unit: str) -> str | None:
    normalized = str(unit or "").strip()
    if not normalized:
        return None
    if normalized == "%":
        return "percent"
    if normalized.casefold() in _COUNT_UNIT_KEYS:
        return "count"
    compact = re.sub(r"[\s{}()]", "", normalized.casefold())
    if compact in {"/100wbc", "per100wbc", "per100wbcs"}:
        return "ratio"
    return None


def _expected_cbc_differential_unit_class(loinc_code: str) -> str | None:
    if loinc_code in _CBC_DIFFERENTIAL_PERCENT_LOINC_CODES:
        return "percent"
    if loinc_code in _CBC_DIFFERENTIAL_COUNT_LOINC_CODES:
        return "count"
    if loinc_code in _CBC_DIFFERENTIAL_MISMATCH_LOINC_CODES:
        return "mismatch"
    return None


def _loinc_unit_class_is_valid(loinc_code: str, unit: str) -> bool | None:
    expected = _expected_cbc_differential_unit_class(str(loinc_code or "").strip())
    actual = _unit_class(unit)
    if expected is None or actual is None:
        return None
    if expected == "mismatch":
        return False
    return expected == actual


def validate_loinc_unit_class(loinc_code: str, unit: str, source_label: str) -> None:
    validity = _loinc_unit_class_is_valid(loinc_code, unit)
    if validity is not False:
        return
    expected = _expected_cbc_differential_unit_class(str(loinc_code or "").strip())
    raise LabSyncError(
        f"potential LOINC/unit mismatch in {source_label}: "
        f"LOINC {loinc_code!r} expects {expected}, got unit {unit!r}"
    )


def validate_unit_matches_test_name(test_name: str, unit: str, source_label: str) -> None:
    if not unit:
        return

    expected_units = EXPECTED_UNITS_BY_TEST.get(test_name.casefold())
    if not expected_units:
        return

    if any(unit.casefold() == expected.casefold() for expected in expected_units):
        return

    expected_text = ", ".join(expected_units)
    raise LabSyncError(
        f"potential unit mismatch for {test_name!r} in {source_label}: "
        f"got {unit!r}, expected one of [{expected_text}]"
    )


VALID_INTERPRETATION_CODES: frozenset[str] = frozenset({"H", "L", "A", "AA", "N"})


def source_interpretation_code(raw_code: Any) -> str | None:
    """Return the source's own abnormality flag, or ``None`` if it stated none.

    Only a flag the source asserts is persisted. HL7 v3 ObservationInterpretation:
    ``H`` high, ``L`` low, ``A`` abnormal, ``AA`` critically abnormal, ``N``
    normal. This cannot be re-derived, because a lab marks results abnormal that
    sit inside their own stated reference range -- 75 real panels depend on that.
    """
    stated = str(raw_code or "").strip().upper()
    return stated if stated in VALID_INTERPRETATION_CODES else None


def interpretation_for_reading(
    stored_code: Any,
    value: float | None,
    reference_low: float | None,
    reference_high: float | None,
) -> str | None:
    """Return the abnormality flag to display for one reading.

    A stored, source-stated flag always wins. Otherwise the flag is derived from
    the reading's own bounds at read time rather than persisted: deriving keeps
    every row current without a backfill, cannot go stale when a reference range
    is later corrected, and avoids duplicating derivable data into canonical
    storage (CORE principle 12).

    Returns ``None`` when the source is silent and no bound is available, so an
    unflagged reading is never asserted normal by default.
    """
    stated = source_interpretation_code(stored_code)
    if stated:
        return stated
    if value is None or (reference_low is None and reference_high is None):
        return None
    if reference_low is not None and value < reference_low:
        return "L"
    if reference_high is not None and value > reference_high:
        return "H"
    return "N"


_IMPLAUSIBLE_RANGE_FACTOR = 2.0


def suspected_truncated_value(
    value: Any,
    reference_low: Any,
    reference_high: Any,
    interpretation_code: Any = None,
) -> float | None:
    """Return the value a lost leading digit would restore, or ``None``.

    PDF text extraction can drop a value's leading digit, so a reading arrives as
    ``3.5`` under a stated range of ``31.5-35.7``. Three signals must agree
    before a reading is treated as suspect, because a genuine critical result
    looks similar from any one of them alone:

    1. the value sits at least ``_IMPLAUSIBLE_RANGE_FACTOR`` times outside its
       own stated reference range;
    2. prepending a single digit lands it back inside that range;
    3. the source states no abnormality flag for it.

    Condition 3 is what makes this safe. A real severe anemia of 5.9 g/dL under
    a 13.0-18.0 range satisfies the first two conditions exactly as a truncation
    would -- but the lab flagged it ``AA``, and that corroboration is decisive.
    Measured against the whole store, the three together select the truncation
    class and no corroborated result.

    This never rewrites the value. The caller records it for human review; a
    reading that might be real is not discarded on a heuristic.
    """

    def _as_float(raw: Any) -> float | None:
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # Callers reach this on several paths, not all of which have normalised the
    # payload yet, so the inputs are coerced rather than assumed numeric.
    value = _as_float(value)
    reference_low = _as_float(reference_low)
    reference_high = _as_float(reference_high)
    if value is None or value <= 0:
        return None
    if source_interpretation_code(interpretation_code):
        return None
    if reference_low is None and reference_high is None:
        return None
    outside = (
        reference_low is not None and reference_low / value >= _IMPLAUSIBLE_RANGE_FACTOR
    ) or (
        reference_high is not None
        and reference_high > 0
        and value / reference_high >= _IMPLAUSIBLE_RANGE_FACTOR
    )
    if not outside:
        return None
    rendered = f"{value:g}"
    for digit in "123456789":
        try:
            candidate = float(digit + rendered)
        except ValueError:
            continue
        if (reference_low is None or candidate >= reference_low) and (
            reference_high is None or candidate <= reference_high
        ):
            return candidate
    return None


def lab_payload_from_mapping(
    raw_data: dict[str, Any],
    source_label: str,
    default_person_id: str | None = None,
) -> dict[str, Any]:
    person_id = normalize_person_id(raw_data.get("person_id") or default_person_id)

    loinc_code = str(raw_data.get("loinc_code") or "").strip()
    # Keyed on the raw source label: normalization needs the unit, so the
    # canonical name is not available yet, and the reviewed fill table is
    # keyed on the label the source actually emits.
    unit = fill_unit(loinc_code, normalize_unit(raw_data.get("unit")), raw_data.get("test_name"))
    # A reviewed analyte-scoped equivalence (ADR-035) applies on every source
    # path, not only the C-CDA one: a truncated or vendor-specific unit spelling
    # arrives from PDFs too.
    unit = reviewed_unit_equivalence(raw_data.get("test_name"), unit) or unit
    test_name = normalize_test_name(raw_data.get("test_name"), loinc_code, unit)
    if not test_name:
        raise LabSyncError(f"test_name is required in {source_label}")

    observed_at = normalize_observed_at(raw_data.get("observed_at"), source_label)

    validate_unit_matches_test_name(test_name, unit, source_label)
    source_type = normalize_source_type(raw_data.get("source_type"))
    value_text = str(raw_data.get("value_text") or "").strip()

    payload: dict[str, Any] = {
        "person_id": person_id,
        "loinc_code": loinc_code,
        "loinc_code_origin": classify_loinc_code_origin(
            loinc_code, source_type, raw_data.get("loinc_code_origin")
        ),
        "test_name": test_name,
        "value": normalize_optional_float(raw_data.get("value"), "value"),
        "value_text": value_text,
        "unit": unit,
        "reference_range_low": normalize_optional_float(
            raw_data.get("reference_range_low"), "reference_range_low"
        ),
        "reference_range_high": normalize_optional_float(
            raw_data.get("reference_range_high"), "reference_range_high"
        ),
        "observed_at": observed_at,
        "source_type": source_type,
        "source_path": str(raw_data.get("source_path") or "").strip() or None,
    }
    payload["interpretation_code"] = source_interpretation_code(raw_data.get("interpretation_code"))
    return payload


def lab_payloads_from_mapping(raw_data: dict[str, Any], source_label: str) -> list[dict[str, Any]]:
    raw_tests = raw_data.get("tests")
    if raw_tests in (None, ""):
        return [lab_payload_from_mapping(raw_data, source_label)]

    if not isinstance(raw_tests, list):
        raise LabSyncError(f"tests must be a list in {source_label}")
    tests_list = cast(list[Any], raw_tests)  # type: ignore[redundant-cast]

    person_id = normalize_person_id(raw_data.get("person_id"))
    default_observed_at = raw_data.get("observed_at")
    default_source_type = normalize_source_type(raw_data.get("source_type"))

    payloads: list[dict[str, Any]] = []
    for index, raw_test in enumerate(tests_list, start=1):
        if not isinstance(raw_test, dict):
            raise LabSyncError(f"tests[{index}] must be a mapping in {source_label}")

        raw_test_mapping = cast(dict[str, Any], raw_test)
        merged: dict[str, Any] = dict(raw_test_mapping)
        merged["person_id"] = person_id
        merged["observed_at"] = merged.get("observed_at") or default_observed_at
        if not merged.get("source_type"):
            merged["source_type"] = default_source_type

        payloads.append(lab_payload_from_mapping(merged, f"{source_label}:tests[{index}]"))

    if not payloads:
        raise LabSyncError(f"tests must contain at least one item in {source_label}")

    return payloads


def load_labs_from_note(path: Path) -> list[dict[str, Any]]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabSyncError(f"failed to read note {path}: {exc}") from exc

    try:
        front_matter = parse_front_matter(content)
    except ValueError as exc:
        raise LabSyncError(f"invalid front matter in {path}: {exc}") from exc

    # Backward compatibility: existing notes may still store tests in front matter.
    if front_matter.get("tests"):
        return lab_payloads_from_mapping(front_matter, str(path))

    # Single-test notes continue to use front-matter fields.
    if front_matter.get("test_name"):
        return [lab_payload_from_mapping(front_matter, str(path))]

    # New format: panel tests are stored in editable markdown table rows.
    return lab_payloads_from_markdown_table(content, front_matter, str(path))


def lab_payloads_from_markdown_table(
    markdown_content: str,
    front_matter: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    person_id = normalize_person_id(front_matter.get("person_id"))
    default_observed_at = normalize_observed_at(front_matter.get("observed_at"), source_label)
    default_source_type = normalize_source_type(front_matter.get("source_type"))

    lines = [line.strip() for line in markdown_content.splitlines()]
    header_index: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("| test_name ") and line.endswith("|"):
            header_index = index
            break

    if header_index is None:
        raise LabSyncError(
            "lab panel note must include a markdown table with a 'test_name' column "
            f"in {source_label}"
        )

    if header_index + 1 >= len(lines):
        raise LabSyncError(f"markdown lab table is missing alignment row in {source_label}")

    alignment = lines[header_index + 1]
    if not alignment.startswith("| ---"):
        raise LabSyncError(f"markdown lab table alignment row is invalid in {source_label}")

    payloads: list[dict[str, Any]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) < 7:
            continue

        if not any(columns):
            continue

        mapping: dict[str, Any] = {
            "person_id": person_id,
            "test_name": columns[0],
            "loinc_code": columns[1],
            "value": columns[2],
            "value_text": columns[2],
            "unit": columns[3],
            "reference_range_low": columns[4],
            "reference_range_high": columns[5],
            "observed_at": columns[6] or default_observed_at,
            "source_type": default_source_type,
        }
        if front_matter.get("source_path"):
            mapping["source_path"] = front_matter["source_path"]
        if columns[2] and not can_parse_optional_float(columns[2]):
            mapping["value"] = None
        payloads.append(lab_payload_from_mapping(mapping, source_label))

    if not payloads:
        raise LabSyncError(f"markdown lab table does not contain any test rows in {source_label}")

    return payloads


def render_lab_note(lab: dict[str, Any]) -> str:
    front_matter: dict[str, Any] = {
        "person_id": lab["person_id"],
        "record_type": "lab_result",
        "observed_at": lab["observed_at"],
        "loinc_code": lab["loinc_code"],
        "test_name": lab["test_name"],
        "value": lab["value"],
        "unit": lab["unit"],
        "reference_range_low": lab["reference_range_low"],
        "reference_range_high": lab["reference_range_high"],
        "source_type": lab["source_type"],
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    return (
        f"---\n{yaml_block}\n---\n\n"
        f"# {lab['test_name']}\n\n"
        "## Notes\n\n"
        "- Add interpretation notes or context.\n"
    )


def render_lab_panel_note(
    labs: list[dict[str, Any]],
    source_file_name: str | None = None,
    source_path: str | None = None,
    projection_role: str | None = None,
) -> str:
    if not labs:
        raise LabSyncError("at least one lab test is required to render a lab panel note")

    person_ids = {lab["person_id"] for lab in labs}
    if len(person_ids) != 1:
        raise LabSyncError("all tests in a lab panel note must belong to the same person_id")

    observed_ats = [lab["observed_at"] for lab in labs]
    panel_date = min(observed_ats)[:10]

    front_matter: dict[str, Any] = {
        "person_id": labs[0]["person_id"],
        "record_type": "lab_panel",
        "observed_at": min(observed_ats),
        "source_type": labs[0]["source_type"],
    }
    if source_file_name:
        front_matter["source_file_name"] = source_file_name
    if source_path:
        front_matter["source_path"] = source_path
    if projection_role:
        front_matter["projection_role"] = projection_role
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    table_rows = [LAB_PANEL_TABLE_HEADER, "| --- | --- | --- | --- | --- | --- | --- |"]
    for lab in labs:
        table_rows.append(
            "| "
            + " | ".join(
                [
                    str(lab.get("test_name") or ""),
                    str(lab.get("loinc_code") or ""),
                    (
                        str(lab.get("value_text") or "")
                        if str(lab.get("value_text") or "").strip()
                        else ("" if lab.get("value") is None else str(lab.get("value")))
                    ),
                    str(lab.get("unit") or ""),
                    ""
                    if lab.get("reference_range_low") is None
                    else str(lab.get("reference_range_low")),
                    ""
                    if lab.get("reference_range_high") is None
                    else str(lab.get("reference_range_high")),
                    str(lab.get("observed_at") or ""),
                ]
            )
            + " |"
        )
    table_block = "\n".join(table_rows)

    return (
        f"---\n{yaml_block}\n---\n\n"
        f"# Lab Panel — {panel_date}\n\n"
        "[[_index|← Labs]]\n\n"
        "## Results\n\n"
        f"{table_block}\n\n"
        "## Notes\n\n"
        + (f"- Source file: {source_file_name}\n" if source_file_name else "")
        + "- Edit table values directly if corrections are needed before syncing.\n"
    )


def lab_row_id(person_id: str, source_path: str, row_index: int) -> str:
    raw = f"{person_id}|{source_path}|{row_index}".encode()
    return hashlib.sha1(raw).hexdigest()


def upsert_lab_result(conn: sqlite3.Connection, lab: dict[str, Any]) -> None:
    source_path = lab.get("source_path")
    if not source_path:
        raise LabSyncError("source_path is required when writing to the database")

    restored = suspected_truncated_value(
        lab.get("value"),
        lab.get("reference_range_low"),
        lab.get("reference_range_high"),
        lab.get("interpretation_code"),
    )
    if restored is not None:
        # Persisted, not discarded: the reading may be real, and dropping a real
        # medical value on a heuristic is the worse failure. The event routes it
        # to human review (REQ-005).
        log_audit_event(
            conn,
            event_type=AUDIT_LAB_VALUE_IMPLAUSIBLE,
            subject_type="lab_result",
            subject_id=str(lab.get("person_id") or ""),
            source_path=str(source_path),
            status=STATE_AWAITING_USER_DECISION,
            message=(
                f"{lab.get('test_name')!r} value {lab.get('value')} is far outside its "
                f"stated range {lab.get('reference_range_low')}-"
                f"{lab.get('reference_range_high')} and the source states no "
                f"abnormality flag; a lost leading digit would give e.g. {restored}. "
                f"Value persisted unchanged pending review."
            ),
        )

    conn.execute(
        """
        INSERT INTO lab_results (
            id,
            person_id,
            loinc_code,
            loinc_code_origin,
            test_name,
            value,
            value_text,
            unit,
            reference_range_low,
            reference_range_high,
            interpretation_code,
            observed_at,
            source_type,
            source_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            loinc_code = excluded.loinc_code,
            loinc_code_origin = excluded.loinc_code_origin,
            test_name = excluded.test_name,
            value = excluded.value,
            value_text = excluded.value_text,
            unit = excluded.unit,
            reference_range_low = excluded.reference_range_low,
            reference_range_high = excluded.reference_range_high,
            interpretation_code = excluded.interpretation_code,
            observed_at = excluded.observed_at,
            source_type = excluded.source_type,
            source_path = excluded.source_path
        """,
        (
            lab_row_id(lab["person_id"], source_path, int(lab.get("row_index", 0))),
            lab["person_id"],
            lab["loinc_code"],
            lab.get("loinc_code_origin")
            or classify_loinc_code_origin(lab.get("loinc_code"), lab.get("source_type")),
            lab["test_name"],
            lab["value"],
            str(lab.get("value_text") or "") or None,
            lab["unit"],
            lab["reference_range_low"],
            lab["reference_range_high"],
            lab.get("interpretation_code"),
            lab["observed_at"],
            lab["source_type"],
            source_path,
        ),
    )


def _find_existing_observation(
    conn: sqlite3.Connection,
    person_id: str,
    test_name: str,
    observed_at: str,
    current_source_path: str,
) -> tuple[str, float | None] | None:
    """Return (source_path, value) of a matching observation from a different source, or None."""
    row = conn.execute(
        """
        SELECT source_path, value FROM lab_results
        WHERE person_id = ? AND test_name = ? AND observed_at = ?
          AND source_path != ?
        LIMIT 1
        """,
        (person_id, test_name, observed_at, current_source_path),
    ).fetchone()
    if row is None:
        return None
    return (str(row[0]), float(row[1]) if row[1] is not None else None)


def _find_redundant_observations(
    conn: sqlite3.Connection,
    person_id: str,
    test_name: str,
    observed_at: str,
    value: Any,
    value_text: str,
    current_source_path: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, loinc_code, value, value_text, unit,
               reference_range_low, reference_range_high,
               source_type, source_path
        FROM lab_results
        WHERE person_id = ? AND test_name = ? AND observed_at = ? AND source_path != ?
        """,
        (person_id, test_name, observed_at, current_source_path),
    ).fetchall()
    redundant: list[dict[str, Any]] = []
    for row in rows:
        existing_value = float(row[2]) if row[2] is not None else None
        if existing_value != value:
            continue
        existing_value_text = _value_text_identity(existing_value, row[3])
        candidate_value_text = _value_text_identity(value, value_text)
        if existing_value_text != candidate_value_text:
            continue
        redundant.append(
            {
                "id": str(row[0]),
                "loinc_code": row[1],
                "value": existing_value,
                "value_text": row[3],
                "unit": row[4],
                "reference_range_low": row[5],
                "reference_range_high": row[6],
                "source_type": row[7],
                "source_path": str(row[8] or ""),
            }
        )
    return redundant


def _lab_field_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _value_text_identity(value: Any, value_text: Any) -> str:
    text = " ".join(str(value_text or "").split())
    if value is None or text.startswith(("<", ">")):
        return text.casefold()
    return ""


def _lab_dedup_link_identity(lab: dict[str, Any]) -> str:
    """Return the complete stable identity of one reversibly suppressed fact."""
    return repr(
        (
            str(lab.get("test_name") or ""),
            str(lab.get("observed_at") or ""),
            lab.get("value"),
            _value_text_identity(lab.get("value"), lab.get("value_text")),
            str(lab.get("loinc_code") or ""),
            str(lab.get("unit") or ""),
            lab.get("reference_range_low"),
            lab.get("reference_range_high"),
        )
    )


def _lab_completeness_score(lab: dict[str, Any]) -> int:
    field_weights = {
        "loinc_code": 3,
        "unit": 1,
        "reference_range_low": 1,
        "reference_range_high": 1,
    }
    score = sum(
        weight for field, weight in field_weights.items() if _lab_field_present(lab.get(field))
    )
    if _value_text_identity(lab.get("value"), lab.get("value_text")):
        score += 1
    if (
        _loinc_unit_class_is_valid(str(lab.get("loinc_code") or ""), str(lab.get("unit") or ""))
        is True
    ):
        score += 2
    return score


def _same_draw_window_match(existing_observed_at: str, incoming_observed_at: str) -> bool:
    existing_dt = _parse_observed_at_for_dedup(existing_observed_at)
    incoming_dt = _parse_observed_at_for_dedup(incoming_observed_at)
    if existing_dt is None or incoming_dt is None:
        return False
    return abs(incoming_dt - existing_dt).total_seconds() <= 48 * 60 * 60


def _is_archive_restatement_match(
    *,
    existing_source_path: str,
    candidate_source_path: str,
    existing_value: Any,
    candidate_value: Any,
    existing_value_text: Any,
    candidate_value_text: Any,
    existing_loinc: Any,
    candidate_loinc: Any,
    existing_unit: Any,
    candidate_unit: Any,
    existing_ref_low: Any,
    candidate_ref_low: Any,
    existing_ref_high: Any,
    candidate_ref_high: Any,
    existing_observed_at: str,
    candidate_observed_at: str,
) -> bool:
    if existing_source_path == candidate_source_path:
        return False
    if not (
        _is_archive_source_path(existing_source_path)
        or _is_archive_source_path(candidate_source_path)
    ):
        return False
    existing_text = _value_text_identity(existing_value, existing_value_text)
    candidate_text = _value_text_identity(candidate_value, candidate_value_text)
    if existing_value is None or candidate_value is None:
        if existing_value is not None or candidate_value is not None:
            return False
        if not existing_text or existing_text != candidate_text:
            return False
    elif existing_value != candidate_value:
        return False
    elif (
        existing_text
        and candidate_text
        and (existing_text.startswith(("<", ">")) or candidate_text.startswith(("<", ">")))
        and existing_text != candidate_text
    ):
        return False
    existing_unit_text = str(existing_unit or "")
    candidate_unit_text = str(candidate_unit or "")
    units_match = existing_unit_text == candidate_unit_text
    blank_unit_match = (
        not units_match
        and (not existing_unit_text or not candidate_unit_text)
        and bool(existing_loinc)
        and str(existing_loinc) == str(candidate_loinc or "")
        and existing_observed_at[:10] == candidate_observed_at[:10]
    )
    ref_low_match = existing_ref_low == candidate_ref_low or (
        blank_unit_match and (existing_ref_low is None or candidate_ref_low is None)
    )
    ref_high_match = existing_ref_high == candidate_ref_high or (
        blank_unit_match and (existing_ref_high is None or candidate_ref_high is None)
    )
    if not (units_match or blank_unit_match) or not ref_low_match or not ref_high_match:
        return False
    return _same_draw_window_match(existing_observed_at, candidate_observed_at)


def _find_same_draw_miscode_redundant_observations(
    conn: sqlite3.Connection,
    person_id: str,
    test_name: str,
    observed_at: str,
    value: Any,
    value_text: str,
    loinc_code: str,
    unit: str,
    reference_range_low: Any,
    reference_range_high: Any,
    current_source_path: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, loinc_code, value, value_text, unit,
               reference_range_low, reference_range_high,
               observed_at, source_type, source_path
        FROM lab_results
        WHERE person_id = ? AND test_name = ? AND source_path != ?
        """,
        (person_id, test_name, current_source_path),
    ).fetchall()
    redundant: list[dict[str, Any]] = []
    for row in rows:
        existing_value = float(row[2]) if row[2] is not None else None
        if not _is_archive_restatement_match(
            existing_source_path=str(row[9] or ""),
            candidate_source_path=current_source_path,
            existing_value=existing_value,
            candidate_value=value,
            existing_value_text=row[3],
            candidate_value_text=value_text,
            existing_loinc=row[1],
            candidate_loinc=loinc_code,
            existing_unit=row[4],
            candidate_unit=unit,
            existing_ref_low=row[5],
            candidate_ref_low=reference_range_low,
            existing_ref_high=row[6],
            candidate_ref_high=reference_range_high,
            existing_observed_at=str(row[7] or ""),
            candidate_observed_at=observed_at,
        ):
            continue
        redundant.append(
            {
                "id": str(row[0]),
                "loinc_code": row[1],
                "value": existing_value,
                "value_text": row[3],
                "unit": row[4],
                "reference_range_low": row[5],
                "reference_range_high": row[6],
                "observed_at": str(row[7] or ""),
                "source_type": row[8],
                "source_path": str(row[9] or ""),
            }
        )
    return redundant


def _incoming_lab_replaces_redundant_rows(
    incoming_lab: dict[str, Any],
    redundant_rows: list[dict[str, Any]],
    incoming_source_path: str,
) -> bool:
    incoming_score = _lab_completeness_score(incoming_lab)
    if not redundant_rows:
        return False
    incoming_is_source_provided = str(incoming_lab.get("source_type") or "") == "source_import"
    if incoming_is_source_provided and all(
        str(row.get("source_type") or "") != "source_import" for row in redundant_rows
    ):
        return True
    if not incoming_is_source_provided and any(
        str(row.get("source_type") or "") == "source_import" for row in redundant_rows
    ):
        return False
    incoming_is_archive = _is_archive_source_path(incoming_source_path)
    if not incoming_is_archive and all(
        _is_archive_source_path(str(row.get("source_path") or "")) for row in redundant_rows
    ):
        return True
    for row in redundant_rows:
        if str(row.get("source_type") or "") in {"user_correction", "user_update"}:
            return False
        if incoming_score <= _lab_completeness_score(row):
            return False
    return True


def _prefer_lab_candidate(
    existing_lab: dict[str, Any],
    candidate_lab: dict[str, Any],
) -> bool:
    existing_is_source_provided = str(existing_lab.get("source_type") or "") == "source_import"
    candidate_is_source_provided = str(candidate_lab.get("source_type") or "") == "source_import"
    if existing_is_source_provided != candidate_is_source_provided:
        return candidate_is_source_provided
    existing_is_archive = _is_archive_source_path(str(existing_lab.get("source_path") or ""))
    candidate_is_archive = _is_archive_source_path(str(candidate_lab.get("source_path") or ""))
    if existing_is_archive != candidate_is_archive:
        return not candidate_is_archive
    return _lab_completeness_score(candidate_lab) > _lab_completeness_score(existing_lab)


def collapse_archive_restated_lab_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse same-fact ``lab_results`` rows restated from a HealthSummary
    archive against an existing differently-sourced row for the same test.

    Each row must carry ``test_name``, ``loinc_code``, ``observed_at``,
    ``value``, ``unit``, ``reference_range_low``, ``reference_range_high``,
    and ``source_path``/``source_type``; any other caller-specific keys
    (e.g. ``id``) are preserved unchanged on whichever row wins. Matching
    groups rows by ``normalize_test_name`` and applies the same
    ``_is_archive_restatement_match`` / ``_prefer_lab_candidate`` predicates
    ``build_lab_trends_from_db``'s first collapse pass already uses, so a
    consumer that doesn't need trend/in-range aggregation can still get the
    same archive-restatement collapse. Read-only: never mutates the rows it
    is given or any persisted ``lab_results`` row.
    """

    def _grouping_name(row: dict[str, Any]) -> str:
        return normalize_test_name(
            str(row.get("test_name") or ""),
            str(row.get("loinc_code") or ""),
            str(row.get("unit") or ""),
        )

    collapsed: list[dict[str, Any]] = []
    for row in rows:
        test_name = _grouping_name(row)
        matching_index = next(
            (
                index
                for index, existing in enumerate(collapsed)
                if _grouping_name(existing) == test_name
                and _is_archive_restatement_match(
                    existing_source_path=str(existing.get("source_path") or ""),
                    candidate_source_path=str(row.get("source_path") or ""),
                    existing_value=existing.get("value"),
                    candidate_value=row.get("value"),
                    existing_value_text=existing.get("value_text"),
                    candidate_value_text=row.get("value_text"),
                    existing_loinc=existing.get("loinc_code"),
                    candidate_loinc=row.get("loinc_code"),
                    existing_unit=existing.get("unit"),
                    candidate_unit=row.get("unit"),
                    existing_ref_low=existing.get("reference_range_low"),
                    candidate_ref_low=row.get("reference_range_low"),
                    existing_ref_high=existing.get("reference_range_high"),
                    candidate_ref_high=row.get("reference_range_high"),
                    existing_observed_at=str(existing.get("observed_at") or ""),
                    candidate_observed_at=str(row.get("observed_at") or ""),
                )
            ),
            None,
        )
        if matching_index is None:
            collapsed.append(row)
        elif _prefer_lab_candidate(collapsed[matching_index], row):
            collapsed[matching_index] = row
    return collapsed


def _find_user_corrected_observation(
    conn: sqlite3.Connection,
    person_id: str,
    test_name: str,
    observed_at: str,
) -> tuple[str, float | None] | None:
    """Return (source_path, value) for a matching user-corrected observation."""
    row = conn.execute(
        """
        SELECT source_path, value FROM lab_results
        WHERE person_id = ?
          AND test_name = ?
          AND observed_at = ?
          AND source_type = 'user_correction'
        LIMIT 1
        """,
        (person_id, test_name, observed_at),
    ).fetchone()
    if row is None:
        return None
    return (str(row[0] or ""), float(row[1]) if row[1] is not None else None)


def _lab_row_is_user_correction(
    conn: sqlite3.Connection,
    person_id: str,
    source_path: str,
    row_index: int,
) -> bool:
    row_id = lab_row_id(person_id, source_path, row_index)
    row = conn.execute(
        "SELECT source_type FROM lab_results WHERE id = ? LIMIT 1", (row_id,)
    ).fetchone()
    return bool(row and row[0] == "user_correction")


def replace_lab_results_for_source_path(
    conn: sqlite3.Connection,
    source_path: str,
    labs: list[dict[str, Any]],
    *,
    non_interactive: bool = True,
    conflict_resolver: Callable[[str, str, str, Any, Any], ConflictChoice] | None = None,
) -> tuple[int, int]:
    """Replace rows for source_path, skipping same-value duplicates from other sources.

    Redundant: same (person_id, test_name, observed_at, value) already exists from a
    different source → skip to avoid duplicate trend points.
    Conflicting: same identity but different value → insert both to preserve the
    discrepancy for human review (ADR-006: no silent conflict discard).

    When a user_correction row conflicts with an incoming import value:
    - non_interactive=True → skip + emit skipped_user_correction
    - non_interactive=False + conflict_resolver → call resolver(person_id, test_name,
      observed_at, corrected_value, import_value) → ConflictChoice:
        "keep_correction"  → skip + emit skipped_user_correction
        "accept_import"    → set source_type='manual', insert import row
        "revoke_and_import"→ set source_type='manual' (or delete synthetic), emit
                             user_correction_revoked, insert import row
    - non_interactive=False + no resolver → raise LabSyncError

    Returns (inserted, skipped_redundant).
    """
    ensure_dedup_links_table(conn)
    # Only people this source touches can have a link orphaned by this call.
    affected_person_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT person_id FROM lab_results WHERE source_path = ?",
            (source_path,),
        )
        if row[0]
    }
    affected_person_ids.update(str(lab.get("person_id")) for lab in labs if lab.get("person_id"))
    # Keep explicit user corrections and user update notes authoritative across re-imports.
    conn.execute(
        """
        DELETE FROM lab_results
        WHERE source_path = ?
          AND source_type NOT IN ('user_correction', 'user_update')
        """,
        (source_path,),
    )
    # This source's own suppression links are re-derived below from the incoming rows.
    clear_dedup_links_for_source(
        conn, subject_type=DEDUP_SUBJECT_LAB_RESULT, suppressed_source_path=source_path
    )
    inserted = 0
    skipped = 0
    for row_index, lab in enumerate(labs):
        person_id = str(lab.get("person_id") or "")
        test_name = str(lab.get("test_name") or "")
        observed_at = str(lab.get("observed_at") or "")
        validate_loinc_unit_class(
            str(lab.get("loinc_code") or ""),
            str(lab.get("unit") or ""),
            f"{source_path}:tests[{row_index}]",
        )
        corrected = _find_user_corrected_observation(conn, person_id, test_name, observed_at)
        if corrected is not None:
            correction_source_path, corrected_value = corrected
            if corrected_value == lab.get("value"):
                skipped += 1
                continue
            if non_interactive:
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="lab_result",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        "skipped import value for user-corrected lab "
                        f"{test_name!r} at {observed_at} "
                        f"(correction source: {correction_source_path or 'unknown'})"
                    ),
                )
                skipped += 1
                continue
            if conflict_resolver is None:
                raise LabSyncError(
                    "interactive user-correction conflict encountered but no conflict_resolver "
                    "was provided; rerun non-interactively or resolve via fm-correct-fact"
                )
            choice = conflict_resolver(
                person_id,
                test_name,
                observed_at,
                corrected_value,
                lab.get("value"),
            )
            if choice == "keep_correction":
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="lab_result",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        "user chose keep_correction for lab "
                        f"{test_name!r} at {observed_at}; import value skipped"
                    ),
                )
                skipped += 1
                continue
            # accept_import or revoke_and_import: clear the user_correction flag
            conn.execute(
                "UPDATE lab_results SET source_type = 'manual' WHERE person_id = ? "
                "AND test_name = ? AND observed_at = ? AND source_type = 'user_correction'",
                (person_id, test_name, observed_at),
            )
            if choice == "revoke_and_import":
                log_audit_event(
                    conn,
                    event_type=AUDIT_USER_CORRECTION_REVOKED,
                    subject_type="lab_result",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        f"user chose revoke_and_import for lab {test_name!r} at {observed_at}; "
                        f"correction value {corrected_value!r} revoked; import value applied"
                    ),
                )
            # Fall through to insert the import row below.

        elif _lab_row_is_user_correction(conn, person_id, source_path, row_index):
            if non_interactive:
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="lab_result",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        "skipped import row-index overwrite of user-corrected lab "
                        f"{test_name!r} at {observed_at}"
                    ),
                )
                skipped += 1
                continue
            if conflict_resolver is None:
                raise LabSyncError(
                    "interactive user-correction conflict encountered but no conflict_resolver "
                    "was provided; rerun non-interactively or resolve via fm-correct-fact"
                )
            corrected_row = conn.execute(
                "SELECT value FROM lab_results WHERE person_id = ? AND source_path = ? "
                "AND source_type = 'user_correction' LIMIT 1",
                (person_id, source_path),
            ).fetchone()
            row_corrected_value = corrected_row[0] if corrected_row else None
            choice = conflict_resolver(
                person_id,
                test_name,
                observed_at,
                row_corrected_value,
                lab.get("value"),
            )
            if choice == "keep_correction":
                log_audit_event(
                    conn,
                    event_type=AUDIT_SKIPPED_USER_CORRECTION,
                    subject_type="lab_result",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        "user chose keep_correction for row-index-corrected lab "
                        f"{test_name!r} at {observed_at}; import skipped"
                    ),
                )
                skipped += 1
                continue
            conn.execute(
                "DELETE FROM lab_results WHERE person_id = ? AND source_path = ? "
                "AND source_type = 'user_correction'",
                (person_id, source_path),
            )
            if choice == "revoke_and_import":
                log_audit_event(
                    conn,
                    event_type=AUDIT_USER_CORRECTION_REVOKED,
                    subject_type="lab_result",
                    subject_id=person_id,
                    source_path=source_path,
                    status=STATE_PROCESSED,
                    message=(
                        f"user chose revoke_and_import for row-index-corrected lab "
                        f"{test_name!r} at {observed_at}"
                    ),
                )
            # Fall through to insert.

        same_draw_rows = _find_same_draw_miscode_redundant_observations(
            conn,
            person_id,
            test_name,
            observed_at,
            lab.get("value"),
            str(lab.get("value_text") or ""),
            str(lab.get("loinc_code") or ""),
            str(lab.get("unit") or ""),
            lab.get("reference_range_low"),
            lab.get("reference_range_high"),
            source_path,
        )
        if same_draw_rows:
            if _incoming_lab_replaces_redundant_rows(lab, same_draw_rows, source_path):
                conn.executemany(
                    "DELETE FROM lab_results WHERE id = ?",
                    [(str(row["id"]),) for row in same_draw_rows],
                )
            else:
                suppressed = dict(lab)
                suppressed["source_path"] = source_path
                suppressed["row_index"] = row_index
                record_dedup_link(
                    conn,
                    subject_type=DEDUP_SUBJECT_LAB_RESULT,
                    person_id=person_id,
                    suppressed_source_path=source_path,
                    owner_source_path=str(same_draw_rows[0]["source_path"]),
                    identity=_lab_dedup_link_identity(lab),
                    payload=suppressed,
                )
                skipped += 1
                continue

        existing = _find_existing_observation(conn, person_id, test_name, observed_at, source_path)
        if existing is not None:
            owner_source_path, existing_value = existing
            if existing_value == lab.get("value"):
                redundant_rows = _find_redundant_observations(
                    conn,
                    person_id,
                    test_name,
                    observed_at,
                    lab.get("value"),
                    str(lab.get("value_text") or ""),
                    source_path,
                )
                if _incoming_lab_replaces_redundant_rows(
                    lab,
                    redundant_rows,
                    source_path,
                ):
                    conn.executemany(
                        "DELETE FROM lab_results WHERE id = ?",
                        [(str(row["id"]),) for row in redundant_rows],
                    )
                else:
                    suppressed = dict(lab)
                    suppressed["source_path"] = source_path
                    suppressed["row_index"] = row_index
                    record_dedup_link(
                        conn,
                        subject_type=DEDUP_SUBJECT_LAB_RESULT,
                        person_id=person_id,
                        suppressed_source_path=source_path,
                        owner_source_path=owner_source_path,
                        identity=_lab_dedup_link_identity(lab),
                        payload=suppressed,
                    )
                    skipped += 1
                    continue
        lab_copy = dict(lab)
        lab_copy["source_path"] = source_path
        lab_copy["row_index"] = row_index
        upsert_lab_result(conn, lab_copy)
        inserted += 1
    _rematerialize_orphaned_observations(conn, affected_person_ids)
    return inserted, skipped


def _rematerialize_orphaned_observations(conn: sqlite3.Connection, person_ids: set[str]) -> None:
    """Restore skipped observations whose owning source no longer reports them.

    A cross-source redundant observation is skipped because an equivalent already
    exists elsewhere. When that owning row is later removed — typically because its
    own source is reprocessed and no longer yields the analyte — the observation
    would otherwise disappear from the canonical store while still rendering in
    every source-evidence note (ADR-006, ADR-038, ADR-006).
    """
    for link_id, person_id, suppressed_source_path, payload in load_dedup_links(
        conn, subject_type=DEDUP_SUBJECT_LAB_RESULT, person_ids=person_ids
    ):
        lab = dict(payload)
        test_name = str(lab.get("test_name") or "")
        observed_at = str(lab.get("observed_at") or "")
        if (
            _find_existing_observation(
                conn, person_id, test_name, observed_at, suppressed_source_path
            )
            is not None
        ):
            continue
        if _find_same_draw_miscode_redundant_observations(
            conn,
            person_id,
            test_name,
            observed_at,
            lab.get("value"),
            str(lab.get("value_text") or ""),
            str(lab.get("loinc_code") or ""),
            str(lab.get("unit") or ""),
            lab.get("reference_range_low"),
            lab.get("reference_range_high"),
            suppressed_source_path,
        ):
            # Archive restatements may use a different timestamp, LOINC, or blank
            # unit while still satisfying the same bounded equivalence rule used
            # when the suppression link was created.
            continue
        if _find_user_corrected_observation(conn, person_id, test_name, observed_at) is not None:
            # A user correction outranks a retained source (ADR-004). Leave the link
            # in place so the observation stays recoverable if that correction is revoked.
            continue
        upsert_lab_result(conn, lab)
        delete_dedup_link(conn, link_id)
        log_audit_event(
            conn,
            event_type=AUDIT_LAB_DEDUPLICATED_ROW_RESTORED,
            subject_type="lab_result",
            subject_id=person_id,
            source_path=suppressed_source_path,
            status=STATE_PROCESSED,
            message=(
                f"restored cross-source deduplicated observation {test_name!r} at "
                f"{observed_at} after its owning source stopped reporting it"
            ),
        )


def _observed_at_sort_key(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _display_lab_value(lab: dict[str, Any]) -> str:
    value_text = str(lab.get("value_text") or "").strip()
    if value_text:
        return value_text
    value = lab.get("value")
    return "" if value is None else str(value)


def _display_reading_value(reading: dict[str, Any]) -> str:
    value_text = str(reading.get("value_text") or "").strip()
    if value_text:
        return value_text
    value = reading.get("value")
    return "—" if value is None else str(value)


def _markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("|", r"\|")


# Caregiver report sections. Assignment uses official LOINC fields only:
# the specimen (SYSTEM) first, then a reviewed COMPONENT table for chemistry
# panels, then the LOINC CLASS. No test-name keywords (ADR-018).
LAB_SECTION_ORDER: tuple[str, ...] = (
    "Blood counts",
    "Lipids",
    "Glucose & diabetes",
    "Kidney",
    "Electrolytes & minerals",
    "Liver",
    "Pancreas",
    "Thyroid",
    "Hormones",
    "Iron & vitamins",
    "Heart",
    "Inflammation",
    "Prostate (PSA)",
    "Other chemistry",
    "Coagulation",
    "Urine",
    "Stool (colon screening)",
    "Infection & immunology",
    "Allergy testing",
    "Drug levels & toxicology",
    "Heart tracing (ECG)",
    "Eye measurements",
    "Other",
    "No standard code",
)
_LOINC_CLASS_SECTIONS: dict[str, str] = {
    "HEM/BC": "Blood counts",
    "CHEM": "Other chemistry",
    "COAG": "Coagulation",
    "UA": "Urine",
    "SERO": "Infection & immunology",
    "MICRO": "Infection & immunology",
    "MOLPATH": "Infection & immunology",
    "ALLERGY": "Allergy testing",
    "DRUG/TOX": "Drug levels & toxicology",
    "EKG.MEAS": "Heart tracing (ECG)",
}
# Reviewed LOINC component roots (lowercase text before the first ".", "/",
# "^" or "+") -> panel section. Extend when a new analyte lands in "Other chemistry".
_LOINC_COMPONENT_SECTIONS: dict[str, str] = {
    "cholesterol": "Lipids",
    "triglyceride": "Lipids",
    "lipoprotein": "Lipids",
    "lipoprotein (little a)": "Lipids",
    "lipoprotein insulin resistance score": "Lipids",
    "glucose": "Glucose & diabetes",
    "hemoglobin a1c": "Glucose & diabetes",
    "estimated average glucose": "Glucose & diabetes",
    "insulin": "Glucose & diabetes",
    "creatinine": "Kidney",
    "urea nitrogen": "Kidney",
    "glomerular filtration rate": "Kidney",
    "cystatin c": "Kidney",
    "sodium": "Electrolytes & minerals",
    "potassium": "Electrolytes & minerals",
    "chloride": "Electrolytes & minerals",
    "carbon dioxide": "Electrolytes & minerals",
    "anion gap": "Electrolytes & minerals",
    "calcium": "Electrolytes & minerals",
    "magnesium": "Electrolytes & minerals",
    "phosphate": "Electrolytes & minerals",
    "alanine aminotransferase": "Liver",
    "aspartate aminotransferase": "Liver",
    "alkaline phosphatase": "Liver",
    "gamma glutamyl transferase": "Liver",
    "bilirubin": "Liver",
    "albumin": "Liver",
    "globulin": "Liver",
    "protein": "Liver",
    "amylase": "Pancreas",
    "triacylglycerol lipase": "Pancreas",
    "thyrotropin": "Thyroid",
    "thyroxine": "Thyroid",
    "triiodothyronine": "Thyroid",
    "thyroperoxidase ab": "Thyroid",
    "thyroglobulin ab": "Thyroid",
    "testosterone": "Hormones",
    "follitropin": "Hormones",
    "lutropin": "Hormones",
    "estradiol": "Hormones",
    "dehydroepiandrosterone sulfate": "Hormones",
    "prolactin": "Hormones",
    "cortisol": "Hormones",
    "iron": "Iron & vitamins",
    "iron binding capacity": "Iron & vitamins",
    "iron saturation": "Iron & vitamins",
    "ferritin": "Iron & vitamins",
    "cobalamins": "Iron & vitamins",
    "folate": "Iron & vitamins",
    "calcidiol": "Iron & vitamins",
    "homocysteine": "Iron & vitamins",
    "troponin i": "Heart",
    "troponin t": "Heart",
    "natriuretic peptide": "Heart",
    "c reactive protein": "Inflammation",
    "prostate specific ag": "Prostate (PSA)",
}
_ALL_TESTS_SECTION = "All tests"

LoincAttributes = str | tuple[str, str, str]


def _component_root(component: str) -> str:
    return re.split(r"[./^+]", component.casefold(), maxsplit=1)[0].strip()


def lab_report_section(loinc_code: str | None, loinc_classes: Mapping[str, LoincAttributes]) -> str:
    """Caregiver section for a test from its official LOINC fields."""
    if not loinc_code:
        return "No standard code"
    attributes = loinc_classes.get(loinc_code)
    if attributes is None:
        return "Other"
    loinc_class, component, system = (
        (attributes, "", "") if isinstance(attributes, str) else attributes
    )
    specimen = system.casefold()
    # Drug screens and infection tests keep their own sections whatever the specimen.
    if not loinc_class.startswith(("DRUG/TOX", "MICRO", "PANEL.DRUG/TOX")):
        if specimen.startswith("urine"):
            return "Urine"
        if specimen.startswith("stool"):
            return "Stool (colon screening)"
    panel = _LOINC_COMPONENT_SECTIONS.get(_component_root(component))
    if panel is not None and not loinc_class.startswith(("HEM/BC", "ALLERGY", "DRUG/TOX")):
        return panel
    loinc_class = loinc_class.removeprefix("PANEL.")
    if loinc_class.startswith("EYE"):
        return "Eye measurements"
    if loinc_class.startswith("CHAL"):
        return "Other chemistry"
    return _LOINC_CLASS_SECTIONS.get(loinc_class, "Other")


def _group_lab_rows(
    rows: list[dict[str, Any]], loinc_classes: Mapping[str, LoincAttributes] | None
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Rows grouped by section in LAB_SECTION_ORDER; one group without a LOINC cache."""
    if not loinc_classes:
        return [(_ALL_TESTS_SECTION, rows)] if rows else []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        section = lab_report_section(row.get("loinc_code"), loinc_classes)
        grouped.setdefault(section, []).append(row)
    return [(section, grouped[section]) for section in LAB_SECTION_ORDER if section in grouped]


def lab_report_loinc_classes(vault_root: Path) -> dict[str, tuple[str, str, str]]:
    """LOINC classes from the reference cache beside the vault, never the CWD."""
    return load_loinc_classes(vault_root.parent / "data" / "reference")


def build_lab_conflicts_from_db(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT observed_at, test_name, value, value_text, unit, source_path
        FROM lab_results
        WHERE person_id = ?
        ORDER BY observed_at DESC, test_name COLLATE NOCASE, source_path
        """,
        (person_id,),
    ).fetchall()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for observed_at, test_name, value, value_text, unit, source_path in rows:
        key = (str(observed_at), str(test_name))
        grouped.setdefault(key, []).append(
            {
                "observed_at": str(observed_at),
                "test_name": str(test_name),
                "value": value,
                "value_text": str(value_text or ""),
                "unit": str(unit or ""),
                "source_path": str(source_path or ""),
            }
        )

    conflicts: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        rows_with_values = [r for r in group_rows if r["value"] is not None or r["value_text"]]
        has_cross_source_conflict = any(
            (r1["value"], _value_text_identity(r1["value"], r1["value_text"]))
            != (r2["value"], _value_text_identity(r2["value"], r2["value_text"]))
            and r1["source_path"] != r2["source_path"]
            for i, r1 in enumerate(rows_with_values)
            for r2 in rows_with_values[i + 1 :]
        )
        if has_cross_source_conflict:
            conflicts.extend(group_rows)

    return conflicts


def render_lab_conflicts_report(person_id: str, conflict_rows: list[dict[str, Any]]) -> str:
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "lab_conflicts",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()

    if not conflict_rows:
        conflicts_block = "_No conflicting lab values found._"
    else:
        table_rows = [LAB_CONFLICTS_TABLE_HEADER, "| --- | --- | --- | --- | --- |"]
        for row in conflict_rows:
            table_rows.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(row.get("observed_at")),
                        _markdown_cell(row.get("test_name")),
                        _markdown_cell(_display_lab_value(row)),
                        _markdown_cell(row.get("unit")),
                        _markdown_cell(row.get("source_path")),
                    ]
                )
                + " |"
            )
        conflicts_block = "\n".join(table_rows)

    return (
        f"---\n{yaml_block}\n---\n\n"
        "# Lab Conflicts\n\n"
        "Rows appear here when the same person, test, and observation date have different "
        "values from different sources. Review the retained source files before resolving.\n\n"
        "## Conflicts\n\n"
        f"{conflicts_block}\n"
    )


# Test names matching these substrings (case-insensitive) are CCDA specimen/
# device metadata (reagent lot numbers, kit expiration dates), not patient
# lab results, and are excluded from trends entirely.
_NON_RESULT_TEST_NAME_SUBSTRINGS = ("lot number", "lot #", "expiration date")


# LOINC concepts whose `system` is `^Patient`: they describe the panel or the
# patient, not a specimen measurement, so they cannot trend and must not render
# as analytes. 56850-1 is the lab's overall verdict for a panel, which per-test
# interpretation flags now express directly and per result.
_PANEL_CONTEXT_LOINC_CODES: frozenset[str] = frozenset({"56850-1"})


def _is_non_result_test_name(test_name: str) -> bool:
    lowered = test_name.casefold()
    return any(substring in lowered for substring in _NON_RESULT_TEST_NAME_SUBSTRINGS)


_TREND_UP = "↑"
_TREND_DOWN = "↓"
_TREND_STABLE = "→"
_TREND_QUALITATIVE = "—"

_TREND_CHANGE_THRESHOLD = 0.001

# Exclusion 1 (ADR-030): the latest in-range value must be within this
# proportion of a reference boundary and moving toward it to appear in the
# Flagged for Discussion report's Trending section.
_FLAGGED_THRESHOLD_PCT = 0.10


def build_lab_trends_from_db(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    """Return one trend dict per distinct test name for *person_id*.

    Each dict contains:
    - ``test_name``, ``loinc_code`` (may be empty string)
    - ``readings``: list of {date, value, unit, ref_low, ref_high} in
      chronological order; duplicate (test_name, observed_at) pairs are
      de-duplicated by keeping the first-inserted row (lowest rowid).
    - ``trend``: ``"up"``, ``"down"``, ``"stable"``, ``"qualitative"``
      (all values null), or ``None`` (single reading only).
    - ``in_range_latest``: ``True``/``False`` when a numeric latest value and
      at least one reference bound are available (see ``_reading_in_range``);
      ``None`` otherwise.
    """
    rows = conn.execute(
        """
        SELECT test_name, loinc_code, observed_at, value, value_text,
               unit, reference_range_low, reference_range_high, source_path,
               source_type, interpretation_code
        FROM lab_results
        WHERE person_id = ?
        ORDER BY test_name COLLATE NOCASE, observed_at ASC, rowid ASC
        """,
        (person_id,),
    ).fetchall()

    collapsed_rows: list[dict[str, Any]] = []
    for (
        raw_test_name,
        loinc_code,
        observed_at,
        value,
        value_text,
        unit,
        ref_low,
        ref_high,
        source_path,
        source_type,
        interpretation_code,
    ) in rows:
        normalized_loinc = str(loinc_code or "")
        if normalized_loinc in _PANEL_CONTEXT_LOINC_CODES:
            continue
        test_name = normalize_test_name(raw_test_name, normalized_loinc, unit)
        if _is_non_result_test_name(test_name):
            continue
        candidate = {
            "test_name": test_name,
            "loinc_code": normalized_loinc,
            "observed_at": str(observed_at),
            "value": value,
            "value_text": str(value_text or ""),
            "unit": str(unit or ""),
            "interpretation_code": interpretation_code,
            "reference_range_low": ref_low,
            "reference_range_high": ref_high,
            "source_path": str(source_path or ""),
            "source_type": str(source_type or ""),
        }
        matching_index = next(
            (
                index
                for index, existing in enumerate(collapsed_rows)
                if existing["test_name"] == test_name
                and _is_archive_restatement_match(
                    existing_source_path=str(existing.get("source_path") or ""),
                    candidate_source_path=str(candidate.get("source_path") or ""),
                    existing_value=existing.get("value"),
                    candidate_value=candidate.get("value"),
                    existing_value_text=existing.get("value_text"),
                    candidate_value_text=candidate.get("value_text"),
                    existing_loinc=existing.get("loinc_code"),
                    candidate_loinc=candidate.get("loinc_code"),
                    existing_unit=existing.get("unit"),
                    candidate_unit=candidate.get("unit"),
                    existing_ref_low=existing.get("reference_range_low"),
                    candidate_ref_low=candidate.get("reference_range_low"),
                    existing_ref_high=existing.get("reference_range_high"),
                    candidate_ref_high=candidate.get("reference_range_high"),
                    existing_observed_at=str(existing.get("observed_at") or ""),
                    candidate_observed_at=str(candidate.get("observed_at") or ""),
                )
            ),
            None,
        )
        if matching_index is None:
            collapsed_rows.append(candidate)
        elif _prefer_lab_candidate(collapsed_rows[matching_index], candidate):
            collapsed_rows[matching_index] = candidate

    # Group by canonical test name only — different LOINC codes for the same
    # normalized name are the same analyte (different lab systems use different
    # codes; source-provided codes can't be controlled at import time).
    # When the group's stored LOINC is blank and a reading has one, promote it.
    grouped: dict[str, dict[str, Any]] = {}
    for candidate in collapsed_rows:
        normalized_loinc = str(candidate["loinc_code"] or "")
        test_name = str(candidate["test_name"] or "")

        if test_name not in grouped:
            grouped[test_name] = {
                "test_name": test_name,
                "loinc_code": normalized_loinc,
                "readings": {},
            }
        elif not grouped[test_name]["loinc_code"] and normalized_loinc:
            grouped[test_name]["loinc_code"] = normalized_loinc

        date_key = str(candidate["observed_at"])
        reading = {
            "date": date_key,
            "value": candidate["value"],
            "value_text": str(candidate.get("value_text") or ""),
            "unit": str(candidate["unit"] or ""),
            "ref_low": candidate["reference_range_low"],
            "ref_high": candidate["reference_range_high"],
            "interpretation_code_stated": candidate.get("interpretation_code"),
            "interpretation": interpretation_for_reading(
                candidate.get("interpretation_code"),
                candidate["value"],
                candidate["reference_range_low"],
                candidate["reference_range_high"],
            ),
            "source_path": str(candidate["source_path"] or ""),
        }
        if any(
            _is_archive_restatement_of_existing_reading(existing, reading)
            for existing in grouped[test_name]["readings"].values()
        ):
            continue
        if date_key not in grouped[test_name]["readings"]:
            grouped[test_name]["readings"][date_key] = reading

    result: list[dict[str, Any]] = []
    for data in grouped.values():
        test_name = str(data["test_name"])
        readings = sorted(data["readings"].values(), key=lambda r: r["date"])
        numeric = [r for r in readings if r["value"] is not None]

        if len(readings) == 1:
            trend: str | None = None
        elif len(numeric) < 2:
            trend = "qualitative"
        else:
            prev_val: float = float(numeric[-2]["value"])
            last_val: float = float(numeric[-1]["value"])
            if prev_val == 0:
                trend = "stable" if last_val == 0 else "up" if last_val > 0 else "down"
            elif last_val > prev_val * (1 + _TREND_CHANGE_THRESHOLD):
                trend = "up"
            elif last_val < prev_val * (1 - _TREND_CHANGE_THRESHOLD):
                trend = "down"
            else:
                trend = "stable"

        latest_numeric = numeric[-1] if numeric else None
        in_range: bool | None = (
            None if latest_numeric is None else _reading_in_range(latest_numeric)
        )

        result.append(
            {
                "test_name": test_name,
                "loinc_code": data["loinc_code"],
                "readings": readings,
                "trend": trend,
                "in_range_latest": in_range,
            }
        )

    return result


def _is_archive_source_path(source_path: str) -> bool:
    return "/HealthSummary/" in source_path and ".zip#" in source_path


def _parse_observed_at_for_dedup(observed_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_archive_restatement_of_existing_reading(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    return _is_archive_restatement_match(
        existing_source_path=str(existing.get("source_path") or ""),
        candidate_source_path=str(candidate.get("source_path") or ""),
        existing_value=existing.get("value"),
        candidate_value=candidate.get("value"),
        existing_value_text=existing.get("value_text"),
        candidate_value_text=candidate.get("value_text"),
        existing_loinc="",
        candidate_loinc="",
        existing_unit=existing.get("unit"),
        candidate_unit=candidate.get("unit"),
        existing_ref_low=existing.get("ref_low"),
        candidate_ref_low=candidate.get("ref_low"),
        existing_ref_high=existing.get("ref_high"),
        candidate_ref_high=candidate.get("ref_high"),
        existing_observed_at=str(existing.get("date") or ""),
        candidate_observed_at=str(candidate.get("date") or ""),
    )


def _trend_symbol(trend: str | None) -> str:
    return {
        "up": _TREND_UP,
        "down": _TREND_DOWN,
        "stable": _TREND_STABLE,
        "qualitative": _TREND_QUALITATIVE,
    }.get(trend or "", "—")


def _in_range_label(in_range: bool | None) -> str:
    if in_range is True:
        return "Yes"
    if in_range is False:
        return "No"
    return "—"


def _reading_in_range(reading: dict[str, Any]) -> bool | None:
    value = reading.get("value")
    ref_low = reading.get("ref_low")
    ref_high = reading.get("ref_high")
    if value is None or (ref_low is None and ref_high is None):
        return None
    try:
        value_f = float(value)
        low_f = float(ref_low) if ref_low is not None else None
        high_f = float(ref_high) if ref_high is not None else None
    except (TypeError, ValueError):
        return None
    if low_f is not None and value_f < low_f:
        return False
    if high_f is not None and value_f > high_f:
        return False
    return True


def _reference_range_changed(previous: dict[str, Any], latest: dict[str, Any]) -> bool:
    """Return whether two readings were evaluated under different reference bounds.

    A reference-range change between consecutive readings (e.g. a lab switching
    assay/threshold policy, such as standard-range vs. narrow-range TSH) is a
    policy discontinuity, not a physiological trend (ADR-037).
    """

    def _as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return _as_float(previous.get("ref_low")) != _as_float(latest.get("ref_low")) or _as_float(
        previous.get("ref_high")
    ) != _as_float(latest.get("ref_high"))


def _is_approaching_reference_threshold(row: dict[str, Any]) -> bool:
    """Return whether an in-range trend is approaching a reference bound.

    Two-sided ranges use 10% of the range width as the proximity margin.
    Single-sided ranges use 10% of the available bound's magnitude. The latest
    numeric value must remain in range and move toward the nearby bound.
    """
    numeric = [reading for reading in row["readings"] if reading["value"] is not None]
    if len(numeric) < 2:
        return False
    previous, latest = numeric[-2:]
    if _reading_in_range(latest) is not True:
        return False
    if _reference_range_changed(previous, latest):
        return False
    ref_low = latest["ref_low"]
    ref_high = latest["ref_high"]
    if ref_low is None and ref_high is None:
        return False
    try:
        previous_f = float(previous["value"])
        latest_f = float(latest["value"])
        low_f = float(ref_low) if ref_low is not None else None
        high_f = float(ref_high) if ref_high is not None else None
    except (TypeError, ValueError):
        return False

    if low_f is not None and high_f is not None:
        range_width = high_f - low_f
        if range_width <= 0:
            return False
        low_margin = high_margin = _FLAGGED_THRESHOLD_PCT * range_width
    else:
        low_margin = _FLAGGED_THRESHOLD_PCT * abs(low_f) if low_f is not None else None
        high_margin = _FLAGGED_THRESHOLD_PCT * abs(high_f) if high_f is not None else None

    approaching_low = (
        low_f is not None
        and low_margin is not None
        and latest_f <= low_f + low_margin
        and latest_f < previous_f
    )
    approaching_high = (
        high_f is not None
        and high_margin is not None
        and latest_f >= high_f - high_margin
        and latest_f > previous_f
    )
    return approaching_low or approaching_high


def _two_most_recent_panel_dates(trend_rows: list[dict[str, Any]]) -> frozenset[str]:
    """Return the 2 most recent distinct observed dates across all trend rows."""
    all_dates: set[str] = set()
    for row in trend_rows:
        for reading in row["readings"]:
            if reading.get("date"):
                all_dates.add(reading["date"])
    sorted_dates = sorted(all_dates, reverse=True)
    return frozenset(sorted_dates[:2])


def _is_stale_result(row: dict[str, Any], recent_dates: frozenset[str]) -> bool:
    """Return True when the row's most recent reading is not in recent_dates.

    A result is stale when its most recent panel date falls outside the person's
    2 most recent distinct panel dates (ADR-030 Exclusion 2).
    """
    if not recent_dates or not row["readings"]:
        return False
    return row["readings"][-1]["date"] not in recent_dates


def _parse_iso_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _is_point_of_care_test_name(test_name: str) -> bool:
    """Return True for POCT (point-of-care) test names, e.g. ``POCT Glucose iSTAT``."""
    return test_name.upper().startswith("POCT")


def _most_recent_panel_date(trend_rows: list[dict[str, Any]]) -> date | None:
    """Return the most recent reading date, excluding point-of-care tests.

    POCT dipstick/iSTAT checks are isolated single-analyte visits, not a
    comprehensive panel draw. Counting them would let an unrelated POCT visit
    silently shrink the Out-of-Range recency window for unrelated tests that
    haven't been re-checked — the same "newer unrelated panel hides a
    clinically recent abnormal result" failure mode ADR-030 already excluded
    the 2-panel-date rule from Out of Range to avoid.
    """
    latest: date | None = None
    for row in trend_rows:
        if _is_point_of_care_test_name(str(row.get("test_name", ""))):
            continue
        for reading in row["readings"]:
            observed_date = _parse_iso_date(reading.get("date"))
            if observed_date is not None and (latest is None or observed_date > latest):
                latest = observed_date
    return latest


def _is_out_of_range_result_stale(
    row: dict[str, Any],
    anchor_date: date | None,
    *,
    window_days: int = _FLAGGED_OUT_OF_RANGE_WINDOW_DAYS,
) -> bool:
    """Return True when the latest reading falls outside the anchored day window.

    Despite the name, this is a generic recency check on the latest reading's
    date and is reused as-is for the Recently Normalized section, not only
    Out of Range; it does not inspect ``in_range_latest``.
    """
    if anchor_date is None or not row["readings"]:
        return False
    latest_date = _parse_iso_date(row["readings"][-1].get("date"))
    if latest_date is None:
        return False
    cutoff = anchor_date - timedelta(days=window_days)
    return latest_date < cutoff


def _read_accepted_history_facts(note_path: Path) -> list[dict[str, Any]]:
    try:
        content = note_path.read_text(encoding="utf-8")
        front_matter = parse_front_matter(content)
    except (OSError, PersonSyncError):
        return []
    raw_facts = front_matter.get("accepted_facts")
    if not isinstance(raw_facts, list):
        return []
    return [fact for fact in raw_facts if isinstance(fact, dict)]


def _extract_coronary_calcium_findings(vault_root: Path, person_id: str) -> list[str]:
    history_dir = vault_root / "Records" / person_id / "History"
    if not history_dir.is_dir():
        return []

    findings: list[tuple[date, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for note_path in sorted(history_dir.glob("*.md")):
        if note_path.name == "_index.md":
            continue
        source_link = f"[[Records/{person_id}/History/{note_path.stem}|{note_path.stem}]]"
        for fact in _read_accepted_history_facts(note_path):
            description = str(fact.get("description") or "").strip()
            if not description:
                continue
            normalized = description.casefold()
            if not any(term in normalized for term in _CAC_DESCRIPTION_TERMS):
                continue
            recorded_date = _parse_iso_date(fact.get("recorded_date"))
            rendered_date = recorded_date.isoformat() if recorded_date is not None else "Undated"
            value = str(fact.get("value") or "").strip()
            unit = str(fact.get("unit") or "").strip()
            measure = f": {value} {unit}".rstrip() if value else ""
            rendered = f"- {rendered_date} — {description}{measure}. Source: {source_link}"
            dedupe_key = (rendered_date, description.casefold(), value)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            findings.append((recorded_date or date.min, rendered))

    findings.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [rendered for _sort_key, rendered in findings]


def _format_reference_range(ref_low: Any, ref_high: Any) -> str:
    if ref_low is not None and ref_high is not None:
        return f"{ref_low}–{ref_high}"
    if ref_high is not None:
        return f"≤{ref_high}"
    if ref_low is not None:
        return f"≥{ref_low}"
    return "—"


def _numeric_readings(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [reading for reading in row["readings"] if reading["value"] is not None]


def _previous_numeric_reading(row: dict[str, Any]) -> dict[str, Any] | None:
    numeric_readings = _numeric_readings(row)
    if len(numeric_readings) < 2:
        return None
    return numeric_readings[-2]


def _previous_numeric_value(row: dict[str, Any]) -> str:
    previous = _previous_numeric_reading(row)
    if previous is None:
        return "—"
    return str(previous["value"])


def _previous_numeric_reading_was_out_of_range(row: dict[str, Any]) -> bool:
    previous = _previous_numeric_reading(row)
    return previous is not None and _reading_in_range(previous) is False


def _previous_numeric_reading_was_in_range(row: dict[str, Any]) -> bool:
    previous = _previous_numeric_reading(row)
    return previous is not None and _reading_in_range(previous) is True


# ---------------------------------------------------------------------------
# SVG sparkline charts for flagged-for-discussion (ADR-030 visual rendering)
# ---------------------------------------------------------------------------

_SPARKLINE_W = 420
_SPARKLINE_H = 96
_SPARKLINE_X0 = 16.0
_SPARKLINE_X1 = 404.0
_SPARKLINE_Y = 58
_SPARKLINE_GREEN = "#3b9a57"
_SPARKLINE_RED = "#c0392b"
_SPARKLINE_GRAY = "#888"


def _sparkline_axis(
    ref_low: float | None,
    ref_high: float | None,
    prev_val: float | None,
    latest_val: float,
) -> tuple[float, float]:
    candidates = [v for v in (ref_low, ref_high, prev_val) if v is not None] + [latest_val]
    data_min = min(candidates)
    data_max = max(candidates)
    if ref_low is not None and ref_high is not None:
        ref_span = ref_high - ref_low
    elif ref_low is not None or ref_high is not None:
        bound = ref_low if ref_low is not None else ref_high
        ref_span = max(abs(bound), data_max - data_min, 1e-9) * 0.25  # type: ignore[arg-type]
    else:
        ref_span = max(data_max - data_min, abs(latest_val) * 0.25, 1e-9)
    margin = max(0.25 * ref_span, 1e-9)
    axis_min = data_min - margin
    axis_max = data_max + margin
    return axis_min, axis_max if axis_max > axis_min else axis_min + 1.0


def _val_to_sparkline_x(v: float, axis_min: float, axis_max: float) -> float:
    span = axis_max - axis_min
    return round(_SPARKLINE_X0 + (v - axis_min) / span * (_SPARKLINE_X1 - _SPARKLINE_X0), 1)


def _sparkline_label_x(dot_x: float, other_x: float | None) -> float:
    """Nudge label away from the other dot when they overlap."""
    if other_x is None:
        return dot_x
    gap = abs(dot_x - other_x)
    threshold = 26.0
    if gap >= threshold:
        return dot_x
    nudge = (threshold - gap) / 2.0 + 2.0
    return round(dot_x + nudge if dot_x >= other_x else dot_x - nudge, 1)


def _sparkline_fmt(v: float) -> str:
    return f"{v:g}"


def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_lab_sparkline(row: dict[str, Any], in_range: bool | None) -> str | None:
    """Return a single-line SVG sparkline for one trend row, or None if non-numeric."""
    numeric = _numeric_readings(row)
    if not numeric:
        return None

    latest = numeric[-1]
    prev = numeric[-2] if len(numeric) >= 2 else None

    latest_val = float(latest["value"])
    prev_val = float(prev["value"]) if prev is not None else None
    ref_low = float(latest["ref_low"]) if latest.get("ref_low") is not None else None
    ref_high = float(latest["ref_high"]) if latest.get("ref_high") is not None else None

    axis_min, axis_max = _sparkline_axis(ref_low, ref_high, prev_val, latest_val)
    lx = _val_to_sparkline_x(latest_val, axis_min, axis_max)
    px = _val_to_sparkline_x(prev_val, axis_min, axis_max) if prev_val is not None else None

    unit = latest.get("unit") or ""
    title = _svg_escape(row["test_name"])
    if unit:
        title += f" ({_svg_escape(unit)})"
    previous_or_latest = prev if prev is not None else latest
    title += f" — {previous_or_latest['date'][:10]} → {latest['date'][:10]}"

    dot_color = _SPARKLINE_RED if in_range is False else _SPARKLINE_GREEN

    elems: list[str] = [
        f'<svg width="{_SPARKLINE_W}" height="{_SPARKLINE_H}"'
        f' viewBox="0 0 {_SPARKLINE_W} {_SPARKLINE_H}"'
        f' xmlns="http://www.w3.org/2000/svg" font-family="sans-serif">',
        f'<text x="{_SPARKLINE_X0}" y="14" font-size="12" font-weight="600"'
        f' fill="{_SPARKLINE_GRAY}">{title}</text>',
        f'<line x1="{_SPARKLINE_X0}" y1="{_SPARKLINE_Y}"'
        f' x2="{_SPARKLINE_X1}" y2="{_SPARKLINE_Y}" stroke="#999" stroke-width="1"/>',
    ]

    if ref_low is not None or ref_high is not None:
        rl = (
            _val_to_sparkline_x(ref_low, axis_min, axis_max)
            if ref_low is not None
            else _SPARKLINE_X0
        )
        rh = (
            _val_to_sparkline_x(ref_high, axis_min, axis_max)
            if ref_high is not None
            else _SPARKLINE_X1
        )
        elems.append(
            f'<rect x="{rl}" y="49" width="{round(rh - rl, 1)}" height="18"'
            f' fill="{_SPARKLINE_GREEN}" opacity="0.18"/>'
        )
        if ref_low is not None:
            elems += [
                f'<line x1="{rl}" y1="46" x2="{rl}" y2="70"'
                f' stroke="{_SPARKLINE_GREEN}" stroke-width="1"/>',
                f'<text x="{rl}" y="83" font-size="9.5"'
                f' fill="{_SPARKLINE_GREEN}" text-anchor="middle">{_sparkline_fmt(ref_low)}</text>',
            ]
        if ref_high is not None:
            elems += [
                f'<line x1="{rh}" y1="46" x2="{rh}" y2="70"'
                f' stroke="{_SPARKLINE_GREEN}" stroke-width="1"/>',
                f'<text x="{rh}" y="83" font-size="9.5" fill="{_SPARKLINE_GREEN}"'
                f' text-anchor="middle">{_sparkline_fmt(ref_high)}</text>',
            ]

    if px is not None and prev_val is not None:
        elems += [
            f'<circle cx="{px}" cy="{_SPARKLINE_Y}" r="5" fill="none"'
            f' stroke="{_SPARKLINE_GRAY}" stroke-width="1.5"/>',
            f'<text x="{_sparkline_label_x(px, lx)}" y="36" font-size="11"'
            f' fill="{_SPARKLINE_GRAY}" text-anchor="middle">{_sparkline_fmt(prev_val)}</text>',
        ]

    flx = _sparkline_label_x(lx, px)
    fs, fw = ("14", ' font-weight="700"') if px is not None else ("12", "")
    elems += [
        f'<circle cx="{lx}" cy="{_SPARKLINE_Y}" r="7" fill="{dot_color}"/>',
        f'<text x="{flx}" y="36" font-size="{fs}"'
        f' fill="{dot_color}"{fw} text-anchor="middle">{_sparkline_fmt(latest_val)}</text>',
        "</svg>",
    ]
    return "".join(elems)


def _render_lab_visual_section(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        svg = _render_lab_sparkline(row, row["in_range_latest"])
        if svg:
            lines.append(svg)
    lines.append("")
    return lines


_LAB_FLAG_LABELS: dict[str, str] = {
    "AA": "**Critically abnormal**",
    "A": "Abnormal",
    "H": "High",
    "L": "Low",
    "N": "Normal",
}


def _lab_flag_label(reading: dict[str, Any]) -> str:
    """Render the lab's own abnormality flag for one reading.

    Only a flag the source stated is shown. A flag derived from the reference
    range would merely restate the In Range column, whereas a source flag can
    mark a result abnormal that sits inside its own stated bounds -- which is
    the case this column exists to surface.
    """
    stated = source_interpretation_code(reading.get("interpretation_code_stated"))
    if not stated:
        return "—"
    return _LAB_FLAG_LABELS.get(stated, stated)


_METHOD_VARIANT_FAMILIES: tuple[tuple[str, str, str], ...] = (
    (
        "gfr",
        "Kidney function (eGFR / GFR)",
        "These series estimate the same kidney-function measure using different "
        "formulas — for example CKD-EPI, the race-free CKD-EPI 2021 revision, "
        "MDRD, and race-reported variants. They are kept separate on purpose: a "
        "laboratory switching formula produces a step change in the number that "
        "is a methodology change, not a change in kidney function. Compare "
        "readings within one series, and treat a jump between series as a "
        "difference in method until a clinician confirms otherwise.",
    ),
)


def _method_variant_note(trend_rows: list[dict[str, Any]]) -> str:
    """Explain co-existing series that measure one analyte by different methods.

    Returns an empty string unless a person actually has more than one series in
    a known method-variant family, so a single-series report stays uncluttered.
    """
    names = [str(row.get("test_name") or "") for row in trend_rows]
    for token, heading, explanation in _METHOD_VARIANT_FAMILIES:
        matching = sorted({name for name in names if token in name.casefold()})
        if len(matching) < 2:
            continue
        listed = ", ".join(f"`{name}`" for name in matching)
        return f"> **{heading}.** {listed}\n>\n> {explanation}\n"
    return ""


_CHART_W, _CHART_H = 560, 150
_CHART_X0, _CHART_X1, _CHART_Y0, _CHART_Y1 = 46.0, 540.0, 28.0, 118.0
_OLDER_ROW_COLOR = "#888"


def _reading_outside_range(reading: dict[str, Any]) -> bool:
    value = reading.get("value")
    if not isinstance(value, int | float):
        return False
    low, high = reading.get("ref_low"), reading.get("ref_high")
    return (high is not None and value > high) or (low is not None and value < low)


def _reading_flag(reading: dict[str, Any]) -> str:
    value = reading.get("value")
    if not isinstance(value, int | float):
        return ""
    low, high = reading.get("ref_low"), reading.get("ref_high")
    if high is not None and value > high:
        return "H"
    if low is not None and value < low:
        return "L"
    return ""


def _render_lab_trend_chart(row: dict[str, Any]) -> str | None:
    """Time-series SVG: dates across, values up, reference band, date anchors."""
    points = [r for r in row["readings"] if isinstance(r.get("value"), int | float)]
    if len(points) < 2:
        return None
    dates = [date.fromisoformat(str(p["date"])[:10]) for p in points]
    values = [float(p["value"]) for p in points]
    latest = points[-1]
    low, high = latest.get("ref_low"), latest.get("ref_high")
    span = values + [float(v) for v in (low, high) if v is not None]
    v_min, v_max = min(span), max(span)
    pad = (v_max - v_min) * 0.12 or 1.0
    v_min, v_max = v_min - pad, v_max + pad
    first, last = dates[0], dates[-1]
    days = (last - first).days or 1

    def x_of(day: date) -> float:
        return round(_CHART_X0 + (_CHART_X1 - _CHART_X0) * (day - first).days / days, 1)

    def y_of(value: float) -> float:
        return round(_CHART_Y1 - (_CHART_Y1 - _CHART_Y0) * (value - v_min) / (v_max - v_min), 1)

    unit = latest.get("unit") or ""
    title = _svg_escape(row["test_name"]) + (f" ({_svg_escape(unit)})" if unit else "")
    parts = [
        f'<svg width="{_CHART_W}" height="{_CHART_H}" viewBox="0 0 {_CHART_W} {_CHART_H}"'
        ' xmlns="http://www.w3.org/2000/svg" font-family="sans-serif">',
        f'<text x="{_CHART_X0}" y="14" font-size="12" font-weight="600"'
        f' fill="{_SPARKLINE_GRAY}">{title}</text>',
    ]
    if low is not None or high is not None:
        top = y_of(float(high)) if high is not None else _CHART_Y0
        bottom = y_of(float(low)) if low is not None else _CHART_Y1
        parts.append(
            f'<rect x="{_CHART_X0}" y="{top}" width="{_CHART_X1 - _CHART_X0}"'
            f' height="{round(bottom - top, 1)}" fill="{_SPARKLINE_GREEN}" opacity="0.15"/>'
        )
        for bound in (low, high):
            if bound is not None:
                label = _sparkline_fmt(float(bound))
                parts.append(
                    f'<text x="{_CHART_X0 - 4}" y="{y_of(float(bound)) + 3}" font-size="9.5"'
                    f' fill="{_SPARKLINE_GREEN}" text-anchor="end">{label}</text>'
                )
    # Date anchors: each reading's date when few and spread out, else year marks.
    if len(dates) <= 6 and days >= 60:
        ticks = [(day, day.strftime("%b %Y")) for day in dates]
    else:
        ticks = [(date(year, 1, 1), str(year)) for year in range(first.year + 1, last.year + 1)]
        if len(ticks) < 2:
            ticks = [(first, first.strftime("%b %Y")), (last, last.strftime("%b %Y"))]
    last_label_x = -100.0
    for day, label in ticks:
        x = x_of(day)
        parts.append(
            f'<line x1="{x}" y1="{_CHART_Y0}" x2="{x}" y2="{_CHART_Y1}"'
            f' stroke="{_SPARKLINE_GRAY}" stroke-width="0.5" opacity="0.35"/>'
        )
        if x - last_label_x >= 44:
            parts.append(
                f'<text x="{x}" y="{_CHART_H - 8}" font-size="9.5" fill="{_SPARKLINE_GRAY}"'
                f' text-anchor="middle">{label}</text>'
            )
            last_label_x = x
    polyline = " ".join(f"{x_of(d)},{y_of(v)}" for d, v in zip(dates, values, strict=True))
    parts.append(
        f'<polyline points="{polyline}" fill="none" stroke="{_SPARKLINE_GRAY}" stroke-width="1.5"/>'
    )
    for index, (day, point) in enumerate(zip(dates, points, strict=True)):
        color = _SPARKLINE_RED if _reading_outside_range(point) else _SPARKLINE_GREEN
        radius = 5.5 if index == len(points) - 1 else 3.5
        y = y_of(float(point["value"]))
        parts.append(f'<circle cx="{x_of(day)}" cy="{y}" r="{radius}" fill="{color}"/>')
    latest_color = _SPARKLINE_RED if _reading_outside_range(latest) else _SPARKLINE_GRAY
    parts.append(
        f'<text x="{x_of(last)}" y="{y_of(values[-1]) - 9}" font-size="12" font-weight="700"'
        f' fill="{latest_color}" text-anchor="end">{_sparkline_fmt(values[-1])}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _grey(text: str) -> str:
    return f'<span style="color:{_OLDER_ROW_COLOR}">{text}</span>' if text else text


def render_lab_results_report(
    person_id: str,
    trend_rows: list[dict[str, Any]],
    display_name: str | None = None,
    *,
    evidence_index_link: str | None = None,
    loinc_classes: Mapping[str, LoincAttributes] | None = None,
) -> str:
    """One lab report: per section, the newest draw first
    and older rows grey, with collapsed trend charts and exact readings."""
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "lab_results",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    name = display_name or person_id
    newest_draw = max(str(r["readings"][-1]["date"])[:10] for r in trend_rows)
    lines = [
        f"---\n{yaml_block}\n---\n",
        f"# Lab Results — {name}\n",
        f"[[{person_id}|← {name}]]\n",
        f"_Newest draw: {newest_draw}. Its results lead each section; older results are grey._"
        f" See [[Records/{person_id}/Reports/flagged-for-discussion|flagged for discussion]]"
        " for what to raise with a clinician.\n",
    ]
    for section, rows in _group_lab_rows(trend_rows, loinc_classes):
        ordered = sorted(
            rows,
            key=lambda r: (str(r["readings"][-1]["date"])[:10] != newest_draw, *_severity_order(r)),
        )
        lines += [
            f"## {section}\n",
            "| Test | Latest | Date | Range | Flag | Previous | Trend |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in ordered:
            reading = row["readings"][-1]
            observed = str(reading["date"])[:10]
            value = _display_reading_value(reading)
            unit = reading.get("unit") or ""
            cells = [
                _markdown_cell(row["test_name"]),
                _markdown_cell(f"{value} {unit}".strip()),
                observed,
            ]
            if observed != newest_draw:
                cells = [_grey(cell) for cell in cells]
            flag = _reading_flag(reading)
            cells += [
                _format_reference_range(reading.get("ref_low"), reading.get("ref_high")),
                f"**{flag}**" if flag else "",
                _previous_numeric_value(row),
                _trend_symbol(row["trend"]),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        charts = [
            chart
            for chart in (_render_lab_trend_chart(row) for row in sorted(rows, key=_severity_order))
            if chart
        ]
        if charts:
            lines.append(f"> [!example]- Trend charts ({len(charts)})")
            lines += [f"> {chart}" for chart in charts]
            lines.append("")
        history = [row for row in rows if len(row["readings"]) > 1]
        if history:
            lines.append("> [!info]- All readings")
            for row in sorted(history, key=lambda r: r["test_name"].casefold()):
                lines += [
                    f"> **{_markdown_cell(row['test_name'])}**",
                    ">",
                    "> | Date | Value | Range | Flag |",
                    "> | --- | --- | --- | --- |",
                ]
                for reading in row["readings"]:
                    value = _display_reading_value(reading)
                    unit = reading.get("unit") or ""
                    shown = _markdown_cell(f"{value} {unit}".strip())
                    bounds = _format_reference_range(
                        reading.get("ref_low"), reading.get("ref_high")
                    )
                    lines.append(
                        f"> | {str(reading['date'])[:10]} | {shown} | {bounds}"
                        f" | {_reading_flag(reading)} |"
                    )
                lines.append(">")
            lines.append("")
    if evidence_index_link:
        lines += ["## Source Evidence\n", f"Source-scoped notes: {evidence_index_link}\n"]
    return "\n".join(lines)


def write_lab_results_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> tuple[Path | None, int]:
    """Write lab-results.md, or remove it when the person has no labs (ADR-031)."""
    trend_rows = build_lab_trends_from_db(conn, person_id)
    if not trend_rows:
        _remove_inapplicable_report(vault_root, person_id, "lab-results.md")
        return None, 0
    display_name = load_person_display_name(vault_root, person_id)
    reports_dir = vault_root / "Records" / person_id / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "lab-results.md"
    report_path.write_text(
        render_lab_results_report(
            person_id,
            trend_rows,
            display_name,
            evidence_index_link=_lab_source_evidence_index_link(vault_root, person_id),
            loinc_classes=lab_report_loinc_classes(vault_root),
        ),
        encoding="utf-8",
    )
    write_category_index_note(
        reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return report_path, len(trend_rows)


def lab_range_distance(value: float | None, low: float | None, high: float | None) -> float:
    """Distance beyond the reference range as a fraction of its width; 0 inside.

    One-sided ranges measure relative to the stated bound. This is the single
    severity measure every caregiver and doctor surface ranks by.
    """
    if value is None:
        return 0.0
    if low is not None and high is not None:
        width = (high - low) or abs(high) or 1.0
        if value > high:
            return (value - high) / width
        if value < low:
            return (low - value) / width
        return 0.0
    if high is not None and value > high:
        return (value - high) / (abs(high) or 1.0)
    if low is not None and value < low:
        return (low - value) / (abs(low) or 1.0)
    return 0.0


def _flag_severity(row: dict[str, Any]) -> float:
    latest = row["readings"][-1]
    try:
        value = float(latest.get("value"))
    except (TypeError, ValueError):
        return 0.0
    return lab_range_distance(value, latest.get("ref_low"), latest.get("ref_high"))


def _severity_order(row: dict[str, Any]) -> tuple[float, str]:
    return (-_flag_severity(row), str(row["test_name"]).casefold())


def _filter_flagged_lab_rows(
    trend_rows: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Return (out_of_range, trending, normalized, newly_out_of_range, other_out_of_range).

    Applies ADR-030 exclusions:
    - Exclusion 1 (Trending only): include in-range tests approaching a bound.
    - Exclusion 2 (Trending only): omit tests not in the 2 most recent panel dates.
    """
    latest_panel_date = _most_recent_panel_date(trend_rows)
    recent_dates = _two_most_recent_panel_dates(trend_rows)

    out_of_range = sorted(
        (
            r
            for r in trend_rows
            if r["in_range_latest"] is False
            and not _is_out_of_range_result_stale(r, latest_panel_date)
        ),
        key=_severity_order,
    )
    newly_out_of_range: list[dict[str, Any]] = []
    other_out_of_range: list[dict[str, Any]] = []
    for r in out_of_range:
        (
            newly_out_of_range if _previous_numeric_reading_was_in_range(r) else other_out_of_range
        ).append(r)
    trending = sorted(
        (
            r
            for r in trend_rows
            if r["trend"] in ("up", "down")
            and not _is_stale_result(r, recent_dates)
            and _is_approaching_reference_threshold(r)
        ),
        key=lambda r: r["test_name"].casefold(),
    )
    trending_keys = {str(row["test_name"]).casefold() for row in trending}
    normalized = sorted(
        (
            r
            for r in trend_rows
            if str(r["test_name"]).casefold() not in trending_keys
            and r["in_range_latest"] is True
            and _previous_numeric_reading_was_out_of_range(r)
            and not _is_out_of_range_result_stale(r, latest_panel_date)
        ),
        key=lambda r: r["test_name"].casefold(),
    )
    return out_of_range, trending, normalized, newly_out_of_range, other_out_of_range


def build_lab_observations_for_advisory(trend_rows: list[dict[str, Any]]) -> list[str]:
    """Build lab observation bullet lines for the health advisory.

    Returns an empty list when nothing is flagged — callers should omit the
    Lab Observations section entirely rather than showing a "no findings" stub.
    """
    out_of_range, trending, normalized, _, _ = _filter_flagged_lab_rows(trend_rows)
    # Out-of-range first (furthest from range first), then trending, then normalized.
    flagged = [*out_of_range, *trending, *normalized]
    lines: list[str] = []
    seen: set[str] = set()
    for row in flagged:
        key = str(row["test_name"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        latest = row["readings"][-1]
        value_str = (
            str(latest["value"])
            if not latest.get("unit")
            else f"{latest['value']} {latest['unit']}"
        )
        ref_range = _format_reference_range(latest.get("ref_low"), latest.get("ref_high"))
        trend = _trend_symbol(row["trend"])
        in_range = _in_range_label(row["in_range_latest"])
        previous = _previous_numeric_value(row)
        observed_date = str(latest.get("date") or "")[:10]
        previous_suffix = f" · **Previous:** {previous}" if previous and previous != "—" else ""
        date_suffix = f" · **Date:** {observed_date}" if observed_date else ""
        lines.append(
            f"- **{row['test_name']}** — **Latest:** {value_str}"
            f" · **Reference Range:** {ref_range}"
            f" · **Trend:** {trend} · **In range:** {in_range}{previous_suffix}{date_suffix}"
        )
    return lines


def build_lab_discussion_prompts(trend_rows: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    """Discussion prompts for the most out-of-range current results."""
    out_of_range, _, _, _, _ = _filter_flagged_lab_rows(trend_rows)
    prompts: list[str] = []
    for row in out_of_range[:limit]:
        latest = row["readings"][-1]
        unit = f" {latest['unit']}" if latest.get("unit") else ""
        ref_range = _format_reference_range(latest.get("ref_low"), latest.get("ref_high"))
        previous = _previous_numeric_value(row)
        previous_text = f", previously {previous}" if previous and previous != "—" else ""
        prompts.append(
            f"- Ask about {row['test_name']}: {latest['value']}{unit} is outside the "
            f"reference range ({ref_range}{previous_text})."
        )
    return prompts


def render_flagged_for_discussion_report(
    person_id: str,
    trend_rows: list[dict[str, Any]],
    display_name: str | None = None,
    coronary_calcium_findings: list[str] | None = None,
) -> str:
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "flagged_for_discussion",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    name = display_name or person_id
    calcium_findings = coronary_calcium_findings or []

    out_of_range, trending, normalized, newly_out_of_range, other_out_of_range = (
        _filter_flagged_lab_rows(trend_rows)
    )

    lines: list[str] = [
        f"---\n{yaml_block}\n---\n",
        f"# Flagged for Discussion — {name}\n",
        f"[[{person_id}|← {name}]]\n",
        "_Out-of-range and trending results from recent lab data."
        f" See [[Records/{person_id}/Reports/lab-results|lab results]] for all tests._\n",
        "## Out of Range\n",
    ]
    if newly_out_of_range:
        lines.append("### Newly Out of Range\n")
        lines.extend(_render_lab_visual_section(newly_out_of_range))
        if other_out_of_range:
            lines.append("### Other Out of Range\n")
            lines.extend(_render_lab_visual_section(other_out_of_range))
    elif out_of_range:
        lines.extend(_render_lab_visual_section(out_of_range))
    else:
        lines.append("_No out-of-range lab results in recent panels._\n")

    lines.append("## Trending\n")
    if trending:
        lines.extend(_render_lab_visual_section(trending))
    else:
        lines.append("_No notably trending lab results in recent panels._\n")

    if normalized:
        lines.append("## Recently Normalized\n")
        lines.extend(_render_lab_visual_section(normalized))

    if calcium_findings:
        lines.append("## Coronary Calcium\n")
        lines.extend(calcium_findings)
        lines.append("")

    return "\n".join(lines)


def _remove_inapplicable_report(vault_root: Path, person_id: str, filename: str) -> None:
    """Drop a report whose inputs are absent (ADR-031 section 4) and refresh the index."""
    reports_dir = vault_root / "Records" / person_id / "Reports"
    (reports_dir / filename).unlink(missing_ok=True)
    if reports_dir.is_dir():
        write_category_index_note(
            reports_dir,
            person_id,
            "Reports",
            note_label="report",
            display_name=load_person_display_name(vault_root, person_id),
        )


def write_flagged_for_discussion_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> tuple[Path | None, int]:
    trend_rows = build_lab_trends_from_db(conn, person_id)
    coronary_calcium_findings = _extract_coronary_calcium_findings(vault_root, person_id)
    if not trend_rows and not coronary_calcium_findings:
        _remove_inapplicable_report(vault_root, person_id, "flagged-for-discussion.md")
        return None, 0
    display_name = load_person_display_name(vault_root, person_id)

    person_reports_dir = vault_root / "Records" / person_id / "Reports"
    person_reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = person_reports_dir / "flagged-for-discussion.md"
    report_path.write_text(
        render_flagged_for_discussion_report(
            person_id,
            trend_rows,
            display_name,
            coronary_calcium_findings=coronary_calcium_findings,
        ),
        encoding="utf-8",
    )
    write_category_index_note(
        person_reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return report_path, len(trend_rows)


def write_lab_conflicts_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> tuple[Path, int]:
    conflict_rows = build_lab_conflicts_from_db(conn, person_id)

    person_reports_dir = vault_root / "Records" / person_id / "Reports"
    person_reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = person_reports_dir / "lab-conflicts.md"
    report_path.write_text(render_lab_conflicts_report(person_id, conflict_rows), encoding="utf-8")
    display_name = load_person_display_name(vault_root, person_id)
    write_category_index_note(
        person_reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    return report_path, len(conflict_rows)


def build_latest_lab_results_from_notes(note_paths: list[Path]) -> list[dict[str, Any]]:
    """Return the latest value per test, ordered by its contributing panel.

    Tests are ordered by the panel (note) that produced their surviving
    value, in that panel's own table order, with panels ordered by their
    earliest draw time. This preserves the source lab report's grouping
    instead of an arbitrary "whichever panel first mentioned this test name"
    historical order.
    """
    latest_by_test: dict[str, dict[str, Any]] = {}
    winner_note_by_test: dict[str, Path] = {}
    rows_by_panel: dict[Path, list[dict[str, Any]]] = {}

    for note_path in sorted(note_paths):
        try:
            labs = load_labs_from_note(note_path)
        except LabSyncError:
            continue
        if not labs:
            continue
        rows_by_panel[note_path] = labs
        for lab in labs:
            test_name = str(lab["test_name"])
            current = latest_by_test.get(test_name)
            if current is None or _observed_at_sort_key(
                str(lab["observed_at"])
            ) >= _observed_at_sort_key(str(current["observed_at"])):
                latest_by_test[test_name] = lab
                winner_note_by_test[test_name] = note_path

    def _panel_sort_key(note_path: Path) -> datetime:
        return min(
            _observed_at_sort_key(str(row["observed_at"])) for row in rows_by_panel[note_path]
        )

    ordered_rows: list[dict[str, Any]] = []
    for note_path in sorted(rows_by_panel, key=_panel_sort_key):
        for lab in rows_by_panel[note_path]:
            test_name = str(lab["test_name"])
            if winner_note_by_test[test_name] == note_path and latest_by_test[test_name] is lab:
                ordered_rows.append(lab)

    return ordered_rows


def build_latest_lab_results_from_db(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    """Return the deduplicated latest canonical reading for each test."""
    latest_rows: list[dict[str, Any]] = []
    for trend in build_lab_trends_from_db(conn, person_id):
        reading = trend["readings"][-1]
        latest_rows.append(
            {
                "test_name": trend["test_name"],
                "loinc_code": trend["loinc_code"],
                "value": reading["value"],
                "value_text": reading["value_text"],
                "unit": reading["unit"],
                "reference_range_low": reading["ref_low"],
                "reference_range_high": reading["ref_high"],
                "observed_at": reading["date"],
                "source_path": reading["source_path"],
            }
        )
    return sorted(
        latest_rows,
        key=lambda row: (
            _observed_at_sort_key(str(row["observed_at"])),
            str(row["test_name"]).casefold(),
        ),
    )


def _lab_source_evidence_index_link(vault_root: Path, person_id: str) -> str | None:
    labs_dir = vault_root / "Records" / person_id / "Labs"
    for note_path in labs_dir.glob("*.md"):
        if note_path.name.startswith("_"):
            continue
        try:
            front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if front_matter.get("projection_role") != "source_evidence":
            continue
        return f"[[Records/{person_id}/Labs/_index|Lab Source Evidence]]"
    return None


def _extract_user_notes_section(existing_content: str) -> str:
    """Return the text that follows the last ``## Notes`` heading in *existing_content*.

    Only the ``## Notes`` section is preserved on re-import; any content above it
    (front matter, ``## Results``) is replaced by the fresh import.  If there is no
    ``## Notes`` heading the caller receives an empty string and the default rendered
    note is used verbatim.
    """
    # Find the last occurrence so nested sub-headings don't confuse the split.
    marker = "\n## Notes"
    pos = existing_content.rfind(marker)
    if pos == -1:
        return ""
    return existing_content[pos:]


def write_lab_panel_note(
    note_path: Path,
    labs: list[dict[str, Any]],
    source_file_name: str | None = None,
    source_path: str | None = None,
    projection_role: str | None = None,
) -> str:
    """Write a lab panel note idempotently.

    * If the note does not exist yet, render it fresh and write it.
    * If the note already exists, regenerate only the front matter and
      ``## Results`` table while preserving the user-editable ``## Notes``
      section verbatim.

    Returns the final content that was written.
    """
    fresh = render_lab_panel_note(
        labs,
        source_file_name=source_file_name,
        source_path=source_path,
        projection_role=projection_role,
    )

    if not note_path.exists():
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    existing = note_path.read_text(encoding="utf-8")
    user_notes = _extract_user_notes_section(existing)

    if not user_notes:
        # No Notes section found — safe to overwrite entirely.
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    # Replace everything up to (but not including) ## Notes with the fresh header.
    fresh_marker = "\n## Notes"
    fresh_pos = fresh.rfind(fresh_marker)
    if fresh_pos == -1:
        # Unexpected: rendered note has no Notes section; just overwrite.
        note_path.write_text(fresh, encoding="utf-8")
        return fresh

    updated = fresh[:fresh_pos] + user_notes
    note_path.write_text(updated, encoding="utf-8")
    return updated


def renormalize_lab_panel_note(note_path: Path) -> bool:
    """Surgically fill reviewed-LOINC unit cells and drop non-result rows.

    Edits only the ``unit`` cell of empty-unit rows whose ``loinc_code`` is in
    the reviewed ``CANONICAL_UNIT_BY_LOINC`` map, and removes non-result
    narrative rows (ADR-035). Every other cell is left byte-for-byte
    untouched, so pre-existing non-canonical units on unrelated rows are neither
    re-validated nor rewritten. Returns True when the note changed. Idempotent.
    """
    try:
        content = note_path.read_text(encoding="utf-8")
    except OSError:
        return False

    lines = content.splitlines(keepends=True)
    in_table = False
    past_alignment = False
    updated_lines: list[str] = []
    note_changed = False

    for line in lines:
        line_content = line.rstrip("\r\n")
        line_ending = line[len(line_content) :]
        stripped = line_content.strip()

        if not in_table and stripped.startswith("| test_name ") and stripped.endswith("|"):
            in_table = True
            past_alignment = False
            updated_lines.append(line)
            continue

        if in_table and not past_alignment:
            if stripped.startswith("| ---"):
                past_alignment = True
            updated_lines.append(line)
            continue

        if in_table and past_alignment:
            if not stripped.startswith("|"):
                in_table = False
                updated_lines.append(line)
                continue
            parts = line_content.split("|")
            # layout: ['', ' test_name ', ' loinc_code ', ' value ', ' unit ', ...]
            if len(parts) >= 9 and is_non_result_lab_row(parts[1].strip(), parts[2].strip()):
                note_changed = True  # drop the row
                continue
            new_content, changed = _fill_unit_in_table_line(line_content)
            if changed:
                updated_lines.append(new_content + line_ending)
                note_changed = True
            else:
                updated_lines.append(line)
            continue

        updated_lines.append(line)

    if note_changed:
        note_path.write_text("".join(updated_lines), encoding="utf-8")
    return note_changed


def _fill_unit_in_table_line(line: str) -> tuple[str, bool]:
    """Return (updated_line, changed) with the unit cell filled if blank.

    Mirrors ``fill_unit``: only fills when the unit cell is empty and the
    loinc_code cell is in the reviewed ``CANONICAL_UNIT_BY_LOINC`` map. Existing
    units are never overwritten. See ADR-035.
    """
    parts = line.split("|")
    # layout: ['', ' test_name ', ' loinc_code ', ' value ', ' unit ', ..., '']
    if len(parts) < 9:
        return line, False
    if parts[4].strip():  # unit already present
        return line, False
    unit = CANONICAL_UNIT_BY_LOINC.get(parts[2].strip())
    if not unit:
        return line, False
    parts[4] = f" {unit} "
    return "|".join(parts), True


def backfill_lab_units_in_db(conn: sqlite3.Connection) -> int:
    """Fill empty units for reviewed LOINCs in ``lab_results``.

    Mirrors ``fill_unit``: only rows with an empty unit and a LOINC in the
    reviewed ``CANONICAL_UNIT_BY_LOINC`` map are updated. Returns the number of
    rows updated. Idempotent. See ADR-035.
    """
    updated = 0
    for loinc, unit in CANONICAL_UNIT_BY_LOINC.items():
        cursor = conn.execute(
            "UPDATE lab_results SET unit = ? "
            "WHERE loinc_code = ? AND (unit IS NULL OR TRIM(unit) = '')",
            (unit, loinc),
        )
        updated += cursor.rowcount
    return updated


def backfill_reviewed_inferred_loinc_codes(
    conn: sqlite3.Connection,
) -> list[InferredLoincRepair]:
    """Correct the closed set of reviewed, previously inferred LOINC mistakes.

    Source-provided codes are never selected. Each changed row is audited, and
    the returned repair records can be applied surgically to matching vault
    panel rows with ``repair_inferred_lab_loinc_codes_in_notes``.
    """
    rows = conn.execute(
        """
        SELECT id, person_id, source_path, test_name, observed_at, unit, loinc_code
        FROM lab_results
        WHERE loinc_code_origin = ?
        ORDER BY id
        """,
        (LOINC_CODE_ORIGIN_INFERRED,),
    ).fetchall()
    repairs: list[InferredLoincRepair] = []
    for row_id, person_id, source_path, test_name, observed_at, unit, old_code in rows:
        normalized_unit = normalize_unit(unit)
        repair_key = (
            _compact_test_name_key(str(test_name or "")),
            normalized_unit.casefold(),
            str(old_code or "").strip(),
        )
        if repair_key not in _REVIEWED_INFERRED_LOINC_REPAIRS:
            continue
        new_code = _REVIEWED_INFERRED_LOINC_REPAIRS[repair_key]
        # An empty reviewed target clears a wrong code entirely (no analyte).
        cursor = conn.execute(
            """
            UPDATE lab_results
            SET loinc_code = ?, loinc_code_origin = ?
            WHERE id = ? AND loinc_code = ? AND loinc_code_origin = ?
            """,
            (
                new_code or None,
                LOINC_CODE_ORIGIN_INFERRED if new_code else None,
                row_id,
                old_code,
                LOINC_CODE_ORIGIN_INFERRED,
            ),
        )
        if cursor.rowcount != 1:
            continue
        repair = InferredLoincRepair(
            row_id=str(row_id),
            person_id=str(person_id),
            source_path=str(source_path or ""),
            test_name=str(test_name),
            observed_at=str(observed_at),
            unit=normalized_unit,
            old_code=str(old_code),
            new_code=new_code,
        )
        repairs.append(repair)
        log_audit_event(
            conn,
            event_type=AUDIT_LAB_INFERRED_LOINC_REPAIRED,
            subject_type="lab_result",
            subject_id=repair.row_id,
            source_path=repair.source_path,
            status=STATE_PROCESSED,
            message=(
                f"repaired inferred LOINC for {repair.test_name!r} at "
                f"{repair.observed_at}: {repair.old_code} -> {repair.new_code}"
            ),
        )
    return repairs


# ADR-035: individually reviewed source-provided LOINC overrides. Each entry
# names an exact row id -- never a test-name/unit pattern -- so this can never
# silently grow into a general override lane for source-provided data the way
# _REVIEWED_INFERRED_LOINC_REPAIRS does for inferred data. Row ids are derived
# from local records, so the reviewed entries live in an untracked local file,
# one JSON object per line: row_id, expected_old_code, new_code, reason (an
# authoritative citation showing the source's own code is wrong for that row).
REVIEWED_SOURCE_LOINC_OVERRIDES_FILENAME = "reviewed_source_loinc_overrides.jsonl"


@dataclass(frozen=True)
class SourceLoincOverride:
    row_id: str
    expected_old_code: str
    new_code: str
    reason: str


def load_reviewed_source_loinc_overrides(path: Path) -> list[SourceLoincOverride]:
    """Load reviewed row-scoped overrides; a missing file means none."""
    if not path.is_file():
        return []
    overrides: list[SourceLoincOverride] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        try:
            override = SourceLoincOverride(
                row_id=str(entry["row_id"]),
                expected_old_code=str(entry["expected_old_code"]),
                new_code=str(entry["new_code"]),
                reason=str(entry["reason"]),
            )
        except KeyError as exc:
            raise ValueError(f"{path}:{line_number}: missing field {exc}") from exc
        if not (override.row_id and override.expected_old_code and override.new_code):
            raise ValueError(f"{path}:{line_number}: row_id and codes must be non-empty")
        overrides.append(override)
    return overrides


def apply_reviewed_source_provided_loinc_overrides(
    conn: sqlite3.Connection,
    overrides: list[SourceLoincOverride],
) -> list[InferredLoincRepair]:
    """Apply reviewed, row-scoped ADR-035 source-provided LOINC overrides.

    Each override names an exact row id and only applies if that row still
    carries the expected old code and ``source_provided`` origin. Unlike
    ``backfill_reviewed_inferred_loinc_codes``, this is never a pattern-match
    lane and must never be extended with a test-name/unit key -- new entries
    require their own individually reviewed row id. Idempotent: a row already
    at its new code is skipped.
    """
    repairs: list[InferredLoincRepair] = []
    for override in overrides:
        row_id, expected_old_code, new_code = (
            override.row_id,
            override.expected_old_code,
            override.new_code,
        )
        row = conn.execute(
            """
            SELECT person_id, source_path, test_name, observed_at, unit, loinc_code
            FROM lab_results
            WHERE id = ? AND loinc_code_origin = ?
            """,
            (row_id, LOINC_CODE_ORIGIN_SOURCE_PROVIDED),
        ).fetchone()
        if row is None:
            continue
        person_id, source_path, test_name, observed_at, unit, current_code = row
        if str(current_code or "") != expected_old_code:
            continue
        cursor = conn.execute(
            """
            UPDATE lab_results
            SET loinc_code = ?
            WHERE id = ? AND loinc_code = ? AND loinc_code_origin = ?
            """,
            (new_code, row_id, expected_old_code, LOINC_CODE_ORIGIN_SOURCE_PROVIDED),
        )
        if cursor.rowcount != 1:
            continue
        repair = InferredLoincRepair(
            row_id=str(row_id),
            person_id=str(person_id),
            source_path=str(source_path or ""),
            test_name=str(test_name),
            observed_at=str(observed_at),
            unit=normalize_unit(str(unit or "")),
            old_code=expected_old_code,
            new_code=new_code,
        )
        repairs.append(repair)
        log_audit_event(
            conn,
            event_type=AUDIT_LAB_SOURCE_PROVIDED_LOINC_OVERRIDDEN,
            subject_type="lab_result",
            subject_id=repair.row_id,
            source_path=repair.source_path,
            status=STATE_PROCESSED,
            message=(
                f"ADR-035 reviewed source-provided override for {repair.test_name!r} "
                f"at {repair.observed_at}: {repair.old_code} -> {repair.new_code} "
                f"({override.reason})"
            ),
        )
    return repairs


def purge_non_result_lab_rows_in_db(conn: sqlite3.Connection) -> int:
    """Delete non-result narrative rows (lot numbers, control flags) from the DB.

    Mirrors ``is_non_result_lab_row``: only rows with no LOINC whose test name
    matches a non-result marker are deleted. Returns the number of rows deleted.
    Idempotent. See ADR-035.
    """
    candidates = conn.execute(
        "SELECT id, test_name, loinc_code FROM lab_results "
        "WHERE loinc_code IS NULL OR TRIM(loinc_code) = ''"
    ).fetchall()
    ids = [row[0] for row in candidates if is_non_result_lab_row(row[1], row[2])]
    for row_id in ids:
        conn.execute("DELETE FROM lab_results WHERE id = ?", (row_id,))
    return len(ids)


# Reports index grouped by audience (ADR-031): (group, [(stem, title,
# reason it may be absent)]). Absent reports are named with the reason instead of
# silently missing, so a caregiver can tell "not applicable" from "lost".
_REPORTS_INDEX_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Needs attention",
        (
            ("pending-review", "Pending review", "not generated yet"),
            ("flagged-for-discussion", "Flagged for discussion", "no lab results"),
            ("medication-safety-alerts", "Medication safety alerts", "no medication data"),
            ("health-advisory", "Health advisory", "not generated yet"),
        ),
    ),
    (
        "Records",
        (
            ("medical-history-summary", "Medical history summary", "no history recorded"),
            ("current-medications", "Current medications", "no current medications"),
            ("lab-results", "Lab results", "no lab results"),
            ("immunizations", "Immunizations", "no immunizations recorded"),
        ),
    ),
    ("Genetics", (("genetics", "Genetics", "no DNA imported"),)),
    (
        "Data review",
        (
            ("lab-conflicts", "Lab conflicts", "not generated yet"),
            ("medication-conflicts", "Medication conflicts", "not generated yet"),
        ),
    ),
)


def _render_grouped_reports_index(reports_dir: Path) -> str:
    present = {path.stem for path in reports_dir.glob("*.md") if not path.name.startswith("_")}
    packets = reports_dir / "doctor-visit-packets" / "_index.md"
    sections: list[str] = [
        "## For the doctor\n\n"
        + (
            "- [[doctor-visit-packets/_index|Doctor visit packets]]"
            if packets.exists()
            else "- _Run `fm-prepare-doctor-visit` to create a printable visit packet._"
        )
    ]
    listed: set[str] = set()
    for group, entries in _REPORTS_INDEX_GROUPS:
        lines = []
        for stem, title, reason in entries:
            listed.add(stem)
            if stem in present:
                lines.append(f"- [[{stem}|{title}]]")
            else:
                lines.append(f"- {title} — _not applicable: {reason}_")
        sections.append(f"## {group}\n\n" + "\n".join(lines))
    others = sorted(present - listed)
    if others:
        sections.append("## Other reports\n\n" + "\n".join(f"- [[{stem}]]" for stem in others))
    return "\n\n".join(sections) + "\n"


def write_category_index_note(
    category_dir: Path,
    person_id: str,
    category: str,
    *,
    note_label: str | None = None,
    display_name: str | None = None,
) -> Path:
    """Create or fully regenerate the ``_index.md`` hub for a person's category directory.

    Lists every non-template note as a wiki link.  The index is regenerated from
    scratch on every call so it stays current without requiring delta tracking.

    The generated note back-links to the person profile via a human-readable display name
    when provided, otherwise falls back to person_id.
    """
    note_paths = sorted(path for path in category_dir.glob("*.md") if not path.name.startswith("_"))
    child_index_paths = sorted(
        path / "_index.md"
        for path in category_dir.iterdir()
        if path.is_dir() and (path / "_index.md").exists()
    )

    link_lines: list[str] = []
    for path in note_paths:
        superseded_by = None
        projection_role = None
        try:
            front_matter = parse_front_matter(path.read_text(encoding="utf-8"))
            superseded_by = front_matter.get("superseded_by")
            projection_role = front_matter.get("projection_role")
        except (OSError, ValueError):
            pass
        if superseded_by:
            link_lines.append(f"- [[{path.stem}]] — superseded by [[{superseded_by}]]")
        elif category in {"Labs", "Medications"} and projection_role == "source_evidence":
            link_lines.append(f"- [[{path.stem}]] — source evidence")
        else:
            link_lines.append(f"- [[{path.stem}]]")
    for path in child_index_paths:
        link_lines.append(f"- [[{path.parent.name}/_index|{path.parent.name}]]")

    label = note_label or category.lower()
    links_block = "\n".join(link_lines) if link_lines else f"_No {label} notes yet._"

    name = display_name or person_id
    person_backlink = f"← {name}" if person_id == "_person_id" else f"[[{person_id}|← {name}]]"
    record_type = f"{category.lower()}_index"
    if category == "Labs":
        # Link the canonical report only when it exists (inputs-only, ADR-031).
        has_results = (category_dir.parent / "Reports" / "lab-results.md").exists()
        current_view = (
            f"See [[Records/{person_id}/Reports/lab-results|Lab Results]] "
            "for the deduplicated canonical view."
            if has_results
            else "No current lab results are recorded."
        )
        projection_notice = (
            "> [!info] Canonical current view\n"
            f"> These notes preserve source-reported evidence. {current_view}\n\n"
        )
    elif category == "Medications":
        projection_notice = (
            "> [!info] Canonical current view\n"
            "> These notes preserve source-reported evidence. See "
            f"[[Records/{person_id}/Reports/current-medications|Current Medications]] "
            "for the deduplicated canonical view.\n\n"
        )
    else:
        projection_notice = ""
    body = (
        _render_grouped_reports_index(category_dir)
        if category == "Reports"
        else f"## {category} Notes\n\n{links_block}\n"
    )
    content = (
        f"---\nperson_id: {person_id}\nrecord_type: {record_type}\n---\n\n"
        f"# {category} — {name}\n\n"
        f"{person_backlink}\n\n"
        f"{projection_notice}"
        f"{body}"
    )

    index_path = category_dir / "_index.md"
    index_path.write_text(content, encoding="utf-8")
    return index_path


def write_labs_index_note(
    person_labs_dir: Path, person_id: str, *, display_name: str | None = None
) -> Path:
    """Create or fully regenerate the ``_index.md`` hub for a person's Labs directory.

    Delegates to :func:`write_category_index_note` with ``category="Labs"``.
    """
    return write_category_index_note(
        person_labs_dir, person_id, "Labs", note_label="lab", display_name=display_name
    )


def iter_date_panel_note_paths(person_labs_dir: Path, date_str: str) -> list[Path]:
    """Return sorted ``{date_str}-*-panel.md`` note paths, excluding index notes."""
    return [
        note_path
        for note_path in sorted(person_labs_dir.glob(f"{date_str}-*-panel.md"))
        if not note_path.name.startswith("_")
    ]


def find_overlapping_panel_note(
    person_labs_dir: Path,
    date_str: str,
    test_names: frozenset[str],
    *,
    prefer_source_less: bool = False,
) -> Path | None:
    """Return the path of a lab_panel note that shares at least one test with
    *test_names* on *date_str*, or ``None`` if no such note exists.

    Only matches ``{date_str}-*-panel.md`` files.  Index notes and unreadable
    files are skipped.
    """
    if not test_names:
        return None
    matching_paths: list[Path] = []
    for note_path in iter_date_panel_note_paths(person_labs_dir, date_str):
        try:
            existing_rows = load_labs_from_note(note_path)
        except (LabSyncError, OSError):
            continue
        existing_names = frozenset(str(r.get("test_name") or "") for r in existing_rows)
        if existing_names & test_names:
            matching_paths.append(note_path)

    if not matching_paths:
        return None
    if not prefer_source_less:
        return matching_paths[0]

    for note_path in matching_paths:
        try:
            front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not front_matter.get("source_path"):
            return note_path
    return matching_paths[0]


_LabValueKey = tuple[str, str] | float


def _lab_fact_value_key(value: Any, value_text: Any) -> _LabValueKey:
    """Return a hashable identity for a reading's result.

    Numeric readings key on the rounded float so a note's rendered value and
    the persisted column compare equal despite formatting. Non-numeric
    readings fall back to a bounded, case-folded prefix of ``value_text``.

    A hand-edited note can render a non-numeric string in the ``value``
    column, so the numeric branch degrades to the text branch rather than
    raising: this is a read-only diagnostic and one malformed row must not
    abort the whole scan.
    """
    if value is not None:
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return ("text", str(value).strip().casefold()[:24])
    return ("text", str(value_text or "").strip().casefold()[:24])


def _current_lab_fact_sources(
    conn: sqlite3.Connection, person_id: str
) -> tuple[
    dict[tuple[str, str], set[str]],
    set[str],
    dict[tuple[str, _LabValueKey], set[tuple[str, str]]],
]:
    """Return exact (name, date) coverage, all current source_paths, and a
    value-anchored (name, value) index used for adjacent-date coverage.

    Keyed by ``normalize_test_name`` output, not the raw persisted
    ``test_name`` column: ``load_labs_from_note`` normalizes every row it
    reads from a note's rendered table (e.g. ``"RDW"`` + unit ``"%"`` ->
    ``"RDW-CV"``), but the DB's own ``test_name`` column is not always
    normalized to the same alias. Comparing a normalized note test_name
    against a raw DB test_name would report a perfectly current, matching
    fact as uncovered.

    The third index exists because the same draw can carry different calendar
    dates across sources: a provider PDF states a local collection date while
    a C-CDA export timestamps the same reading in UTC, so one lands just past
    midnight on the next day. Matching on the date string alone reported those
    facts as uncovered even though the identical value is canonical under the
    other source. See ``_covering_source_paths`` for how the two are combined.
    """
    current_fact_sources: dict[tuple[str, str], set[str]] = {}
    current_source_paths: set[str] = set()
    by_value: dict[tuple[str, _LabValueKey], set[tuple[str, str]]] = {}
    for test_name, loinc_code, unit, observed_at, source_path, value, value_text in conn.execute(
        "SELECT test_name, loinc_code, unit, observed_at, source_path, value, value_text "
        "FROM lab_results WHERE person_id = ? AND source_path IS NOT NULL",
        (person_id,),
    ):
        path = str(source_path)
        current_source_paths.add(path)
        date_str = str(observed_at or "")[:10]
        normalized_name = normalize_test_name(
            str(test_name or ""), str(loinc_code or ""), str(unit or "")
        )
        current_fact_sources.setdefault((normalized_name, date_str), set()).add(path)
        value_key = _lab_fact_value_key(value, value_text)
        by_value.setdefault((normalized_name, value_key), set()).add((date_str, path))
    return current_fact_sources, current_source_paths, by_value


def _dates_within_one_day(left: str, right: str) -> bool:
    try:
        delta = date.fromisoformat(left[:10]) - date.fromisoformat(right[:10])
    except ValueError:
        return False
    return abs(delta.days) <= 1


def _covering_source_paths(
    test_name: str,
    value_key: _LabValueKey,
    date_str: str,
    current_fact_sources: dict[tuple[str, str], set[str]],
    by_value: dict[tuple[str, _LabValueKey], set[tuple[str, str]]],
) -> set[str]:
    """Return the current source_paths covering one rendered fact.

    Exact same-date coverage wins. Otherwise a reading is covered only when
    the *identical* value for the same analyte is canonical within one day.
    Anchoring on the value is what makes the date tolerance safe: two genuine
    draws a day apart keep distinct values and stay uncovered, while the same
    draw recorded under two source timezones converges.
    """
    exact = current_fact_sources.get((test_name, date_str))
    if exact:
        return set(exact)
    return {
        path
        for candidate_date, path in by_value.get((test_name, value_key), ())
        if _dates_within_one_day(candidate_date, date_str)
    }


def _lab_panel_note_paths(person_labs_dir: Path) -> list[Path]:
    return sorted(p for p in person_labs_dir.glob("*-panel.md") if not p.name.startswith("_"))


def _lab_panel_note_by_source_path(note_paths: list[Path]) -> dict[str, Path]:
    note_by_source_path: dict[str, Path] = {}
    for note_path in note_paths:
        try:
            front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if front_matter.get("record_type") != "lab_panel":
            continue
        source_path = str(front_matter.get("source_path") or "")
        if source_path:
            note_by_source_path[source_path] = note_path
    return note_by_source_path


@dataclass(frozen=True)
class LabPanelNoteCoverage:
    """Read-only diagnostic: how much of a stale-source lab_panel
    note's content is covered by current DB rows, and where.

    Categories (see ``category``):
    - ``single_replacement``: every fact covered, all under one current note
      -- the only category ``find_superseded_lab_panel_notes``/
      ``reconcile_superseded_lab_panel_notes`` auto-labels.
    - ``multiple_replacements``: every fact covered, but scattered across
      more than one current note -- no single unambiguous pointer exists.
    - ``covered_no_note``: every fact covered by current DB rows, but at
      least one covering source_path maps to no known current note (e.g. a
      C-CDA document whose lab facts never got their own lab_panel note
      written because of a merge_lab_panel_rows completeness tie). This
      includes the *mixed* case where some covering sources do have notes: a
      note only qualifies for ``single_replacement``/``multiple_replacements``
      when *every* covering source maps to a note. Silently ignoring an unmapped source and
      pointing only at the mapped remainder would mislabel a large note whose
      bulk traces to an unlinkable source as fully explained by an unrelated
      small note that covers only a few of its facts.
    - ``partial``: some facts covered, some not -- likely a merged note that
      absorbed several documents over past reprocessing.
    - ``no_replacement``: none of its facts have current coverage --
      a quarantine/deletion case, not a converged-duplicate case.

    This never writes anything; it exists to make a reconciliation backlog
    tractable for human triage instead of opening each note cold.
    """

    note_path: Path
    person_id: str
    source_path: str
    date_str: str
    total_facts: int
    covered_facts: int
    covering_notes: tuple[Path, ...]
    uncovered_test_names: tuple[str, ...]

    @property
    def category(self) -> str:
        if self.covered_facts == 0:
            return "no_replacement"
        if self.covered_facts < self.total_facts:
            return "partial"
        if len(self.covering_notes) == 0:
            return "covered_no_note"
        if len(self.covering_notes) == 1:
            return "single_replacement"
        return "multiple_replacements"


def assess_lab_panel_note_coverage(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> list[LabPanelNoteCoverage]:
    """Diagnose every stale-source lab_panel note for one person.

    A note is a candidate when its source has zero current DB rows and it
    isn't already labeled ``superseded_by`` (already resolved, so excluded
    from both this diagnostic and auto-reconciliation). Read-only: this is
    the sole fact-coverage computation: `find_superseded_lab_panel_notes`
    filters its output to the ``single_replacement`` category rather than
    recomputing coverage itself.
    """
    person_labs_dir = vault_root / "Records" / person_id / "Labs"
    if not person_labs_dir.is_dir():
        return []

    current_fact_sources, current_source_paths, by_value = _current_lab_fact_sources(
        conn, person_id
    )
    note_paths = _lab_panel_note_paths(person_labs_dir)
    note_by_source_path = _lab_panel_note_by_source_path(note_paths)

    coverage: list[LabPanelNoteCoverage] = []
    for note_path in note_paths:
        try:
            content = note_path.read_text(encoding="utf-8")
            front_matter = parse_front_matter(content)
        except (OSError, ValueError):
            continue
        if front_matter.get("record_type") != "lab_panel":
            continue
        if front_matter.get("superseded_by"):
            continue  # already resolved
        source_path = str(front_matter.get("source_path") or "")
        observed_at = str(front_matter.get("observed_at") or "")
        date_str = observed_at[:10]
        if not source_path or not date_str or source_path in current_source_paths:
            continue  # still current itself; nothing to diagnose
        try:
            rows = load_labs_from_note(note_path)
        except (LabSyncError, OSError):
            continue
        facts_by_name: dict[str, set[_LabValueKey]] = {}
        for row in rows:
            name = str(row.get("test_name") or "")
            if not name:
                continue
            facts_by_name.setdefault(name, set()).add(
                _lab_fact_value_key(row.get("value"), row.get("value_text"))
            )
        test_names = sorted(facts_by_name)
        if not test_names:
            continue

        covering_source_paths: set[str] = set()
        uncovered: list[str] = []
        for test_name in test_names:
            sources: set[str] = set()
            for value_key in facts_by_name[test_name]:
                sources |= _covering_source_paths(
                    test_name, value_key, date_str, current_fact_sources, by_value
                )
            if sources:
                covering_source_paths |= sources
            else:
                uncovered.append(test_name)

        # Every covering source must resolve to a note, or the covering set is
        # incomplete: silently dropping unmapped sources previously let a note
        # whose bulk of facts trace to an unlinkable source (e.g. covered_no_note's
        # own DOC0001.XML case) look like it was fully explained by whichever
        # small subset of facts happened to also have a linkable note.
        all_covering_sources_mapped = covering_source_paths <= set(note_by_source_path)
        covering_notes = (
            tuple(
                sorted(
                    {
                        note_by_source_path[src]
                        for src in covering_source_paths
                        if note_by_source_path[src] != note_path
                    }
                )
            )
            if all_covering_sources_mapped
            else ()
        )
        coverage.append(
            LabPanelNoteCoverage(
                note_path=note_path,
                person_id=person_id,
                source_path=source_path,
                date_str=date_str,
                total_facts=len(test_names),
                covered_facts=len(test_names) - len(uncovered),
                covering_notes=covering_notes,
                uncovered_test_names=tuple(uncovered),
            )
        )
    return coverage


@dataclass(frozen=True)
class SupersededLabPanelNote:
    """A lab_panel note whose source's facts are now covered by another,
    current note for the same person and date (REQ-006 no-orphan-islands)."""

    note_path: Path
    replacement_path: Path
    person_id: str
    source_path: str


def find_superseded_lab_panel_notes(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> list[SupersededLabPanelNote]:
    """Find lab_panel notes every one of whose facts is now covered by
    current DB rows under a single other, current note (REQ-006
    no-orphan-islands).

    A note qualifies only when *every* fact (test_name) it contains has a
    current DB row for the same person and date, and all of those current
    rows trace back to exactly one other note. Partial coverage -- some
    facts current, some not, or facts scattered across more than one current
    note -- is left alone for human judgement rather than auto-labeled: a
    single ``superseded_by`` pointer would misrepresent a note that is only
    partly redundant (e.g. a merged panel note that absorbed several
    documents over past reprocessing runs, only some of which have since
    moved elsewhere). A note whose source has no current rows and none of
    its facts are covered elsewhere is also left alone: that is a
    quarantine/deletion case, not a converged-duplicate case. Already-marked
    notes (existing ``superseded_by`` front matter) are skipped by
    ``assess_lab_panel_note_coverage``.
    """
    return [
        SupersededLabPanelNote(
            note_path=entry.note_path,
            replacement_path=entry.covering_notes[0],
            person_id=entry.person_id,
            source_path=entry.source_path,
        )
        for entry in assess_lab_panel_note_coverage(conn, vault_root, person_id)
        if entry.category == "single_replacement"
    ]


def mark_lab_panel_note_superseded(
    note_path: Path,
    replacement_path: Path,
) -> bool:
    """Add ``superseded_by`` front matter pointing at *replacement_path*.

    Never deletes the note (CORE.md invariant: no destructive merges) --
    only labels it and lets the vault index render the pointer, so the
    reconciliation is reversible and audited. Idempotent: returns ``False``
    without writing if the note already carries the same value.
    """
    content = note_path.read_text(encoding="utf-8")
    front_matter = parse_front_matter(content)
    replacement_stem = replacement_path.stem
    if front_matter.get("superseded_by") == replacement_stem:
        return False
    end_marker_index = content.find("\n---\n", 4)
    body = content[end_marker_index + 5 :]
    front_matter["superseded_by"] = replacement_stem
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    note_path.write_text(f"---\n{yaml_block}\n---\n{body}", encoding="utf-8")
    return True


def reconcile_superseded_lab_panel_notes(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
) -> list[SupersededLabPanelNote]:
    """Detect and label superseded lab_panel notes for one person (REQ-006).

    Combines ``find_superseded_lab_panel_notes`` and
    ``mark_lab_panel_note_superseded``, auditing each note actually changed.
    Idempotent: already-marked notes are skipped by the finder, so a repeat
    call returns an empty list. Caller is responsible for regenerating the
    person's Labs index afterward so the superseded annotation renders.
    """
    superseded = find_superseded_lab_panel_notes(conn, vault_root, person_id)
    applied: list[SupersededLabPanelNote] = []
    for entry in superseded:
        if not mark_lab_panel_note_superseded(entry.note_path, entry.replacement_path):
            continue
        applied.append(entry)
        log_audit_event(
            conn,
            event_type=AUDIT_LAB_PANEL_NOTE_MARKED_SUPERSEDED,
            subject_type="lab_result",
            subject_id=str(entry.note_path.relative_to(vault_root)),
            source_path=entry.source_path,
            status=STATE_PROCESSED,
            message=(
                f"{entry.note_path.relative_to(vault_root)} superseded by "
                f"{entry.replacement_path.relative_to(vault_root)}: source has no "
                "current DB rows, facts covered by the replacement note"
            ),
        )
    return applied


def merge_lab_panel_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return *existing_rows* + *new_rows* with duplicates coalesced.

    Dedup key: (test_name, observed_at, value, value_text). When the same test appears
    with a different value both rows are kept so the conflict is visible for
    human review (ADR-006: no silent conflict discard). When an exact duplicate
    arrives with more complete metadata (for example a retained-source reprocess
    adds a missing ``loinc_code``), the more complete row replaces the older one
    in-place so note order is preserved while data quality improves. On an exact
    completeness tie, an incoming ``source_provided`` row replaces an existing
    row lacking that origin (note-loaded existing rows never carry
    ``loinc_code_origin`` at all, since notes don't render it) -- otherwise a
    newer, more authoritative source's LOINC code could silently lose to an
    older, unlabeled one reporting the identical value.

    A second pass collapses precision-degraded duplicates: the same
    (test_name, value) reported once with a full timestamp and once with a
    date-only ``observed_at`` for the same calendar date (e.g. a CCDA
    encounter summary and a cumulative summary both reporting the same
    result, one with time-of-day and units and one without). These represent
    the same fact at different precision, not a conflict, so only the more
    complete row is kept. See ADR-006.
    """
    result: list[dict[str, Any]] = []
    seen_indices: dict[tuple[str, str, float | None, str], int] = {}

    for row in [*existing_rows, *new_rows]:
        key = (
            str(row.get("test_name") or ""),
            str(row.get("observed_at") or ""),
            row.get("value"),
            _value_text_identity(row.get("value"), row.get("value_text")),
        )
        existing_index = seen_indices.get(key)
        if existing_index is None:
            seen_indices[key] = len(result)
            result.append(row)
            continue
        current = result[existing_index]
        new_score = _lab_completeness_score(row)
        current_score = _lab_completeness_score(current)
        if new_score > current_score or (
            new_score == current_score
            and row.get("loinc_code_origin") == LOINC_CODE_ORIGIN_SOURCE_PROVIDED
            and current.get("loinc_code_origin") != LOINC_CODE_ORIGIN_SOURCE_PROVIDED
        ):
            result[existing_index] = row

    return _drop_precision_degraded_duplicates(result)


def _drop_precision_degraded_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop date-only rows that duplicate a same-day, same-value, full-timestamp row.

    Groups rows by (test_name, calendar date, value, value_text). Within a group that
    contains both a date-only ``observed_at`` (e.g. ``"2022-08-24"``) and a
    full-timestamp ``observed_at`` for the same date (e.g.
    ``"2022-08-24T04:33:00"``), the date-only rows are dropped: they record
    the same fact with less precision and (typically) no unit/loinc_code.
    Groups with only date-only rows, or only full-timestamp rows, are left
    untouched since those are genuinely distinct entries.
    """
    groups: dict[tuple[str, str, float | None, str], list[int]] = {}
    for index, row in enumerate(rows):
        observed_at = str(row.get("observed_at") or "")
        key = (
            str(row.get("test_name") or ""),
            observed_at[:10],
            row.get("value"),
            _value_text_identity(row.get("value"), row.get("value_text")),
        )
        groups.setdefault(key, []).append(index)

    drop: set[int] = set()
    for indices in groups.values():
        date_only = [i for i in indices if len(str(rows[i].get("observed_at") or "")) == 10]
        timestamped = [i for i in indices if len(str(rows[i].get("observed_at") or "")) > 10]
        if date_only and timestamped:
            drop.update(date_only)

    return [row for index, row in enumerate(rows) if index not in drop]


def log_ingestion_event(
    conn: sqlite3.Connection,
    source_type: str,
    source_path: str,
    source_checksum: str | None,
    status: ProcessingState | str,
    message: str | None = None,
) -> None:
    """Append a row to ``ingestion_log``."""
    import uuid as _uuid

    status = validate_processing_state(status)

    conn.execute(
        """
        INSERT INTO ingestion_log (id, source_type, source_path, source_checksum, status, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(_uuid.uuid4()),
            source_type,
            source_path,
            source_checksum,
            status,
            message,
        ),
    )


def source_file_checksum(path: Path) -> str:
    """Return a SHA-256 hex digest of *path*'s contents."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_labs_from_csv(path: Path, default_person_id: str | None = None) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise LabSyncError(f"CSV {path} is missing a header row")
            rows = [row for row in reader]
    except OSError as exc:
        raise LabSyncError(f"failed to read CSV {path}: {exc}") from exc

    labs: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=2):
        source_label = f"{path}:{row_index}"
        payload = lab_payload_from_mapping(row, source_label, default_person_id=default_person_id)
        payload["source_type"] = (
            payload["source_type"] if payload["source_type"] != "manual" else "csv"
        )
        labs.append(payload)

    return labs


def _fill_loinc_in_table_line(line: str, loinc_index: dict[str, str]) -> tuple[str, bool]:
    """Return (updated_line, changed) with loinc_code cell filled if it was blank.

    Expects a pipe-separated markdown table row with at least 7 data cells.
    The test_name is in cell index 0, loinc_code in cell index 1.
    Existing non-blank codes are never overwritten.
    """
    parts = line.split("|")
    # layout: ['', ' test_name ', ' loinc_code ', ..., ' observed_at ', '']
    if len(parts) < 9:
        return line, False
    test_name = parts[1].strip()
    if not test_name:
        return line, False
    if parts[2].strip():  # already has a code
        return line, False
    code = lookup_loinc_code(test_name, loinc_index)
    if not code:
        return line, False
    parts[2] = f" {code} "
    return "|".join(parts), True


def repair_lab_units(vault_root: Path) -> int:
    """Re-normalize every lab panel note under *vault_root* in place.

    Applies the reviewed ``fill_unit`` lane to empty-unit cells and drops
    non-result rows (ADR-035) via ``renormalize_lab_panel_note``.
    Returns the number of notes changed. Idempotent.
    """
    records_root = vault_root / "Records"
    if not records_root.is_dir():
        return 0

    notes_updated = 0
    for note_path in sorted(records_root.glob("*/Labs/*.md")):
        if note_path.name.startswith("_"):
            continue
        try:
            front_matter = parse_front_matter(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if front_matter.get("record_type") != "lab_panel":
            continue
        if renormalize_lab_panel_note(note_path):
            notes_updated += 1
    return notes_updated


def repair_lab_loinc_codes(
    vault_root: Path,
    loinc_index: dict[str, str],
) -> tuple[int, int]:
    """Fill blank loinc_code cells in all lab panel notes under vault_root.

    Reads every non-index ``*.md`` note under ``Records/*/Labs/``, finds rows in
    the Results table where the ``loinc_code`` cell is empty, looks up the code
    by ``test_name`` from *loinc_index*, and writes the note back only when at
    least one cell changed.  Existing non-blank codes are never overwritten.

    Returns ``(notes_updated, cells_filled)``.
    """
    if not loinc_index:
        return 0, 0

    records_root = vault_root / "Records"
    if not records_root.is_dir():
        return 0, 0

    notes_updated = 0
    cells_filled = 0

    for note_path in sorted(records_root.glob("*/Labs/*.md")):
        if note_path.name.startswith("_"):
            continue

        try:
            content = note_path.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            front_matter = parse_front_matter(content)
        except ValueError:
            continue

        if front_matter.get("record_type") != "lab_panel":
            continue

        lines = content.splitlines(keepends=True)
        in_table = False
        past_alignment = False
        updated_lines: list[str] = []
        note_changed = False

        for line in lines:
            line_content = line.rstrip("\r\n")
            line_ending = line[len(line_content) :]
            stripped = line_content.strip()

            if not in_table and stripped.startswith("| test_name ") and stripped.endswith("|"):
                in_table = True
                past_alignment = False
                updated_lines.append(line)
                continue

            if in_table and not past_alignment:
                if stripped.startswith("| ---"):
                    past_alignment = True
                updated_lines.append(line)
                continue

            if in_table and past_alignment:
                if not stripped.startswith("|"):
                    in_table = False
                    updated_lines.append(line)
                    continue
                new_content, changed = _fill_loinc_in_table_line(line_content, loinc_index)
                if changed:
                    updated_lines.append(new_content + line_ending)
                    cells_filled += 1
                    note_changed = True
                else:
                    updated_lines.append(line)
                continue

            updated_lines.append(line)

        if note_changed:
            note_path.write_text("".join(updated_lines), encoding="utf-8")
            notes_updated += 1

    return notes_updated, cells_filled


def repair_reviewed_inferred_loinc_codes_in_notes(
    vault_root: Path,
) -> tuple[int, int]:
    """Rewrite lab panel note cells still carrying a reviewed-wrong inferred code.

    ``repair_inferred_lab_loinc_codes_in_notes`` only reaches note rows that
    match a repaired DB row by retained-source identity or clinical identity.
    That leaves two gaps: a note whose own source lost
    cross-source deduplication (its fact is canonical under another source, on
    an adjacent calendar day), and a stale projection whose DB row already
    carries the correct source-provided code. Both kept rendering the wrong
    inferred code.

    This is a note-only sweep driven by the same
    ``_REVIEWED_INFERRED_LOINC_REPAIRS`` table as the DB lane, so the two can
    never disagree. It rewrites only a cell whose test name, unit, and current
    code all match a reviewed entry -- a source-provided code is never a key in
    that table, so authoritative codes cannot be touched. Returns
    ``(notes_updated, cells_updated)``.
    """
    notes_updated = 0
    cells_updated = 0
    for note_path in sorted(vault_root.rglob("*.md")):
        try:
            lines = note_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            continue
        note_changed = False
        for index, line in enumerate(lines):
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4:
                continue
            key = (
                _compact_test_name_key(cells[0]),
                normalize_unit(cells[3]).casefold(),
                cells[1],
            )
            if key not in _REVIEWED_INFERRED_LOINC_REPAIRS:
                continue
            new_code = _REVIEWED_INFERRED_LOINC_REPAIRS[key]
            lines[index] = line.replace(f"| {cells[1]} |", f"| {new_code} |", 1)
            cells_updated += 1
            note_changed = True
        if note_changed:
            note_path.write_text("".join(lines), encoding="utf-8")
            notes_updated += 1
    return notes_updated, cells_updated


def repair_inferred_lab_loinc_codes_in_notes(
    conn: sqlite3.Connection,
    vault_root: Path,
    repairs: list[InferredLoincRepair],
) -> tuple[int, int]:
    """Apply reviewed inferred-code repairs to matching vault panel rows.

    Prefer the full retained-source identity. Equivalent duplicate vault rows may
    use a sibling archive member or overlapping PDF path after DB deduplication;
    those rows are updated by clinical identity only when no current
    source-provided DB row shares that identity.
    """
    if not repairs:
        return 0, 0
    repair_by_key = {
        (
            repair.person_id,
            repair.source_path,
            repair.test_name,
            repair.observed_at,
            repair.unit.casefold(),
            repair.old_code,
        ): repair.new_code
        for repair in repairs
    }
    repair_by_identity = {
        (
            repair.person_id,
            repair.test_name,
            repair.observed_at,
            repair.unit.casefold(),
            repair.old_code,
        ): repair.new_code
        for repair in repairs
    }
    source_provided_identities = {
        (
            str(person_id),
            str(test_name),
            str(observed_at),
            normalize_unit(str(unit or "")).casefold(),
            str(loinc_code or ""),
        )
        for person_id, test_name, observed_at, unit, loinc_code in conn.execute(
            """
            SELECT person_id, test_name, observed_at, unit, loinc_code
            FROM lab_results
            WHERE loinc_code_origin = ?
            """,
            (LOINC_CODE_ORIGIN_SOURCE_PROVIDED,),
        )
    }
    records_root = vault_root / "Records"
    if not records_root.is_dir():
        return 0, 0

    notes_updated = 0
    cells_updated = 0
    for note_path in sorted(records_root.glob("*/Labs/*.md")):
        if note_path.name.startswith("_"):
            continue
        try:
            content = note_path.read_text(encoding="utf-8")
            front_matter = parse_front_matter(content)
        except (OSError, ValueError):
            continue
        if front_matter.get("record_type") != "lab_panel":
            continue
        person_id = str(front_matter.get("person_id") or "")
        source_path = str(front_matter.get("source_path") or "")

        note_changed = False
        updated_lines: list[str] = []
        for line in content.splitlines(keepends=True):
            line_content = line.rstrip("\r\n")
            line_ending = line[len(line_content) :]
            parts = line_content.split("|")
            if len(parts) < 9:
                updated_lines.append(line)
                continue
            test_name = parts[1].strip()
            old_code = parts[2].strip()
            observed_at = parts[7].strip()
            unit = normalize_unit(parts[4].strip()).casefold()
            # None means "no reviewed repair"; "" means "clear the wrong code".
            new_code = repair_by_key.get(
                (person_id, source_path, test_name, observed_at, unit, old_code)
            )
            identity = (person_id, test_name, observed_at, unit, old_code)
            if new_code is None and identity not in source_provided_identities:
                new_code = repair_by_identity.get(identity)
            if new_code is None:
                updated_lines.append(line)
                continue
            parts[2] = f" {new_code} "
            updated_lines.append("|".join(parts) + line_ending)
            note_changed = True
            cells_updated += 1
        if note_changed:
            note_path.write_text("".join(updated_lines), encoding="utf-8")
            notes_updated += 1
    return notes_updated, cells_updated
