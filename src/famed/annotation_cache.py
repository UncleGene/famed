"""Genomics annotation reference database (famed-reference.db).

Provides CPIC pharmacogenomics guidelines, ClinVar variant pathogenicity,
ClinGen-origin GenCC gene-disease inheritance assertions, and a targeted gnomAD
frequency snapshot in a rebuildable, gitignored SQLite database separate from
user data in famed.db. Follows ADR-011, and ADR-023.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REFERENCE_DB_FILENAME = "famed-reference.db"

CPIC_RECOMMENDATIONS_URL = "https://api.cpicpgx.org/v1/recommendation_view"
CPIC_PAIRS_URL = "https://api.cpicpgx.org/v1/pair_view"
CPIC_DIPLOTYPES_URL = "https://api.cpicpgx.org/v1/diplotype"
CLINVAR_SUMMARY_URL = (
    "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz"
)
GENCC_SUBMISSIONS_URL = "https://thegencc.org/download/action/submissions-export-tsv?format=new"
GNOMAD_API_URL = "https://gnomad.broadinstitute.org/api"
GNOMAD_DATASET_ID = "gnomad_r4"
GNOMAD_RELEASE = "4.1.1"
GNOMAD_RELEASE_URL = "https://gnomad.broadinstitute.org/news/2026-03-gnomad-v4-1-1/"
GNOMAD_BATCH_SIZE = 100
ANNOTATION_DATASETS = (
    "cpic",
    "cpic_alleles",
    "cpic_pairs",
    "cpic_diplotypes",
    "clinvar",
    "gnomad",
    "gencc",
)

HTTP_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_DELAY = 2
_CLINVAR_BATCH_SIZE = 10_000

ANNOTATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS annotation_meta (
    dataset TEXT PRIMARY KEY,
    refreshed_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_version TEXT,
    last_attempted_at TEXT,
    last_attempt_status TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS cpic_guidelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gene TEXT NOT NULL,
    drug TEXT NOT NULL,
    phenotype TEXT,
    activity_score TEXT,
    recommendation TEXT,
    evidence_level TEXT
);

CREATE INDEX IF NOT EXISTS idx_cpic_gene ON cpic_guidelines(gene);
CREATE INDEX IF NOT EXISTS idx_cpic_drug ON cpic_guidelines(drug);
CREATE INDEX IF NOT EXISTS idx_cpic_gene_drug ON cpic_guidelines(gene, drug);

CREATE TABLE IF NOT EXISTS clinvar_variants (
    rsid TEXT PRIMARY KEY,
    variation_id TEXT,
    significance TEXT,
    condition TEXT,
    review_status TEXT,
    last_evaluated TEXT,
    gene TEXT,
    chromosome TEXT,
    position INTEGER,
    reference_allele TEXT,
    alternate_allele TEXT,
    gene_curie TEXT,
    phenotype_ids TEXT
);

CREATE INDEX IF NOT EXISTS idx_clinvar_gene ON clinvar_variants(gene);

CREATE TABLE IF NOT EXISTS gnomad_frequencies (
    chromosome TEXT NOT NULL,
    position INTEGER NOT NULL,
    reference_allele TEXT NOT NULL,
    alternate_allele TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    source_dataset TEXT NOT NULL,
    source_release TEXT NOT NULL,
    status TEXT NOT NULL,
    unavailable_reason TEXT,
    joint_ac INTEGER,
    joint_an INTEGER,
    joint_af REAL,
    exome_ac INTEGER,
    exome_an INTEGER,
    exome_af REAL,
    genome_ac INTEGER,
    genome_an INTEGER,
    genome_af REAL,
    variant_filters TEXT NOT NULL,
    exome_filters TEXT NOT NULL,
    genome_filters TEXT NOT NULL,
    joint_filters TEXT NOT NULL,
    discordance_test TEXT,
    discordance_p_value REAL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (chromosome, position, reference_allele, alternate_allele)
);

CREATE TABLE IF NOT EXISTS gencc_gene_disease (
    sgc_id TEXT PRIMARY KEY,
    version_number INTEGER NOT NULL,
    gene_curie TEXT NOT NULL,
    gene_symbol TEXT NOT NULL,
    disease_curie TEXT NOT NULL,
    disease_original_curie TEXT,
    classification_curie TEXT NOT NULL,
    classification_title TEXT NOT NULL,
    moi_curie TEXT NOT NULL,
    moi_title TEXT NOT NULL,
    submitter_curie TEXT NOT NULL,
    submitter_title TEXT NOT NULL,
    submitted_as_date TEXT,
    public_report_url TEXT,
    assertion_criteria_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_gencc_gene_curie
    ON gencc_gene_disease(gene_curie);

CREATE TABLE IF NOT EXISTS cpic_allele_definitions (
    gene TEXT NOT NULL,
    allele_name TEXT NOT NULL,
    rsid TEXT NOT NULL,
    variant_allele TEXT NOT NULL,
    clinical_functional_status TEXT,
    strength TEXT,
    PRIMARY KEY (gene, allele_name, rsid)
);

CREATE INDEX IF NOT EXISTS idx_cpic_allele_def_gene
    ON cpic_allele_definitions(gene);

CREATE TABLE IF NOT EXISTS cpic_pairs (
    drug TEXT NOT NULL,
    gene TEXT NOT NULL,
    cpiclevel TEXT,
    guidelinename TEXT,
    guidelineurl TEXT,
    PRIMARY KEY (drug, gene)
);

CREATE INDEX IF NOT EXISTS idx_cpic_pairs_drug ON cpic_pairs(drug);

CREATE TABLE IF NOT EXISTS cpic_diplotypes (
    gene TEXT NOT NULL,
    diplotype_key TEXT NOT NULL,
    diplotype TEXT NOT NULL,
    gene_result TEXT NOT NULL,
    result_vocabulary TEXT NOT NULL,
    function1 TEXT,
    function2 TEXT,
    activity_value1 TEXT,
    activity_value2 TEXT,
    total_activity_score TEXT,
    PRIMARY KEY (gene, diplotype_key)
);

CREATE INDEX IF NOT EXISTS idx_cpic_diplotypes_gene
    ON cpic_diplotypes(gene);
"""


def _reference_db_path(cache_dir: Path) -> Path:
    return cache_dir / REFERENCE_DB_FILENAME


