"""Tests for health advisory report generation."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from famed.annotation_cache import open_annotation_db
from famed.generate_health_advisory import main as generate_advisory_main
from famed.health_advisory import (
    assess_genetic_risk_flags,
    build_genetic_risk_flags,
    persist_synthesized_insights,
    write_health_advisory,
    write_health_advisory_unattended,
)
from famed.lab_sync import write_category_index_note
from famed.persist_synthesized_insights import main as persist_insights_main
from famed.person_sync import validate_note_front_matter
from famed.setup import SCHEMA_SQL


def _user_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_genotype(
    conn: sqlite3.Connection,
    person_id: str,
    rsid: str,
    allele1: str,
    allele2: str,
    *,
    source_path: str = "data/processed/dummy_alice/Genes/genome.txt",
) -> None:
    conn.execute(
        """
        INSERT INTO genotypes
            (id, person_id, rsid, chromosome, position, allele1, allele2, provider, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{person_id}-{rsid}-{source_path}",
            person_id,
            rsid,
            "1",
            100,
            allele1,
            allele2,
            "23andme",
            source_path,
        ),
    )


def _insert_clinvar(
    ref_db_path: Path,
    *,
    rsid: str,
    gene: str,
    alternate_allele: str,
    significance: str = "Pathogenic",
    condition: str = "Example condition",
    review_status: str = "reviewed by expert panel",
    gene_curie: str | None = None,
    phenotype_ids: str | None = None,
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO clinvar_variants
            (rsid, variation_id, significance, condition, review_status,
             last_evaluated, gene, gene_curie, phenotype_ids, chromosome, position,
             reference_allele, alternate_allele)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rsid,
            "var1",
            significance,
            condition,
            review_status,
            "2021-01-01",
            gene,
            gene_curie,
            phenotype_ids,
            "1",
            100,
            "A",
            alternate_allele,
        ),
    )
    conn.commit()
    conn.close()


def _insert_gnomad_frequency(
    ref_db_path: Path,
    *,
    alternate_allele: str = "T",
    status: str = "available",
    unavailable_reason: str | None = None,
    joint_ac: int | None = 2,
    joint_an: int | None = 200,
    exome_ac: int | None = 1,
    exome_an: int | None = 100,
    genome_ac: int | None = 1,
    genome_an: int | None = 100,
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO gnomad_frequencies (
            chromosome, position, reference_allele, alternate_allele,
            variant_id, source_dataset, source_release, status, unavailable_reason,
            joint_ac, joint_an, joint_af, exome_ac, exome_an, exome_af,
            genome_ac, genome_an, genome_af, variant_filters, exome_filters,
            genome_filters, joint_filters, discordance_test, discordance_p_value,
            retrieved_at
        ) VALUES (
            '1', 100, 'A', ?, ?, 'gnomad_r4', '4.1.1', ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', '[]', ?,
            ?, ?, '2023-08-05T00:00:00+00:00'
        )
        """,
        (
            alternate_allele,
            f"1-100-A-{alternate_allele}",
            status,
            unavailable_reason,
            joint_ac,
            joint_an,
            joint_ac / joint_an if joint_ac is not None and joint_an else None,
            exome_ac,
            exome_an,
            exome_ac / exome_an if exome_ac is not None and exome_an else None,
            genome_ac,
            genome_an,
            genome_ac / genome_an if genome_ac is not None and genome_an else None,
            '["exome_genome_frequency_discordance"]' if status == "discordant" else "[]",
            "contingency_table_test" if status == "discordant" else None,
            1e-8 if status == "discordant" else None,
        ),
    )
    conn.commit()
    conn.close()


def _insert_gencc(
    ref_db_path: Path,
    *,
    sgc_id: str = "SGC-100001",
    gene_curie: str = "HGNC:1884",
    disease_curie: str = "MONDO:0009061",
    disease_original_curie: str = "OMIM:219700",
    classification_title: str = "Definitive",
    moi_curie: str = "HP:0000007",
    moi_title: str = "Autosomal recessive",
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO gencc_gene_disease
            (sgc_id, version_number, gene_curie, gene_symbol, disease_curie,
             disease_original_curie, classification_curie, classification_title,
             moi_curie, moi_title, submitter_curie, submitter_title,
             submitted_as_date, public_report_url, assertion_criteria_url)
        VALUES (?, 1, ?, 'CFTR', ?, ?, 'GENCC:100001', ?, ?, ?,
                'GENCC:000102', 'ClinGen', '2023-01-01',
                'https://clinicalgenome.org/example', 'https://clinicalgenome.org/criteria')
        """,
        (
            sgc_id,
            gene_curie,
            disease_curie,
            disease_original_curie,
            classification_title,
            moi_curie,
            moi_title,
        ),
    )
    conn.commit()
    conn.close()


