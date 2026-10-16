from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from famed.rebuild_compare import (
    _markdown_difference_kind,
    _mismatched_vault_triage,
    _vault_category,
    _vault_triage,
    compare_rebuild,
    refresh_isolated_household,
)
from famed.rebuild_vault import RebuildError
from famed.setup import initialize_database


def _stores(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    source_vault = tmp_path / "working-vault"
    target_vault = tmp_path / "redriven-vault"
    source_processed = tmp_path / "working-processed"
    target_processed = tmp_path / "redriven-processed"
    for path in (source_vault, target_vault, source_processed, target_processed):
        path.mkdir()
    source_db = tmp_path / "working.db"
    target_db = tmp_path / "redriven.db"
    initialize_database(source_db)
    initialize_database(target_db)
    return (
        source_vault,
        source_db,
        source_processed,
        target_vault,
        target_db,
        target_processed,
    )


def _compare(tmp_path: Path, *, output_name: str = "audit.json") -> dict[str, object]:
    (
        source_vault,
        source_db,
        source_processed,
        target_vault,
        target_db,
        target_processed,
    ) = _stores(tmp_path)
    return compare_rebuild(
        source_vault=source_vault,
        source_db=source_db,
        source_processed=source_processed,
        target_vault=target_vault,
        target_db=target_db,
        target_processed=target_processed,
        output_path=tmp_path / output_name,
        source_findings_ledger=None,
    )


def _differences(audit: dict[str, object]) -> list[dict[str, object]]:
    value = audit["differences"]
    assert isinstance(value, list)
    return value


def test_identical_stores_have_no_differences_and_are_deterministic(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    output = tmp_path / "first.json"
    kwargs = {
        "source_vault": stores[0],
        "source_db": stores[1],
        "source_processed": stores[2],
        "target_vault": stores[3],
        "target_db": stores[4],
        "target_processed": stores[5],
        "source_findings_ledger": None,
    }

    first = compare_rebuild(**kwargs, output_path=output)
    second_output = tmp_path / "second.json"
    second = compare_rebuild(**kwargs, output_path=second_output)

    assert first == second
    assert output.read_bytes() == second_output.read_bytes()
    assert first["working_storage_unchanged"] is True
    assert first["summary"] == {
        "difference_classes": 0,
        "difference_count": 0,
        "candidate_bug_count": 0,
        "needs_review_count": 0,
        "contract_explained_count": 0,
    }


def test_database_audit_separates_canonical_and_volatile_differences(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    for database, created_at in ((stores[1], "2098-01-01"), (stores[4], "2099-01-01")):
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO persons "
                "(id, display_name, birth_year, sex, created_at) VALUES (?, ?, ?, ?, ?)",
                ("dummy_person", "Dummy Person", 1990, "unknown", created_at),
            )
    with sqlite3.connect(stores[4]) as conn:
        conn.execute(
            "INSERT INTO lab_results "
            "(id, person_id, test_name, value, observed_at, source_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "synthetic-result",
                "dummy_person",
                "Synthetic Analyte",
                1.0,
                "2099-02-01",
                "fixture",
            ),
        )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert indexed["lab_results:redriven_only"]["triage_status"] == "candidate_bug"
    assert indexed["lab_results:redriven_only"]["count"] == 1
    assert indexed["lab_results:provenance_redriven_only"]["triage_status"] == "needs_review"
    assert indexed["persons:volatile_values"]["triage_status"] == "contract_explained"
    assert indexed["persons:volatile_values"]["count"] == 1


def test_database_audit_compares_shared_fields_across_additive_schema_mismatch(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores[1]) as conn:
        conn.execute("ALTER TABLE lab_results DROP COLUMN value_text")
        conn.execute(
            "INSERT INTO persons (id, display_name, birth_year, sex) VALUES (?, ?, ?, ?)",
            ("dummy_person", "Dummy Person", 1990, "unknown"),
        )
    with sqlite3.connect(stores[4]) as conn:
        conn.execute(
            "INSERT INTO persons (id, display_name, birth_year, sex) VALUES (?, ?, ?, ?)",
            ("dummy_person", "Dummy Person", 1990, "unknown"),
        )
        conn.execute(
            "INSERT INTO lab_results "
            "(id, person_id, test_name, value, observed_at, source_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "synthetic-result",
                "dummy_person",
                "Synthetic Analyte",
                1.0,
                "2099-02-01",
                "fixture",
            ),
        )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert indexed["lab_results:schema"]["count"] == 1
    assert indexed["lab_results:redriven_only"]["count"] == 1
    samples = indexed["lab_results:redriven_only"]["samples"]
    assert isinstance(samples, list)
    sample = samples[0]
    assert isinstance(sample, dict)
    assert sample["test_name"] == "Synthetic Analyte"