def open_annotation_db(ref_db_path: Path) -> sqlite3.Connection:
    """Open (or create) famed-reference.db and ensure the schema exists."""
    ref_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ref_db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(ANNOTATION_SCHEMA_SQL)
    _migrate_annotation_meta_columns(conn)
    _migrate_clinvar_variant_columns(conn)
    conn.commit()
    return conn


def _migrate_annotation_meta_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(annotation_meta)")}
    for column in (
        "source_version",
        "last_attempted_at",
        "last_attempt_status",
        "last_error",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE annotation_meta ADD COLUMN {column} TEXT")


def _migrate_clinvar_variant_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clinvar_variants)")}
    for column in (
        "reference_allele",
        "alternate_allele",
        "gene_curie",
        "phenotype_ids",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE clinvar_variants ADD COLUMN {column} TEXT")


@dataclass(frozen=True)
class InheritanceResolution:
    status: str
    moi_curie: str | None = None
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, order=True)
class VariantIdentity:
    chromosome: str
    position: int
    reference_allele: str
    alternate_allele: str

    @property
    def variant_id(self) -> str:
        return f"{self.chromosome}-{self.position}-{self.reference_allele}-{self.alternate_allele}"


def _http_stream(url: str) -> urllib.request.addinfourl:
    """Open a streaming HTTP connection with retries; caller is responsible for closing."""
    headers = {"User-Agent": "famed-annotation-cache/0.1"}
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Failed to open stream {url} after {MAX_RETRIES} attempts: {last_error}")


def _extract_cpic_rows(item: Any) -> list[dict[str, str | None]]:
    """Extract normalised rows from one CPIC recommendation API object.

    The CPIC API has evolved; try multiple field name spellings defensively.
    Returns an empty list when the item lacks a usable gene+drug pair.
    """
    if not isinstance(item, dict):
        return []

    def _get(*keys: str) -> str:
        for k in keys:
            v = item.get(k)
            if v and isinstance(v, str):
                return v.strip()
            # Nested dict (e.g. drug: {name: ...})
            if isinstance(v, dict):
                inner = v.get("name") or v.get("symbol") or v.get("label") or ""
                if inner:
                    return str(inner).strip()
        return ""

    def _json_keys(*keys: str) -> list[str]:
        genes: list[str] = []
        for k in keys:
            v = item.get(k)
            if isinstance(v, dict) and v:
                genes.extend(str(key).strip() for key in v if str(key).strip())
        return list(dict.fromkeys(genes))

    def _json_value_for(gene_symbol: str, *keys: str) -> str:
        for k in keys:
            v = item.get(k)
            if isinstance(v, dict):
                value = v.get(gene_symbol)
                if value:
                    return str(value).strip()
        return ""

    gene = _get("genesymbol", "gene", "geneSymbol", "gene_symbol")
    genes = (
        [gene]
        if gene
        else _json_keys(
            "lookupkey",
            "implications",
            "phenotypes",
            "activityscore",
            "allelestatus",
        )
    )
    drug = _get("drugname", "drug", "drugName", "drug_name", "guideline")

    if not genes or not drug:
        return []

    rows: list[dict[str, str | None]] = []
    for gene_symbol in genes:
        rows.append(
            {
                "gene": gene_symbol,
                "drug": drug,
                "phenotype": _get("phenotype", "lookupkey", "lookup_key")
                or _json_value_for(gene_symbol, "phenotypes", "lookupkey")
                or None,
                "activity_score": _get("activityscore", "activity_score", "activityScore")
                or _json_value_for(gene_symbol, "activityscore")
                or None,
                "recommendation": _get(
                    "drugrecommendation",
                    "drug_recommendation",
                    "drugRecommendation",
                    "recommendation",
                    "implications",
                )
                or None,
                "evidence_level": _get(
                    "classification",
                    "cpic_level",
                    "cpicLevel",
                    "pgxLevel",
                    "evidence_level",
                )
                or None,
            }
        )
    return rows


def _extract_cpic_row(item: Any) -> dict[str, str | None] | None:
    rows = _extract_cpic_rows(item)
    return rows[0] if rows else None


def _fetch_cpic_recommendations() -> list[dict[str, str | None]]:
    from famed.reference_cache import _fetch_json

    data = _fetch_json(CPIC_RECOMMENDATIONS_URL)
    if not isinstance(data, list):
        raise ValueError(
            f"CPIC /recommendation returned unexpected type {type(data).__name__}; expected list"
        )
    rows: list[dict[str, str | None]] = []
    for item in data:
        rows.extend(_extract_cpic_rows(item))
    return rows


def fetch_cpic_data(conn: sqlite3.Connection) -> int:
    """Download CPIC recommendations and write to cpic_guidelines table.

    Clears the table before writing so refresh is idempotent. Returns the
    number of rows written.
    """
    rows = _fetch_cpic_recommendations()
    conn.execute("DELETE FROM cpic_guidelines")
    conn.executemany(
        """
        INSERT INTO cpic_guidelines (gene, drug, phenotype, activity_score,
                                     recommendation, evidence_level)
        VALUES (:gene, :drug, :phenotype, :activity_score,
                :recommendation, :evidence_level)
        """,
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO annotation_meta (dataset, refreshed_at, row_count, source_url)"
        " VALUES (?, ?, ?, ?)",
        ("cpic", datetime.now(UTC).isoformat(), len(rows), CPIC_RECOMMENDATIONS_URL),
    )
    conn.commit()
    return len(rows)


