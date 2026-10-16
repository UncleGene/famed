from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from famed.correctness_audit import (
    CorrectnessAuditError,
    all_findings,
    dismiss_finding,
    distinct_dna_variant_annotations,
    distinct_lab_codes,
    distinct_medication_codes,
    findings_path,
    load_events,
    open_findings,
    record_finding,
    resolve_finding,
    validate_scrubbed_query,
)

VALID_KWARGS = dict(
    category="labs",
    local_record="Records/dummy_alice/Reports/latest-lab-results.md",
    local_field="loinc_code",
    local_value="2571-8",
    retrieved_value="10886-0",
    source_url="https://loinc.org/2571-8",
    source_publisher="LOINC.org",
    query_issued="LOINC code Free PSA",
    confidence="high",
)


def test_record_finding_appends_jsonl_and_is_open(tmp_path: Path) -> None:
    event = record_finding(tmp_path, finding_id="finding-1", **VALID_KWARGS)

    path = findings_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    assert event.finding_id == "finding-1"
    assert payload["event_type"] == "finding_recorded"
    assert payload["source_url"] == "https://loinc.org/2571-8"

    open_now = open_findings(tmp_path)
    assert len(open_now) == 1
    assert open_now[0].finding_id == "finding-1"
    assert open_now[0].status == "open"


@pytest.mark.parametrize(
    "missing_field",
    [
        "local_record",
        "local_field",
        "local_value",
        "retrieved_value",
        "source_url",
        "source_publisher",
    ],
)
def test_record_finding_requires_citation_fields(tmp_path: Path, missing_field: str) -> None:
    kwargs = dict(VALID_KWARGS)
    kwargs[missing_field] = "   "

    with pytest.raises(CorrectnessAuditError, match=missing_field):
        record_finding(tmp_path, **kwargs)


def test_record_finding_requires_query_issued(tmp_path: Path) -> None:
    kwargs = dict(VALID_KWARGS)
    kwargs["query_issued"] = ""

    with pytest.raises(CorrectnessAuditError, match="query_issued"):
        record_finding(tmp_path, **kwargs)


def test_record_finding_rejects_query_containing_person_id(tmp_path: Path) -> None:
    kwargs = dict(VALID_KWARGS)
    kwargs["query_issued"] = "dummy_alice Free PSA LOINC code"

    with pytest.raises(CorrectnessAuditError, match="person_id"):
        record_finding(tmp_path, person_id="dummy_alice", **kwargs)


def test_record_finding_allows_generic_query_with_person_scope(tmp_path: Path) -> None:
    event = record_finding(tmp_path, person_id="dummy_alice", **VALID_KWARGS)
    assert event.person_id == "dummy_alice"


def test_record_finding_rejects_invalid_category(tmp_path: Path) -> None:
    kwargs = dict(VALID_KWARGS)
    kwargs["category"] = "not-a-category"

    with pytest.raises(CorrectnessAuditError, match="invalid category"):
        record_finding(tmp_path, **kwargs)


def test_record_finding_rejects_invalid_confidence(tmp_path: Path) -> None:
    kwargs = dict(VALID_KWARGS)
    kwargs["confidence"] = "certain"

    with pytest.raises(CorrectnessAuditError, match="invalid confidence"):
        record_finding(tmp_path, **kwargs)


def test_validate_scrubbed_query_case_insensitive() -> None:
    with pytest.raises(CorrectnessAuditError, match="person_id"):
        validate_scrubbed_query("DUMMY_ALICE lookup", person_id="dummy_alice")

    # A generic query with no person_id present is fine.
    assert validate_scrubbed_query("LOINC code Free PSA", person_id="dummy_alice") == (
        "LOINC code Free PSA"
    )


def test_resolve_finding_marks_resolved_and_excludes_from_open(tmp_path: Path) -> None:
    record_finding(tmp_path, finding_id="finding-1", **VALID_KWARGS)

    resolve_finding(
        tmp_path, finding_id="finding-1", reason="Confirmed and repaired via ADR-035 lane."
    )

    assert open_findings(tmp_path) == []
    resolved = all_findings(tmp_path, status="resolved")
    assert len(resolved) == 1
    assert resolved[0].reason == "Confirmed and repaired via ADR-035 lane."


def test_dismiss_finding_requires_reason(tmp_path: Path) -> None:
    record_finding(tmp_path, finding_id="finding-1", **VALID_KWARGS)

    with pytest.raises(CorrectnessAuditError, match="reason"):
        dismiss_finding(tmp_path, finding_id="finding-1", reason="   ")


def test_cannot_resolve_unknown_or_already_resolved_finding(tmp_path: Path) -> None:
    with pytest.raises(CorrectnessAuditError, match="not open"):
        resolve_finding(tmp_path, finding_id="does-not-exist", reason="n/a")

    record_finding(tmp_path, finding_id="finding-1", **VALID_KWARGS)
    resolve_finding(tmp_path, finding_id="finding-1", reason="first resolution")

    with pytest.raises(CorrectnessAuditError, match="not open"):
        resolve_finding(tmp_path, finding_id="finding-1", reason="second resolution")


