"""PGx medication-gene coverage reporting from medications, CPIC, and DNA data."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast, get_args

from famed.findings_ledger import (
    FindingEvent,
    active_findings,
    mark_orphaned_findings_stale,
    record_finding,
    supersede_stale_variants,
)
from famed.reference_cache import (
    load_reviewed_rxnorm_cpic_aliases,
    lookup_reviewed_rxnorm_cpic_alias,
)

ReferenceState = Literal[
    "covered by CPIC reference",
    "no CPIC match",
    "CPIC reference missing",
]
GenotypeState = Literal[
    "genotype data present",
    "no local gene-linked genotype data",
    "not assessed",
]
InterpretationState = Literal[
    "phenotype not interpreted",
    "poor metabolizer",
    "intermediate metabolizer",
    "normal metabolizer",
    "rapid metabolizer",
    "ultrarapid metabolizer",
    "indeterminate",
    "not interpretable",
    "normal function",
    "decreased function",
    "poor function",
    "increased function",
    "possible decreased function",
]
ResultVocabulary = Literal["metabolizer", "function", "uninterpreted"]

_ALL_INTERPRETATION_STATES: tuple[InterpretationState, ...] = get_args(InterpretationState)


@dataclass(frozen=True)
class ActiveMedication:
    medication_name: str
    medication_type: str
    rxnorm_cui: str | None
    source_path: str | None


@dataclass(frozen=True)
class CpicCoverageMatch:
    gene: str
    drug: str
    evidence_levels: tuple[str, ...]
    phenotypes: tuple[str, ...]


@dataclass(frozen=True)
class CpicPhenotypeActivityRow:
    phenotype: str
    activity_score: str


@dataclass(frozen=True)
class PgxCoverageRow:
    medication_name: str
    medication_type: str
    rxnorm_cui: str | None
    medication_source_path: str | None
    cpic_drug: str | None
    gene: str | None
    evidence_levels: tuple[str, ...]
    phenotypes: tuple[str, ...]
    reference_state: ReferenceState
    genotype_state: GenotypeState
    interpretation_state: InterpretationState = "phenotype not interpreted"
    result_vocabulary: ResultVocabulary = "uninterpreted"
    gene_results: tuple[str, ...] = ()
    diplotype: str | None = None
    guideline_only: bool = False
    cpic_guideline_level: str | None = None
    defining_rsids_measured: int | None = None
    defining_rsids_total: int | None = None


def normalize_drug_name(value: str | None) -> str:
    """Normalize medication/drug names for exact local matching."""
    return " ".join(str(value or "").split()).casefold()


_PARENTHETICAL_DRUG_RE = re.compile(r"\(([^)]*)\)")

# Common brand-name → CPIC generic mappings for household medication lists.
# CPIC uses generic (INN) names only; portal medication lists frequently use brand names.
_CPIC_BRAND_SYNONYMS: dict[str, str] = {
    # Statins (SLCO1B1 / ABCG2 / CYP2C9)
    "lipitor": "atorvastatin",
    "crestor": "rosuvastatin",
    "zocor": "simvastatin",
    "pravachol": "pravastatin",
    "lescol": "fluvastatin",
    "lescol xl": "fluvastatin",
    "mevacor": "lovastatin",
    "altoprev": "lovastatin",
    "livalo": "pitavastatin",
    "zypitamag": "pitavastatin",
    # Antiplatelet (CYP2C19)
    "plavix": "clopidogrel",
    # Anticoagulant (CYP2C9 / VKORC1 / CYP4F2 — pair_view only)
    "coumadin": "warfarin",
    "jantoven": "warfarin",
    # PPIs (CYP2C19)
    "prilosec": "omeprazole",
    "zegerid": "omeprazole",
    "prevacid": "lansoprazole",
    "protonix": "pantoprazole",
    "dexilant": "dexlansoprazole",
    # SSRIs / SNRIs (CYP2D6 / CYP2C19)
    "zoloft": "sertraline",
    "paxil": "paroxetine",
    "paxil cr": "paroxetine",
    "pexeva": "paroxetine",
    "lexapro": "escitalopram",
    "celexa": "citalopram",
    "luvox": "fluvoxamine",
    "effexor": "venlafaxine",
    "effexor xr": "venlafaxine",
    "trintellix": "vortioxetine",
    "brintellix": "vortioxetine",
    # TCAs (CYP2D6 / CYP2C19)
    "elavil": "amitriptyline",
    "anafranil": "clomipramine",
    "norpramin": "desipramine",
    "sinequan": "doxepin",
    "silenor": "doxepin",
    "tofranil": "imipramine",
    "pamelor": "nortriptyline",
    # Opioids (CYP2D6)
    "ultram": "tramadol",
    "conzip": "tramadol",
    "vicodin": "hydrocodone",
    "norco": "hydrocodone",
    "lortab": "hydrocodone",
    # Anticonvulsants (CYP2C9 / HLA-B / HLA-A)
    "tegretol": "carbamazepine",
    "carbatrol": "carbamazepine",
    "epitol": "carbamazepine",
    "trileptal": "oxcarbazepine",
    "dilantin": "phenytoin",
    "phenytek": "phenytoin",
    "cerebyx": "fosphenytoin",
    # Immunosuppressants (CYP3A5 / TPMT / NUDT15)
    "prograf": "tacrolimus",
    "astagraf xl": "tacrolimus",
    "envarsus xr": "tacrolimus",
    "imuran": "azathioprine",
    "azasan": "azathioprine",
    "purinethol": "mercaptopurine",
    "purixan": "mercaptopurine",
    "tabloid": "thioguanine",
    # HIV (CYP2B6 / HLA-B)
    "ziagen": "abacavir",
    "sustiva": "efavirenz",
    "reyataz": "atazanavir",
    # Oncology (DPYD / TPMT / NUDT15)
    "xeloda": "capecitabine",
    "adrucil": "fluorouracil",
    "efudex": "fluorouracil",
    "adriamycin": "doxorubicin",
    # Other
    "zyloprim": "allopurinol",
    "strattera": "atomoxetine",
    "kalydeco": "ivacaftor",
    "zofran": "ondansetron",
    "nolvadex": "tamoxifen",
    "soltamox": "tamoxifen",
    "plaquenil": "hydroxychloroquine",
    "celebrex": "celecoxib",
    "lopressor": "metoprolol",
    "toprol xl": "metoprolol",
    "micronase": "glyburide",
    "diabeta": "glyburide",
    "glynase": "glyburide",
    "advil": "ibuprofen",
    "motrin": "ibuprofen",
    "elitek": "rasburicase",
    "krystexxa": "pegloticase",
}


def _candidate_cpic_drug_names(
    medication_name: str,
    reviewed_cpic_aliases: dict[str, str],
) -> tuple[str, ...]:
    """Return reviewed and display-derived names to try against CPIC drug rows."""
    candidates: list[str] = [medication_name]
    reviewed_alias = lookup_reviewed_rxnorm_cpic_alias(medication_name, reviewed_cpic_aliases)
    if reviewed_alias:
        candidates.append(reviewed_alias)
    brand_generic = _CPIC_BRAND_SYNONYMS.get(normalize_drug_name(medication_name))
    if brand_generic:
        candidates.append(brand_generic)
    for parenthetical in _PARENTHETICAL_DRUG_RE.findall(medication_name):
        candidates.append(parenthetical)
        parenthetical_alias = lookup_reviewed_rxnorm_cpic_alias(
            parenthetical, reviewed_cpic_aliases
        )
        if parenthetical_alias:
            candidates.append(parenthetical_alias)
        paren_brand = _CPIC_BRAND_SYNONYMS.get(normalize_drug_name(parenthetical))
        if paren_brand:
            candidates.append(paren_brand)
    return _compact_unique(candidates)


def load_active_medications(
    conn: sqlite3.Connection,
    person_id: str,
) -> list[ActiveMedication]:
    """Load the medications current-medications.md shows for one person.

    Reuses that report's query rather than re-deriving "active" from each row's
    status column, so stopped, aborted, completed, not-taken, and user-suppressed
    rows never reach PGx coverage, the wallet card, or their ledger findings.
    """
    from famed.medication_sync import query_current_medications

    rows = sorted(
        query_current_medications(conn, person_id),
        key=lambda row: (
            str(row["medication_type"] or "prescription"),
            str(row["medication_name"]).casefold(),
            str(row["source_path"] or ""),
        ),
    )
    return [
        ActiveMedication(
            medication_name=str(row["medication_name"]),
            medication_type=str(row["medication_type"] or "prescription"),
            rxnorm_cui=row["rxnorm_cui"],
            source_path=row["source_path"],
        )
        for row in rows
    ]


def _compact_unique(values: Iterable[str | None]) -> tuple[str, ...]:
    seen: set[str] = set()
    compacted: list[str] = []
    for raw in values:
        value = " ".join(str(raw or "").split())
        if not value or value in seen:
            continue
        seen.add(value)
        compacted.append(value)
    return tuple(compacted)


def load_cpic_matches_by_drug(ref_db_path: Path) -> dict[str, list[CpicCoverageMatch]]:
    """Load CPIC rows grouped by normalized exact drug name."""
    if not ref_db_path.is_file():
        return {}

    ref_conn = sqlite3.connect(ref_db_path)
    ref_conn.row_factory = sqlite3.Row
    try:
        rows = ref_conn.execute(
            """
            SELECT gene, drug, evidence_level, phenotype
            FROM cpic_guidelines
            ORDER BY drug COLLATE NOCASE, gene COLLATE NOCASE
            """
        ).fetchall()
    finally:
        ref_conn.close()

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        drug = " ".join(str(row["drug"] or "").split())
        gene = " ".join(str(row["gene"] or "").split())
        if not drug or not gene:
            continue
        key = (normalize_drug_name(drug), gene)
        group = grouped.setdefault(
            key,
            {
                "drug": drug,
                "gene": gene,
                "evidence_levels": [],
                "phenotypes": [],
            },
        )
        group["evidence_levels"].append(row["evidence_level"])
        group["phenotypes"].append(row["phenotype"])

    by_drug: dict[str, list[CpicCoverageMatch]] = {}
    for (drug_key, _gene), group in grouped.items():
        by_drug.setdefault(drug_key, []).append(
            CpicCoverageMatch(
                gene=group["gene"],
                drug=group["drug"],
                evidence_levels=_compact_unique(group["evidence_levels"]),
                phenotypes=_compact_unique(group["phenotypes"]),
            )
        )
    return by_drug


def load_cpic_pair_matches_by_drug(
    ref_db_path: Path,
) -> dict[str, list[CpicCoverageMatch]]:
    """Load CPIC pair_view A/B rows grouped by normalized drug name.

    Returns only drugs that lack recommendation_view rows (guideline-only coverage).
    Used as a fallback in build_pgx_coverage_rows for drugs like warfarin.
    """
    if not ref_db_path.is_file():
        return {}
    try:
        ref_conn = sqlite3.connect(ref_db_path)
        ref_conn.row_factory = sqlite3.Row
        rows = ref_conn.execute(
            "SELECT drug, gene, cpiclevel FROM cpic_pairs ORDER BY drug, gene"
        ).fetchall()
        ref_conn.close()
    except sqlite3.OperationalError:
        return {}

    by_drug: dict[str, list[CpicCoverageMatch]] = {}
    for row in rows:
        drug = " ".join(str(row["drug"] or "").split())
        gene = " ".join(str(row["gene"] or "").split())
        if not drug or not gene:
            continue
        drug_key = normalize_drug_name(drug)
        by_drug.setdefault(drug_key, []).append(
            CpicCoverageMatch(
                gene=gene,
                drug=drug,
                evidence_levels=(str(row["cpiclevel"]),) if row["cpiclevel"] else (),
                phenotypes=(),
            )
        )
    return by_drug


def _genes_with_local_genotype_signal(
    conn: sqlite3.Connection,
    ref_db_path: Path,
    person_id: str,
) -> set[str]:
    genotype_rsids = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT rsid FROM genotypes WHERE person_id = ?",
            (person_id,),
        ).fetchall()
    }
    if not genotype_rsids:
        return set()
    if not ref_db_path.is_file():
        return set()

    ref_conn = sqlite3.connect(ref_db_path)
    ref_conn.row_factory = sqlite3.Row
    try:
        clinvar_rows = ref_conn.execute(
            """
            SELECT gene, rsid FROM clinvar_variants
            WHERE gene IS NOT NULL AND gene != ''
              AND rsid IS NOT NULL AND rsid != ''
            """
        ).fetchall()
    finally:
        ref_conn.close()

    genes_by_rsid: dict[str, set[str]] = {}
    for row in clinvar_rows:
        genes_by_rsid.setdefault(str(row["rsid"]), set()).add(str(row["gene"]))
    return {gene for rsid in genotype_rsids for gene in genes_by_rsid.get(rsid, set())}


def _gene_has_local_genotype_signal(
    genes_with_signal: set[str],
    gene: str,
) -> bool:
    return gene in genes_with_signal


def load_cpic_allele_definitions(
    ref_db_path: Path,
    gene: str,
) -> dict[str, list[tuple[str, str]]]:
    """Return {allele_name: [(rsid, variant_allele), ...]} for a gene."""
    if not ref_db_path.is_file():
        return {}
    ref_conn = sqlite3.connect(ref_db_path)
    ref_conn.row_factory = sqlite3.Row
    try:
        rows = ref_conn.execute(
            """
            SELECT allele_name, rsid, variant_allele
            FROM cpic_allele_definitions
            WHERE gene = ?
            ORDER BY allele_name, rsid
            """,
            (gene,),
        ).fetchall()
    finally:
        ref_conn.close()

    defs: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        defs.setdefault(str(row["allele_name"]), []).append(
            (str(row["rsid"]), str(row["variant_allele"]))
        )
    return defs


def load_allele_functional_status(
    ref_db_path: Path,
    gene: str,
) -> dict[str, str]:
    """Return {allele_name: clinical_functional_status} for a gene.

    *1 is always "normal function" by CPIC definition and is added as a
    default when not present in the reference DB (CPIC stores *1 without
    defining variant locations, so it never appears in cpic_allele_definitions).
    """
    if not ref_db_path.is_file():
        return {"*1": "normal function"}
    ref_conn = sqlite3.connect(ref_db_path)
    ref_conn.row_factory = sqlite3.Row
    try:
        rows = ref_conn.execute(
            """
            SELECT DISTINCT allele_name, clinical_functional_status
            FROM cpic_allele_definitions
            WHERE gene = ? AND clinical_functional_status IS NOT NULL
              AND clinical_functional_status != ''
            """,
            (gene,),
        ).fetchall()
    finally:
        ref_conn.close()
    result = {str(r["allele_name"]): str(r["clinical_functional_status"]) for r in rows}
    result.setdefault("*1", "normal function")
    return result


def load_cpic_phenotype_activity_rows(
    ref_db_path: Path,
    gene: str,
) -> list[CpicPhenotypeActivityRow]:
    """Return CPIC phenotype/activity-score rows for one gene.

    The local cache stores these rows per guideline recommendation. We dedupe them
    by phenotype/activity_score so phenotype validation can use the same CPIC-backed
    vocabulary without depending on one specific drug row.
    """
    if not ref_db_path.is_file():
        return []
    ref_conn = sqlite3.connect(ref_db_path)
    ref_conn.row_factory = sqlite3.Row
    try:
        rows = ref_conn.execute(
            """
            SELECT DISTINCT phenotype, activity_score
            FROM cpic_guidelines
            WHERE gene = ?
              AND phenotype IS NOT NULL AND phenotype != ''
              AND activity_score IS NOT NULL AND activity_score != ''
            ORDER BY phenotype, activity_score
            """,
            (gene,),
        ).fetchall()
    finally:
        ref_conn.close()
    return [
        CpicPhenotypeActivityRow(
            phenotype=str(row["phenotype"]),
            activity_score=str(row["activity_score"]),
        )
        for row in rows
    ]


def load_cpic_diplotype_result(
    ref_db_path: Path,
    gene: str,
    diplotype: str,
) -> tuple[str, ResultVocabulary] | None:
    if not ref_db_path.is_file():
        return None
    key = "/".join(sorted(diplotype.split("/"), key=str.casefold))
    ref_conn = sqlite3.connect(ref_db_path)
    try:
        row = ref_conn.execute(
            """
            SELECT gene_result, result_vocabulary
            FROM cpic_diplotypes
            WHERE gene = ? AND diplotype_key = ?
            """,
            (gene, key),
        ).fetchone()
    finally:
        ref_conn.close()
    if row is None:
        return None
    result = " ".join(str(row[0]).split()).casefold()
    if result not in get_args(InterpretationState):
        return None
    vocabulary = str(row[1])
    if vocabulary not in get_args(ResultVocabulary):
        return None
    return cast(InterpretationState, result), cast(ResultVocabulary, vocabulary)


_FUNCTIONAL_STATUS_ACTIVITY: dict[str, float] = {
    "normal function": 1.0,
    "increased function": 1.5,
    "decreased function": 0.5,
    "no function": 0.0,
    "uncertain function": 0.5,
    "unknown function": 0.5,
    "likely decreased function": 0.5,
    "likely normal function": 1.0,
    "likely increased function": 1.5,
    "likely no function": 0.0,
    "possible decreased function": 0.5,
    "possible normal function": 1.0,
    "indeterminate": 0.5,
}


def _activity_score(functional_status: str | None) -> float:
    if not functional_status:
        return 0.5
    normalized = " ".join(functional_status.lower().split())
    for key, score in _FUNCTIONAL_STATUS_ACTIVITY.items():
        if key in normalized:
            return score
    return 0.5


def _diplotype_activity_score(
    alleles: list[str],
    functional_status: dict[str, str],
) -> float | None:
    if not alleles:
        return None
    return sum(_activity_score(functional_status.get(allele, "")) for allele in alleles)


def _normalize_cpic_phenotype(value: str) -> str:
    return " ".join(value.split()).casefold()


def _activity_score_matches(total_activity: float, expression: str) -> bool:
    normalized = (
        expression.strip()
        .replace("–", "-")
        .replace("—", "-")
        .replace("≥", ">=")
        .replace("≤", "<=")
        .replace(" ", "")
    )
    if not normalized:
        return False
    if normalized.startswith(">="):
        return total_activity >= float(normalized[2:])
    if normalized.startswith("<="):
        return total_activity <= float(normalized[2:])
    if normalized.startswith(">"):
        return total_activity > float(normalized[1:])
    if normalized.startswith("<"):
        return total_activity < float(normalized[1:])
    if "-" in normalized:
        lower_text, upper_text = normalized.split("-", 1)
        return float(lower_text) <= total_activity <= float(upper_text)
    return total_activity == float(normalized)


_CPIC_ACTIVITY_SCORE_NOT_APPLICABLE = "n/a"


def validate_cpic_metabolizer_phenotype(
    ref_db_path: Path,
    gene: str,
    phenotype: InterpretationState,
    total_activity: float | None,
) -> bool:
    """Return whether a computed phenotype matches locally cached CPIC rows.

    Missing local translation rows are treated as a validation failure so the
    caller can conservatively suppress unvalidated phenotype output. Some
    CPIC genes (e.g. CYP2C19, CYP2B6) publish phenotype labels without a
    numeric activity-score range at all, using the literal sentinel "n/a" in
    place of a score for every row of that gene+phenotype. Only when none of
    the matching rows carry a usable numeric score does the phenotype-label
    match alone count as validation; a numeric row for the same phenotype is
    always checked first, so a real activity-score mismatch is never bypassed
    by an "n/a" row. Other malformed or non-numeric values are still treated
    as unvalidated rows.
    """
    if total_activity is None:
        return False
    rows = load_cpic_phenotype_activity_rows(ref_db_path, gene)
    if not rows:
        return False
    target = _normalize_cpic_phenotype(phenotype)
    matching_rows = [row for row in rows if _normalize_cpic_phenotype(row.phenotype) == target]
    if not matching_rows:
        return False
    for row in matching_rows:
        try:
            if _activity_score_matches(total_activity, row.activity_score):
                return True
        except ValueError:
            continue
    return all(
        row.activity_score.strip().casefold() == _CPIC_ACTIVITY_SCORE_NOT_APPLICABLE
        for row in matching_rows
    )


def _all_rsids(allele_defs: dict[str, list[tuple[str, str]]]) -> set[str]:
    return {rsid for variants in allele_defs.values() for rsid, _ in variants}


def call_star_alleles(
    gene: str,
    genotype_alleles: dict[str, tuple[str, str]],
    allele_defs: dict[str, list[tuple[str, str]]],
) -> list[str]:
    """Determine which CPIC star alleles a person carries for a gene.

    For each non-*1 allele, checks whether ALL defining rsIDs have at least
    one matching variant allele in the person's genotype.  Assigns *1 on the
    remaining chromosome(s) when fewer than two non-*1 copies are called.

    Returns a list of 0-2 allele names (e.g. ``["*1", "*2"]`` or ``["*1"]``
    when no non-*1 allele is detected).  Returns an empty list when no
    genotype signal exists for the gene.
    """
    if not allele_defs:
        return []

    called: list[str] = []
    variant_copy_count = 0

    for allele_name in sorted(allele_defs):
        if allele_name == "*1":
            continue
        variants = allele_defs[allele_name]
        if not variants:
            continue
        matches = 0
        for rsid, expected_allele in variants:
            alleles = genotype_alleles.get(rsid)
            if alleles is None:
                matches = 0
                break
            a1, a2 = alleles
            if a1 == expected_allele and a2 == expected_allele:
                matches = max(matches, 2)
            elif a1 == expected_allele or a2 == expected_allele:
                matches = max(matches, 1)
            else:
                matches = 0
                break
        if matches > 0:
            called.append(allele_name)
            variant_copy_count += min(matches, 2 - variant_copy_count)

    if variant_copy_count < 2 and called:
        called.append("*1")
    elif not called:
        if any(genotype_alleles.get(rsid) is not None for rsid in _all_rsids(allele_defs)):
            return ["*1", "*1"]  # homozygous wildtype diplotype
        return []

    return called


def assign_metabolizer_phenotype(
    alleles: list[str],
    functional_status: dict[str, str],
) -> InterpretationState:
    """Map a diplotype and allele functional statuses to a metabolizer phenotype.

    Uses CPIC activity scoring: sums activity scores of called alleles and
    maps to the standard metabolizer phenotype vocabulary.  Returns
    ``"not interpretable"`` when the allele list is empty.  *1 is treated as
    normal function (activity 1.0) by CPIC definition when no explicit
    functional status entry is present in the reference DB.
    """
    if not alleles:
        return "not interpretable"

    total_activity = 0.0
    for allele in alleles:
        status = functional_status.get(allele, "")
        total_activity += _activity_score(status)

    has_no_function = any(functional_status.get(a, "").lower() == "no function" for a in alleles)

    if has_no_function and total_activity <= 0.5:
        return "poor metabolizer"
    if total_activity >= 2.5:
        return "ultrarapid metabolizer"
    if total_activity > 1.0:
        return "normal metabolizer"
    if total_activity >= 0.5:
        return "intermediate metabolizer"
    return "indeterminate"


def assign_validated_metabolizer_phenotype(
    ref_db_path: Path,
    gene: str,
    alleles: list[str],
    functional_status: dict[str, str],
) -> InterpretationState:
    """Assign and locally validate a metabolizer phenotype against CPIC rows."""
    phenotype = assign_metabolizer_phenotype(alleles, functional_status)
    if phenotype in ("not interpretable", "indeterminate"):
        return phenotype
    total_activity = _diplotype_activity_score(alleles, functional_status)
    if validate_cpic_metabolizer_phenotype(ref_db_path, gene, phenotype, total_activity):
        return phenotype
    return "phenotype not interpreted"


def assign_cpic_gene_result(
    ref_db_path: Path,
    gene: str,
    alleles: list[str],
    functional_status: dict[str, str],
) -> tuple[InterpretationState, ResultVocabulary]:
    diplotype = _format_diplotype(alleles)
    if gene in {"SLCO1B1", "ABCG2"} and diplotype is not None:
        translated = load_cpic_diplotype_result(ref_db_path, gene, diplotype)
        if translated is not None:
            return cast(tuple[InterpretationState, ResultVocabulary], translated)
        return "phenotype not interpreted", "uninterpreted"
    phenotype = assign_validated_metabolizer_phenotype(
        ref_db_path, gene, alleles, functional_status
    )
    vocabulary: ResultVocabulary = (
        "metabolizer"
        if phenotype not in ("phenotype not interpreted", "not interpretable")
        else "uninterpreted"
    )
    return phenotype, vocabulary


def _format_diplotype(alleles: list[str]) -> str | None:
    if not alleles:
        return None
    if len(alleles) == 1:
        return f"{alleles[0]}/{alleles[0]}"
    return "/".join(alleles)


def _person_genotype_alleles(
    conn: sqlite3.Connection,
    person_id: str,
    rsids: set[str],
) -> dict[str, tuple[str, str]]:
    """Return {rsid: (allele1, allele2)} for the given rsids from a person's genotype."""
    if not rsids:
        return {}
    placeholders = ",".join("?" for _ in rsids)
    rows = conn.execute(
        f"""
        SELECT rsid, allele1, allele2 FROM genotypes
        WHERE person_id = ? AND rsid IN ({placeholders})
        """,
        (person_id, *list(rsids)),
    ).fetchall()
    return {str(r[0]): (str(r[1] or ""), str(r[2] or "")) for r in rows}