def fetch_cpic_pairs(conn: sqlite3.Connection) -> int:
    """Download CPIC pair_view (level A/B, used-for-recommendation) into cpic_pairs.

    Captures drug-gene pairs like warfarin/CYP2C9 that have CPIC A guidelines but
    no per-phenotype recommendations in recommendation_view. Clears and rewrites the
    table so refresh is idempotent. Returns the number of rows written.
    """
    from famed.reference_cache import _fetch_json

    data = _fetch_json(CPIC_PAIRS_URL)
    if not isinstance(data, list):
        raise ValueError(
            f"CPIC /pair_view returned unexpected type {type(data).__name__}; expected list"
        )

    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, str | None]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        drug = " ".join(str(item.get("drugname") or "").split())
        gene = " ".join(str(item.get("genesymbol") or "").split())
        cpiclevel = " ".join(str(item.get("cpiclevel") or "").split()) or None
        used = str(item.get("usedforrecommendation") or "").strip().lower()
        provisional = bool(item.get("provisional"))
        if not drug or not gene or provisional:
            continue
        if cpiclevel not in ("A", "B"):
            continue
        if used != "yes":
            continue
        key = (drug.casefold(), gene.casefold())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "drug": drug,
                "gene": gene,
                "cpiclevel": cpiclevel,
                "guidelinename": " ".join(str(item.get("guidelinename") or "").split()) or None,
                "guidelineurl": " ".join(str(item.get("guidelineurl") or "").split()) or None,
            }
        )

    conn.execute("DELETE FROM cpic_pairs")
    conn.executemany(
        "INSERT OR REPLACE INTO cpic_pairs"
        " (drug, gene, cpiclevel, guidelinename, guidelineurl)"
        " VALUES (:drug, :gene, :cpiclevel, :guidelinename, :guidelineurl)",
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO annotation_meta"
        " (dataset, refreshed_at, row_count, source_url)"
        " VALUES (?, ?, ?, ?)",
        ("cpic_pairs", datetime.now(UTC).isoformat(), len(rows), CPIC_PAIRS_URL),
    )
    conn.commit()
    return len(rows)


def _normalize_diplotype_key(value: str) -> str:
    alleles = [" ".join(part.split()) for part in value.split("/") if part.strip()]
    return "/".join(sorted(alleles, key=str.casefold))


def fetch_cpic_diplotypes(conn: sqlite3.Connection) -> int:
    """Cache CPIC's exact diplotype-to-gene-result translations."""
    rows: list[dict[str, str | None]] = []
    for gene in ("SLCO1B1", "ABCG2"):
        data = _fetch_cpic_json(f"{CPIC_DIPLOTYPES_URL}?genesymbol=eq.{gene}")
        for item in data:
            diplotype = " ".join(str(item.get("diplotype") or "").split())
            gene_result = " ".join(str(item.get("generesult") or "").split())
            if not diplotype or not gene_result:
                continue
            rows.append(
                {
                    "gene": gene,
                    "diplotype_key": _normalize_diplotype_key(diplotype),
                    "diplotype": diplotype,
                    "gene_result": gene_result,
                    "result_vocabulary": "function",
                    "function1": item.get("function1"),
                    "function2": item.get("function2"),
                    "activity_value1": item.get("activityvalue1"),
                    "activity_value2": item.get("activityvalue2"),
                    "total_activity_score": item.get("totalactivityscore"),
                }
            )

    conn.execute("DELETE FROM cpic_diplotypes")
    conn.executemany(
        """
        INSERT INTO cpic_diplotypes
            (gene, diplotype_key, diplotype, gene_result, result_vocabulary,
             function1, function2, activity_value1, activity_value2,
             total_activity_score)
        VALUES
            (:gene, :diplotype_key, :diplotype, :gene_result, :result_vocabulary,
             :function1, :function2, :activity_value1, :activity_value2,
             :total_activity_score)
        """,
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO annotation_meta"
        " (dataset, refreshed_at, row_count, source_url) VALUES (?, ?, ?, ?)",
        ("cpic_diplotypes", datetime.now(UTC).isoformat(), len(rows), CPIC_DIPLOTYPES_URL),
    )
    conn.commit()
    return len(rows)


CPIC_ALLELE_DEFINITIONS_URL = "https://api.cpicpgx.org/v1/allele_definition"
CPIC_SEQUENCE_LOCATION_URL = "https://api.cpicpgx.org/v1/sequence_location"
CPIC_ALLELE_LOCATION_VALUE_URL = "https://api.cpicpgx.org/v1/allele_location_value"
CPIC_ALLELE_URL = "https://api.cpicpgx.org/v1/allele"

_PGX_GENES = [
    "CYP2C19",
    "CYP2D6",
    "CYP2C9",
    "CYP3A5",
    "CYP2B6",
    "SLCO1B1",
    "DPYD",
    "TPMT",
    "NUDT15",
    "UGT1A1",
    "ABCG2",
    "G6PD",
    "HLA-A",
    "HLA-B",
    "CACNA1S",
    "RYR1",
    "CFTR",
]


def _fetch_cpic_json(url: str) -> list[dict[str, Any]]:
    from famed.reference_cache import _fetch_json

    data = _fetch_json(url)
    if not isinstance(data, list):
        raise ValueError(f"CPIC API returned unexpected type for {url}")
    return data


