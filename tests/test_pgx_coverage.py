"""Tests for conservative PGx medication-gene coverage reporting."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from famed.annotation_cache import open_annotation_db
from famed.findings_ledger import active_findings, load_events
from famed.pgx_coverage import (
    PgxCoverageRow,
    assign_metabolizer_phenotype,
    assign_validated_metabolizer_phenotype,
    build_pgx_coverage_rows,
    call_star_alleles,
    load_active_medications,
    record_pgx_coverage_findings,
    validate_cpic_metabolizer_phenotype,
)
from famed.setup import SCHEMA_SQL


def _user_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_medication(
    conn: sqlite3.Connection,
    person_id: str,
    medication_name: str,
    *,
    medication_type: str = "prescription",
    rxnorm_cui: str | None = "32968",
    status: str = "active",
    source_path: str | None = "Records/dummy_alice/Medications/2023-01-01-meds.md",
    source_type: str = "manual",
) -> None:
    conn.execute(
        """
        INSERT INTO medications
            (id, person_id, rxnorm_cui, medication_name, medication_type,
             dose, frequency, status, source_type, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{person_id}-{medication_name}-{source_path}",
            person_id,
            rxnorm_cui,
            medication_name,
            medication_type,
            "75 mg",
            "daily",
            status,
            source_type,
            source_path,
        ),
    )