_PHENOTYPE_PRIORITY: tuple[InterpretationState, ...] = (
    "poor metabolizer",
    "intermediate metabolizer",
    "normal metabolizer",
    "rapid metabolizer",
    "ultrarapid metabolizer",
)


@dataclass(frozen=True)
class GeneResult:
    """One gene's call from local genotypes: result, diplotype, rsID coverage."""

    gene: str
    interpretation_state: InterpretationState
    result_vocabulary: ResultVocabulary
    diplotype: str | None
    defining_rsids_measured: int
    defining_rsids_total: int

    @property
    def interpreted(self) -> bool:
        return self.result_vocabulary != "uninterpreted"


def build_gene_results(
    conn: sqlite3.Connection, ref_db_path: Path, person_id: str
) -> dict[str, GeneResult]:
    """Call every CPIC gene the person's local genotypes touch (shared by coverage rows
    and the gene-centric Genetics report, ADR-031)."""
    results: dict[str, GeneResult] = {}
    if not ref_db_path.is_file():
        return results
    for gene in _genes_with_local_genotype_signal(conn, ref_db_path, person_id):
        allele_defs = load_cpic_allele_definitions(ref_db_path, gene)
        if not allele_defs:
            continue
        all_rsids = {rsid for variants in allele_defs.values() for rsid, _ in variants}
        genotype = _person_genotype_alleles(conn, person_id, all_rsids)
        state: InterpretationState = "not interpretable"
        vocabulary: ResultVocabulary = "uninterpreted"
        diplotype = None
        if genotype:
            alleles = call_star_alleles(gene, genotype, allele_defs)
            if alleles:
                diplotype = _format_diplotype(alleles)
                state, vocabulary = assign_cpic_gene_result(
                    ref_db_path, gene, alleles, load_allele_functional_status(ref_db_path, gene)
                )
            else:
                state = "phenotype not interpreted"
        results[gene] = GeneResult(
            gene, state, vocabulary, diplotype, len(genotype), len(all_rsids)
        )
    return results