def fetch_cpic_allele_definitions(conn: sqlite3.Connection) -> int:
    """Download CPIC star allele definitions and write to cpic_allele_definitions.

    Queries the allele_definition, sequence_location, allele_location_value,
    and allele endpoints from the CPIC API. Builds a rsID→allele→variant_allele
    mapping for each pharmacogene. Clears the table before writing so refresh
    is idempotent. Returns the number of rows written.
    """
    all_allele_defs = _fetch_cpic_json(
        f"{CPIC_ALLELE_DEFINITIONS_URL}?genesymbol=in.({','.join(_PGX_GENES)})"
    )
    allele_def_by_id: dict[int, dict[str, Any]] = {}
    allele_def_ids: list[int] = []
    for ad in all_allele_defs:
        def_id = int(ad["id"])
        allele_def_by_id[def_id] = ad
        allele_def_ids.append(def_id)

    if not allele_def_ids:
        return 0

    location_values: list[dict[str, Any]] = []
    chunk_size = 200
    for i in range(0, len(allele_def_ids), chunk_size):
        chunk = allele_def_ids[i : i + chunk_size]
        location_values.extend(
            _fetch_cpic_json(
                f"{CPIC_ALLELE_LOCATION_VALUE_URL}?alleledefinitionid="
                f"in.({','.join(map(str, chunk))})"
            )
        )
    location_ids = list({int(lv["locationid"]) for lv in location_values})
    loc_values_by_def: dict[int, list[dict[str, Any]]] = {}
    for lv in location_values:
        def_id = int(lv["alleledefinitionid"])
        loc_values_by_def.setdefault(def_id, []).append(lv)

    seq_locations: dict[int, dict[str, Any]] = {}
    if location_ids:
        chunk_size = 100
        for i in range(0, len(location_ids), chunk_size):
            chunk = location_ids[i : i + chunk_size]
            seq_data = _fetch_cpic_json(
                f"{CPIC_SEQUENCE_LOCATION_URL}?id=in.({','.join(map(str, chunk))})"
            )
            for sl in seq_data:
                seq_locations[int(sl["id"])] = sl

    allele_functional: dict[tuple[str, str], str] = {}
    all_alleles = _fetch_cpic_json(f"{CPIC_ALLELE_URL}?genesymbol=in.({','.join(_PGX_GENES)})")
    for allele in all_alleles:
        gene = allele.get("genesymbol", "")
        name = allele.get("name", "")
        status = allele.get("clinicalfunctionalstatus", "")
        if gene and name:
            allele_functional[(gene, name)] = status

    rows: list[dict[str, str | None]] = []
    for def_id, ad in allele_def_by_id.items():
        gene = ad.get("genesymbol", "")
        allele_name = ad.get("name", "")
        lvs = loc_values_by_def.get(def_id, [])
        for lv in lvs:
            loc_id = int(lv["locationid"])
            seq = seq_locations.get(loc_id)
            if seq is None:
                continue
            rsid = seq.get("dbsnpid", "")
            if not rsid:
                continue
            variant_allele = lv.get("variantallele", "")
            functional_status = allele_functional.get((gene, allele_name), "")
            strength = ad.get("strength", "")
            rows.append(
                {
                    "gene": gene,
                    "allele_name": allele_name,
                    "rsid": rsid,
                    "variant_allele": variant_allele,
                    "clinical_functional_status": functional_status or None,
                    "strength": strength or None,
                }
            )

    conn.execute("DELETE FROM cpic_allele_definitions")
    conn.executemany(
        """
        INSERT INTO cpic_allele_definitions
            (gene, allele_name, rsid, variant_allele,
             clinical_functional_status, strength)
        VALUES
            (:gene, :allele_name, :rsid, :variant_allele,
             :clinical_functional_status, :strength)
        """,
        rows,
    )

    conn.execute(
        "INSERT OR REPLACE INTO annotation_meta (dataset, refreshed_at, row_count, source_url)"
        " VALUES (?, ?, ?, ?)",
        ("cpic_alleles", datetime.now(UTC).isoformat(), len(rows), CPIC_ALLELE_DEFINITIONS_URL),
    )
    conn.commit()
    return len(rows)


def _stream_clinvar_rows(text_stream: io.TextIOBase) -> Generator[dict[str, Any], None, None]:
    """Yield parsed ClinVar rows with valid rsIDs from a tab-delimited text stream.

    The variant_summary.txt header row starts with '#AlleleID'; we strip the '#'
    so DictReader keys are consistent. Rows with RS# == -1 are skipped.
    """
    reader = csv.DictReader(text_stream, delimiter="\t")

    if reader.fieldnames:
        fieldnames = list(reader.fieldnames)
        if fieldnames[0].startswith("#"):
            fieldnames[0] = fieldnames[0].lstrip("#")
        reader.fieldnames = fieldnames

    rsid_col = "RS# (dbSNP)"
    for row in reader:
        rs_raw = (row.get(rsid_col) or "-1").strip()
        if rs_raw in ("-1", "", ".", "na"):
            continue
        try:
            rsid = f"rs{int(rs_raw)}"
        except ValueError:
            continue

        position_raw = (row.get("Start") or "").strip()
        position: int | None
        try:
            position = int(position_raw) if position_raw else None
        except ValueError:
            position = None

        hgnc_raw = (row.get("HGNC_ID") or "").strip()
        gene_curie = (
            (hgnc_raw if hgnc_raw.casefold().startswith("hgnc:") else f"HGNC:{hgnc_raw}")
            if hgnc_raw
            else None
        )

        yield {
            "rsid": rsid,
            "variation_id": (row.get("VariationID") or "").strip() or None,
            "significance": (row.get("ClinicalSignificance") or "").strip() or None,
            "condition": (row.get("PhenotypeList") or "").strip() or None,
            "review_status": (row.get("ReviewStatus") or "").strip() or None,
            "last_evaluated": (row.get("LastEvaluated") or "").strip() or None,
            "gene": (row.get("GeneSymbol") or "").strip() or None,
            "gene_curie": gene_curie,
            "phenotype_ids": (row.get("PhenotypeIDS") or "").strip() or None,
            "chromosome": (row.get("Chromosome") or "").strip() or None,
            "position": position,
            "reference_allele": (row.get("ReferenceAlleleVCF") or "").strip() or None,
            "alternate_allele": (row.get("AlternateAlleleVCF") or "").strip() or None,
        }


def fetch_clinvar_data(
    conn: sqlite3.Connection,
    *,
    source_override: io.TextIOBase | None = None,
) -> int:
    """Download and parse ClinVar variant_summary.txt.gz, writing to clinvar_variants.

    Clears the table before writing so refresh is idempotent. Streams the
    gzip-compressed file to avoid loading it into memory.  Accepts an optional
    ``source_override`` text stream for testing without network access.
    Returns the number of rows written.
    """
    conn.execute("DELETE FROM clinvar_variants")

    count = 0
    batch: list[dict[str, Any]] = []

    def _flush(b: list[dict[str, Any]]) -> None:
        conn.executemany(
            """
            INSERT OR REPLACE INTO clinvar_variants
                (rsid, variation_id, significance, condition, review_status,
                 last_evaluated, gene, gene_curie, phenotype_ids, chromosome, position,
                 reference_allele, alternate_allele)
            VALUES (:rsid, :variation_id, :significance, :condition, :review_status,
                    :last_evaluated, :gene, :gene_curie, :phenotype_ids,
                    :chromosome, :position,
                    :reference_allele, :alternate_allele)
            """,
            b,
        )

    if source_override is not None:
        stream: io.TextIOBase = source_override
    else:
        http_resp = _http_stream(CLINVAR_SUMMARY_URL)
        gz_stream = gzip.GzipFile(fileobj=http_resp)
        stream = io.TextIOWrapper(gz_stream, encoding="utf-8", errors="replace")

    try:
        for row in _stream_clinvar_rows(stream):
            batch.append(row)
            count += 1
            if len(batch) >= _CLINVAR_BATCH_SIZE:
                _flush(batch)
                batch.clear()
        if batch:
            _flush(batch)
    finally:
        if source_override is None:
            stream.close()

    conn.execute(
        "INSERT OR REPLACE INTO annotation_meta (dataset, refreshed_at, row_count, source_url)"
        " VALUES (?, ?, ?, ?)",
        ("clinvar", datetime.now(UTC).isoformat(), count, CLINVAR_SUMMARY_URL),
    )
    conn.commit()
    return count