def test_database_audit_keeps_schema_finding_for_empty_shared_projection(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores[1]) as conn:
        conn.execute("DROP TABLE audit_events")
        conn.execute("CREATE TABLE audit_events (working_detail TEXT)")
    with sqlite3.connect(stores[4]) as conn:
        conn.execute("DROP TABLE audit_events")
        conn.execute("CREATE TABLE audit_events (redriven_detail TEXT)")

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert indexed["audit_events:schema"]["count"] == 1
    assert "audit_events:working_only" not in indexed
    assert "audit_events:redriven_only" not in indexed


def test_source_vault_and_ledger_differences_remain_visible(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for root in (stores[0], stores[3]):
        (root / "Notes").mkdir()
    (stores[0] / "Notes" / "context.md").write_text(
        "---\nupdated_at: 2098-01-01\nkind: synthetic\n---\nBody\n",
        encoding="utf-8",
    )
    (stores[3] / "Notes" / "context.md").write_text(
        "---\nupdated_at: 2099-01-01\nkind: synthetic\n---\nBody\n",
        encoding="utf-8",
    )
    (stores[2] / "same.txt").write_text("working", encoding="utf-8")
    (stores[5] / "same.txt").write_text("redriven", encoding="utf-8")
    (stores[2] / "missing.txt").write_text("source", encoding="utf-8")
    ledger = tmp_path / "findings.jsonl"
    ledger.write_text('{"event_type":"finding_recorded"}\n', encoding="utf-8")

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=ledger,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert indexed["working_only"]["samples"] == ["missing.txt"]
    assert indexed["content_mismatch"]["count"] == 1
    assert indexed["volatile_front_matter"]["samples"] == ["Notes/context.md"]
    assert indexed["working_events_excluded_from_redrive"]["count"] == 1
    assert audit["working_storage_unchanged"] is True


def test_vault_category_checks_reports_before_template_records() -> None:
    assert (
        _vault_category("Records/_person_id/Reports/medical-history-summary.md")
        == "derived_artifact"
    )
    assert _vault_category("Records/dummy_person/Reports/summary.md") == "derived_artifact"
    assert _vault_category("Records/dummy_person/History/entry.md") == "canonical_data"


def test_path_only_needs_review_note_remains_review_triage(tmp_path: Path) -> None:
    note = tmp_path / "review.md"
    note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: vitals\n"
        "verification_status: needs_review\n"
        "---\n\n"
        "# Review\n",
        encoding="utf-8",
    )

    assert _vault_triage("Records/dummy_person/Vitals/review.md", note) == (
        "review_item",
        "needs_review",
    )


def test_source_evidence_note_remains_visible_without_becoming_canonical_bug(
    tmp_path: Path,
) -> None:
    note = tmp_path / "source-evidence.md"
    note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: lab_panel\n"
        "observed_at: 2099-01-01\n"
        "source_type: pdf:synthetic\n"
        "source_path: data/processed/dummy_person/Labs/source.pdf\n"
        "projection_role: source_evidence\n"
        "---\n\n"
        "# Source Evidence\n",
        encoding="utf-8",
    )

    assert _vault_triage("Records/dummy_person/Labs/source-evidence.md", note) == (
        "source_evidence",
        "needs_review",
    )


def test_legacy_source_backed_lab_note_migrates_to_source_evidence_triage(
    tmp_path: Path,
) -> None:
    note = tmp_path / "legacy-source-note.md"
    note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: lab_panel\n"
        "observed_at: 2099-01-01\n"
        "source_type: pdf:synthetic\n"
        "source_path: data/processed/dummy_person/Labs/source.pdf\n"
        "---\n\n"
        "# Legacy Source Note\n",
        encoding="utf-8",
    )

    assert _vault_triage("Records/dummy_person/Labs/legacy-source-note.md", note) == (
        "source_evidence",
        "needs_review",
    )