def _insert_genotype(
    conn: sqlite3.Connection,
    person_id: str,
    rsid: str,
    *,
    allele1: str = "A",
    allele2: str = "G",
) -> None:
    conn.execute(
        """
        INSERT INTO genotypes
            (id, person_id, rsid, chromosome, position, allele1, allele2, provider, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{person_id}-{rsid}",
            person_id,
            rsid,
            "10",
            94781859,
            allele1,
            allele2,
            "23andme",
            "data/processed/dummy_alice/Genes/genome.txt",
        ),
    )


def _insert_cpic(
    ref_db_path: Path,
    *,
    gene: str,
    drug: str,
    evidence_level: str = "A",
    phenotype: str = "normal metabolizer",
    activity_score: str | None = None,
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT INTO cpic_guidelines
            (gene, drug, phenotype, activity_score, recommendation, evidence_level)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (gene, drug, phenotype, activity_score, "test recommendation not rendered", evidence_level),
    )
    conn.commit()
    conn.close()


def _insert_cpic_pair(
    ref_db_path: Path,
    *,
    gene: str,
    drug: str,
    cpiclevel: str = "A",
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        "INSERT OR REPLACE INTO cpic_pairs (drug, gene, cpiclevel) VALUES (?, ?, ?)",
        (drug, gene, cpiclevel),
    )
    conn.commit()
    conn.close()


def _insert_allele_definition(
    ref_db_path: Path,
    *,
    gene: str,
    allele_name: str,
    rsid: str,
    variant_allele: str,
    clinical_functional_status: str | None = None,
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO cpic_allele_definitions
            (gene, allele_name, rsid, variant_allele, clinical_functional_status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (gene, allele_name, rsid, variant_allele, clinical_functional_status),
    )
    conn.commit()
    conn.close()


def _insert_cpic_diplotype(
    ref_db_path: Path,
    *,
    gene: str,
    diplotype: str,
    gene_result: str,
) -> None:
    conn = open_annotation_db(ref_db_path)
    key = "/".join(sorted(diplotype.split("/"), key=str.casefold))
    conn.execute(
        """
        INSERT OR REPLACE INTO cpic_diplotypes
            (gene, diplotype_key, diplotype, gene_result, result_vocabulary)
        VALUES (?, ?, ?, ?, 'function')
        """,
        (gene, key, diplotype, gene_result),
    )
    conn.commit()
    conn.close()


def _insert_clinvar_gene_rsid(
    ref_db_path: Path,
    *,
    gene: str,
    rsid: str,
) -> None:
    conn = open_annotation_db(ref_db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO clinvar_variants
            (rsid, variation_id, significance, condition, review_status,
             last_evaluated, gene, chromosome, position, reference_allele, alternate_allele)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rsid,
            "var1",
            "Benign",
            "Example",
            "criteria provided",
            "2021-01-01",
            gene,
            "10",
            94781859,
            "A",
            "G",
        ),
    )
    conn.commit()
    conn.close()


def _seed_person_vault_with_pgx_source_reports(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Persons").mkdir(parents=True)
    (vault / "Persons" / "dummy_alice.md").write_text(
        (
            "---\nrecord_type: person\nperson_id: dummy_al"
            "ice\ndisplay_name: Dummy Alice Example\n---\n"
        ),
        encoding="utf-8",
    )
    reports_dir = vault / "Records" / "dummy_alice" / "Reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "current-medications.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: report\n"
        "report_type: current_medications\n"
        "generated_at: 2023-01-01\n"
        "---\n",
        encoding="utf-8",
    )
    (reports_dir / "dna-profile-summary.md").write_text(
        "---\n"
        "person_id: dummy_alice\n"
        "record_type: report\n"
        "report_type: dna_profile_summary\n"
        "generated_at: 2023-01-01\n"
        "---\n",
        encoding="utf-8",
    )
    return vault


def test_cpic_match_with_gene_linked_genotype_is_covered(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs4244285")
    _insert_genotype(user, "dummy_alice", "rs4244285")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    row = rows[0]
    assert row.reference_state == "covered by CPIC reference"
    assert row.genotype_state == "genotype data present"
    assert row.gene == "CYP2C19"
    assert row.evidence_levels == ("A",)
    assert row.interpretation_state == "phenotype not interpreted"


def test_medication_without_cpic_match_remains_explicit(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "exampledrug")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].reference_state == "no CPIC match"
    assert rows[0].genotype_state == "not assessed"
    assert rows[0].gene is None


def test_cpic_match_uses_reviewed_rxnorm_cpic_alias(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    rxnorm_dir = tmp_path / "rxnorm"
    rxnorm_dir.mkdir()
    (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
        '{"name":"Zoloft","rxcui":"82728","cpic_drug":"sertraline",'
        '"reviewed_by":"local_manual_review","reviewed_at":"2023-05-30"}\n',
        encoding="utf-8",
    )
    _insert_medication(user, "dummy_alice", "Zoloft", rxnorm_cui="82728")
    _insert_cpic(ref_db, gene="CYP2C19", drug="sertraline")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].reference_state == "covered by CPIC reference"
    assert rows[0].cpic_drug == "sertraline"
    assert rows[0].gene == "CYP2C19"


def test_cpic_match_uses_parenthetical_generic_name(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "Zetia (ezetimibe)", rxnorm_cui="353099")
    _insert_cpic(ref_db, gene="SLCO1B1", drug="ezetimibe")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].reference_state == "covered by CPIC reference"
    assert rows[0].cpic_drug == "ezetimibe"


def test_slco1b1_without_exact_translation_remains_uninterpreted(
    tmp_path: Path,
) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "atorvastatin")
    _insert_cpic_pair(ref_db, gene="SLCO1B1", drug="atorvastatin")
    _insert_clinvar_gene_rsid(ref_db, gene="SLCO1B1", rsid="rs4149056")
    _insert_allele_definition(
        ref_db,
        gene="SLCO1B1",
        allele_name="*5",
        rsid="rs4149056",
        variant_allele="C",
        clinical_functional_status="no function",
    )
    _insert_genotype(user, "dummy_alice", "rs4149056", allele1="C", allele2="C")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert rows[0].interpretation_state == "phenotype not interpreted"
    assert rows[0].result_vocabulary == "uninterpreted"


def test_discontinued_medications_are_excluded(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel", status="discontinued")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert rows == []


def test_load_active_medications_drops_user_discontinued_cross_source_row() -> None:
    """A user-reported discontinuation under a brand name must suppress an active
    row imported under its generic/source name, matching current-medications.md."""
    user = _user_conn()
    _insert_medication(
        user,
        "dummy_alice",
        "sertraline (Zoloft) 50 mg tablet",
        rxnorm_cui="312941",
        status="active",
        source_path="data/processed/dummy_alice/HealthSummary/archive.zip#DOC0001.XML",
        source_type="manual",
    )
    _insert_medication(
        user,
        "dummy_alice",
        "Zoloft",
        rxnorm_cui="82728",
        status="discontinued",
        source_path="Records/dummy_alice/Medications/2023-06-23-dummy_alice-medications.md",
        source_type="user_update",
    )

    medications = load_active_medications(user, "dummy_alice")

    assert medications == []


@pytest.mark.parametrize("status", ["aborted", "completed", "not-taken", "stopped"])
def test_load_active_medications_matches_current_medications_set(status: str) -> None:
    """PGx reviews exactly the medications current-medications.md shows."""
    user = _user_conn()
    _insert_medication(user, "dummy_alice", "atorvastatin", status="active")
    _insert_medication(
        user,
        "dummy_alice",
        "clopidogrel",
        status=status,
        source_path="data/processed/dummy_alice/Medications/old.txt",
    )

    names = [m.medication_name for m in load_active_medications(user, "dummy_alice")]

    assert names == ["atorvastatin"]


def test_cpic_match_without_gene_linked_genotype_is_not_interpreted(
    tmp_path: Path,
) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs4244285")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].reference_state == "covered by CPIC reference"
    assert rows[0].genotype_state == "no local gene-linked genotype data"
    assert rows[0].interpretation_state == "phenotype not interpreted"


def test_missing_reference_db_writes_missing_reference_state(tmp_path: Path) -> None:
    user = _user_conn()
    _insert_medication(user, "dummy_alice", "clopidogrel")

    rows = build_pgx_coverage_rows(user, tmp_path / "missing-reference.db", "dummy_alice")

    assert len(rows) == 1
    assert rows[0].reference_state == "CPIC reference missing"
    assert rows[0].genotype_state == "not assessed"


def _minimal_pgx_row(interpretation_state: str = "phenotype not interpreted") -> PgxCoverageRow:
    return PgxCoverageRow(
        medication_name="clopidogrel",
        medication_type="prescription",
        rxnorm_cui=None,
        medication_source_path=None,
        cpic_drug="clopidogrel",
        gene="CYP2C19",
        evidence_levels=("A",),
        phenotypes=("poor metabolizer",),
        reference_state="covered by CPIC reference",
        genotype_state="genotype data present",
        interpretation_state=interpretation_state,  # type: ignore[arg-type]
    )


def _record_pgx(
    ledger_path: Path,
    ref_db: Path,
    interpretation_state: str = "phenotype not interpreted",
) -> None:
    record_pgx_coverage_findings(
        ledger_path,
        person_id="dummy_alice",
        rows=[_minimal_pgx_row(interpretation_state)],
        source_reports=["Records/dummy_alice/Reports/current-medications.md"],
        report_relative_path="Records/dummy_alice/Reports/pgx-coverage.md",
        reference_db_path=ref_db,
    )


def test_pgx_finding_superseded_when_interpretation_state_improves(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger" / "findings.jsonl"
    ref_db = tmp_path / "ref.db"

    _record_pgx(ledger_path, ref_db, "phenotype not interpreted")
    assert len(active_findings(ledger_path)) == 1
    assert "local CPIC reference data is incomplete" in active_findings(ledger_path)[0].statement

    _record_pgx(ledger_path, ref_db, "normal metabolizer")

    active = active_findings(ledger_path)
    assert len(active) == 1
    assert "normal metabolizer" in active[0].statement
    events = load_events(ledger_path)
    superseded = [e for e in events if e.event_type == "finding_superseded"]
    assert len(superseded) == 1


def test_pgx_finding_superseded_on_second_state_change(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger" / "findings.jsonl"
    ref_db = tmp_path / "ref.db"

    _record_pgx(ledger_path, ref_db, "normal metabolizer")
    _record_pgx(ledger_path, ref_db, "intermediate metabolizer")

    active = active_findings(ledger_path)
    assert len(active) == 1
    assert "intermediate metabolizer" in active[0].statement
    events = load_events(ledger_path)
    superseded = [e for e in events if e.event_type == "finding_superseded"]
    assert len(superseded) == 1


def test_pgx_finding_idempotent_after_state_change(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger" / "findings.jsonl"
    ref_db = tmp_path / "ref.db"

    _record_pgx(ledger_path, ref_db, "normal metabolizer")
    _record_pgx(ledger_path, ref_db, "normal metabolizer")

    assert len(active_findings(ledger_path)) == 1
    assert len(load_events(ledger_path)) == 1


def test_pgx_findings_mark_disappeared_identity_stale(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger" / "findings.jsonl"
    ref_db = tmp_path / "ref.db"
    clopidogrel = _minimal_pgx_row("normal metabolizer")
    pantoprazole = replace(
        clopidogrel,
        medication_name="pantoprazole",
        cpic_drug="pantoprazole",
    )

    record_pgx_coverage_findings(
        ledger_path,
        person_id="dummy_alice",
        rows=[clopidogrel, pantoprazole],
        source_reports=["Records/dummy_alice/Reports/current-medications.md"],
        report_relative_path="Records/dummy_alice/Reports/pgx-coverage.md",
        reference_db_path=ref_db,
    )
    record_pgx_coverage_findings(
        ledger_path,
        person_id="dummy_alice",
        rows=[clopidogrel],
        source_reports=["Records/dummy_alice/Reports/current-medications.md"],
        report_relative_path="Records/dummy_alice/Reports/pgx-coverage.md",
        reference_db_path=ref_db,
    )

    active = active_findings(ledger_path)
    assert len(active) == 1
    assert active[0].statement.startswith("clopidogrel:")
    stale_events = [event for event in load_events(ledger_path) if event.status == "stale"]
    assert len(stale_events) == 1


# --- Star allele calling unit tests ---


def test_call_star_alleles_wildtype_returns_two_copies_of_star1() -> None:
    allele_defs = {"*2": [("rs4244285", "A")]}
    genotype = {"rs4244285": ("G", "G")}  # reference allele, not variant
    assert call_star_alleles("CYP2C19", genotype, allele_defs) == ["*1", "*1"]


def test_call_star_alleles_heterozygous_variant_returns_diplotype() -> None:
    allele_defs = {"*2": [("rs4244285", "A")]}
    genotype = {"rs4244285": ("G", "A")}  # one variant allele
    assert call_star_alleles("CYP2C19", genotype, allele_defs) == ["*2", "*1"]


def test_call_star_alleles_homozygous_variant_returns_single_allele() -> None:
    allele_defs = {"*2": [("rs4244285", "A")]}
    genotype = {"rs4244285": ("A", "A")}  # homozygous variant
    assert call_star_alleles("CYP2C19", genotype, allele_defs) == ["*2"]


def test_call_star_alleles_empty_genotype_returns_empty() -> None:
    allele_defs = {"*2": [("rs4244285", "A")]}
    assert call_star_alleles("CYP2C19", {}, allele_defs) == []


def test_assign_metabolizer_phenotype_wildtype_is_normal_metabolizer() -> None:
    # *1 functional status is "normal function" — as provided by load_allele_functional_status
    functional_status = {"*1": "normal function"}
    assert assign_metabolizer_phenotype(["*1", "*1"], functional_status) == "normal metabolizer"


def test_assign_metabolizer_phenotype_heterozygous_pm_allele_is_intermediate() -> None:
    functional_status = {"*2": "no function", "*1": "normal function"}
    result = assign_metabolizer_phenotype(["*2", "*1"], functional_status)
    assert result == "intermediate metabolizer"


def test_assign_metabolizer_phenotype_homozygous_pm_allele_is_poor() -> None:
    functional_status = {"*2": "no function", "*1": "normal function"}
    assert assign_metabolizer_phenotype(["*2"], functional_status) == "poor metabolizer"


def test_assign_metabolizer_phenotype_empty_alleles_is_not_interpretable() -> None:
    assert assign_metabolizer_phenotype([], {}) == "not interpretable"


def test_assign_validated_metabolizer_phenotype_accepts_matching_cpic_activity_row(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Intermediate Metabolizer",
        activity_score="0.5-1.0",
    )
    functional_status = {"*2": "no function", "*1": "normal function"}

    result = assign_validated_metabolizer_phenotype(
        ref_db,
        "CYP2C19",
        ["*2", "*1"],
        functional_status,
    )

    assert result == "intermediate metabolizer"


def test_assign_validated_metabolizer_phenotype_downgrades_when_cpic_rows_disagree(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score=">=1.5",
    )
    functional_status = {"*2": "no function", "*1": "normal function"}

    result = assign_validated_metabolizer_phenotype(
        ref_db,
        "CYP2C19",
        ["*2", "*1"],
        functional_status,
    )

    assert result == "phenotype not interpreted"


def test_validate_cpic_metabolizer_phenotype_matches_greater_than_expression(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2D6",
        drug="atomoxetine",
        phenotype="Ultrarapid Metabolizer",
        activity_score=">2.25",
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2D6", "ultrarapid metabolizer", 3.0)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2D6", "ultrarapid metabolizer", 2.25)


def test_validate_cpic_metabolizer_phenotype_matches_less_than_expression(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2D6",
        drug="atomoxetine",
        phenotype="Poor Metabolizer",
        activity_score="<0.25",
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2D6", "poor metabolizer", 0.0)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2D6", "poor metabolizer", 0.25)


def test_validate_cpic_metabolizer_phenotype_matches_less_equal_expression(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Poor Metabolizer",
        activity_score="<=0.5",
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "poor metabolizer", 0.5)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "poor metabolizer", 0.75)


def test_validate_cpic_metabolizer_phenotype_matches_exact_expression(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score="2.0",
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", 2.0)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", 2.1)


def test_validate_cpic_metabolizer_phenotype_matches_en_dash_range(tmp_path: Path) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Intermediate Metabolizer",
        activity_score="0.5–1.0",  # en dash, as CPIC source data renders ranges
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "intermediate metabolizer", 0.75)


def test_validate_cpic_metabolizer_phenotype_no_matching_phenotype_row_returns_false(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score=">1.0",
    )
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "poor metabolizer", 0.0)


def test_validate_cpic_metabolizer_phenotype_skips_malformed_activity_score(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score="not-a-number",
    )
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", 2.0)


def test_validate_cpic_metabolizer_phenotype_none_activity_returns_false(tmp_path: Path) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score=">1.0",
    )
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", None)


def test_validate_cpic_metabolizer_phenotype_matches_unicode_greater_equal_expression(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2D6",
        drug="atomoxetine",
        phenotype="Ultrarapid Metabolizer",
        activity_score="≥3.0",  # CPIC source data renders this range with a Unicode ≥
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2D6", "ultrarapid metabolizer", 3.5)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2D6", "ultrarapid metabolizer", 2.0)


def test_validate_cpic_metabolizer_phenotype_accepts_na_sentinel_activity_score(
    tmp_path: Path,
) -> None:
    """CYP2C19/CYP2B6-style genes publish phenotype labels with a literal "n/a"
    activity score instead of a numeric range; the label match alone validates."""
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score="n/a",
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", 2.0)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "poor metabolizer", 0.0)


def test_validate_cpic_metabolizer_phenotype_na_sentinel_still_requires_total_activity(
    tmp_path: Path,
) -> None:
    """An "n/a" row must not bypass the None-activity guard: validation requires
    real diplotype evidence (a computed total_activity), not just a label match."""
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score="n/a",
    )
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", None)


def test_validate_cpic_metabolizer_phenotype_numeric_row_checked_before_na_row(
    tmp_path: Path,
) -> None:
    """When the same gene+phenotype has both a numeric row and an "n/a" row, a
    real activity-score mismatch on the numeric row must not be bypassed by the
    "n/a" row validating on label match alone."""
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score=">=1.5",
    )
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="omeprazole",
        phenotype="Normal Metabolizer",
        activity_score="n/a",
    )
    assert validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", 2.0)
    assert not validate_cpic_metabolizer_phenotype(ref_db, "CYP2C19", "normal metabolizer", 1.0)


def test_assign_validated_metabolizer_phenotype_accepts_na_sentinel_activity_score(
    tmp_path: Path,
) -> None:
    ref_db = tmp_path / "famed-reference.db"
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score="n/a",
    )
    functional_status = {"*1": "normal function"}

    result = assign_validated_metabolizer_phenotype(
        ref_db,
        "CYP2C19",
        ["*1", "*1"],
        functional_status,
    )

    assert result == "normal metabolizer"


# --- Integration: star allele calling through build_pgx_coverage_rows ---


def test_star_allele_wildtype_produces_normal_metabolizer_interpretation(
    tmp_path: Path,
) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score=">1.0",
    )
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs4244285")
    _insert_allele_definition(
        ref_db,
        gene="CYP2C19",
        allele_name="*2",
        rsid="rs4244285",
        variant_allele="A",
        clinical_functional_status="no function",
    )
    # Person has reference allele G/G — wildtype *1/*1
    _insert_genotype(user, "dummy_alice", "rs4244285", allele1="G", allele2="G")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].genotype_state == "genotype data present"
    assert rows[0].interpretation_state == "normal metabolizer"
    assert rows[0].diplotype == "*1/*1"


def test_star_allele_heterozygous_poor_allele_produces_intermediate_interpretation(
    tmp_path: Path,
) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Intermediate Metabolizer",
        activity_score="0.5-1.0",
    )
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs4244285")
    _insert_allele_definition(
        ref_db,
        gene="CYP2C19",
        allele_name="*2",
        rsid="rs4244285",
        variant_allele="A",
        clinical_functional_status="no function",
    )
    _insert_genotype(user, "dummy_alice", "rs4244285", allele1="G", allele2="A")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].genotype_state == "genotype data present"
    assert rows[0].interpretation_state == "intermediate metabolizer"
    assert rows[0].diplotype == "*2/*1"


def test_cpic_allele_defs_with_no_genotype_rsid_overlap_is_not_interpretable(
    tmp_path: Path,
) -> None:
    """CPIC allele definitions exist but person's chip doesn't cover those rsIDs."""
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")
    # ClinVar links gene to rsid_clinvar — gives person "genotype data present"
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs_clinvar")
    # CPIC allele definitions use a different rsid not in person's genotype
    _insert_allele_definition(
        ref_db,
        gene="CYP2C19",
        allele_name="*2",
        rsid="rs_cpic_only",
        variant_allele="A",
        clinical_functional_status="no function",
    )
    # Person has rsid_clinvar but not rs_cpic_only
    _insert_genotype(user, "dummy_alice", "rs_clinvar")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].genotype_state == "genotype data present"
    assert rows[0].interpretation_state == "not interpretable"


def test_identical_genotype_produces_identical_coverage_rows(tmp_path: Path) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs4244285")
    _insert_allele_definition(
        ref_db,
        gene="CYP2C19",
        allele_name="*2",
        rsid="rs4244285",
        variant_allele="A",
        clinical_functional_status="no function",
    )
    _insert_genotype(user, "dummy_alice", "rs4244285", allele1="G", allele2="A")

    rows1 = build_pgx_coverage_rows(user, ref_db, "dummy_alice")
    rows2 = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert rows1 == rows2


def test_build_rows_downgrades_unvalidated_phenotype_to_not_interpreted(
    tmp_path: Path,
) -> None:
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(
        ref_db,
        gene="CYP2C19",
        drug="clopidogrel",
        phenotype="Normal Metabolizer",
        activity_score=">=1.5",
    )
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs4244285")
    _insert_allele_definition(
        ref_db,
        gene="CYP2C19",
        allele_name="*2",
        rsid="rs4244285",
        variant_allele="A",
        clinical_functional_status="no function",
    )
    _insert_genotype(user, "dummy_alice", "rs4244285", allele1="G", allele2="A")

    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")

    assert len(rows) == 1
    assert rows[0].genotype_state == "genotype data present"
    assert rows[0].interpretation_state == "phenotype not interpreted"


def _not_interpretable_row_setup(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, Path, list]:
    """Shared setup: gene with allele defs but no CPIC rsID overlap in person's genotype."""
    user = _user_conn()
    ref_db = tmp_path / "famed-reference.db"
    _insert_medication(user, "dummy_alice", "clopidogrel")
    _insert_cpic(ref_db, gene="CYP2C19", drug="clopidogrel")
    _insert_clinvar_gene_rsid(ref_db, gene="CYP2C19", rsid="rs_clinvar")
    _insert_allele_definition(
        ref_db,
        gene="CYP2C19",
        allele_name="*2",
        rsid="rs_cpic_only",
        variant_allele="A",
        clinical_functional_status="no function",
    )
    _insert_genotype(user, "dummy_alice", "rs_clinvar")
    rows = build_pgx_coverage_rows(user, ref_db, "dummy_alice")
    return user, ref_db, rows


# --- Per-gene CPIC coverage fraction ---