def build_pgx_coverage_rows(
    conn: sqlite3.Connection,
    ref_db_path: Path,
    person_id: str,
) -> list[PgxCoverageRow]:
    """Build conservative CPIC medication-gene coverage rows for one person."""
    medications = load_active_medications(conn, person_id)
    cpic_by_drug = load_cpic_matches_by_drug(ref_db_path)
    cpic_pairs_by_drug = load_cpic_pair_matches_by_drug(ref_db_path)
    reviewed_cpic_aliases = load_reviewed_rxnorm_cpic_aliases(ref_db_path.parent)
    reference_missing = not ref_db_path.is_file()
    genes_with_signal = _genes_with_local_genotype_signal(conn, ref_db_path, person_id)

    gene_results = build_gene_results(conn, ref_db_path, person_id)
    result_by_gene: dict[str, tuple[InterpretationState, ResultVocabulary]] = {
        gene: (result.interpretation_state, result.result_vocabulary)
        for gene, result in gene_results.items()
    }
    diplotype_by_gene = {
        gene: result.diplotype for gene, result in gene_results.items() if result.diplotype
    }
    coverage_by_gene = {
        gene: (result.defining_rsids_measured, result.defining_rsids_total)
        for gene, result in gene_results.items()
    }

    unique_meds: dict[tuple[str, str, str | None], tuple[ActiveMedication, set[str]]] = {}
    for med in medications:
        canonical_name = _candidate_cpic_drug_names(med.medication_name, reviewed_cpic_aliases)[-1]
        key = (normalize_drug_name(canonical_name), med.medication_type, med.rxnorm_cui)
        if key not in unique_meds:
            unique_meds[key] = (med, set())
        if med.source_path:
            unique_meds[key][1].add(med.source_path)

    deduped_meds: dict[tuple[str, str], tuple[ActiveMedication, set[str]]] = {}
    for (_, med_type, _rxcui), (med, sources) in unique_meds.items():
        canonical_name = _candidate_cpic_drug_names(med.medication_name, reviewed_cpic_aliases)[-1]
        dkey = (normalize_drug_name(canonical_name), med_type)
        if dkey not in deduped_meds:
            deduped_meds[dkey] = (med, sources)
        else:
            deduped_meds[dkey][1].update(sources)
            existing = deduped_meds[dkey][0]
            if len(med.medication_name) < len(existing.medication_name):
                deduped_meds[dkey] = (med, deduped_meds[dkey][1])

    rows: list[PgxCoverageRow] = []

    for med, sources in deduped_meds.values():
        source_path = ", ".join(sorted(sources)) if sources else med.source_path
        if reference_missing:
            rows.append(
                PgxCoverageRow(
                    medication_name=med.medication_name,
                    medication_type=med.medication_type,
                    rxnorm_cui=med.rxnorm_cui,
                    medication_source_path=source_path,
                    cpic_drug=None,
                    gene=None,
                    evidence_levels=(),
                    phenotypes=(),
                    reference_state="CPIC reference missing",
                    genotype_state="not assessed",
                )
            )
            continue

        matches: list[CpicCoverageMatch] = []
        for candidate_drug_name in _candidate_cpic_drug_names(
            med.medication_name, reviewed_cpic_aliases
        ):
            matches = cpic_by_drug.get(normalize_drug_name(candidate_drug_name), [])
            if matches:
                break
        guideline_only = False
        if not matches:
            pair_matches: list[CpicCoverageMatch] = []
            for candidate_drug_name in _candidate_cpic_drug_names(
                med.medication_name, reviewed_cpic_aliases
            ):
                pair_matches = cpic_pairs_by_drug.get(normalize_drug_name(candidate_drug_name), [])
                if pair_matches:
                    break
            if not pair_matches:
                rows.append(
                    PgxCoverageRow(
                        medication_name=med.medication_name,
                        medication_type=med.medication_type,
                        rxnorm_cui=med.rxnorm_cui,
                        medication_source_path=source_path,
                        cpic_drug=None,
                        gene=None,
                        evidence_levels=(),
                        phenotypes=(),
                        reference_state="no CPIC match",
                        genotype_state="not assessed",
                    )
                )
                continue
            matches = pair_matches
            guideline_only = True

        for match in matches:
            genotype_state: GenotypeState = (
                "genotype data present"
                if _gene_has_local_genotype_signal(genes_with_signal, match.gene)
                else "no local gene-linked genotype data"
            )
            interpretation_state, result_vocabulary = (
                result_by_gene.get(match.gene, ("phenotype not interpreted", "uninterpreted"))
                if genotype_state == "genotype data present"
                else ("phenotype not interpreted", "uninterpreted")
            )
            if guideline_only:
                cpic_guideline_level: str | None = (
                    match.evidence_levels[0] if match.evidence_levels else None
                )
            else:
                pair_list = cpic_pairs_by_drug.get(normalize_drug_name(match.drug), [])
                cpic_guideline_level = next(
                    (
                        pm.evidence_levels[0]
                        for pm in pair_list
                        if pm.gene == match.gene and pm.evidence_levels
                    ),
                    None,
                )
            coverage = coverage_by_gene.get(match.gene)
            rows.append(
                PgxCoverageRow(
                    medication_name=med.medication_name,
                    medication_type=med.medication_type,
                    rxnorm_cui=med.rxnorm_cui,
                    medication_source_path=source_path,
                    cpic_drug=match.drug,
                    gene=match.gene,
                    evidence_levels=match.evidence_levels,
                    phenotypes=match.phenotypes,
                    reference_state="covered by CPIC reference",
                    genotype_state=genotype_state,
                    interpretation_state=interpretation_state,
                    result_vocabulary=result_vocabulary,
                    gene_results=(
                        (f"{match.gene}: {interpretation_state}",) if match.gene is not None else ()
                    ),
                    diplotype=diplotype_by_gene.get(match.gene),
                    guideline_only=guideline_only,
                    cpic_guideline_level=cpic_guideline_level,
                    defining_rsids_measured=coverage[0] if coverage else None,
                    defining_rsids_total=coverage[1] if coverage else None,
                )
            )

    return rows