def normalize_grch38_variant(
    chromosome: str,
    position: int,
    reference_allele: str,
    alternate_allele: str,
) -> VariantIdentity | None:
    """Return one exact minimally represented GRCh38 allele identity."""
    chrom = chromosome.strip().removeprefix("chr").upper()
    ref = reference_allele.strip().upper()
    alt = alternate_allele.strip().upper()
    if chrom not in {str(value) for value in range(1, 23)} | {"X", "Y"}:
        return None
    if position <= 0 or not re.fullmatch(r"[ACGT]+", ref) or not re.fullmatch(r"[ACGT]+", alt):
        return None
    if ref == alt:
        return None

    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref = ref[:-1]
        alt = alt[:-1]
    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        ref = ref[1:]
        alt = alt[1:]
        position += 1

    return VariantIdentity(chrom, position, ref, alt)


def public_clinvar_gnomad_targets(conn: sqlite3.Connection) -> tuple[VariantIdentity, ...]:
    """Derive gnomAD targets exclusively from public ClinVar reference rows."""
    rows = conn.execute(
        """
        SELECT chromosome, position, reference_allele, alternate_allele
        FROM clinvar_variants
        WHERE LOWER(COALESCE(significance, '')) LIKE '%pathogenic%'
        ORDER BY chromosome, position, reference_allele, alternate_allele
        """
    ).fetchall()
    targets = {
        identity
        for row in rows
        if (
            identity := normalize_grch38_variant(
                str(row["chromosome"] or ""),
                int(row["position"] or 0),
                str(row["reference_allele"] or ""),
                str(row["alternate_allele"] or ""),
            )
        )
        is not None
    }
    return tuple(sorted(targets))


_GNOMAD_VARIANT_FRAGMENT = """
fragment FamedVariantFrequency on VariantDetails {
    variant_id
    chrom
    pos
    ref
    alt
    flags
    exome { ac an filters flags }
    genome { ac an filters flags }
    joint {
      ac
      an
      filters
      freq_comparison_stats {
        stat_union { p_value stat_test_name }
      }
    }
}
"""