@pytest.mark.parametrize("projection_role", [None, "source_evidence"])
def test_source_backed_medication_note_is_source_evidence(
    tmp_path: Path, projection_role: str | None
) -> None:
    note = tmp_path / "medications.md"
    marker = f"projection_role: {projection_role}\n" if projection_role is not None else ""
    note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        "source_path: data/processed/dummy_person/Medications/source.txt\n"
        f"{marker}"
        "---\n\n"
        "# Medication List\n",
        encoding="utf-8",
    )

    assert _vault_triage("Records/dummy_person/Medications/source.md", note) == (
        "source_evidence",
        "needs_review",
    )


@pytest.mark.parametrize("source_type", ["user_update", "user_correction"])
def test_authority_medication_note_remains_canonical(tmp_path: Path, source_type: str) -> None:
    note = tmp_path / "authority.md"
    note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        f"source_type: {source_type}\n"
        "source_path: Records/dummy_person/Medications/authority.md\n"
        "projection_role: source_evidence\n"
        "---\n\n"
        "# Medication Update\n",
        encoding="utf-8",
    )

    assert _vault_triage("Records/dummy_person/Medications/authority.md", note) == (
        "canonical_data",
        "candidate_bug",
    )


def test_source_evidence_migration_mismatch_remains_reviewable(tmp_path: Path) -> None:
    working = tmp_path / "working.md"
    redriven = tmp_path / "redriven.md"
    shared = (
        "person_id: dummy_person\n"
        "record_type: lab_panel\n"
        "observed_at: 2099-01-01\n"
        "source_type: pdf:synthetic\n"
        "source_path: data/processed/dummy_person/Labs/source.pdf\n"
    )
    working.write_text(f"---\n{shared}---\n\n# Legacy Lab Note\n", encoding="utf-8")
    redriven.write_text(
        f"---\n{shared}projection_role: source_evidence\n---\n\n# Source Evidence\n",
        encoding="utf-8",
    )

    assert _mismatched_vault_triage(
        "Records/dummy_person/Labs/source.md",
        working,
        redriven,
    ) == ("source_evidence", "needs_review")


def test_accepted_to_needs_review_mismatch_remains_candidate_bug(tmp_path: Path) -> None:
    accepted = tmp_path / "accepted.md"
    needs_review = tmp_path / "needs-review.md"
    for path, status in ((accepted, "accepted"), (needs_review, "needs_review")):
        path.write_text(
            "---\n"
            "person_id: dummy_person\n"
            "record_type: history\n"
            f"verification_status: {status}\n"
            "---\n\n"
            "# History\n",
            encoding="utf-8",
        )

    assert _mismatched_vault_triage(
        "Records/dummy_person/History/entry.md",
        accepted,
        needs_review,
    ) == ("canonical_data", "candidate_bug")


def test_clinical_interpreter_history_difference_remains_review_triage(
    tmp_path: Path,
) -> None:
    working = tmp_path / "working.md"
    redriven = tmp_path / "redriven.md"
    working.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: history\n"
        "verification_status: accepted\n"
        "---\n\n"
        "# Working interpretation\n",
        encoding="utf-8",
    )
    redriven.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: history\n"
        "verification_status: accepted\n"
        "extraction_mode: clinical_interpreter\n"
        "---\n\n"
        "# Redriven interpretation\n",
        encoding="utf-8",
    )

    assert _vault_triage("Records/dummy_person/History/entry.md", redriven) == (
        "review_item",
        "needs_review",
    )
    assert _mismatched_vault_triage(
        "Records/dummy_person/History/entry.md",
        working,
        redriven,
    ) == ("review_item", "needs_review")


def test_markdown_difference_separates_formatting_volatile_and_substantive() -> None:
    quoted = b'---\nrecord_type: "history"\ngenerated_at: 2098-01-01\n---\nBody\n'
    reformatted = b"---\nrecord_type: history\ngenerated_at: 2098-01-01\n---\nBody\n"
    regenerated = b"---\nrecord_type: history\ngenerated_at: 2099-01-01\n---\nBody\n"
    changed = b"---\nrecord_type: history\ngenerated_at: 2099-01-01\n---\nChanged\n"

    assert _markdown_difference_kind(quoted, reformatted) == "format_only_front_matter"
    assert _markdown_difference_kind(quoted, regenerated) == "volatile_front_matter"
    assert _markdown_difference_kind(quoted, changed) == "substantive"