def _is_recordable_pgx_finding(row: PgxCoverageRow) -> bool:
    return (
        row.reference_state == "covered by CPIC reference"
        and row.gene is not None
        and row.genotype_state == "genotype data present"
    )


def _pgx_finding_stable_key(person_id: str, row: PgxCoverageRow) -> str:
    """Stable identity for a medication-gene pair without interpretation state.

    Old-format finding IDs were computed with this exact formula. Used to detect
    and supersede stale ledger entries when interpretation state improves.
    """
    identity = "|".join(
        [
            person_id,
            normalize_drug_name(row.medication_name),
            row.medication_type,
            row.rxnorm_cui or "",
            normalize_drug_name(row.cpic_drug),
            row.gene or "",
            row.reference_state,
            row.genotype_state,
        ]
    )
    return "pgx-coverage-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _pgx_stale_finding_ids(person_id: str, row: PgxCoverageRow, new_finding_id: str) -> list[str]:
    """Return candidate stale finding IDs for this medication-gene pair.

    Covers both old-format entries (recorded before interpretation_state was
    part of the hash) and new-format entries for every other interpretation state,
    so that any change in computed phenotype supersedes the previous active finding.
    """
    candidates: list[str] = [_pgx_finding_stable_key(person_id, row)]
    for alt_state in _ALL_INTERPRETATION_STATES:
        if alt_state == row.interpretation_state:
            continue
        alt_id = _pgx_finding_id(person_id, replace(row, interpretation_state=alt_state))
        candidates.append(alt_id)
    return [c for c in candidates if c != new_finding_id]