def test_open_findings_filters_by_category(tmp_path: Path) -> None:
    record_finding(tmp_path, finding_id="lab-finding", **VALID_KWARGS)
    med_kwargs = dict(VALID_KWARGS)
    med_kwargs.update(
        category="medications",
        local_field="rxnorm_cui",
        local_value="6809",
        retrieved_value="6918",
        source_url="https://rxnav.nlm.nih.gov/",
        source_publisher="RxNav/NLM",
        query_issued="RxNorm CUI metformin hydrochloride",
    )
    record_finding(tmp_path, finding_id="med-finding", **med_kwargs)

    assert [f.finding_id for f in open_findings(tmp_path, category="labs")] == ["lab-finding"]
    assert [f.finding_id for f in open_findings(tmp_path, category="medications")] == [
        "med-finding"
    ]
    assert len(open_findings(tmp_path)) == 2


def test_ledger_is_append_only_across_multiple_events(tmp_path: Path) -> None:
    record_finding(tmp_path, finding_id="finding-1", **VALID_KWARGS)
    resolve_finding(tmp_path, finding_id="finding-1", reason="done")

    events = load_events(tmp_path)
    assert [e.event_type for e in events] == ["finding_recorded", "finding_resolved"]
    # The recorded event's citation fields are preserved verbatim in the log.
    assert events[0].source_url == "https://loinc.org/2571-8"


def test_load_events_rejects_malformed_jsonl(tmp_path: Path) -> None:
    path = findings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(CorrectnessAuditError, match="invalid JSON"):
        load_events(tmp_path)


def _make_test_db(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE lab_results (id TEXT, person_id TEXT, loinc_code TEXT, test_name TEXT)"
    )
    con.execute(
        "CREATE TABLE medications (id TEXT, person_id TEXT, rxnorm_cui TEXT, medication_name TEXT)"
    )
    con.executemany(
        "INSERT INTO lab_results VALUES (?, ?, ?, ?)",
        [
            ("1", "dummy_alice", "2571-8", "Free PSA"),
            ("2", "dummy_alice", "2571-8", "Free PSA"),
            ("3", "dummy_bob", "2571-8", "Free PSA"),
            ("4", "dummy_bob", None, "Some Unmapped Test"),
        ],
    )
    con.executemany(
        "INSERT INTO medications VALUES (?, ?, ?, ?)",
        [
            ("1", "dummy_alice", "6809", "Metformin"),
            ("2", "dummy_bob", "6809", "Metformin"),
        ],
    )
    con.commit()
    con.close()


def _make_dna_test_dbs(db_path: Path, ref_db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE genotypes (
            id TEXT,
            person_id TEXT,
            rsid TEXT,
            chromosome TEXT,
            position INTEGER,
            allele1 TEXT,
            allele2 TEXT,
            provider TEXT,
            source_path TEXT
        )
        """
    )
    con.executemany(
        "INSERT INTO genotypes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("1", "dummy_alice", "rs123", "1", 100, "A", "T", "synthetic", "dna-a.txt"),
            ("2", "dummy_bob", "rs123", "1", 100, "A", "A", "synthetic", "dna-b.txt"),
            ("3", "dummy_alice", "rs456", "1", 150, "G", "C", "synthetic", "dna-a.txt"),
            ("4", "dummy_bob", "rs999", "2", 200, "C", "G", "synthetic", "dna-b.txt"),
        ],
    )
    con.commit()
    con.close()

    ref_con = sqlite3.connect(ref_db_path)
    ref_con.execute(
        """
        CREATE TABLE clinvar_variants (
            rsid TEXT PRIMARY KEY,
            variation_id TEXT,
            significance TEXT,
            condition TEXT,
            review_status TEXT,
            gene TEXT,
            alternate_allele TEXT
        )
        """
    )
    ref_con.executemany(
        """
        INSERT INTO clinvar_variants
            (rsid, variation_id, significance, condition, review_status, gene, alternate_allele)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "rs123",
                "VCV000000123",
                "Pathogenic",
                "Synthetic condition",
                "criteria provided, multiple submitters, no conflicts",
                "GENE1",
                "T",
            ),
            (
                "rs456",
                "VCV000000456",
                "Benign",
                "Synthetic benign condition",
                "criteria provided, multiple submitters, no conflicts",
                "GENE2",
                "C",
            ),
        ],
    )
    ref_con.commit()
    ref_con.close()


def test_distinct_lab_codes_groups_and_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "famed.db"
    _make_test_db(db_path)

    usages = distinct_lab_codes(db_path)

    by_name = {u.test_name: u for u in usages}
    assert by_name["Free PSA"].loinc_code == "2571-8"
    assert by_name["Free PSA"].reading_count == 3
    assert by_name["Some Unmapped Test"].loinc_code is None


def test_distinct_medication_codes_groups_and_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "famed.db"
    _make_test_db(db_path)

    usages = distinct_medication_codes(db_path)

    assert len(usages) == 1
    assert usages[0].medication_name == "Metformin"
    assert usages[0].rxnorm_cui == "6809"
    assert usages[0].row_count == 2


