"""Tests for famed.annotation_cache — genomics annotation reference database."""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from famed.annotation_cache import (
    CPIC_RECOMMENDATIONS_URL,
    GENCC_SUBMISSIONS_URL,
    GNOMAD_API_URL,
    GNOMAD_BATCH_SIZE,
    GNOMAD_RELEASE,
    REFERENCE_DB_FILENAME,
    VariantIdentity,
    _extract_cpic_row,
    _extract_cpic_rows,
    _fetch_gnomad_batch,
    _fetch_gnomad_release,
    _reference_db_path,
    _stream_clinvar_rows,
    _stream_gencc_rows,
    fetch_clinvar_data,
    fetch_cpic_data,
    fetch_cpic_diplotypes,
    fetch_gencc_data,
    fetch_gnomad_data,
    get_annotation_status,
    get_clinvar_significance,
    get_cpic_recommendations,
    normalize_grch38_variant,
    open_annotation_db,
    public_clinvar_gnomad_targets,
    refresh_annotation_dataset,
    resolve_gencc_inheritance,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CPIC_SAMPLE: list[dict] = [
    {
        "genesymbol": "CYP2C19",
        "drugname": "clopidogrel",
        "phenotype": "Normal Metabolizer",
        "activityscore": ">=1.0",
        "drugrecommendation": "Use label recommended dose",
        "classification": "Strong",
    },
    {
        "genesymbol": "CYP2D6",
        "drugname": "codeine",
        "phenotype": "Poor Metabolizer",
        "activityscore": "0",
        "drugrecommendation": "Avoid codeine use",
        "classification": "Strong",
    },
    # Row without gene — should be skipped
    {"drugname": "aspirin"},
    # Row with nested drug dict
    {
        "gene": "CYP2C9",
        "drug": {"name": "warfarin"},
        "phenotype": "Intermediate Metabolizer",
        "activityscore": "1.0-1.5",
        "drugrecommendation": "Consider 25% dose reduction",
        "classification": "Moderate",
    },
]

_CLINVAR_TSV_HEADER = (
    "#AlleleID\tType\tName\tGeneID\tGeneSymbol\tHGNC_ID\tClinicalSignificance"
    "\tClinSigSimple\tLastEvaluated\tRS# (dbSNP)\tnsv/esv (dbVar)\tRCVaccession"
    "\tPhenotypeIDS\tPhenotypeList\tOrigin\tOriginSimple\tAssembly"
    "\tChromosomeAccession\tChromosome\tStart\tStop\tReferenceAllele"
    "\tAlternateAllele\tCytogenetic\tReviewStatus\tNumberSubmitters\tGuidelines"
    "\tTestedInGTR\tOtherIDs\tSubmitterCategories\tVariationID\tPositionVCF"
    "\tReferenceAlleleVCF\tAlternateAlleleVCF\n"
)

_CLINVAR_ROWS = [
    # rsid=2108622, pathogenic, CFTR
    (
        "7105\tSNV\tNM_000492.4(CFTR):c.1521_1523delCTT\t1080\tCFTR\tHGNC:1884\t"
        "Pathogenic\t1\t2020-01-15\t2108622\t-\tRCV000007541\t"
        "OMIM:219700\tCystic fibrosis\tgermline\tgermline\tGRCh38\t"
        "NC_000007.14\t7\t117548628\t117548630\tCTT\t-\t7q31.2\t"
        "reviewed by expert panel\t5\t-\tN\t-\t1\t13\t117548628\tCTT\t-\n"
    ),
    # rsid=-1 — should be skipped
    (
        "8\tSNV\tNM_000016.5:c.286G>A\t34\tACADS\t90\t"
        "Pathogenic\t1\t2016-05-01\t-1\t-\tRCV000000007\t"
        "OMIM:201470\tACADS deficiency\tgermline\tgermline\tGRCh38\t"
        "NC_000012.12\t12\t121175678\t121175678\tG\tA\t12q24.31\t"
        "criteria provided, single submitter\t1\t-\tN\t-\t2\t2\t121175678\tG\tA\n"
    ),
    # rsid=80338943, benign
    (
        "15\tSNV\tNM_007294.3(BRCA1):c.5096G>A\t672\tBRCA1\t1100\t"
        "Benign\t0\t2018-03-10\t80338943\t-\tRCV000013437\t"
        "OMIM:604370\tHereditary breast and ovarian cancer syndrome\tgermline\tgermline\tGRCh38\t"
        "NC_000017.11\t17\t43076614\t43076614\tG\tA\t17q21.31\t"
        "criteria provided, multiple submitters, no conflicts\t8\t-\tY\t-\t16\t16\t43076614\tG\tA\n"
    ),
]


def _make_clinvar_stream(rows: list[str] | None = None) -> io.StringIO:
    content = _CLINVAR_TSV_HEADER + "".join(rows or _CLINVAR_ROWS)
    return io.StringIO(content)


def _insert_clinvar_target(
    conn: sqlite3.Connection,
    *,
    rsid: str,
    chromosome: str = "1",
    position: int = 100,
    reference_allele: str = "A",
    alternate_allele: str = "T",
    significance: str = "Pathogenic",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO clinvar_variants
            (rsid, significance, chromosome, position, reference_allele, alternate_allele)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (rsid, significance, chromosome, position, reference_allele, alternate_allele),
    )
    conn.commit()


def _gnomad_payload(
    variant_id: str,
    *,
    joint_ac: int | None = 2,
    joint_an: int | None = 200,
    exome_ac: int | None = 1,
    exome_an: int | None = 100,
    genome_ac: int | None = 1,
    genome_an: int | None = 100,
    variant_flags: list[str] | None = None,
    exome_filters: list[str] | None = None,
    genome_filters: list[str] | None = None,
    joint_filters: list[str] | None = None,
    p_value: float | None = 0.5,
) -> dict[str, Any]:
    chrom, pos, ref, alt = variant_id.split("-", 3)
    return {
        "data": {
            "variant": {
                "variant_id": variant_id,
                "chrom": chrom,
                "pos": int(pos),
                "ref": ref,
                "alt": alt,
                "flags": variant_flags or [],
                "exome": {
                    "ac": exome_ac,
                    "an": exome_an,
                    "filters": exome_filters or [],
                    "flags": [],
                },
                "genome": {
                    "ac": genome_ac,
                    "an": genome_an,
                    "filters": genome_filters or [],
                    "flags": [],
                },
                "joint": {
                    "ac": joint_ac,
                    "an": joint_an,
                    "filters": joint_filters or [],
                    "freq_comparison_stats": {
                        "stat_union": {
                            "p_value": p_value,
                            "stat_test_name": "contingency_table_test",
                        }
                    },
                },
            }
        }
    }


_GENCC_FIELDS = [
    "sgc_id",
    "version_number",
    "gene_curie",
    "gene_symbol",
    "disease_curie",
    "disease_title",
    "disease_original_curie",
    "disease_original_title",
    "classification_curie",
    "classification_title",
    "moi_curie",
    "moi_title",
    "submitter_curie",
    "submitter_title",
    "submitted_as_date",
    "submitted_as_public_report_url",
    "submitted_as_assertion_criteria_url",
]


def _make_gencc_stream(rows: list[dict[str, str]] | None = None) -> io.StringIO:
    default = {
        "sgc_id": "SGC-100001",
        "version_number": "1",
        "gene_curie": "HGNC:1884",
        "gene_symbol": "CFTR",
        "disease_curie": "MONDO:0009061",
        "disease_title": "cystic fibrosis",
        "disease_original_curie": "OMIM:219700",
        "disease_original_title": "Cystic fibrosis",
        "classification_curie": "GENCC:100001",
        "classification_title": "Definitive",
        "moi_curie": "HP:0000007",
        "moi_title": "Autosomal recessive",
        "submitter_curie": "GENCC:000102",
        "submitter_title": "ClinGen",
        "submitted_as_date": "2023-01-01",
        "submitted_as_public_report_url": "https://clinicalgenome.org/example",
        "submitted_as_assertion_criteria_url": "https://clinicalgenome.org/criteria",
    }
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_GENCC_FIELDS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows or [default]:
        writer.writerow({**default, **row})
    output.seek(0)
    return output


@pytest.fixture
def tmp_ref_db(tmp_path: Path) -> Path:
    return _reference_db_path(tmp_path)


@pytest.fixture
def open_db(tmp_ref_db: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = open_annotation_db(tmp_ref_db)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Schema and DB creation
# ---------------------------------------------------------------------------


class TestOpenAnnotationDb:
    def test_creates_db_file(self, tmp_path: Path) -> None:
        ref_db = _reference_db_path(tmp_path)
        assert not ref_db.exists()
        conn = open_annotation_db(ref_db)
        conn.close()
        assert ref_db.exists()
        assert ref_db.name == REFERENCE_DB_FILENAME

    def test_creates_required_tables(self, open_db: sqlite3.Connection) -> None:
        tables = {
            row[0] for row in open_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "annotation_meta",
            "cpic_guidelines",
            "clinvar_variants",
            "gencc_gene_disease",
        } <= tables

    def test_idempotent_creation(self, tmp_ref_db: Path) -> None:
        conn1 = open_annotation_db(tmp_ref_db)
        conn1.close()
        conn2 = open_annotation_db(tmp_ref_db)
        conn2.close()
        assert tmp_ref_db.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / REFERENCE_DB_FILENAME
        conn = open_annotation_db(nested)
        conn.close()
        assert nested.exists()

    def test_migrates_existing_clinvar_allele_columns(self, tmp_path: Path) -> None:
        ref_db = _reference_db_path(tmp_path)
        conn = sqlite3.connect(ref_db)
        conn.execute(
            """
            CREATE TABLE clinvar_variants (
                rsid TEXT PRIMARY KEY,
                variation_id TEXT,
                significance TEXT,
                condition TEXT,
                review_status TEXT,
                last_evaluated TEXT,
                gene TEXT,
                chromosome TEXT,
                position INTEGER
            )
            """
        )
        conn.commit()
        conn.close()

        migrated = open_annotation_db(ref_db)
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(clinvar_variants)")}
        migrated.close()

        assert "reference_allele" in columns
        assert "alternate_allele" in columns
        assert "gene_curie" in columns
        assert "phenotype_ids" in columns


# ---------------------------------------------------------------------------
# _extract_cpic_row
# ---------------------------------------------------------------------------


class TestExtractCpicRow:
    def test_standard_fields(self) -> None:
        row = _extract_cpic_row(
            {
                "genesymbol": "CYP2C19",
                "drugname": "clopidogrel",
                "phenotype": "Normal Metabolizer",
                "activityscore": ">=1.0",
                "drugrecommendation": "Use label dose",
                "classification": "Strong",
            }
        )
        assert row is not None
        assert row["gene"] == "CYP2C19"
        assert row["drug"] == "clopidogrel"
        assert row["evidence_level"] == "Strong"

    def test_nested_drug_dict(self) -> None:
        row = _extract_cpic_row(
            {
                "gene": "CYP2C9",
                "drug": {"name": "warfarin"},
                "classification": "Moderate",
            }
        )
        assert row is not None
        assert row["gene"] == "CYP2C9"
        assert row["drug"] == "warfarin"

    def test_current_recommendation_view_shape(self) -> None:
        row = _extract_cpic_row(
            {
                "lookupkey": {"HLA-B": "*57:01 positive"},
                "drugname": "abacavir",
                "drugrecommendation": "Abacavir is not recommended",
                "classification": "Strong",
                "phenotypes": {},
                "activityscore": {},
                "implications": {
                    "HLA-B": "Significantly increased risk of abacavir hypersensitivity"
                },
            }
        )
        assert row is not None
        assert row["gene"] == "HLA-B"
        assert row["drug"] == "abacavir"
        assert row["phenotype"] == "*57:01 positive"
        assert row["recommendation"] == "Abacavir is not recommended"
        assert row["evidence_level"] == "Strong"

    def test_recommendation_view_multiple_gene_keys_expand_to_rows(self) -> None:
        rows = _extract_cpic_rows(
            {
                "lookupkey": {
                    "CYP2C9": "Poor Metabolizer",
                    "VKORC1": "rs9923231 A/A",
                },
                "drugname": "warfarin",
                "drugrecommendation": "Use pharmacogenetic algorithm",
                "classification": "Strong",
            }
        )
        assert [row["gene"] for row in rows] == ["CYP2C9", "VKORC1"]
        assert {row["drug"] for row in rows} == {"warfarin"}
        assert rows[0]["phenotype"] == "Poor Metabolizer"
        assert rows[1]["phenotype"] == "rs9923231 A/A"

    def test_missing_gene_returns_none(self) -> None:
        assert _extract_cpic_row({"drugname": "aspirin"}) is None

    def test_missing_drug_returns_none(self) -> None:
        assert _extract_cpic_row({"genesymbol": "CYP2C19"}) is None

    def test_non_dict_returns_none(self) -> None:
        assert _extract_cpic_row("not a dict") is None  # type: ignore[arg-type]

    def test_optional_fields_none_when_absent(self) -> None:
        row = _extract_cpic_row({"genesymbol": "CYP2D6", "drugname": "codeine"})
        assert row is not None
        assert row["phenotype"] is None
        assert row["activity_score"] is None
        assert row["recommendation"] is None
        assert row["evidence_level"] is None


# ---------------------------------------------------------------------------
# _stream_clinvar_rows
# ---------------------------------------------------------------------------


class TestStreamClinvarRows:
    def test_yields_valid_rsid_rows(self) -> None:
        rows = list(_stream_clinvar_rows(_make_clinvar_stream()))
        rsids = [r["rsid"] for r in rows]
        assert "rs2108622" in rsids
        assert "rs80338943" in rsids

    def test_skips_minus_one_rsid(self) -> None:
        rows = list(_stream_clinvar_rows(_make_clinvar_stream()))
        rsids = [r["rsid"] for r in rows]
        # The row with RS# -1 must not appear
        assert all(r != "rs-1" and not r.endswith("-1") for r in rsids)
        assert len(rows) == 2

    def test_row_fields(self) -> None:
        rows = list(_stream_clinvar_rows(_make_clinvar_stream()))
        cftr_row = next(r for r in rows if r["rsid"] == "rs2108622")
        assert cftr_row["gene"] == "CFTR"
        assert cftr_row["significance"] == "Pathogenic"
        assert cftr_row["chromosome"] == "7"
        assert cftr_row["position"] == 117548628
        assert cftr_row["reference_allele"] == "CTT"
        assert cftr_row["alternate_allele"] == "-"
        assert cftr_row["gene_curie"] == "HGNC:1884"
        assert cftr_row["phenotype_ids"] == "OMIM:219700"

    def test_empty_stream(self) -> None:
        stream = io.StringIO(_CLINVAR_TSV_HEADER)
        assert list(_stream_clinvar_rows(stream)) == []

    def test_strips_hash_prefix_from_header(self) -> None:
        # Ensure no KeyError — reader handles '#AlleleID' correctly
        rows = list(_stream_clinvar_rows(_make_clinvar_stream()))
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# fetch_cpic_data
# ---------------------------------------------------------------------------


class TestFetchCpicData:
    def _mock_fetch(self) -> mock.MagicMock:
        return mock.patch(
            "famed.annotation_cache._fetch_cpic_recommendations",
            return_value=[
                r for r in (_extract_cpic_row(item) for item in _CPIC_SAMPLE) if r is not None
            ],
        )

    def test_writes_rows(self, open_db: sqlite3.Connection) -> None:
        with self._mock_fetch():
            count = fetch_cpic_data(open_db)
        assert count == 3  # 4 items, 1 skipped (no gene)
        rows = open_db.execute("SELECT * FROM cpic_guidelines").fetchall()
        assert len(rows) == 3

    def test_idempotent_refresh(self, open_db: sqlite3.Connection) -> None:
        with self._mock_fetch():
            fetch_cpic_data(open_db)
        with self._mock_fetch():
            count = fetch_cpic_data(open_db)
        rows = open_db.execute("SELECT * FROM cpic_guidelines").fetchall()
        assert len(rows) == count == 3

    def test_updates_annotation_meta(self, open_db: sqlite3.Connection) -> None:
        with self._mock_fetch():
            fetch_cpic_data(open_db)
        meta = open_db.execute("SELECT * FROM annotation_meta WHERE dataset='cpic'").fetchone()
        assert meta is not None
        assert meta["row_count"] == 3
        assert meta["source_url"] == CPIC_RECOMMENDATIONS_URL

    def test_gene_and_drug_stored_correctly(self, open_db: sqlite3.Connection) -> None:
        with self._mock_fetch():
            fetch_cpic_data(open_db)
        row = open_db.execute("SELECT * FROM cpic_guidelines WHERE gene='CYP2C19'").fetchone()
        assert row["drug"] == "clopidogrel"
        assert row["evidence_level"] == "Strong"


class TestFetchCpicDiplotypes:
    def test_writes_exact_function_results_idempotently(self, open_db: sqlite3.Connection) -> None:
        payload = [
            {
                "diplotype": "*5/*1",
                "generesult": "Decreased Function",
                "function1": "No function",
                "function2": "Normal function",
                "activityvalue1": "n/a",
                "activityvalue2": "n/a",
                "totalactivityscore": "n/a",
            }
        ]
        with mock.patch("famed.annotation_cache._fetch_cpic_json", return_value=payload):
            assert fetch_cpic_diplotypes(open_db) == 2
            assert fetch_cpic_diplotypes(open_db) == 2

        rows = open_db.execute(
            "SELECT gene, diplotype_key, gene_result, result_vocabulary "
            "FROM cpic_diplotypes ORDER BY gene"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("ABCG2", "*1/*5", "Decreased Function", "function"),
            ("SLCO1B1", "*1/*5", "Decreased Function", "function"),
        ]
        meta = open_db.execute(
            "SELECT row_count, source_url FROM annotation_meta WHERE dataset='cpic_diplotypes'"
        ).fetchone()
        assert meta is not None
        assert meta["row_count"] == 2
        assert meta["source_url"].endswith("/v1/diplotype")

    def test_skips_rows_without_diplotype_or_result(self, open_db: sqlite3.Connection) -> None:
        payload = [
            {"diplotype": "", "generesult": "Normal Function"},
            {"diplotype": "*1/*1", "generesult": ""},
        ]
        with mock.patch("famed.annotation_cache._fetch_cpic_json", return_value=payload):
            assert fetch_cpic_diplotypes(open_db) == 0


# ---------------------------------------------------------------------------
# fetch_clinvar_data
# ---------------------------------------------------------------------------


class TestFetchClinvarData:
    def test_writes_rows(self, open_db: sqlite3.Connection) -> None:
        count = fetch_clinvar_data(open_db, source_override=_make_clinvar_stream())
        assert count == 2
        rows = open_db.execute("SELECT * FROM clinvar_variants").fetchall()
        assert len(rows) == 2

    def test_idempotent_refresh(self, open_db: sqlite3.Connection) -> None:
        fetch_clinvar_data(open_db, source_override=_make_clinvar_stream())
        count = fetch_clinvar_data(open_db, source_override=_make_clinvar_stream())
        rows = open_db.execute("SELECT * FROM clinvar_variants").fetchall()
        assert len(rows) == count == 2

    def test_updates_annotation_meta(self, open_db: sqlite3.Connection) -> None:
        fetch_clinvar_data(open_db, source_override=_make_clinvar_stream())
        meta = open_db.execute("SELECT * FROM annotation_meta WHERE dataset='clinvar'").fetchone()
        assert meta is not None
        assert meta["row_count"] == 2

    def test_rsid_normalised(self, open_db: sqlite3.Connection) -> None:
        fetch_clinvar_data(open_db, source_override=_make_clinvar_stream())
        row = open_db.execute("SELECT * FROM clinvar_variants WHERE rsid='rs2108622'").fetchone()
        assert row is not None
        assert row["gene"] == "CFTR"
        assert row["reference_allele"] == "CTT"
        assert row["alternate_allele"] == "-"


class TestGnomadData:
    def test_normalizes_exact_grch38_identity_and_rejects_invalid_alleles(self) -> None:
        assert normalize_grch38_variant("chr1", 100, "AC", "AT") == VariantIdentity(
            "1", 101, "C", "T"
        )
        assert normalize_grch38_variant("1", 100, "ACG", "ATG") == VariantIdentity(
            "1", 101, "C", "T"
        )
        assert normalize_grch38_variant("M", 100, "A", "T") is None
        assert normalize_grch38_variant("1", 100, "-", "T") is None

    def test_targets_are_public_clinvar_only_and_keep_multiallelic_identity(
        self, open_db: sqlite3.Connection
    ) -> None:
        _insert_clinvar_target(open_db, rsid="rs1", alternate_allele="T")
        _insert_clinvar_target(open_db, rsid="rs2", alternate_allele="G")
        _insert_clinvar_target(open_db, rsid="rs3", alternate_allele="C", significance="Benign")
        _insert_clinvar_target(open_db, rsid="rs4", alternate_allele="-")

        assert public_clinvar_gnomad_targets(open_db) == (
            VariantIdentity("1", 100, "A", "G"),
            VariantIdentity("1", 100, "A", "T"),
        )

    def test_refresh_reconciles_rows_and_records_release_provenance(
        self, open_db: sqlite3.Connection
    ) -> None:
        _insert_clinvar_target(open_db, rsid="rs1")
        requested: list[str] = []

        def fetcher(variant_id: str) -> dict[str, object]:
            requested.append(variant_id)
            return _gnomad_payload(variant_id)

        assert fetch_gnomad_data(open_db, fetcher=fetcher) == 1
        assert fetch_gnomad_data(open_db, fetcher=fetcher) == 1
        assert requested == ["1-100-A-T", "1-100-A-T"]
        row = open_db.execute("SELECT * FROM gnomad_frequencies").fetchone()
        assert row["status"] == "available"
        assert row["joint_af"] == pytest.approx(0.01)
        assert row["exome_af"] == pytest.approx(0.01)
        assert row["genome_af"] == pytest.approx(0.01)
        assert row["source_release"] == GNOMAD_RELEASE
        meta = open_db.execute("SELECT * FROM annotation_meta WHERE dataset = 'gnomad'").fetchone()
        assert meta["row_count"] == 1
        assert meta["source_url"] == GNOMAD_API_URL
        assert meta["source_version"] == GNOMAD_RELEASE

    @pytest.mark.parametrize(
        ("payload", "status", "reason"),
        [
            (
                _gnomad_payload("1-100-A-T", joint_an=0),
                "unavailable",
                "missing_or_zero_allele_number",
            ),
            (
                _gnomad_payload("1-100-A-T", exome_filters=["RF"]),
                "unavailable",
                "source_filtered",
            ),
            (
                _gnomad_payload(
                    "1-100-A-T",
                    joint_filters=["exome_genome_frequency_discordance"],
                    p_value=1e-8,
                ),
                "discordant",
                "source_flagged_discordance",
            ),
            (
                {
                    "data": {"variant": None},
                    "errors": [{"message": "Variant not found"}],
                },
                "unavailable",
                "not_found",
            ),
        ],
    )
    def test_refresh_preserves_unavailable_and_discordant_states(
        self,
        open_db: sqlite3.Connection,
        payload: dict[str, object],
        status: str,
        reason: str,
    ) -> None:
        _insert_clinvar_target(open_db, rsid="rs1")

        fetch_gnomad_data(open_db, fetcher=lambda _variant_id: payload)

        row = open_db.execute("SELECT * FROM gnomad_frequencies").fetchone()
        assert row["status"] == status
        assert row["unavailable_reason"] == reason
        if status == "discordant":
            assert row["discordance_p_value"] == pytest.approx(1e-8)

    def test_failed_refresh_preserves_last_good_snapshot(self, open_db: sqlite3.Connection) -> None:
        _insert_clinvar_target(open_db, rsid="rs1")
        fetch_gnomad_data(
            open_db,
            fetcher=lambda variant_id: _gnomad_payload(variant_id, joint_ac=5),
        )
        before = dict(open_db.execute("SELECT * FROM gnomad_frequencies").fetchone())

        with pytest.raises(RuntimeError, match="network unavailable"):
            fetch_gnomad_data(
                open_db,
                fetcher=lambda _variant_id: (_ for _ in ()).throw(
                    RuntimeError("network unavailable")
                ),
            )

        after = dict(open_db.execute("SELECT * FROM gnomad_frequencies").fetchone())
        assert after == before
        meta = open_db.execute("SELECT * FROM annotation_meta WHERE dataset = 'gnomad'").fetchone()
        assert meta["last_attempt_status"] == "failed"
        assert meta["last_error"] == "network unavailable"
        assert (
            get_annotation_status(Path(open_db.execute("PRAGMA database_list").fetchone()[2]))[
                "gnomad"
            ]["status"]
            == "STALE"
        )

    def test_default_refresh_batches_large_target_sets(self, open_db: sqlite3.Connection) -> None:
        target_count = (GNOMAD_BATCH_SIZE * 2) + 5
        for index in range(target_count):
            _insert_clinvar_target(
                open_db,
                rsid=f"rs{index}",
                position=index + 1,
            )
        batch_sizes: list[int] = []

        def batch_fetcher(
            targets: tuple[VariantIdentity, ...],
        ) -> tuple[dict[str, object], ...]:
            batch_sizes.append(len(targets))
            return tuple(_gnomad_payload(target.variant_id) for target in targets)

        assert (
            fetch_gnomad_data(
                open_db,
                batch_fetcher=batch_fetcher,
                release_fetcher=lambda: GNOMAD_RELEASE,
            )
            == target_count
        )
        assert batch_sizes == [GNOMAD_BATCH_SIZE, GNOMAD_BATCH_SIZE, 5]

    def test_batch_query_uses_aliases_and_preserves_target_order(self) -> None:
        targets = (
            VariantIdentity("1", 100, "A", "T"),
            VariantIdentity("2", 200, "C", "G"),
        )

        def post(query: str, variables: dict[str, str]) -> dict[str, object]:
            assert "v0: variant" in query
            assert "v1: variant" in query
            assert variables == {
                "variant0": "1-100-A-T",
                "variant1": "2-200-C-G",
            }
            return {
                "data": {
                    "v0": _gnomad_payload("1-100-A-T")["data"]["variant"],
                    "v1": _gnomad_payload("2-200-C-G")["data"]["variant"],
                }
            }

        with mock.patch("famed.annotation_cache._post_gnomad_query", side_effect=post):
            payloads = _fetch_gnomad_batch(targets)

        assert payloads[0]["data"]["variant"]["variant_id"] == "1-100-A-T"
        assert payloads[1]["data"]["variant"]["variant_id"] == "2-200-C-G"

    def test_release_drift_is_recorded_without_requesting_variants(
        self, open_db: sqlite3.Connection
    ) -> None:
        _insert_clinvar_target(open_db, rsid="rs1")

        def fail_if_called(
            _targets: tuple[VariantIdentity, ...],
        ) -> tuple[dict[str, object], ...]:
            raise AssertionError("variant batches must not run after release drift")

        with pytest.raises(ValueError, match="release drift"):
            fetch_gnomad_data(
                open_db,
                batch_fetcher=fail_if_called,
                release_fetcher=lambda: "4.2",
            )

        meta = open_db.execute("SELECT * FROM annotation_meta WHERE dataset = 'gnomad'").fetchone()
        assert meta["last_attempt_status"] == "failed"
        assert "release drift" in meta["last_error"]
        assert open_db.execute("SELECT COUNT(*) FROM gnomad_frequencies").fetchone()[0] == 0
        status = get_annotation_status(Path(open_db.execute("PRAGMA database_list").fetchone()[2]))[
            "gnomad"
        ]
        assert status["status"] == "UNAVAILABLE"
        assert status["source_version"] is None
        assert status["refreshed_at"] is None

    def test_release_metadata_must_confirm_configured_release(self) -> None:
        response = io.BytesIO(b"<title>gnomAD v4.1.1 | gnomAD browser</title>")
        with mock.patch("famed.annotation_cache.urllib.request.urlopen", return_value=response):
            assert _fetch_gnomad_release() == GNOMAD_RELEASE

        wrong_response = io.BytesIO(b"<title>gnomAD v4.2 | gnomAD browser</title>")
        with mock.patch(
            "famed.annotation_cache.urllib.request.urlopen",
            return_value=wrong_response,
        ):
            with pytest.raises(ValueError, match="did not confirm"):
                _fetch_gnomad_release()

    def test_identity_mismatch_blocks_replacement_and_stale_rows_are_removed(
        self, open_db: sqlite3.Connection
    ) -> None:
        _insert_clinvar_target(open_db, rsid="rs1", alternate_allele="T")
        fetch_gnomad_data(open_db, fetcher=lambda variant_id: _gnomad_payload(variant_id))

        with pytest.raises(ValueError, match="identity mismatch"):
            fetch_gnomad_data(
                open_db,
                fetcher=lambda _variant_id: _gnomad_payload("1-100-A-G"),
            )
        assert (
            open_db.execute("SELECT variant_id FROM gnomad_frequencies").fetchone()[0]
            == "1-100-A-T"
        )

        open_db.execute("DELETE FROM clinvar_variants")
        _insert_clinvar_target(open_db, rsid="rs2", alternate_allele="G")
        fetch_gnomad_data(open_db, fetcher=lambda variant_id: _gnomad_payload(variant_id))
        assert [
            row[0]
            for row in open_db.execute(
                "SELECT variant_id FROM gnomad_frequencies ORDER BY variant_id"
            )
        ] == ["1-100-A-G"]


class TestGenccData:
    def test_stream_keeps_only_clingen_rows(self) -> None:
        rows = list(
            _stream_gencc_rows(
                _make_gencc_stream(
                    [
                        {},
                        {
                            "sgc_id": "SGC-OTHER",
                            "submitter_curie": "GENCC:000101",
                            "submitter_title": "Another submitter",
                        },
                    ]
                )
            )
        )
        assert [row["sgc_id"] for row in rows] == ["SGC-100001"]

    def test_fetch_is_idempotent_and_records_provenance(self, open_db: sqlite3.Connection) -> None:
        assert fetch_gencc_data(open_db, source_override=_make_gencc_stream()) == 1
        assert fetch_gencc_data(open_db, source_override=_make_gencc_stream()) == 1
        rows = open_db.execute("SELECT * FROM gencc_gene_disease").fetchall()
        assert len(rows) == 1
        assert rows[0]["version_number"] == 1
        meta = open_db.execute("SELECT * FROM annotation_meta WHERE dataset='gencc'").fetchone()
        assert meta["row_count"] == 1
        assert meta["source_url"] == GENCC_SUBMISSIONS_URL

    def test_rejects_legacy_or_malformed_contract(self, open_db: sqlite3.Connection) -> None:
        fetch_gencc_data(open_db, source_override=_make_gencc_stream())
        with pytest.raises(ValueError, match="missing required fields"):
            fetch_gencc_data(
                open_db,
                source_override=io.StringIO("uuid\tgene_symbol\nlegacy\tCFTR\n"),
            )
        assert open_db.execute("SELECT COUNT(*) FROM gencc_gene_disease").fetchone()[0] == 1

    def test_rejects_empty_clingen_subset_without_replacing_cache(
        self, open_db: sqlite3.Connection
    ) -> None:
        fetch_gencc_data(open_db, source_override=_make_gencc_stream())
        with pytest.raises(ValueError, match="no valid ClinGen"):
            fetch_gencc_data(
                open_db,
                source_override=_make_gencc_stream(
                    [
                        {
                            "submitter_curie": "GENCC:000101",
                            "submitter_title": "Another submitter",
                        }
                    ]
                ),
            )
        assert open_db.execute("SELECT COUNT(*) FROM gencc_gene_disease").fetchone()[0] == 1

    def test_resolves_exact_strong_identifier_match(self, open_db: sqlite3.Connection) -> None:
        fetch_gencc_data(open_db, source_override=_make_gencc_stream())
        result = resolve_gencc_inheritance(open_db, "hgnc:1884", "omim:219700")
        assert result.status == "resolved"
        assert result.moi_curie == "HP:0000007"
        assert result.source_ids == ("SGC-100001",)

    def test_does_not_fuzzy_match_disease_names(self, open_db: sqlite3.Connection) -> None:
        fetch_gencc_data(open_db, source_override=_make_gencc_stream())
        result = resolve_gencc_inheritance(open_db, "HGNC:1884", "OMIM:999999")
        assert result.status == "no_exact_match"

    def test_weak_or_conflicting_modes_stay_unresolved(self, open_db: sqlite3.Connection) -> None:
        fetch_gencc_data(
            open_db,
            source_override=_make_gencc_stream(
                [
                    {"classification_title": "Moderate"},
                ]
            ),
        )
        assert (
            resolve_gencc_inheritance(open_db, "HGNC:1884", "OMIM:219700").status
            == "insufficient_evidence"
        )

        fetch_gencc_data(
            open_db,
            source_override=_make_gencc_stream(
                [
                    {},
                    {
                        "sgc_id": "SGC-100002",
                        "moi_curie": "HP:0000006",
                        "moi_title": "Autosomal dominant",
                    },
                ]
            ),
        )
        assert (
            resolve_gencc_inheritance(open_db, "HGNC:1884", "OMIM:219700").status
            == "conflicting_modes"
        )

    def test_disputed_or_complex_inheritance_stays_unresolved(
        self, open_db: sqlite3.Connection
    ) -> None:
        fetch_gencc_data(
            open_db,
            source_override=_make_gencc_stream([{"classification_title": "Disputed Evidence"}]),
        )
        assert (
            resolve_gencc_inheritance(open_db, "HGNC:1884", "OMIM:219700").status
            == "conflicting_evidence"
        )

        fetch_gencc_data(
            open_db,
            source_override=_make_gencc_stream(
                [{"moi_curie": "HP:0001417", "moi_title": "X-linked"}]
            ),
        )
        assert (
            resolve_gencc_inheritance(open_db, "HGNC:1884", "OMIM:219700").status
            == "complex_inheritance"
        )


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


class TestGetCpicRecommendations:
    def _populate(self, tmp_ref_db: Path) -> None:
        with mock.patch(
            "famed.annotation_cache._fetch_cpic_recommendations",
            return_value=[
                r for r in (_extract_cpic_row(item) for item in _CPIC_SAMPLE) if r is not None
            ],
        ):
            conn = open_annotation_db(tmp_ref_db)
            fetch_cpic_data(conn)
            conn.close()

    def test_returns_by_gene(self, tmp_ref_db: Path) -> None:
        self._populate(tmp_ref_db)
        rows = get_cpic_recommendations(tmp_ref_db, gene="CYP2C19")
        assert len(rows) == 1
        assert rows[0]["drug"] == "clopidogrel"

    def test_returns_by_gene_and_drug(self, tmp_ref_db: Path) -> None:
        self._populate(tmp_ref_db)
        rows = get_cpic_recommendations(tmp_ref_db, gene="CYP2D6", drug="codeine")
        assert len(rows) == 1
        assert rows[0]["phenotype"] == "Poor Metabolizer"

    def test_returns_empty_for_unknown_gene(self, tmp_ref_db: Path) -> None:
        self._populate(tmp_ref_db)
        assert get_cpic_recommendations(tmp_ref_db, gene="UNKNOWNGENE") == []

    def test_returns_empty_when_db_absent(self, tmp_path: Path) -> None:
        ref_db = _reference_db_path(tmp_path)
        assert get_cpic_recommendations(ref_db, gene="CYP2C19") == []


class TestGetClinvarSignificance:
    def _populate(self, tmp_ref_db: Path) -> None:
        conn = open_annotation_db(tmp_ref_db)
        fetch_clinvar_data(conn, source_override=_make_clinvar_stream())
        conn.close()

    def test_found_with_rs_prefix(self, tmp_ref_db: Path) -> None:
        self._populate(tmp_ref_db)
        result = get_clinvar_significance(tmp_ref_db, "rs2108622")
        assert result is not None
        assert result["significance"] == "Pathogenic"
        assert result["gene"] == "CFTR"

    def test_found_without_rs_prefix(self, tmp_ref_db: Path) -> None:
        self._populate(tmp_ref_db)
        result = get_clinvar_significance(tmp_ref_db, "2108622")
        assert result is not None
        assert result["significance"] == "Pathogenic"

    def test_returns_none_for_unknown_rsid(self, tmp_ref_db: Path) -> None:
        self._populate(tmp_ref_db)
        assert get_clinvar_significance(tmp_ref_db, "rs9999999") is None

    def test_returns_none_when_db_absent(self, tmp_path: Path) -> None:
        ref_db = _reference_db_path(tmp_path)
        assert get_clinvar_significance(ref_db, "rs2108622") is None


# ---------------------------------------------------------------------------
# get_annotation_status
# ---------------------------------------------------------------------------


class TestGetAnnotationStatus:
    def test_missing_when_db_absent(self, tmp_path: Path) -> None:
        ref_db = _reference_db_path(tmp_path)
        status = get_annotation_status(ref_db)
        assert status["cpic"]["status"] == "MISSING"
        assert status["clinvar"]["status"] == "MISSING"
        assert status["cpic"]["row_count"] == 0

    def test_missing_when_table_empty(self, tmp_ref_db: Path) -> None:
        open_annotation_db(tmp_ref_db).close()
        status = get_annotation_status(tmp_ref_db)
        assert status["cpic"]["status"] == "MISSING"
        assert status["clinvar"]["status"] == "MISSING"

    def test_ok_after_cpic_refresh(self, tmp_ref_db: Path) -> None:
        conn = open_annotation_db(tmp_ref_db)
        with mock.patch(
            "famed.annotation_cache._fetch_cpic_recommendations",
            return_value=[
                r for r in (_extract_cpic_row(item) for item in _CPIC_SAMPLE) if r is not None
            ],
        ):
            fetch_cpic_data(conn)
        conn.close()
        status = get_annotation_status(tmp_ref_db)
        assert status["cpic"]["status"] == "OK"
        assert status["cpic"]["row_count"] == 3
        assert status["clinvar"]["status"] == "MISSING"

    def test_ok_after_both_refreshed(self, tmp_ref_db: Path) -> None:
        conn = open_annotation_db(tmp_ref_db)
        with mock.patch(
            "famed.annotation_cache._fetch_cpic_recommendations",
            return_value=[_extract_cpic_row(_CPIC_SAMPLE[0])],
        ):
            fetch_cpic_data(conn)
        fetch_clinvar_data(conn, source_override=_make_clinvar_stream())
        conn.close()
        status = get_annotation_status(tmp_ref_db)
        assert status["cpic"]["status"] == "OK"
        assert status["clinvar"]["status"] == "OK"
        assert status["clinvar"]["row_count"] == 2


# ---------------------------------------------------------------------------
# refresh_annotation_dataset
# ---------------------------------------------------------------------------


class TestRefreshAnnotationDataset:
    def test_cpic_refresh(self, tmp_ref_db: Path) -> None:
        with mock.patch(
            "famed.annotation_cache._fetch_cpic_recommendations",
            return_value=[_extract_cpic_row(_CPIC_SAMPLE[0])],
        ):
            count = refresh_annotation_dataset(tmp_ref_db, "cpic")
        assert count == 1
        assert tmp_ref_db.exists()

    def test_raises_for_unknown_dataset(self, tmp_ref_db: Path) -> None:
        with pytest.raises(ValueError, match="Unknown annotation dataset"):
            refresh_annotation_dataset(tmp_ref_db, "unknown")

    def test_gencc_refresh(self, tmp_ref_db: Path) -> None:
        with mock.patch(
            "famed.annotation_cache._http_stream",
            return_value=io.BytesIO(_make_gencc_stream().getvalue().encode()),
        ):
            assert refresh_annotation_dataset(tmp_ref_db, "gencc") == 1


# ---------------------------------------------------------------------------
# reference_cache integration — SUPPORTED_DATASETS and cache_status
# ---------------------------------------------------------------------------


class TestReferenceCacheIntegration:
    def test_cpic_and_clinvar_in_supported_datasets(self) -> None:
        from famed.reference_cache import SUPPORTED_DATASETS

        assert "cpic" in SUPPORTED_DATASETS
        assert "clinvar" in SUPPORTED_DATASETS
        assert "gencc" in SUPPORTED_DATASETS
        assert SUPPORTED_DATASETS["cpic"]["annotation_db"] is True
        assert SUPPORTED_DATASETS["clinvar"]["annotation_db"] is True
        assert SUPPORTED_DATASETS["gencc"]["annotation_db"] is True

    def test_cache_status_includes_annotation_datasets(self, tmp_path: Path) -> None:
        from famed.reference_cache import cache_status

        status = cache_status(tmp_path)
        assert "cpic" in status
        assert "clinvar" in status
        assert "gencc" in status
        assert status["cpic"]["status"] == "MISSING"

    def test_cache_status_ok_after_cpic_refresh(self, tmp_path: Path) -> None:
        from famed.annotation_cache import _reference_db_path
        from famed.reference_cache import cache_status

        ref_db = _reference_db_path(tmp_path)
        conn = open_annotation_db(ref_db)
        with mock.patch(
            "famed.annotation_cache._fetch_cpic_recommendations",
            return_value=[_extract_cpic_row(_CPIC_SAMPLE[0])],
        ):
            fetch_cpic_data(conn)
        conn.close()

        status = cache_status(tmp_path)
        assert status["cpic"]["status"] == "OK"
        assert status["cpic"]["row_count"] == 1