def test_relative_recovered_snapshot_is_contract_explained(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    recovered = Path("dummy_person/Medications/2099-01-01-dummy_person-medications")
    target = stores[5] / recovered
    target.parent.mkdir(parents=True)
    target.write_text("synthetic update", encoding="utf-8")

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
        allowed_target_files={recovered},
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    recovered_difference = indexed["redriven_recovered_authority_snapshot"]
    assert recovered_difference["triage_status"] == "contract_explained"
    assert recovered_difference["samples"] == [recovered.as_posix()]
    assert "redriven_only" not in indexed


def test_database_column_order_is_not_schema_drift(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    with sqlite3.connect(stores[4]) as conn:
        conn.execute("ALTER TABLE medications RENAME TO old_medications")
        conn.execute(
            "CREATE TABLE medications ("
            "id TEXT PRIMARY KEY, person_id TEXT NOT NULL, rxnorm_cui TEXT, "
            "medication_name TEXT NOT NULL, medication_type TEXT NOT NULL DEFAULT 'prescription', "
            "dose TEXT, frequency TEXT, start_date TEXT, end_date TEXT, "
            "status TEXT NOT NULL DEFAULT 'active', source_type TEXT NOT NULL DEFAULT 'manual', "
            "source_path TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            # Deliberately last, not in schema position, so this exercises column order
            # rather than a missing stable column.
            "authorized_through TEXT)"
        )
        conn.execute("DROP TABLE old_medications")

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )

    assert all(item["kind"] != "medications:schema" for item in _differences(audit))


def test_semantic_comparison_preserves_multiplicity(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for database in (stores[1], stores[4]):
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO persons (id, display_name) VALUES (?, ?)",
                ("dummy_person", "Dummy Person"),
            )
    with sqlite3.connect(stores[1]) as conn:
        for row_id in ("first", "second"):
            conn.execute(
                "INSERT INTO lab_results "
                "(id, person_id, test_name, value, observed_at, source_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    "dummy_person",
                    "Synthetic Analyte",
                    1.0,
                    "2099-01-01",
                    "fixture",
                ),
            )
    with sqlite3.connect(stores[4]) as conn:
        conn.execute(
            "INSERT INTO lab_results "
            "(id, person_id, test_name, value, observed_at, source_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "first",
                "dummy_person",
                "Synthetic Analyte",
                1.0,
                "2099-01-01",
                "fixture",
            ),
        )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    semantic = indexed["lab_results:working_only"]
    assert semantic["count"] == 1
    samples = semantic["samples"]
    assert isinstance(samples, list)
    sample = samples[0]
    assert isinstance(sample, dict)
    assert sample["multiplicity"] == 1


def test_authority_change_is_not_downgraded_to_provenance(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for database, source_type in (
        (stores[1], "user_update"),
        (stores[4], "source_import"),
    ):
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO persons (id, display_name) VALUES (?, ?)",
                ("dummy_person", "Dummy Person"),
            )
            conn.execute(
                "INSERT INTO medications "
                "(id, person_id, medication_name, status, source_type, source_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "same-id",
                    "dummy_person",
                    "Synthetic Medication",
                    "active",
                    source_type,
                    "data/processed/dummy_person/Medications/synthetic",
                ),
            )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert "medications:working_only" not in indexed
    assert indexed["medications:provenance_working_only"]["triage_status"] == ("needs_review")
    assert indexed["medications:authority_working_only"]["triage_status"] == ("candidate_bug")
    assert "medications:authority_redriven_only" not in indexed


def test_ordinary_source_type_change_is_provenance(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for database, source_type in (
        (stores[1], "manual"),
        (stores[4], "source_import"),
    ):
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO persons (id, display_name) VALUES (?, ?)",
                ("dummy_person", "Dummy Person"),
            )
            conn.execute(
                "INSERT INTO medications "
                "(id, person_id, medication_name, status, source_type, source_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "same-id",
                    "dummy_person",
                    "Synthetic Medication",
                    "active",
                    source_type,
                    "data/processed/dummy_person/Medications/synthetic",
                ),
            )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert "medications:working_only" not in indexed
    assert "medications:authority_working_only" not in indexed
    assert indexed["medications:provenance_working_only"]["triage_status"] == ("needs_review")
    assert indexed["medications:provenance_redriven_only"]["triage_status"] == ("needs_review")


def test_person_identifier_is_semantic(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for database, person_id in ((stores[1], "dummy_first"), (stores[4], "dummy_second")):
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO persons (id, display_name) VALUES (?, ?)",
                (person_id, "Dummy Person"),
            )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert indexed["persons:working_only"]["count"] == 1
    assert indexed["persons:redriven_only"]["count"] == 1