def _pgx_finding_id(person_id: str, row: PgxCoverageRow) -> str:
    identity = "|".join(
        [
            person_id,
            normalize_drug_name(row.medication_name),
            row.medication_type,
            row.rxnorm_cui or "",
            normalize_drug_name(row.cpic_drug),
            row.gene or "",
            row.reference_state,
            row.genotype_state,
            row.interpretation_state,
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"pgx-coverage-{digest}"


def _pgx_finding_statement(row: PgxCoverageRow) -> str:
    gene_part = f" ({row.gene})" if row.gene else ""
    if row.interpretation_state == "phenotype not interpreted":
        return (
            f"{row.medication_name}: DNA data is available for the gene{gene_part} "
            f"that affects how this medication is processed, but the local CPIC "
            f"reference data is incomplete for a validated phenotype call. "
            f"Refresh the local CPIC allele/reference cache and regenerate the PGx "
            f"coverage report."
        )
    if row.interpretation_state == "not interpretable":
        return (
            f"{row.medication_name}: DNA data is available for the gene{gene_part}, "
            f"but the consumer DNA test did not include the specific markers needed "
            f"to predict how this medication is processed. "
            f"A clinical pharmacogenetics test would give a definitive result."
        )
    if row.result_vocabulary == "function":
        return (
            f"{row.medication_name}: DNA data indicates predicted "
            f"{row.interpretation_state}{gene_part}."
        )
    return (
        f"{row.medication_name}: DNA data indicates this person processes "
        f"this medication as {row.interpretation_state}{gene_part}."
    )


def _pgx_finding_confidence(_row: PgxCoverageRow) -> float:
    return 0.85


def _pgx_finding_source_paths(
    row: PgxCoverageRow,
    *,
    source_reports: list[str],
    report_relative_path: str,
    reference_db_path: Path,
) -> list[str]:
    source_paths = [report_relative_path, str(reference_db_path), *source_reports]
    if row.medication_source_path is not None:
        source_paths.append(row.medication_source_path)
    return list(_compact_unique(source_paths))


def record_pgx_coverage_findings(
    ledger_path: Path,
    *,
    person_id: str,
    rows: list[PgxCoverageRow],
    source_reports: list[str],
    report_relative_path: str,
    reference_db_path: Path,
) -> list[FindingEvent]:
    """Record conservative PGx readiness findings without duplicating active rows.

    When a row's interpretation state changes (e.g. after cpic_alleles are downloaded
    or allele definitions are updated), the previous active finding for the same
    medication-gene pair is superseded and a new finding is recorded with the
    current interpretation.  This covers both old-format ledger entries (recorded
    before interpretation_state was part of the finding ID) and any new-format
    entry whose computed phenotype has since changed.
    """
    active_ids = {finding.finding_id for finding in active_findings(ledger_path)}
    current_finding_ids: set[str] = set()
    recorded: list[FindingEvent] = []
    for row in rows:
        if not _is_recordable_pgx_finding(row):
            continue
        finding_id = _pgx_finding_id(person_id, row)
        current_finding_ids.add(finding_id)
        if finding_id in active_ids:
            continue
        recorded.append(
            record_finding(
                ledger_path,
                finding_id=finding_id,
                person_id=person_id,
                domain="pgx_coverage",
                statement=_pgx_finding_statement(row),
                evidence_tier="E1",
                confidence=_pgx_finding_confidence(row),
                evidence_check="verified",
                source_paths=_pgx_finding_source_paths(
                    row,
                    source_reports=source_reports,
                    report_relative_path=report_relative_path,
                    reference_db_path=reference_db_path,
                ),
            )
        )
        active_ids.add(finding_id)
        supersede_stale_variants(
            ledger_path,
            new_finding_id=finding_id,
            stale_candidate_ids=_pgx_stale_finding_ids(person_id, row, finding_id),
            reason="interpretation state updated: coverage re-recorded with current phenotype",
            active_ids=active_ids,
        )
    mark_orphaned_findings_stale(
        ledger_path,
        person_id=person_id,
        domain="pgx_coverage",
        current_finding_ids=current_finding_ids,
        reason="current PGx recomputation no longer produced this finding identity",
    )
    return recorded


def _person_has_genotypes(conn: sqlite3.Connection, person_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM genotypes WHERE person_id = ? LIMIT 1", (person_id,)).fetchone()
        is not None
    )