def _write_person_note(
    vault_root: Path,
    person_id: str = "dummy_alice",
    birth_year: int = 1970,
    sex: str = "female",
) -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    front_matter = {
        "person_id": person_id,
        "record_type": "person",
        "display_name": "Dummy Alice Example",
        "birth_year": birth_year,
        "sex": sex,
        "relationships": [],
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    person_note.write_text(f"---\n{yaml_block}\n---\n\n# Dummy Alice Example\n", encoding="utf-8")


def _insert_lab_result(
    conn: sqlite3.Connection,
    person_id: str,
    test_name: str,
    observed_at: str,
    *,
    value: float = 1.0,
    unit: str = "",
    ref_low: float | None = None,
    ref_high: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO lab_results
            (id, person_id, loinc_code, test_name, value, unit,
             reference_range_low, reference_range_high, observed_at, source_type)
        VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, 'manual')
        """,
        (
            f"{person_id}-{test_name}-{observed_at}",
            person_id,
            test_name,
            value,
            unit,
            ref_low,
            ref_high,
            observed_at,
        ),
    )


def _write_report(
    reports_dir: Path,
    filename: str,
    report_type: str,
    heading: str,
    body: str,
    *,
    extra_front_matter: dict[str, str] | None = None,
) -> None:
    front_matter: dict[str, object] = {
        "person_id": reports_dir.parent.name,
        "record_type": "report",
        "report_type": report_type,
        "generated_at": "2023-05-21T00:00:00Z",
    }
    front_matter.update(extra_front_matter or {})
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / filename).write_text(
        f"---\n{yaml_block}\n---\n\n# {heading}\n\n[[_index|<- Reports]]\n\n{body}\n",
        encoding="utf-8",
    )


def _scaffold_reports(vault_root: Path, birth_year: int = 1970) -> Path:
    _write_person_note(vault_root, birth_year=birth_year)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    _write_report(
        reports_dir,
        "dna-profile-summary.md",
        "dna_profile_summary",
        "DNA Profile Summary",
        "## Imported Files\n\n- One local DNA file summarized.\n",
    )
    _write_report(
        reports_dir,
        "current-medications.md",
        "current_medications",
        "Current Medications",
        "## Prescriptions\n\n| Medication | Dose |\n| --- | --- |\n| Atorvastatin | 10 mg |\n",
    )
    _write_report(
        reports_dir,
        "medical-history-summary.md",
        "medical_history_summary",
        "Medical History Summary",
        "## Diagnoses\n\n- 2022-03-01: Hypertension. Status: needs_review\n",
    )
    _write_report(
        reports_dir,
        "latest-lab-results.md",
        "latest_lab_results",
        "Latest Lab Results",
        "## Results\n\n| test_name | latest_value |\n| --- | --- |\n| LDL | 140 |\n",
    )
    _write_report(
        reports_dir,
        "flagged-for-discussion.md",
        "flagged_for_discussion",
        "Flagged for discussion",
        "## Out of Range\n\n_No out-of-range lab results in recent panels._\n"
        "\n## Trending\n\n_No notably trending lab results in recent panels._\n",
    )
    _write_report(
        reports_dir,
        "carrier-screening.md",
        "carrier_screening",
        "Carrier Screening",
        "## Findings\n\n- No reportable carrier findings.\n",
    )
    _write_report(
        reports_dir,
        "pgx-coverage.md",
        "pgx_coverage",
        "PGx Coverage",
        "## Coverage\n\n- Phenotype not interpreted.\n",
    )
    write_category_index_note(
        reports_dir, "dummy_alice", "Reports", display_name="Dummy Alice Example"
    )
    return reports_dir


# ---------------------------------------------------------------------------
# build_genetic_risk_flags
# ---------------------------------------------------------------------------


def test_build_genetic_risk_flags_matches_pathogenic_variant(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs123",
        gene="BRCA1",
        alternate_allele="T",
        condition="Hereditary breast and ovarian cancer",
        review_status="reviewed by expert panel",
    )
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert len(flags) == 1
    assert flags[0].rsid == "rs123"
    assert flags[0].gene == "BRCA1"
    assert "breast" in flags[0].condition.casefold()
    assert flags[0].significance == "Pathogenic"
    assert flags[0].evidence_tier == "E2"


def test_build_genetic_risk_flags_heterozygous_zygosity(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="CFTR", alternate_allele="T")
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")  # one alt copy

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].zygosity == "heterozygous"


def test_build_genetic_risk_flags_homozygous_zygosity(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="CFTR", alternate_allele="T")
    _insert_genotype(conn, "dummy_alice", "rs123", "T", "T")  # two alt copies

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].zygosity == "homozygous"


def test_build_genetic_risk_flags_surfaces_variation_id(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="CFTR", alternate_allele="T")
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].variation_id == "var1"


def test_build_genetic_risk_flags_migrates_pre_inheritance_cache(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    legacy = sqlite3.connect(ref_db)
    legacy.execute(
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
            position INTEGER,
            reference_allele TEXT,
            alternate_allele TEXT
        )
        """
    )
    legacy.execute(
        """
        INSERT INTO clinvar_variants VALUES
            ('rs123', 'var1', 'Pathogenic', 'Example condition',
             'reviewed by expert panel', '2021-01-01', 'CFTR', '1', 100, 'A', 'T')
        """
    )
    legacy.commit()
    legacy.close()
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert len(flags) == 1
    assert flags[0].inheritance_mode is None
    migrated = sqlite3.connect(ref_db)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(clinvar_variants)")}
    migrated.close()
    assert {"gene_curie", "phenotype_ids"} <= columns


def test_build_genetic_risk_flags_refines_exact_recessive_match(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs123",
        gene="CFTR",
        gene_curie="HGNC:1884",
        phenotype_ids="MedGen:C0010674,OMIM:219700",
        alternate_allele="T",
    )
    _insert_gencc(ref_db)
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].inheritance_mode == "HP:0000007"
    assert flags[0].inheritance_source_ids == ("SGC-100001",)


def test_build_genetic_risk_flags_refines_exact_dominant_match(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs123",
        gene="CFTR",
        gene_curie="HGNC:1884",
        phenotype_ids="MONDO:0009061",
        alternate_allele="T",
    )
    _insert_gencc(
        ref_db,
        moi_curie="HP:0000006",
        moi_title="Autosomal dominant",
    )
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].inheritance_mode == "HP:0000006"


def test_build_genetic_risk_flags_evidence_tier_e2_multiple_submitters(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs111",
        gene="GeneA",
        alternate_allele="G",
        review_status="criteria provided, multiple submitters, no conflicts",
    )
    _insert_genotype(conn, "dummy_alice", "rs111", "A", "G")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].evidence_tier == "E2"


def test_build_genetic_risk_flags_evidence_tier_e3_single_submitter(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs222",
        gene="GeneB",
        alternate_allele="C",
        review_status="criteria provided, single submitter",
    )
    _insert_genotype(conn, "dummy_alice", "rs222", "A", "C")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags[0].evidence_tier == "E3"


def test_build_genetic_risk_flags_evidence_tier_e4_no_assertion(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs333",
        gene="GeneC",
        alternate_allele="T",
        review_status="no assertion criteria provided",
    )
    _insert_genotype(conn, "dummy_alice", "rs333", "A", "T")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags == []


def test_build_genetic_risk_flags_no_flag_when_alternate_allele_absent(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="CFTR", alternate_allele="G")
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "A")  # G not in (A, A)

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags == []


def test_build_genetic_risk_flags_no_flag_for_benign_variant(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="CFTR", alternate_allele="G", significance="Benign")
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "G")

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags == []


def test_build_genetic_risk_flags_returns_empty_without_ref_db(tmp_path: Path) -> None:
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "G")

    flags = build_genetic_risk_flags(conn, tmp_path / "missing.db", "dummy_alice")

    assert flags == []


def test_build_genetic_risk_flags_returns_empty_without_genotype_data(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="CFTR", alternate_allele="G")
    # no genotype rows

    flags = build_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert flags == []


