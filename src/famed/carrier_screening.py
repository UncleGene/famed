"""Conservative carrier-screening report generation from DNA + ClinVar data."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CarrierState = Literal["carrier", "clear", "not callable from this chip"]
VariantState = Literal[
    "alternate allele observed",
    "measured reference/non-risk allele",
    "not measured",
    "no-call",
    "uninterpretable reference allele",
]

CARRIER_GENES: dict[str, str] = {
    "CFTR": "cystic fibrosis",
    "SMN1": "spinal muscular atrophy",
    "HEXA": "Tay-Sachs disease",
    "HBB": "sickle cell / beta-thalassemia",
    "GJB2": "hereditary hearing loss",
}


@dataclass(frozen=True)
class CarrierVariant:
    rsid: str
    gene: str
    condition: str | None
    significance: str | None
    review_status: str | None
    alternate_allele: str | None
    state: VariantState
    source_path: str | None = None


@dataclass(frozen=True)
class CarrierGeneResult:
    gene: str
    condition: str
    state: CarrierState
    reason: str
    variants: tuple[CarrierVariant, ...]


def _is_pathogenic_significance(significance: str | None) -> bool:
    if not significance:
        return False
    value = significance.casefold()
    excluded = ("benign", "conflicting", "uncertain", "not provided")
    return "pathogenic" in value and not any(term in value for term in excluded)


def _is_no_call(allele1: str | None, allele2: str | None) -> bool:
    return (allele1 or "") in {"", "-", "0"} and (allele2 or "") in {"", "-", "0"}


def _interpretable_alternate_allele(alternate_allele: str | None) -> str | None:
    alt = (alternate_allele or "").strip().upper()
    return alt if alt in {"A", "C", "G", "T"} else None


def _variant_state(
    genotype_rows: list[sqlite3.Row],
    alternate_allele: str | None,
) -> VariantState:
    if not genotype_rows:
        return "not measured"

    alt = _interpretable_alternate_allele(alternate_allele)
    if alt is None:
        return "uninterpretable reference allele"

    saw_interpretable_call = False
    for row in genotype_rows:
        allele1 = str(row["allele1"] or "").upper()
        allele2 = str(row["allele2"] or "").upper()
        if _is_no_call(allele1, allele2):
            continue
        saw_interpretable_call = True
        if allele1 == alt or allele2 == alt:
            return "alternate allele observed"

    if saw_interpretable_call:
        return "measured reference/non-risk allele"
    return "no-call"


def _variant_source_path(
    genotype_rows: list[sqlite3.Row],
    alternate_allele: str | None,
    state: VariantState,
) -> str | None:
    alt = _interpretable_alternate_allele(alternate_allele)
    for row in genotype_rows:
        allele1 = str(row["allele1"] or "").upper()
        allele2 = str(row["allele2"] or "").upper()
        if _is_no_call(allele1, allele2):
            continue
        if state == "alternate allele observed" and alt is not None:
            if allele1 == alt or allele2 == alt:
                return row["source_path"]
            continue
        return row["source_path"]
    return None


def _load_genotypes_by_rsid(
    conn: sqlite3.Connection,
    person_id: str,
) -> dict[str, list[sqlite3.Row]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rsid, allele1, allele2, source_path FROM genotypes WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    by_rsid: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_rsid.setdefault(str(row["rsid"]), []).append(row)
    return by_rsid


def _load_pathogenic_clinvar_rows(
    ref_db_path: Path,
    gene: str,
) -> list[sqlite3.Row]:
    if not ref_db_path.is_file():
        return []
    ref_conn = sqlite3.connect(ref_db_path)
    ref_conn.row_factory = sqlite3.Row
    try:
        rows = ref_conn.execute(
            "SELECT * FROM clinvar_variants WHERE gene = ? ORDER BY rsid",
            (gene,),
        ).fetchall()
    finally:
        ref_conn.close()
    return [row for row in rows if _is_pathogenic_significance(row["significance"])]


def build_carrier_screening_results(
    conn: sqlite3.Connection,
    ref_db_path: Path,
    person_id: str,
) -> list[CarrierGeneResult]:
    """Classify the five ACMG carrier-screening genes from current local data."""
    genotypes_by_rsid = _load_genotypes_by_rsid(conn, person_id)
    results: list[CarrierGeneResult] = []

    for gene, condition in CARRIER_GENES.items():
        clinvar_rows = _load_pathogenic_clinvar_rows(ref_db_path, gene)
        variants: list[CarrierVariant] = []
        for row in clinvar_rows:
            genotype_rows = genotypes_by_rsid.get(str(row["rsid"]), [])
            variant_state = _variant_state(genotype_rows, row["alternate_allele"])
            variants.append(
                CarrierVariant(
                    rsid=str(row["rsid"]),
                    gene=gene,
                    condition=row["condition"],
                    significance=row["significance"],
                    review_status=row["review_status"],
                    alternate_allele=row["alternate_allele"],
                    state=variant_state,
                    source_path=_variant_source_path(
                        genotype_rows, row["alternate_allele"], variant_state
                    ),
                )
            )

        if any(v.state == "alternate allele observed" for v in variants):
            state: CarrierState = "carrier"
            reason = (
                "At least one pathogenic or likely pathogenic ClinVar alternate allele "
                "was observed."
            )
        elif variants and all(v.state == "measured reference/non-risk allele" for v in variants):
            state = "clear"
            reason = (
                "All pathogenic or likely pathogenic ClinVar variants currently in the local "
                "reference DB for this gene were measured without the alternate allele."
            )
        else:
            state = "not callable from this chip"
            if variants:
                reason = (
                    "At least one pathogenic or likely pathogenic ClinVar variant for this gene "
                    "was not measured, no-called, or lacked interpretable allele metadata."
                )
            else:
                reason = (
                    "No pathogenic or likely pathogenic ClinVar variants for this gene are "
                    "available in the local reference DB."
                )

        results.append(
            CarrierGeneResult(
                gene=gene,
                condition=condition,
                state=state,
                reason=reason,
                variants=tuple(variants),
            )
        )
    return results


def _person_has_genotypes(conn: sqlite3.Connection, person_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM genotypes WHERE person_id = ? LIMIT 1", (person_id,)
    ).fetchone()
    return row is not None