def test_distinct_dna_variant_annotations_groups_without_person_or_genotype_values(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "famed.db"
    ref_db_path = tmp_path / "famed-reference.db"
    _make_dna_test_dbs(db_path, ref_db_path)

    usages = distinct_dna_variant_annotations(db_path, ref_db_path)

    by_rsid = {usage.rsid: usage for usage in usages}
    assert by_rsid["rs123"].variation_id == "VCV000000123"
    assert by_rsid["rs123"].gene == "GENE1"
    assert "rs456" not in by_rsid
    assert "rs999" not in by_rsid
    assert all("dummy_" not in repr(usage) for usage in usages)
    assert not any("dna-" in str(value) for usage in usages for value in usage.__dict__.values())


def test_distinct_dna_variant_annotations_requires_reference_table(tmp_path: Path) -> None:
    db_path = tmp_path / "famed.db"
    ref_db_path = tmp_path / "famed-reference.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE genotypes (rsid TEXT, allele1 TEXT, allele2 TEXT)")
    con.execute("INSERT INTO genotypes VALUES ('rs123', 'A', 'T')")
    con.commit()
    con.close()
    sqlite3.connect(ref_db_path).close()

    with pytest.raises(CorrectnessAuditError, match="could not enumerate DNA audit facts"):
        distinct_dna_variant_annotations(db_path, ref_db_path)


def test_record_dna_finding_rejects_person_identifying_query(tmp_path: Path) -> None:
    kwargs = dict(VALID_KWARGS)
    kwargs.update(
        category="dna",
        local_record="data/reference/famed-reference.db:clinvar_variants:rs123",
        local_field="clinvar_annotation",
        local_value="rs123|VCV000000123|GENE1|Pathogenic|Synthetic condition",
        retrieved_value="rs123|VCV000000123|GENE1|Benign|Synthetic condition",
        source_url="https://www.ncbi.nlm.nih.gov/clinvar/variation/123/",
        source_publisher="ClinVar/NCBI",
        query_issued="dummy_alice ClinVar rs123",
    )

    with pytest.raises(CorrectnessAuditError, match="person_id"):
        record_finding(tmp_path, person_id="dummy_alice", **kwargs)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args: list[str], *, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/correctness_audit.py", "--root", str(root), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_cli_record_and_list_open(tmp_path: Path) -> None:
    record_result = _run_cli(
        [
            "record",
            "--category",
            "labs",
            "--local-record",
            "Records/dummy_alice/Reports/latest-lab-results.md",
            "--local-field",
            "loinc_code",
            "--local-value",
            "2571-8",
            "--retrieved-value",
            "10886-0",
            "--source-url",
            "https://loinc.org/2571-8",
            "--source-publisher",
            "LOINC.org",
            "--query-issued",
            "LOINC code Free PSA",
            "--confidence",
            "high",
        ],
        root=tmp_path,
    )
    assert record_result.returncode == 0, record_result.stderr
    assert "recorded finding" in record_result.stdout

    list_result = _run_cli(["list-open"], root=tmp_path)
    assert list_result.returncode == 0, list_result.stderr
    assert "2571-8 -> 10886-0" in list_result.stdout


def test_cli_rejects_query_containing_person_id(tmp_path: Path) -> None:
    result = _run_cli(
        [
            "record",
            "--category",
            "labs",
            "--local-record",
            "Records/dummy_alice/Reports/latest-lab-results.md",
            "--local-field",
            "loinc_code",
            "--local-value",
            "2571-8",
            "--retrieved-value",
            "10886-0",
            "--source-url",
            "https://loinc.org/2571-8",
            "--source-publisher",
            "LOINC.org",
            "--query-issued",
            "dummy_alice Free PSA",
            "--confidence",
            "high",
            "--person-id",
            "dummy_alice",
        ],
        root=tmp_path,
    )
    assert result.returncode != 0
    assert "person_id" in result.stderr


def test_cli_rejects_invalid_category(tmp_path: Path) -> None:
    result = _run_cli(
        [
            "record",
            "--category",
            "not-a-category",
            "--local-record",
            "x",
            "--local-field",
            "x",
            "--local-value",
            "x",
            "--retrieved-value",
            "x",
            "--source-url",
            "x",
            "--source-publisher",
            "x",
            "--query-issued",
            "x",
            "--confidence",
            "high",
        ],
        root=tmp_path,
    )
    assert result.returncode != 0


def test_cli_list_facts_dna_uses_reference_annotation_without_person_values(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "famed.db"
    ref_db_path = tmp_path / "famed-reference.db"
    _make_dna_test_dbs(db_path, ref_db_path)

    result = _run_cli(
        [
            "list-facts",
            "--category",
            "dna",
            "--db-path",
            str(db_path),
            "--ref-db-path",
            str(ref_db_path),
        ],
        root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "rs123\tVCV000000123\tGENE1\tPathogenic" in result.stdout
    assert "rs456" not in result.stdout
    assert "rs999" not in result.stdout
    assert "dummy_" not in result.stdout
    assert "dna-a.txt" not in result.stdout
    assert "\t2" not in result.stdout