def test_build_genetic_risk_flags_deduplicates_same_condition(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    # Two rsIDs map to the same base condition
    _insert_clinvar(
        ref_db,
        rsid="rs111",
        gene="BRCA1",
        alternate_allele="T",
        condition="Hereditary breast cancer",
    )
    _insert_clinvar(
        ref_db,
        rsid="rs222",
        gene="BRCA1",
        alternate_allele="G",
        condition="Hereditary breast cancer",
    )
    _insert_genotype(conn, "dummy_alice", "rs111", "A", "T")
    _insert_genotype(conn, "dummy_alice", "rs222", "C", "G")

    assessment = assess_genetic_risk_flags(conn, ref_db, "dummy_alice")

    conditions = [flag.condition for flag in assessment.flags]
    assert conditions.count("Hereditary breast cancer") == 1
    assert assessment.candidate_matches == 2
    assert assessment.accepted == 1
    assert assessment.duplicate_condition == 1


def test_build_genetic_risk_flags_excludes_low_value_conflicting_or_placeholder_rows(
    tmp_path: Path,
) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs-good",
        gene="CFTR",
        alternate_allele="T",
        condition="Cystic fibrosis",
        significance="Pathogenic",
        review_status="criteria provided, single submitter",
    )
    _insert_clinvar(
        ref_db,
        rsid="rs-conflict",
        gene="GENE1",
        alternate_allele="G",
        condition="Familial Mediterranean fever",
        significance="Conflicting classifications of pathogenicity",
        review_status="criteria provided, conflicting classifications",
    )
    _insert_clinvar(
        ref_db,
        rsid="rs-placeholder",
        gene="GENE2",
        alternate_allele="C",
        condition="not specified",
        significance="Pathogenic",
        review_status="criteria provided, single submitter",
    )
    _insert_clinvar(
        ref_db,
        rsid="rs-susceptibility",
        gene="GENE3",
        alternate_allele="A",
        condition="Leprosy, susceptibility to, 1",
        significance="Pathogenic",
        review_status="criteria provided, single submitter",
    )
    _insert_clinvar(
        ref_db,
        rsid="rs-no-assertion",
        gene="GENE4",
        alternate_allele="T",
        condition="Rare syndrome",
        significance="Pathogenic",
        review_status="no assertion criteria provided",
    )
    _insert_genotype(conn, "dummy_alice", "rs-good", "A", "T")
    _insert_genotype(conn, "dummy_alice", "rs-conflict", "A", "G")
    _insert_genotype(conn, "dummy_alice", "rs-placeholder", "A", "C")
    _insert_genotype(conn, "dummy_alice", "rs-susceptibility", "G", "A")
    _insert_genotype(conn, "dummy_alice", "rs-no-assertion", "A", "T")

    assessment = assess_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert [flag.rsid for flag in assessment.flags] == ["rs-good"]
    assert assessment.flags[0].condition == "Cystic fibrosis"
    assert assessment.candidate_matches == 5
    assert assessment.accepted == 1
    assert assessment.conflicting_classification == 1
    assert assessment.insufficient_review == 1
    assert assessment.policy_excluded == 2
    assert assessment.candidate_matches == (
        assessment.accepted
        + assessment.conflicting_classification
        + assessment.insufficient_review
        + assessment.policy_excluded
        + assessment.duplicate_condition
        + assessment.display_limit
    )


def test_assess_genetic_risk_flags_accounts_for_display_limit(tmp_path: Path) -> None:
    conn = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    for rsid, condition in (("rs111", "Condition one"), ("rs222", "Condition two")):
        _insert_clinvar(
            ref_db,
            rsid=rsid,
            gene="GENE1",
            alternate_allele="T",
            condition=condition,
        )
        _insert_genotype(conn, "dummy_alice", rsid, "A", "T")

    assessment = assess_genetic_risk_flags(conn, ref_db, "dummy_alice", top_n=1)

    assert assessment.candidate_matches == 2
    assert assessment.accepted == 1
    assert assessment.display_limit == 1


# ---------------------------------------------------------------------------
# write_health_advisory — content and structure
# ---------------------------------------------------------------------------


def test_write_health_advisory_creates_report_with_valid_front_matter(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs123",
        gene="BRCA1",
        alternate_allele="T",
        condition="Hereditary breast cancer",
    )
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert report_path == reports_dir / "health-advisory.md"
    assert report_path.exists()
    assert flag_count == 1
    assert validate_note_front_matter(report_path) == []
    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["report_type"] == "health_advisory"
    assert front_matter["person_id"] == "dummy_alice"
    assert (
        "Records/dummy_alice/Reports/dna-profile-summary.md" not in front_matter["source_reports"]
    )
    assert "[!note] Genetic evidence disposition" in content
    assert "Candidate allele matches: 1" in content
    assert "Accepted flags: 1" in content


def test_write_health_advisory_discussion_prompts_skip_low_value_clinvar_noise(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs-good",
        gene="BRCA1",
        alternate_allele="T",
        condition="Hereditary breast cancer",
        significance="Pathogenic",
        review_status="reviewed by expert panel",
    )
    _insert_clinvar(
        ref_db,
        rsid="rs-bad",
        gene="GENE2",
        alternate_allele="G",
        condition="not provided",
        significance="Conflicting classifications of pathogenicity",
        review_status="criteria provided, conflicting classifications",
    )
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs-good", "A", "T")
    _insert_genotype(conn, "dummy_alice", "rs-bad", "A", "G")

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert flag_count == 1
    content = report_path.read_text(encoding="utf-8")
    assert "Hereditary breast cancer" in content
    assert "Discuss ClinVar variant rs-good" in content
    assert "rs-bad" not in content
    assert "not provided" not in content
    assert "Excluded — established report policy: 1" in content


def test_write_health_advisory_explains_filtered_conflicting_candidate(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs-conflict",
        gene="GENE1",
        alternate_allele="G",
        condition="Example inherited condition",
        significance="Conflicting classifications of pathogenicity",
        review_status="criteria provided, conflicting classifications",
    )
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs-conflict", "A", "G")

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert flag_count == 0
    assert "No variants met the conservative ClinVar flag criteria" in content
    assert "Candidate allele matches: 1" in content
    assert "Excluded — conflicting ClinVar classification: 1" in content
    assert "rs-conflict" not in content
    assert "No out-of-range results or genetic risk flags to generate prompts from" in content