def _post_gnomad_query(query: str, variables: dict[str, str]) -> dict[str, Any]:
    request_body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "famed-annotation-cache/0.1",
    }
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            request = urllib.request.Request(
                GNOMAD_API_URL,
                data=request_body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("gnomAD API returned a non-object response")
            return payload
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Failed to query gnomAD after {MAX_RETRIES} attempts: {last_error}")


def _fetch_gnomad_release() -> str:
    """Validate the configured release against gnomAD's official release page."""
    request = urllib.request.Request(
        GNOMAD_RELEASE_URL,
        headers={"User-Agent": "famed-annotation-cache/0.1"},
    )
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                release_page = response.read().decode("utf-8")
            if f"gnomAD v{GNOMAD_RELEASE}" not in release_page:
                raise ValueError(f"gnomAD release metadata did not confirm v{GNOMAD_RELEASE}")
            return GNOMAD_RELEASE
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(
        f"Failed to validate gnomAD release after {MAX_RETRIES} attempts: {last_error}"
    )


def _fetch_gnomad_batch(
    targets: tuple[VariantIdentity, ...],
) -> tuple[dict[str, Any], ...]:
    """Fetch one bounded GraphQL batch and split it into per-variant payloads."""
    if not targets or len(targets) > GNOMAD_BATCH_SIZE:
        raise ValueError(f"gnomAD batch size must be between 1 and {GNOMAD_BATCH_SIZE}")
    declarations = ", ".join(f"$variant{index}: String!" for index in range(len(targets)))
    fields = "\n".join(
        (
            f"v{index}: variant(variantId: $variant{index}, dataset: {GNOMAD_DATASET_ID}) "
            "{ ...FamedVariantFrequency }"
        )
        for index in range(len(targets))
    )
    query = (
        f"query FamedVariantFrequencyBatch({declarations}) {{\n{fields}\n}}\n"
        f"{_GNOMAD_VARIANT_FRAGMENT}"
    )
    variables = {f"variant{index}": target.variant_id for index, target in enumerate(targets)}
    payload = _post_gnomad_query(query, variables)
    data = payload.get("data")
    errors = payload.get("errors")
    if not isinstance(data, dict):
        raise ValueError(f"gnomAD batch returned malformed data: {errors!r}")
    global_errors = [
        error for error in errors or [] if not isinstance(error, dict) or not error.get("path")
    ]
    results: list[dict[str, Any]] = []
    for index in range(len(targets)):
        alias = f"v{index}"
        alias_errors = [
            error
            for error in errors or []
            if isinstance(error, dict)
            and isinstance(error.get("path"), list)
            and error["path"]
            and error["path"][0] == alias
        ]
        results.append(
            {
                "data": {"variant": data.get(alias)},
                "errors": [*global_errors, *alias_errors],
            }
        )
    return tuple(results)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _allele_frequency(ac: int | None, an: int | None) -> float | None:
    if ac is None or an is None or an <= 0:
        return None
    return ac / an


def _source_filters(section: Any, *fields: str) -> tuple[str, ...]:
    if not isinstance(section, dict):
        return ()
    values: set[str] = set()
    for field in fields:
        raw = section.get(field)
        if isinstance(raw, list):
            values.update(str(value) for value in raw if str(value).strip())
    return tuple(sorted(values))


def _gnomad_not_found(errors: Any) -> bool:
    return isinstance(errors, list) and any(
        "variant not found" in str(error.get("message") or "").casefold()
        for error in errors
        if isinstance(error, dict)
    )


def _parse_gnomad_frequency(
    target: VariantIdentity,
    payload: dict[str, Any],
    *,
    retrieved_at: str,
    source_release: str = GNOMAD_RELEASE,
) -> dict[str, Any]:
    base_row = {
        "chromosome": target.chromosome,
        "position": target.position,
        "reference_allele": target.reference_allele,
        "alternate_allele": target.alternate_allele,
        "variant_id": target.variant_id,
        "source_dataset": GNOMAD_DATASET_ID,
        "source_release": source_release,
        "retrieved_at": retrieved_at,
    }
    errors = payload.get("errors")
    data = payload.get("data")
    variant = data.get("variant") if isinstance(data, dict) else None
    if variant is None:
        if not _gnomad_not_found(errors):
            raise ValueError(f"gnomAD query failed for {target.variant_id}: {errors!r}")
        return {
            **base_row,
            "status": "unavailable",
            "unavailable_reason": "not_found",
            "joint_ac": None,
            "joint_an": None,
            "joint_af": None,
            "exome_ac": None,
            "exome_an": None,
            "exome_af": None,
            "genome_ac": None,
            "genome_an": None,
            "genome_af": None,
            "variant_filters": "[]",
            "exome_filters": "[]",
            "genome_filters": "[]",
            "joint_filters": "[]",
            "discordance_test": None,
            "discordance_p_value": None,
        }
    if errors:
        raise ValueError(
            f"gnomAD query returned partial errors for {target.variant_id}: {errors!r}"
        )
    if not isinstance(variant, dict):
        raise ValueError(f"gnomAD returned malformed variant data for {target.variant_id}")

    returned = normalize_grch38_variant(
        str(variant.get("chrom") or ""),
        int(variant.get("pos") or 0),
        str(variant.get("ref") or ""),
        str(variant.get("alt") or ""),
    )
    if returned != target or str(variant.get("variant_id") or "") != target.variant_id:
        raise ValueError(
            f"gnomAD identity mismatch: requested {target.variant_id}, "
            f"received {variant.get('variant_id')!r}"
        )

    exome = variant.get("exome") if isinstance(variant.get("exome"), dict) else {}
    genome = variant.get("genome") if isinstance(variant.get("genome"), dict) else {}
    joint = variant.get("joint") if isinstance(variant.get("joint"), dict) else {}
    joint_ac, joint_an = _optional_int(joint.get("ac")), _optional_int(joint.get("an"))
    exome_ac, exome_an = _optional_int(exome.get("ac")), _optional_int(exome.get("an"))
    genome_ac, genome_an = _optional_int(genome.get("ac")), _optional_int(genome.get("an"))
    variant_filters = _source_filters(variant, "flags")
    exome_filters = _source_filters(exome, "filters", "flags")
    genome_filters = _source_filters(genome, "filters", "flags")
    joint_filters = _source_filters(joint, "filters")

    comparison = joint.get("freq_comparison_stats")
    stat_union = comparison.get("stat_union") if isinstance(comparison, dict) else None
    discordance_test = (
        str(stat_union.get("stat_test_name"))
        if isinstance(stat_union, dict) and stat_union.get("stat_test_name")
        else None
    )
    discordance_p_value = (
        float(stat_union["p_value"])
        if isinstance(stat_union, dict) and stat_union.get("p_value") is not None
        else None
    )

    status = "available"
    unavailable_reason: str | None = None
    if joint_filters:
        status = "discordant"
        unavailable_reason = "source_flagged_discordance"
    elif variant_filters or exome_filters or genome_filters:
        status = "unavailable"
        unavailable_reason = "source_filtered"
    elif joint_an is None or joint_an <= 0:
        status = "unavailable"
        unavailable_reason = "missing_or_zero_allele_number"

    return {
        **base_row,
        "status": status,
        "unavailable_reason": unavailable_reason,
        "joint_ac": joint_ac,
        "joint_an": joint_an,
        "joint_af": _allele_frequency(joint_ac, joint_an),
        "exome_ac": exome_ac,
        "exome_an": exome_an,
        "exome_af": _allele_frequency(exome_ac, exome_an),
        "genome_ac": genome_ac,
        "genome_an": genome_an,
        "genome_af": _allele_frequency(genome_ac, genome_an),
        "variant_filters": json.dumps(variant_filters),
        "exome_filters": json.dumps(exome_filters),
        "genome_filters": json.dumps(genome_filters),
        "joint_filters": json.dumps(joint_filters),
        "discordance_test": discordance_test,
        "discordance_p_value": discordance_p_value,
    }


def fetch_gnomad_data(
    conn: sqlite3.Connection,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    batch_fetcher: Callable[
        [tuple[VariantIdentity, ...]], tuple[dict[str, Any], ...]
    ] = _fetch_gnomad_batch,
    release_fetcher: Callable[[], str] = _fetch_gnomad_release,
) -> int:
    """Refresh the public ClinVar-targeted gnomAD snapshot atomically."""
    retrieved_at = datetime.now(UTC).isoformat()
    try:
        targets = public_clinvar_gnomad_targets(conn)
        if not targets:
            raise ValueError("gnomAD refresh requires valid public ClinVar GRCh38 targets")
        source_release = GNOMAD_RELEASE if fetcher is not None else release_fetcher()
        if source_release != GNOMAD_RELEASE:
            raise ValueError(
                f"gnomAD release drift: expected {GNOMAD_RELEASE}, received {source_release}"
            )

        rows: list[dict[str, Any]] = []
        if fetcher is not None:
            rows = [
                _parse_gnomad_frequency(
                    target,
                    fetcher(target.variant_id),
                    retrieved_at=retrieved_at,
                    source_release=source_release,
                )
                for target in targets
            ]
        else:
            for offset in range(0, len(targets), GNOMAD_BATCH_SIZE):
                batch = targets[offset : offset + GNOMAD_BATCH_SIZE]
                payloads = batch_fetcher(batch)
                if len(payloads) != len(batch):
                    raise ValueError(
                        "gnomAD batch response count did not match the requested target count"
                    )
                rows.extend(
                    _parse_gnomad_frequency(
                        target,
                        payload,
                        retrieved_at=retrieved_at,
                        source_release=source_release,
                    )
                    for target, payload in zip(batch, payloads, strict=True)
                )

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM gnomad_frequencies")
        conn.executemany(
            """
            INSERT INTO gnomad_frequencies (
                chromosome, position, reference_allele, alternate_allele,
                variant_id, source_dataset, source_release, status,
                unavailable_reason, joint_ac, joint_an, joint_af,
                exome_ac, exome_an, exome_af, genome_ac, genome_an, genome_af,
                variant_filters, exome_filters, genome_filters, joint_filters,
                discordance_test, discordance_p_value, retrieved_at
            ) VALUES (
                :chromosome, :position, :reference_allele, :alternate_allele,
                :variant_id, :source_dataset, :source_release, :status,
                :unavailable_reason, :joint_ac, :joint_an, :joint_af,
                :exome_ac, :exome_an, :exome_af, :genome_ac, :genome_an, :genome_af,
                :variant_filters, :exome_filters, :genome_filters, :joint_filters,
                :discordance_test, :discordance_p_value, :retrieved_at
            )
            """,
            rows,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO annotation_meta
                (dataset, refreshed_at, row_count, source_url, source_version,
                 last_attempted_at, last_attempt_status, last_error)
            VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL)
            """,
            (
                "gnomad",
                retrieved_at,
                len(rows),
                GNOMAD_API_URL,
                source_release,
                retrieved_at,
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        current_count = conn.execute("SELECT COUNT(*) FROM gnomad_frequencies").fetchone()[0]
        conn.execute(
            """
            INSERT INTO annotation_meta (
                dataset, refreshed_at, row_count, source_url, source_version,
                last_attempted_at, last_attempt_status, last_error
            ) VALUES ('gnomad', ?, ?, ?, NULL, ?, 'failed', ?)
            ON CONFLICT(dataset) DO UPDATE SET
                last_attempted_at = excluded.last_attempted_at,
                last_attempt_status = excluded.last_attempt_status,
                last_error = excluded.last_error
            """,
            (
                retrieved_at,
                current_count,
                GNOMAD_API_URL,
                retrieved_at,
                str(exc),
            ),
        )
        conn.commit()
        raise
    return len(rows)


_GENCC_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "sgc_id",
        "version_number",
        "gene_curie",
        "gene_symbol",
        "disease_curie",
        "disease_original_curie",
        "classification_curie",
        "classification_title",
        "moi_curie",
        "moi_title",
        "submitter_curie",
        "submitter_title",
        "submitted_as_date",
        "submitted_as_public_report_url",
        "submitted_as_assertion_criteria_url",
    }
)
_CLINGEN_SUBMITTER_CURIE = "GENCC:000102"
_SUPPORTED_INHERITANCE_MODES: frozenset[str] = frozenset({"HP:0000006", "HP:0000007"})
_QUALIFYING_GENCC_CLASSIFICATIONS: frozenset[str] = frozenset({"definitive", "strong"})


def _stream_gencc_rows(text_stream: io.TextIOBase) -> Generator[dict[str, Any], None, None]:
    """Yield current-format ClinGen submissions from the GenCC TSV export."""
    reader = csv.DictReader(text_stream, delimiter="\t")
    fields = set(reader.fieldnames or ())
    missing = sorted(_GENCC_REQUIRED_FIELDS - fields)
    if missing:
        raise ValueError(f"GenCC export is missing required fields: {', '.join(missing)}")

    for row in reader:
        if (row.get("submitter_curie") or "").strip() != _CLINGEN_SUBMITTER_CURIE:
            continue
        row_id = (row.get("sgc_id") or "<missing sgc_id>").strip()
        try:
            version_number = int((row.get("version_number") or "").strip())
        except ValueError as exc:
            raise ValueError(f"GenCC ClinGen row {row_id} has an invalid version_number") from exc
        required_values = {
            key: (row.get(key) or "").strip()
            for key in (
                "sgc_id",
                "gene_curie",
                "gene_symbol",
                "disease_curie",
                "classification_curie",
                "classification_title",
                "moi_curie",
                "moi_title",
                "submitter_curie",
                "submitter_title",
            )
        }
        if not all(required_values.values()):
            raise ValueError(f"GenCC ClinGen row {row_id} has missing required values")
        yield {
            **required_values,
            "version_number": version_number,
            "disease_original_curie": ((row.get("disease_original_curie") or "").strip() or None),
            "submitted_as_date": (row.get("submitted_as_date") or "").strip() or None,
            "public_report_url": (
                (row.get("submitted_as_public_report_url") or "").strip() or None
            ),
            "assertion_criteria_url": (
                (row.get("submitted_as_assertion_criteria_url") or "").strip() or None
            ),
        }


def fetch_gencc_data(
    conn: sqlite3.Connection,
    *,
    source_override: io.TextIOBase | None = None,
) -> int:
    """Refresh the ClinGen-origin subset of GenCC's versioned TSV export."""
    if source_override is not None:
        stream: io.TextIOBase = source_override
    else:
        http_resp = _http_stream(GENCC_SUBMISSIONS_URL)
        stream = io.TextIOWrapper(http_resp, encoding="utf-8", errors="replace")

    try:
        rows = list(_stream_gencc_rows(stream))
    finally:
        if source_override is None:
            stream.close()

    if not rows:
        raise ValueError("GenCC export contains no valid ClinGen submissions")

    conn.execute("DELETE FROM gencc_gene_disease")
    conn.executemany(
        """
        INSERT INTO gencc_gene_disease
            (sgc_id, version_number, gene_curie, gene_symbol, disease_curie,
             disease_original_curie, classification_curie, classification_title,
             moi_curie, moi_title, submitter_curie, submitter_title,
             submitted_as_date, public_report_url, assertion_criteria_url)
        VALUES
            (:sgc_id, :version_number, :gene_curie, :gene_symbol, :disease_curie,
             :disease_original_curie, :classification_curie, :classification_title,
             :moi_curie, :moi_title, :submitter_curie, :submitter_title,
             :submitted_as_date, :public_report_url, :assertion_criteria_url)
        """,
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO annotation_meta (dataset, refreshed_at, row_count, source_url)"
        " VALUES (?, ?, ?, ?)",
        ("gencc", datetime.now(UTC).isoformat(), len(rows), GENCC_SUBMISSIONS_URL),
    )
    conn.commit()
    return len(rows)


def _normalise_curies(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(
        token.strip().casefold()
        for token in re.split(r"[|,;]", raw)
        if ":" in token and token.strip()
    )


def resolve_gencc_inheritance(
    conn: sqlite3.Connection,
    gene_curie: str | None,
    phenotype_ids: str | None,
) -> InheritanceResolution:
    """Resolve a conservative AD/AR mode from exact ClinVar↔GenCC identifiers."""
    disease_ids = _normalise_curies(phenotype_ids)
    if not gene_curie or not disease_ids:
        return InheritanceResolution(status="missing_identifiers")
    try:
        candidates = conn.execute(
            "SELECT * FROM gencc_gene_disease WHERE gene_curie = ? COLLATE NOCASE ORDER BY sgc_id",
            (gene_curie,),
        ).fetchall()
    except sqlite3.Error:
        return InheritanceResolution(status="reference_missing")

    matches = [
        row
        for row in candidates
        if disease_ids
        & _normalise_curies(
            "|".join(
                str(value)
                for value in (row["disease_curie"], row["disease_original_curie"])
                if value
            )
        )
    ]
    if not matches:
        return InheritanceResolution(status="no_exact_match")

    classifications = {str(row["classification_title"]).casefold() for row in matches}
    if any("disputed" in value or "refuted" in value for value in classifications):
        return InheritanceResolution(status="conflicting_evidence")

    material_rows = [
        row
        for row in matches
        if str(row["classification_title"]).casefold()
        not in {"no known disease relationship", "animal model only"}
    ]
    material_modes = {str(row["moi_curie"]) for row in material_rows}
    if any(mode not in _SUPPORTED_INHERITANCE_MODES for mode in material_modes):
        return InheritanceResolution(status="complex_inheritance")
    if len(material_modes) > 1:
        return InheritanceResolution(status="conflicting_modes")

    qualifying = [
        row
        for row in material_rows
        if str(row["classification_title"]).casefold() in _QUALIFYING_GENCC_CLASSIFICATIONS
    ]
    if not qualifying:
        return InheritanceResolution(status="insufficient_evidence")
    qualifying_modes = {str(row["moi_curie"]) for row in qualifying}
    if len(qualifying_modes) != 1:
        return InheritanceResolution(status="conflicting_modes")
    mode = next(iter(qualifying_modes))
    return InheritanceResolution(
        status="resolved",
        moi_curie=mode,
        source_ids=tuple(str(row["sgc_id"]) for row in qualifying if str(row["moi_curie"]) == mode),
    )


def refresh_annotation_dataset(ref_db_path: Path, dataset_id: str) -> int:
    """Rebuild one annotation dataset in famed-reference.db.

    Returns the number of rows written. Raises ValueError for unknown dataset_id.
    """
    if dataset_id not in ANNOTATION_DATASETS:
        raise ValueError(f"Unknown annotation dataset: {dataset_id!r}")
    conn = open_annotation_db(ref_db_path)
    try:
        if dataset_id == "cpic":
            return fetch_cpic_data(conn)
        if dataset_id == "cpic_alleles":
            return fetch_cpic_allele_definitions(conn)
        if dataset_id == "cpic_pairs":
            return fetch_cpic_pairs(conn)
        if dataset_id == "cpic_diplotypes":
            return fetch_cpic_diplotypes(conn)
        if dataset_id == "clinvar":
            return fetch_clinvar_data(conn)
        if dataset_id == "gnomad":
            return fetch_gnomad_data(conn)
        return fetch_gencc_data(conn)
    finally:
        conn.close()


def get_cpic_recommendations(
    ref_db_path: Path,
    gene: str,
    drug: str | None = None,
) -> list[dict[str, Any]]:
    """Return CPIC recommendations for a gene, optionally filtered by drug.

    Returns an empty list when the database is absent or has no matching rows.
    """
    if not ref_db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(ref_db_path)
        conn.row_factory = sqlite3.Row
        if drug:
            cur = conn.execute(
                "SELECT * FROM cpic_guidelines WHERE gene = ? AND drug = ?",
                (gene, drug),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM cpic_guidelines WHERE gene = ?",
                (gene,),
            )
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def get_clinvar_significance(
    ref_db_path: Path,
    rsid: str,
) -> dict[str, Any] | None:
    """Return the ClinVar pathogenicity entry for an rsID, or None if not found.

    Accepts rsIDs with or without the 'rs' prefix.
    """
    if not ref_db_path.is_file():
        return None
    normalised = rsid if rsid.startswith("rs") else f"rs{rsid}"
    try:
        conn = sqlite3.connect(ref_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM clinvar_variants WHERE rsid = ?", (normalised,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def get_annotation_status(ref_db_path: Path) -> dict[str, dict[str, Any]]:
    """Return row counts and refresh timestamps for each annotation dataset.

    Returns MISSING status entries when the DB file does not exist.
    """
    if not ref_db_path.is_file():
        return {
            ds: {"status": "MISSING", "row_count": 0, "refreshed_at": None}
            for ds in ANNOTATION_DATASETS
        }
    try:
        conn = sqlite3.connect(ref_db_path)
        conn.row_factory = sqlite3.Row
        meta_rows = {
            row["dataset"]: dict(row)
            for row in conn.execute("SELECT * FROM annotation_meta").fetchall()
        }
        conn.close()
    except sqlite3.Error:
        return {
            ds: {"status": "MISSING", "row_count": 0, "refreshed_at": None}
            for ds in ANNOTATION_DATASETS
        }

    result: dict[str, dict[str, Any]] = {}
    for ds in ANNOTATION_DATASETS:
        if ds in meta_rows:
            m = meta_rows[ds]
            status = "OK"
            if m.get("last_attempt_status") == "failed":
                status = "STALE" if m.get("row_count", 0) else "UNAVAILABLE"
            refreshed_at = m.get("refreshed_at")
            if status == "UNAVAILABLE" and not m.get("source_version"):
                refreshed_at = None
            result[ds] = {
                "status": status,
                "row_count": m.get("row_count", 0),
                "refreshed_at": refreshed_at,
                "source_url": m.get("source_url"),
                "source_version": m.get("source_version"),
                "last_attempted_at": m.get("last_attempted_at"),
                "last_attempt_status": m.get("last_attempt_status"),
                "last_error": m.get("last_error"),
            }
        else:
            result[ds] = {"status": "MISSING", "row_count": 0, "refreshed_at": None}
    return result