def test_family_link_identifier_is_provenance(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for database, link_id in ((stores[1], "working-id"), (stores[4], "redriven-id")):
        with sqlite3.connect(database) as conn:
            for person_id in ("dummy_first", "dummy_second"):
                conn.execute(
                    "INSERT INTO persons (id, display_name) VALUES (?, ?)",
                    (person_id, person_id),
                )
            conn.execute(
                "INSERT INTO family_links "
                "(id, source_person_id, target_person_id, relationship_type) "
                "VALUES (?, ?, ?, ?)",
                (link_id, "dummy_first", "dummy_second", "sibling"),
            )

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert "family_links:working_only" not in indexed
    assert indexed["family_links:provenance_working_only"]["count"] == 1
    assert indexed["family_links:provenance_redriven_only"]["count"] == 1


def test_new_stable_canonical_column_must_join_a_projection(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for database in (stores[1], stores[4]):
        with sqlite3.connect(database) as conn:
            conn.execute("ALTER TABLE lab_results ADD COLUMN synthetic_unmapped TEXT")

    with pytest.raises(
        RebuildError,
        match="projection omits stable lab_results columns: synthetic_unmapped",
    ):
        compare_rebuild(
            source_vault=stores[0],
            source_db=stores[1],
            source_processed=stores[2],
            target_vault=stores[3],
            target_db=stores[4],
            target_processed=stores[5],
            output_path=tmp_path / "audit.json",
            source_findings_ledger=None,
        )


def test_local_obsidian_runtime_files_are_visible_operational_metadata(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    runtime_file = stores[0] / ".obsidian" / "app.json"
    runtime_file.parent.mkdir()
    runtime_file.write_text("{}\n", encoding="utf-8")

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    difference = next(
        item
        for item in _differences(audit)
        if item["layer"] == "vault" and item["kind"] == "working_only"
    )

    assert difference["category"] == "operational_metadata"
    assert difference["triage_status"] == "contract_explained"
    assert difference["samples"] == [".obsidian/app.json"]


def test_category_indexes_are_derived_artifacts(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    index = stores[0] / "Notes" / "_index.md"
    index.parent.mkdir()
    index.write_text("# Synthetic index\n", encoding="utf-8")

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
    )
    difference = next(
        item
        for item in _differences(audit)
        if item["layer"] == "vault" and item["kind"] == "working_only"
    )

    assert difference["category"] == "derived_artifact"
    assert difference["triage_status"] == "needs_review"


def test_refresh_rolls_back_database_and_person_tree_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_vault = tmp_path / "vault"
    record = target_vault / "Records" / "dummy_person" / "Reports" / "report.md"
    record.parent.mkdir(parents=True)
    record.write_text("before", encoding="utf-8")
    target_db = tmp_path / "target.db"
    initialize_database(target_db)
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "INSERT INTO persons (id, display_name) VALUES (?, ?)",
            ("dummy_person", "Dummy Person"),
        )

    def fail_refresh(
        conn: sqlite3.Connection,
        vault_root: Path,
        **_kwargs: object,
    ) -> dict[str, list[Path]]:
        conn.execute(
            "INSERT INTO audit_events (id, event_type, subject_type) VALUES (?, ?, ?)",
            ("synthetic-event", "fixture", "fixture"),
        )
        record.write_text("after", encoding="utf-8")
        raise RuntimeError("synthetic refresh failure")

    monkeypatch.setattr(
        "famed.dependent_reports.refresh_derived_reports_for_household",
        fail_refresh,
    )
    with sqlite3.connect(target_db) as conn:
        with pytest.raises(RebuildError, match="synthetic refresh failure"):
            refresh_isolated_household(
                root=tmp_path,
                target_vault=target_vault,
                conn=conn,
            )
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)
    assert record.read_text(encoding="utf-8") == "before"


def test_audit_file_contains_complete_counts_with_bounded_samples(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    for index in range(15):
        (stores[2] / f"source-{index:02}.txt").write_text("fixture", encoding="utf-8")
    output = tmp_path / "audit.json"

    compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=output,
        source_findings_ledger=None,
    )
    audit = json.loads(output.read_text(encoding="utf-8"))
    difference = next(item for item in audit["differences"] if item["kind"] == "working_only")

    assert difference["count"] == 15
    assert len(difference["samples"]) == 10


def test_replay_failures_and_staged_sources_are_first_class_triage(
    tmp_path: Path,
) -> None:
    stores = _stores(tmp_path)
    replay_outcomes = [
        ("failed", "dummy_person/Labs/synthetic.pdf", "no rows extracted"),
        (
            "unsupported",
            "dummy_person/Genes/synthetic.zip",
            "no medical handler",
        ),
        (
            "unsupported",
            "persons/dummy_person.yaml",
            "no registered handler",
        ),
    ]

    audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "audit.json",
        source_findings_ledger=None,
        replay_outcomes=replay_outcomes,
    )
    indexed = {str(item["kind"]): item for item in _differences(audit)}

    assert audit["replay_outcomes"] == {"failed": 1, "unsupported": 2}
    assert indexed["failed_source"]["triage_status"] == "candidate_bug"
    assert indexed["staged_or_operational_source"]["triage_status"] == ("contract_explained")
    assert indexed["unsupported_source"]["triage_status"] == "needs_review"

    reversed_audit = compare_rebuild(
        source_vault=stores[0],
        source_db=stores[1],
        source_processed=stores[2],
        target_vault=stores[3],
        target_db=stores[4],
        target_processed=stores[5],
        output_path=tmp_path / "reversed.json",
        source_findings_ledger=None,
        replay_outcomes=reversed(replay_outcomes),
    )
    assert audit == reversed_audit