def test_write_health_advisory_accounts_for_eligible_matches_beyond_limit(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    conn = _user_conn()
    for index in range(21):
        rsid = f"rs{index:03d}"
        _insert_clinvar(
            ref_db,
            rsid=rsid,
            gene="GENE1",
            alternate_allele="T",
            condition=f"Example condition {index}",
        )
        _insert_genotype(conn, "dummy_alice", rsid, "A", "T")

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert flag_count == 20
    assert "Candidate allele matches: 21" in content
    assert "Eligible but beyond the display limit: 1" in content


def test_write_health_advisory_includes_disclaimer(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "[!warning] Clinical Safety" in content
    assert "population-level risk observations" in content
    assert "not individual diagnoses or treatment plans" in content
    assert "conversation starter" in content
    assert "healthcare professional" in content


def test_write_health_advisory_renders_flag_table_with_evidence_tier(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs456",
        gene="CFTR",
        alternate_allele="G",
        condition="Cystic fibrosis",
        review_status="criteria provided, multiple submitters, no conflicts",
    )
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs456", "A", "G")

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert flag_count == 1
    content = report_path.read_text(encoding="utf-8")
    assert "rs456" in content
    assert "CFTR" in content
    assert "Cystic fibrosis" in content
    assert "E2" in content
    assert "moderate" in content


def test_write_health_advisory_renders_zygosity_and_clinvar_id(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs456",
        gene="CFTR",
        alternate_allele="G",
        condition="Cystic fibrosis",
        review_status="criteria provided, multiple submitters, no conflicts",
    )
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs456", "A", "G")  # heterozygous

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    # Zygosity column populated and the carrier-vs-risk reframing note present.
    assert "Heterozygous (1 copy)" in content
    assert "carrier" in content.casefold()
    # ClinVar Variation ID surfaced (accession), closing the evidence-model.md drift.
    assert "ClinVar ID" in content
    assert "var1" in content


def test_write_health_advisory_renders_exact_local_gnomad_frequency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs456",
        gene="GENE1",
        alternate_allele="T",
        condition="Example inherited condition",
    )
    _insert_gnomad_frequency(ref_db)
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs456", "A", "T")

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("report rendering must not query gnomAD")

    monkeypatch.setattr("urllib.request.urlopen", fail_network)
    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 8, 5, tzinfo=UTC),
    )

    assert flag_count == 1
    content = report_path.read_text(encoding="utf-8")
    assert "Population Frequency" in content
    assert "1% (AC 2 / AN 200)" in content
    assert "exomes 1% (AC 1 / AN 100)" in content
    assert "genomes 1% (AC 1 / AN 100)" in content
    assert "gnomAD v4.1.1" in content


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("unavailable", "source_filtered", "Unavailable — gnomAD v4.1.1"),
        (
            "discordant",
            "source_flagged_discordance",
            "Discordant — gnomAD v4.1.1 source flag; frequency not interpreted",
        ),
    ],
)
def test_write_health_advisory_labels_non_interpretable_gnomad_frequency(
    tmp_path: Path,
    status: str,
    reason: str,
    expected: str,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs456",
        gene="GENE1",
        alternate_allele="T",
        condition="Example inherited condition",
    )
    _insert_gnomad_frequency(
        ref_db,
        status=status,
        unavailable_reason=reason,
    )
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs456", "A", "T")

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 8, 5, tzinfo=UTC),
    )

    assert flag_count == 1
    content = report_path.read_text(encoding="utf-8")
    assert expected in content
    assert "Candidate allele matches: 1" in content
    assert "Accepted flags: 1" in content


def test_health_advisory_requires_exact_gnomad_allele_identity(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs456",
        gene="GENE1",
        alternate_allele="T",
        condition="Example inherited condition",
    )
    _insert_gnomad_frequency(ref_db, alternate_allele="G")
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs456", "A", "T")

    assessment = assess_genetic_risk_flags(conn, ref_db, "dummy_alice")

    assert assessment.accepted == 1
    assert assessment.flags[0].frequency_status == "unavailable"
    assert assessment.flags[0].frequency_reason == "not_cached"


@pytest.mark.parametrize(
    ("moi_curie", "moi_title", "allele1", "allele2", "expected"),
    [
        (
            "HP:0000007",
            "Autosomal recessive",
            "A",
            "G",
            "Carrier-compatible observation (autosomal recessive inheritance)",
        ),
        (
            "HP:0000007",
            "Autosomal recessive",
            "G",
            "G",
            "Two-copy risk-associated observation (autosomal recessive inheritance)",
        ),
        (
            "HP:0000006",
            "Autosomal dominant",
            "A",
            "G",
            "Risk-associated observation (autosomal dominant inheritance)",
        ),
    ],
)
def test_write_health_advisory_renders_inheritance_refined_gencc_evidence(
    tmp_path: Path,
    moi_curie: str,
    moi_title: str,
    allele1: str,
    allele2: str,
    expected: str,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(
        ref_db,
        rsid="rs456",
        gene="CFTR",
        gene_curie="HGNC:1884",
        phenotype_ids="OMIM:219700",
        alternate_allele="G",
        condition="Cystic fibrosis",
    )
    _insert_gencc(ref_db, moi_curie=moi_curie, moi_title=moi_title)
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs456", allele1, allele2)

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert expected in content
    assert "GenCC SGC-100001" in content
    assert "confirmatory clinical testing" in content


def test_write_health_advisory_no_flags_message_when_no_genotype(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    ref_db = tmp_path / "famed-reference.db"
    _insert_clinvar(ref_db, rsid="rs123", gene="BRCA1", alternate_allele="T")
    conn = _user_conn()  # no genotype rows

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert flag_count == 0
    content = report_path.read_text(encoding="utf-8")
    assert "[!info] Genetic evidence not evaluated" in content
    assert "No normalized genotype data is available" in content
    assert "unknown-data state, not a negative result" in content
    assert "No variants met the conservative ClinVar flag criteria" not in content


@pytest.mark.parametrize("empty_cache", [False, True])
def test_write_health_advisory_marks_missing_reference_as_not_evaluated(
    tmp_path: Path,
    empty_cache: bool,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    _insert_genotype(conn, "dummy_alice", "rs123", "A", "T")
    ref_db = tmp_path / "reference.db"
    if empty_cache:
        open_annotation_db(ref_db).close()

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=ref_db,
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert flag_count == 0
    assert "[!warning] Genetic evidence not evaluated" in content
    assert "ClinVar reference cache is missing or empty" in content
    assert "unknown-reference state, not a negative result" in content


def test_write_health_advisory_renders_age_appropriate_uspstf_screening(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    # birth_year=1970 → age ~56; Hypertension screening applies for all adults 18+
    _scaffold_reports(vault_root, birth_year=1970)
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Preventive Screening" in content
    assert "Hypertension screening" in content
    assert "No birth year available" not in content


# ---------------------------------------------------------------------------
# Preventive Screening Currency
# ---------------------------------------------------------------------------


def test_colorectal_screening_annotated_current_from_explicit_followup(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    _write_history_note(
        vault_root,
        "dummy_alice",
        [
            {
                "description": "Colonoscopy biopsy results showed adenoma and "
                "hyperplastic polyps; no cancer",
                "category": "diagnosis",
                "recorded_date": "2023-03-20",
            },
            {
                "description": "Surveillance colonoscopy recommended in 5 years",
                "category": "follow_up",
                "recorded_date": "2023-03-20",
            },
        ],
        filename="2023-03-20-colonoscopy-history-review.md",
    )
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Colorectal cancer screening**" in content
    assert "last done 2023-03-20; not due again until ~2028" in content


def test_colorectal_screening_annotated_current_from_default_interval_with_polyp(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    _write_history_note(
        vault_root,
        "dummy_alice",
        [
            {
                "description": "Colonoscopy showed adenoma and hyperplastic polyps; biopsy benign",
                "category": "diagnosis",
                "recorded_date": "2021-01-15",
            },
        ],
        filename="2021-01-15-colonoscopy-history-review.md",
    )
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Colorectal cancer screening**" in content
    # No polyp follow-up interval given -> default to 5 years when polyps were found.
    assert "last done 2021-01-15; not due again until ~2026" in content


def test_colorectal_screening_annotated_current_from_default_interval_without_polyp(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    _write_history_note(
        vault_root,
        "dummy_alice",
        [
            {
                "description": "Colonoscopy (routine)",
                "category": "procedure",
                "recorded_date": "2019-12-20",
            },
            {
                "description": "Colon cancer screening",
                "category": "screening",
                "recorded_date": "2019-12-20",
            },
        ],
        filename="2019-12-20-colonoscopy-history-review.md",
    )
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Colorectal cancer screening**" in content
    # No polyps found -> default to 10 years.
    assert "last done 2019-12-20; not due again until ~2029" in content


def test_colorectal_screening_not_annotated_when_followup_interval_elapsed(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    _write_history_note(
        vault_root,
        "dummy_alice",
        [
            {
                "description": "Polypectomy — adenoma(s) removed during colonoscopy",
                "category": "procedure",
                "recorded_date": "2017-01-10",
            },
            {
                "description": "Repeat colonoscopy recommended in 3 years",
                "category": "follow_up",
                "recorded_date": "2017-01-10",
            },
        ],
        filename="2017-01-10-colonoscopy-history-review.md",
    )
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Colorectal cancer screening**: colonoscopy or stool-based test\n" in content


def test_lipid_screening_annotated_current_from_recent_lab(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    conn = _user_conn()
    _insert_lab_result(conn, "dummy_alice", "Cholesterol", "2020-01-01")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Lipid screening**" in content
    assert "last done 2020-01-01; not due again until ~2025" in content


def test_diabetes_screening_not_annotated_when_lab_interval_elapsed(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    conn = _user_conn()
    _insert_lab_result(conn, "dummy_alice", "Hemoglobin A1c", "2017-12-31")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Diabetes screening**: HbA1c or fasting glucose\n" in content


def test_hepatitis_c_screening_annotated_already_done(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    conn = _user_conn()
    _insert_lab_result(conn, "dummy_alice", "Hepatitis C Ab", "2018-07-21")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "**Hepatitis C screening**" in content
    assert "already done 2018-07-21; one-time screening" in content


def test_write_health_advisory_renders_out_of_range_lab_observations(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    for fn, rt in [
        ("dna-profile-summary.md", "dna_profile_summary"),
        ("current-medications.md", "current_medications"),
        ("medical-history-summary.md", "medical_history_summary"),
        ("latest-lab-results.md", "latest_lab_results"),
        ("flagged-for-discussion.md", "flagged_for_discussion"),
        ("carrier-screening.md", "carrier_screening"),
        ("pgx-coverage.md", "pgx_coverage"),
    ]:
        _write_report(reports_dir, fn, rt, "Report", "_No data._\n")
    write_category_index_note(
        reports_dir, "dummy_alice", "Reports", display_name="Dummy Alice Example"
    )
    conn = _user_conn()
    # Two readings so there is a previous value and a trend can be detected
    _insert_lab_result(
        conn,
        "dummy_alice",
        "LDL",
        "2023-03-01",
        value=140.0,
        unit="mg/dL",
        ref_high=100.0,
    )
    _insert_lab_result(
        conn,
        "dummy_alice",
        "LDL",
        "2023-04-20",
        value=155.0,
        unit="mg/dL",
        ref_high=100.0,
    )
    conn.commit()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Lab Observations" in content
    assert "LDL" in content
    assert "155.0 mg/dL" in content
    assert "Previous:** 140.0" in content
    assert "Date:** 2023-04-20" in content


def test_write_health_advisory_omits_lab_section_when_no_flagged_labs(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    for fn, rt in [
        ("dna-profile-summary.md", "dna_profile_summary"),
        ("current-medications.md", "current_medications"),
        ("medical-history-summary.md", "medical_history_summary"),
        ("latest-lab-results.md", "latest_lab_results"),
        ("flagged-for-discussion.md", "flagged_for_discussion"),
        ("carrier-screening.md", "carrier_screening"),
        ("pgx-coverage.md", "pgx_coverage"),
    ]:
        _write_report(reports_dir, fn, rt, "Report", "_No data._\n")
    write_category_index_note(
        reports_dir, "dummy_alice", "Reports", display_name="Dummy Alice Example"
    )
    conn = _user_conn()  # empty DB — no lab rows

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Lab Observations" not in content


# ---------------------------------------------------------------------------
# write_health_advisory — idempotency and navigation
# ---------------------------------------------------------------------------


def test_write_health_advisory_is_idempotent(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    ts = datetime(2023, 5, 21, 12, 0, 0, tzinfo=UTC)

    path1, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=ts,
    )
    content1 = path1.read_text(encoding="utf-8")

    path2, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=ts,
    )
    content2 = path2.read_text(encoding="utf-8")

    assert content1 == content2


def test_write_health_advisory_registers_reports_index(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    reports_dir = _scaffold_reports(vault_root)
    conn = _user_conn()

    write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    index_content = (reports_dir / "_index.md").read_text(encoding="utf-8")
    assert "[[health-advisory|Health advisory]]" in index_content


def test_write_health_advisory_still_generates_without_source_reports(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _write_person_note(vault_root)  # person note only, no reports directory
    conn = _user_conn()

    report_path, flag_count = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert report_path.exists()
    assert flag_count == 0


# ---------------------------------------------------------------------------
# generate_health_advisory CLI
# ---------------------------------------------------------------------------


def test_generate_health_advisory_helper_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    db_path = tmp_path / "famed.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_health_advisory.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--db-path",
            str(db_path),
            "--reference-db",
            str(tmp_path / "missing-ref.db"),
        ],
    )

    assert generate_advisory_main() == 0
    captured = capsys.readouterr()
    assert "Wrote" in captured.out
    assert "Genetic risk flags: 0" in captured.out


# ---------------------------------------------------------------------------
# Family history context
# ---------------------------------------------------------------------------


def _insert_family_link(
    conn: sqlite3.Connection,
    source_person_id: str,
    target_person_id: str,
    relationship_type: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO family_links
            (id, source_person_id, target_person_id, relationship_type)
        VALUES (?, ?, ?, ?)
        """,
        (
            f"{source_person_id}-{target_person_id}-{relationship_type}",
            source_person_id,
            target_person_id,
            relationship_type,
        ),
    )


def _write_history_note(
    vault_root: Path,
    person_id: str,
    accepted_facts: list[dict[str, str]],
    *,
    filename: str = "2017-01-01-history-review.md",
) -> None:
    history_dir = vault_root / "Records" / person_id / "History"
    history_dir.mkdir(parents=True, exist_ok=True)
    front_matter = {
        "person_id": person_id,
        "record_type": "history_review",
        "verification_status": "accepted",
        "accepted_facts": accepted_facts,
    }
    yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=False).strip()
    (history_dir / filename).write_text(
        f"---\n{yaml_block}\n---\n\n## History Review\n", encoding="utf-8"
    )


def test_family_history_section_rendered_when_relative_has_conditions(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root, "dummy_bob", [{"description": "Type 2 diabetes", "category": "diagnosis"}]
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" in content
    assert "Diabetes / prediabetes" in content
    assert "parent" in content


def test_family_history_section_omitted_when_no_family_links(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()  # no family_links rows

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content


def test_family_history_section_omitted_when_relative_has_no_accepted_facts(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    # History dir exists but no accepted_facts
    history_dir = vault_root / "Records" / "dummy_bob" / "History"
    history_dir.mkdir(parents=True, exist_ok=True)
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content


def test_family_history_section_includes_multiple_relatives(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_person_note(vault_root, "dummy_carol", birth_year=1972)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [{"description": "Coronary artery disease", "category": "diagnosis"}],
    )
    _write_history_note(
        vault_root,
        "dummy_carol",
        [{"description": "Asthma", "category": "diagnosis"}],
        filename="2015-06-01-history-review.md",
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")
    _insert_family_link(conn, "dummy_alice", "dummy_carol", "sibling")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" in content
    assert "Coronary artery disease" in content
    assert "Asthma" in content
    assert "parent" in content
    assert "sibling" in content


def test_write_health_advisory_records_family_history_sources_in_front_matter(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root, "dummy_bob", [{"description": "Type 2 diabetes", "category": "diagnosis"}]
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    assert validate_note_front_matter(report_path) == []
    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["family_history_sources"] == [
        "Records/dummy_bob/History/2017-01-01-history-review.md"
    ]


def test_write_health_advisory_family_history_sources_empty_without_family_links(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()  # no family_links rows

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["family_history_sources"] == []


def test_family_history_excludes_placeholder_non_issue(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [{"description": "No active problems documented", "category": "diagnosis"}],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "No active problems documented" not in content


def test_family_history_excludes_non_chronic_non_heritable_issue(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [{"description": "Appendectomy", "category": "diagnosis"}],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "Appendectomy" not in content


def test_family_history_excludes_female_specific_issue_for_male_subject(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_alice", sex="male")
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [{"description": "Abnormal uterine bleeding", "category": "diagnosis"}],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "Abnormal uterine bleeding" not in content


def test_family_history_excludes_female_specific_issue_for_abbreviated_male_sex(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_alice", sex="m")
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [{"description": "Intramural and submucous leiomyoma of uterus", "category": "diagnosis"}],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "leiomyoma of uterus" not in content


def test_family_history_excludes_symptoms_encounters_and_injuries(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [
            {"description": "Pain in left wrist", "category": "diagnosis"},
            {
                "description": (
                    "Encounter for general adult medical examination without abnormal findings"
                ),
                "category": "diagnosis",
            },
            {"description": "Injury of right clavicle, initial encounter", "category": "diagnosis"},
        ],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "Pain in left wrist" not in content
    assert "Encounter for general adult medical examination" not in content
    assert "Injury of right clavicle" not in content


def test_family_history_keeps_only_explicit_heritable_signal_from_mixed_history(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [
            {
                "description": "Gastro-esophageal reflux disease without esophagitis",
                "category": "diagnosis",
            },
            {"description": "Postnasal drip", "category": "diagnosis"},
            {"description": "Heartburn", "category": "diagnosis"},
            {"description": "Other specified postprocedural states", "category": "diagnosis"},
            {"description": "Screen for colon cancer", "category": "diagnosis"},
            {"description": "Throat tightness", "category": "diagnosis"},
            {"description": "Tobacco use — current smoker", "category": "diagnosis"},
            {
                "description": "Combined form of age-related cataract, both eyes",
                "category": "diagnosis",
            },
            {"description": "Mixed hyperlipidemia", "category": "diagnosis"},
        ],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" in content
    assert "Hyperlipidemia / high cholesterol" in content
    assert "Gastro-esophageal reflux disease" not in content
    assert "Postnasal drip" not in content
    assert "Heartburn" not in content
    assert "postprocedural" not in content
    assert "Screen for colon cancer" not in content
    assert "Throat tightness" not in content
    assert "Tobacco use" not in content
    assert "cataract" not in content


def test_family_history_dedupes_semantically_equivalent_conditions(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [
            {"description": "Familial hypercholesterolemia", "category": "diagnosis"},
            {"description": "Hyperlipidemia (HLD) — PMH", "category": "diagnosis"},
            {"description": "High cholesterol or triglycerides", "category": "diagnosis"},
            {"description": "Tubular adenoma, colon splenic flexure", "category": "diagnosis"},
            {"description": "Polyp of colon (history)", "category": "diagnosis"},
            {
                "description": "Coronary artery disease involving native heart",
                "category": "diagnosis",
            },
            {"description": "CAD in native artery", "category": "diagnosis"},
            {"description": "Depression, recurrent episode", "category": "diagnosis"},
            {"description": "Depression, unspecified depression type", "category": "diagnosis"},
            {
                "description": "Attention deficit disorder (ADD) without hyperactivity",
                "category": "diagnosis",
            },
            {
                "description": "Attention deficit hyperactivity disorder (ADHD)",
                "category": "diagnosis",
            },
        ],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert content.count("Hyperlipidemia / high cholesterol") == 1
    assert content.count("Colon polyps / adenomas") == 1
    assert content.count("Coronary artery disease") == 1
    assert content.count("  - Depression") == 1
    assert content.count("  - ADHD / ADD") == 1
    assert "Familial hypercholesterolemia" not in content
    assert "High cholesterol or triglycerides" not in content
    assert "Tubular adenoma, colon splenic flexure" not in content
    assert "CAD in native artery" not in content
    assert "Depression, recurrent episode" not in content
    assert "Attention deficit disorder (ADD) without hyperactivity" not in content


def test_family_history_excludes_questionable_diagnosis_support_reviews(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [
            {"description": "Coronary artery disease", "category": "diagnosis"},
            {"description": "Essential hypertension", "category": "diagnosis"},
            {"description": "Mixed hyperlipidemia", "category": "diagnosis"},
        ],
    )
    reports_dir = vault_root / "Records" / "dummy_bob" / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "medical-history-summary.md").write_text(
        "\n".join(
            [
                "# Medical History — Dummy Bob",
                "",
                "## Diagnosis Support Review",
                "",
                "| Fact | Review | Why | Local evidence |",
                "| --- | --- | --- | --- |",
                "| coronary artery disease | partial objective support | calcium only "
                "| CAC score present |",
                "| hypertension, unspecified type | source asserted only | no repeated BP "
                "| highest local BP 138/88 |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" in content
    assert "Hyperlipidemia / high cholesterol" in content
    assert "Coronary artery disease" not in content
    assert "  - Hypertension" not in content


def test_family_history_keeps_heritable_issue_when_other_facts_filtered(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_alice", sex="male")
    _write_person_note(vault_root, "dummy_bob", birth_year=1945)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [
            {"description": "No active problems documented", "category": "diagnosis"},
            {"description": "Type 2 diabetes", "category": "diagnosis"},
            {"description": "Abnormal uterine bleeding", "category": "diagnosis"},
        ],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "parent")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" in content
    assert "Diabetes / prediabetes" in content
    assert "No active problems documented" not in content
    assert "Abnormal uterine bleeding" not in content


def test_family_history_excludes_spouse_conditions(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_bob", birth_year=1970)
    _write_history_note(
        vault_root,
        "dummy_bob",
        [{"description": "Spouse-only condition XYZ-test", "category": "diagnosis"}],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_bob", "spouse")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "Spouse-only condition XYZ-test" not in content


def test_family_history_excludes_child_conditions(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    _write_person_note(vault_root, "dummy_charlie", birth_year=2000)
    _write_history_note(
        vault_root,
        "dummy_charlie",
        [{"description": "Child-only condition XYZ-test", "category": "diagnosis"}],
    )
    conn = _user_conn()
    _insert_family_link(conn, "dummy_alice", "dummy_charlie", "child")

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 5, 21, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Family History Context" not in content
    assert "Child-only condition XYZ-test" not in content


def test_generate_health_advisory_returns_error_when_person_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir(parents=True)
    db_path = tmp_path / "famed.db"
    db_path.write_bytes(b"")

    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_health_advisory.py",
            "--person-id",
            "nobody",
            "--vault-path",
            str(vault_root),
            "--db-path",
            str(db_path),
            "--reference-db",
            str(tmp_path / "missing-ref.db"),
        ],
    )

    assert generate_advisory_main() == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err


# ---------------------------------------------------------------------------
# Synthesized insights (ADR-026)
# ---------------------------------------------------------------------------


def _valid_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "statement": (
            "Her lab trend and family history together may be worth raising at the next visit."
        ),
        "linked_evidence": ["Records/dummy_alice/Reports/health-advisory.md"],
        "domains_linked": ["Heart & Circulation", "Systemic Interactions"],
        "evidence_tier": "E3",
        "confidence": 0.5,
        "weakest_cited_tier": None,
    }
    candidate.update(overrides)
    return candidate


def test_persist_synthesized_insights_accepts_valid_candidate(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    section, rejections = persist_synthesized_insights(
        vault_root,
        "dummy_alice",
        [_valid_candidate(domains_linked=["Oncology & Genitourinary", "Systemic Interactions"])],
    )

    assert rejections == ()
    assert len(section.insights) == 1
    assert section.status == "current"
    content = report_path.read_text(encoding="utf-8")
    assert "## Synthesized Insights" in content
    assert "Her lab trend and family history" in content
    assert "Oncology & Genitourinary, Systemic Interactions" in content
    assert content.index("## Synthesized Insights") < content.index("## Source Reports")


def test_persist_synthesized_insights_drops_invalid_without_failing_others(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    section, rejections = persist_synthesized_insights(
        vault_root,
        "dummy_alice",
        [
            _valid_candidate(),
            _valid_candidate(statement="Stop taking the medication immediately."),
        ],
    )

    assert len(section.insights) == 1
    assert len(rejections) == 1
    assert "dosing instruction" in rejections[0]


def test_persist_synthesized_insights_rejects_malformed_candidate_without_failing_others(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    malformed = {"statement": "Missing required fields."}
    section, rejections = persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate(), malformed]
    )

    assert len(section.insights) == 1
    assert len(rejections) == 1
    assert "malformed candidate" in rejections[0]


def test_persist_synthesized_insights_replaces_rather_than_duplicates(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate(statement="First pass insight.")]
    )
    persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate(statement="Second pass insight.")]
    )

    content = report_path.read_text(encoding="utf-8")
    assert content.count("## Synthesized Insights") == 1
    assert "First pass insight" not in content
    assert "Second pass insight" in content


def test_persist_synthesized_insights_updates_front_matter(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    persist_synthesized_insights(vault_root, "dummy_alice", [_valid_candidate()])

    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["synthesis_status"] == "current"
    assert front_matter["synthesis_generated_at"]
    assert front_matter["person_id"] == "dummy_alice"
    assert validate_note_front_matter(report_path) == []


def test_persist_synthesized_insights_raises_when_report_not_yet_written(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)

    with pytest.raises(FileNotFoundError):
        persist_synthesized_insights(vault_root, "dummy_alice", [_valid_candidate()])


def test_persist_synthesized_insights_empty_candidates_renders_placeholder(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    section, rejections = persist_synthesized_insights(vault_root, "dummy_alice", [])

    assert section.insights == ()
    assert rejections == ()
    content = report_path.read_text(encoding="utf-8")
    assert "_No live-agent-synthesized cross-domain insights yet._" in content


def test_write_health_advisory_unattended_carries_over_synthesis_as_stale(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )
    persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate()], generated_at="2023-06-24T00:00:00Z"
    )

    write_health_advisory_unattended(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 25, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["synthesis_status"] == "stale"
    assert front_matter["synthesis_generated_at"] == "2023-06-24T00:00:00Z"
    assert content.count("## Synthesized Insights") == 1
    assert "Her lab trend and family history" in content
    assert "Stale synthesis" in content
    assert "Generated 2023-06-24T00:00:00Z by a live agent" in content


def test_write_health_advisory_unattended_no_prior_synthesis_renders_normally(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()

    report_path, _ = write_health_advisory_unattended(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "## Synthesized Insights" not in content
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert "synthesis_status" not in front_matter


def test_write_health_advisory_unattended_idempotent_when_already_stale(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )
    persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate()], generated_at="2023-06-24T00:00:00Z"
    )

    write_health_advisory_unattended(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 25, tzinfo=UTC),
    )
    content_after_first_unattended = report_path.read_text(encoding="utf-8")
    write_health_advisory_unattended(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 25, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["synthesis_status"] == "stale"
    assert front_matter["synthesis_generated_at"] == "2023-06-24T00:00:00Z"
    assert content.count("## Synthesized Insights") == 1
    assert content.count("Stale synthesis") == 1
    assert content.count("Her lab trend and family history") == 1
    assert content == content_after_first_unattended


def test_write_health_advisory_unattended_carries_over_empty_insights_placeholder(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )
    persist_synthesized_insights(vault_root, "dummy_alice", [], generated_at="2023-06-24T00:00:00Z")

    write_health_advisory_unattended(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 25, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---\n", 2)[1])
    assert front_matter["synthesis_status"] == "stale"
    assert "_No live-agent-synthesized cross-domain insights yet._" in content
    assert "Stale synthesis" in content


def test_persist_synthesized_insights_helper_reports_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "persist_synthesized_insights.py",
            "--person-id",
            "dummy_alice",
            "--vault-path",
            str(vault_root),
            "--insights-json",
            json.dumps(
                [
                    _valid_candidate(),
                    _valid_candidate(statement="Stop taking the medication immediately."),
                ]
            ),
        ],
    )

    assert persist_insights_main() == 0
    captured = capsys.readouterr()
    assert "Accepted 1 synthesized insight(s)" in captured.out
    assert "Rejected:" in captured.out


def test_doctor_prompts_include_out_of_range_labs_without_genetic_flags(tmp_path: Path) -> None:
    """Prompts come from what is actually out of range, not only genetic flags."""
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    conn = _user_conn()
    _insert_lab_result(
        conn, "dummy_alice", "Analyte X", "2023-01-01", value=4.0, ref_low=1.0, ref_high=5.0
    )
    _insert_lab_result(
        conn, "dummy_alice", "Analyte X", "2023-05-01", value=9.0, ref_low=1.0, ref_high=5.0
    )

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    prompts = report_path.read_text(encoding="utf-8").split("## Doctor Discussion Prompts", 1)[1]
    assert "Ask about Analyte X: 9.0 is outside the reference range" in prompts
    assert "No out-of-range results or genetic risk flags" not in prompts


def test_screening_skipped_for_condition_already_in_digest(tmp_path: Path) -> None:
    """No screening reminder for a diagnosed condition; the omission is stated."""
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root, birth_year=1970)
    _write_report(
        vault_root / "Records" / "dummy_alice" / "Reports",
        "medical-history-summary.md",
        "medical_history_summary",
        "Medical History",
        "## Digest\n\n- Hypertension. First noted: 2015-04-20.\n",
    )
    conn = _user_conn()

    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 10, tzinfo=UTC),
    )

    screening = report_path.read_text(encoding="utf-8").split("## Preventive Screening", 1)[1]
    screening = screening.split("\n## ", 1)[0]
    assert "**Hypertension screening**" not in screening
    assert "already diagnosed: Hypertension" in screening
    assert "**Depression screening**" in screening


def test_synthesis_older_than_30_days_gets_dated_warning_banner(tmp_path: Path) -> None:
    """Old live-agent insights can't pass as current (ADR-031)."""
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )
    persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate()], generated_at="2023-06-24T00:00:00Z"
    )

    for regenerated in (datetime(2023, 8, 1, tzinfo=UTC), datetime(2023, 8, 2, tzinfo=UTC)):
        write_health_advisory_unattended(
            vault_root,
            "dummy_alice",
            conn=conn,
            ref_db_path=tmp_path / "missing.db",
            generated_at=regenerated,
        )

    content = report_path.read_text(encoding="utf-8")
    assert "> [!warning] Out-of-date synthesis (39 days old)" in content
    assert content.count("Out-of-date synthesis") == 1
    assert "Stale synthesis" not in content
    assert "Her lab trend and family history" in content


def test_carried_over_synthesis_citations_follow_replaced_reports(tmp_path: Path) -> None:
    """Old insights keep their wording; citations to retired reports point at successors."""
    vault_root = tmp_path / "vault"
    _scaffold_reports(vault_root)
    conn = _user_conn()
    report_path, _ = write_health_advisory(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 24, tzinfo=UTC),
    )
    persist_synthesized_insights(
        vault_root, "dummy_alice", [_valid_candidate()], generated_at="2023-06-24T00:00:00Z"
    )
    text = report_path.read_text(encoding="utf-8")
    text = text.replace(
        "## Synthesized Insights\n",
        "## Synthesized Insights\n\n- Old note "
        "[[Records/dummy_alice/Reports/lab-trends|Lab trends]]"
        " and [[Records/dummy_alice/Reports/pgx-coverage|PGx]].\n",
        1,
    )
    report_path.write_text(text, encoding="utf-8")

    write_health_advisory_unattended(
        vault_root,
        "dummy_alice",
        conn=conn,
        ref_db_path=tmp_path / "missing.db",
        generated_at=datetime(2023, 6, 25, tzinfo=UTC),
    )

    content = report_path.read_text(encoding="utf-8")
    assert "Old note [[Records/dummy_alice/Reports/lab-results|Lab trends]]" in content
    assert "[[Records/dummy_alice/Reports/genetics|PGx]]" in content
    assert "Reports/lab-trends" not in content