@pytest.mark.parametrize(
    "protected_index,relative",
    [
        (0, "rebuild-audit.json"),
        (2, "rebuild-audit.json"),
        (3, "rebuild-audit.json"),
        (5, "rebuild-audit.json"),
        (1, ""),
        (4, ""),
        (1, "-wal"),
    ],
)
def test_audit_output_cannot_mutate_protected_storage(
    tmp_path: Path, protected_index: int, relative: str
) -> None:
    stores = _stores(tmp_path)
    protected = stores[protected_index]
    protected_output = (
        protected / relative if protected.is_dir() else Path(f"{protected}{relative}")
    )
    before = protected_output.read_bytes() if protected_output.exists() else None

    with pytest.raises(
        RebuildError, match="audit output must be outside source and target storage"
    ):
        compare_rebuild(
            source_vault=stores[0],
            source_db=stores[1],
            source_processed=stores[2],
            target_vault=stores[3],
            target_db=stores[4],
            target_processed=stores[5],
            output_path=protected_output,
            source_findings_ledger=None,
        )

    if before is None:
        assert not protected_output.exists()
    else:
        assert protected_output.read_bytes() == before


def test_audit_output_rejects_hardlink_to_protected_database(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    protected_output = tmp_path / "hardlinked-audit.json"
    os.link(stores[1], protected_output)

    with pytest.raises(
        RebuildError, match="audit output must be outside source and target storage"
    ):
        compare_rebuild(
            source_vault=stores[0],
            source_db=stores[1],
            source_processed=stores[2],
            target_vault=stores[3],
            target_db=stores[4],
            target_processed=stores[5],
            output_path=protected_output,
            source_findings_ledger=None,
        )


@pytest.mark.parametrize("protected_index", [0, 2])
def test_audit_output_rejects_hardlink_to_protected_tree_file(
    tmp_path: Path, protected_index: int
) -> None:
    stores = _stores(tmp_path)
    protected_file = stores[protected_index] / "synthetic-source.txt"
    protected_file.write_text("protected\n", encoding="utf-8")
    protected_output = tmp_path / "hardlinked-audit.json"
    os.link(protected_file, protected_output)

    with pytest.raises(
        RebuildError, match="audit output must be outside source and target storage"
    ):
        compare_rebuild(
            source_vault=stores[0],
            source_db=stores[1],
            source_processed=stores[2],
            target_vault=stores[3],
            target_db=stores[4],
            target_processed=stores[5],
            output_path=protected_output,
            source_findings_ledger=None,
        )

    assert protected_file.read_text(encoding="utf-8") == "protected\n"


def test_audit_output_rejects_hardlink_to_sqlite_sidecar(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    sidecar = Path(f"{stores[1]}-wal")
    sidecar.write_text("protected sidecar\n", encoding="utf-8")
    protected_output = tmp_path / "hardlinked-audit.json"
    os.link(sidecar, protected_output)

    with pytest.raises(
        RebuildError, match="audit output must be outside source and target storage"
    ):
        compare_rebuild(
            source_vault=stores[0],
            source_db=stores[1],
            source_processed=stores[2],
            target_vault=stores[3],
            target_db=stores[4],
            target_processed=stores[5],
            output_path=protected_output,
            source_findings_ledger=None,
        )

    assert sidecar.read_text(encoding="utf-8") == "protected sidecar\n"
