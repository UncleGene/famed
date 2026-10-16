from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from famed.clinical_interpreter import FACT_EXTRACTION_SCHEMA_VERSION
from famed.fhir_export import build_fhir_bundle, validate_fhir_bundle
from famed.lab_sync import load_labs_from_note, source_file_checksum
from famed.person_sync import parse_front_matter
from famed.reprocess_sources import (
    ReprocessError,
    ReprocessRegistration,
    ReprocessResult,
)
from famed.retained_replay import (
    RETAINED_SEMANTIC_DECISIONS_SCHEMA,
    RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
    RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
    RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
    authority_backed_medication_narrative_matches_snapshot,
    build_retained_reprocess_registry,
    copy_retained_sources,
    load_retained_history_decisions,
    replay_retained_medical_sources,
    retained_lab_pdf_document_domain,
    validate_replay_storage_paths,
)
from famed.setup import SCHEMA_SQL
from famed.storage_contract import AUDIT_EVENTS_SCHEMA_SQL


def _lab_review_fixture(
    tmp_path: Path, *, action: str = "accept_verbatim", member_count: int = 1
) -> tuple[sqlite3.Connection, Path, str, Path, Path]:
    processed_root = tmp_path / "data" / "processed"
    archive, source = _write_resolvable_ccda_archive(processed_root, member_count=member_count)
    decision_file = tmp_path / "decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive,
                        canonical_source=source.replace("DOC0001.XML", f"DOC{index:04d}.XML"),
                        action=action,
                    )
                    for index in range(1, member_count + 1)
                ],
            }
        ),
        encoding="utf-8",
    )
    vault = tmp_path / "vault"
    person = vault / "Persons" / "dummy_person.md"
    person.parent.mkdir(parents=True)
    person.write_text(
        "---\nperson_id: dummy_person\nrecord_type: person\ndisplay_name: Dummy Person\n---\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_person')")
    return conn, archive, source, vault, decision_file


def test_lab_review_tracks_current_missing_coverage_and_converges_sources(tmp_path: Path) -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.pending_review import derive_pending_review_snapshot, refresh_pending_review_reports

    conn, archive, source, vault, decision_file = _lab_review_fixture(tmp_path, member_count=2)

    def lab_items():
        return [
            item
            for item in derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
            if item.kind == "lab_observation"
        ]

    before = lab_items()
    assert len(before) == 1
    assert len(before[0].source_paths) == 2
    decisions = load_retained_history_decisions(
        decision_file=decision_file, processed_root=tmp_path / "data" / "processed"
    )
    reprocess_retained_history_archive(
        root=tmp_path,
        vault_root=vault,
        processed_root=tmp_path / "data" / "processed",
        conn=conn,
        archive_path=archive,
        lab_resolution_provider=decisions,
        refresh_reports=False,
    )
    assert lab_items() == []
    conn.execute("DELETE FROM lab_results WHERE person_id = 'dummy_person'")
    missing = lab_items()
    assert len(missing) == 1
    assert missing[0].item_id == before[0].item_id
    assert "Previously accepted" in missing[0].reason
    assert source in missing[0].source_paths
    refresh_pending_review_reports(conn, root=tmp_path, vault_root=vault)
    report = vault / "Records" / "dummy_person" / "Reports" / "pending-review.md"
    assert "Previously accepted" in report.read_text(encoding="utf-8")
    reprocess_retained_history_archive(
        root=tmp_path,
        vault_root=vault,
        processed_root=tmp_path / "data" / "processed",
        conn=conn,
        archive_path=archive,
        refresh_reports=False,
    )
    assert lab_items() == []
    assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (1,)


@pytest.mark.parametrize(
    ("authority", "same_identity", "same_source", "covered"),
    [
        ("user_correction", True, True, True),
        ("user_correction", False, True, False),
        ("user_update", True, True, True),
        ("user_update", True, False, False),
    ],
)
def test_lab_review_respects_exact_authority_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    same_identity: bool,
    same_source: bool,
    covered: bool,
) -> None:
    from famed.correct_fact import apply_lab_correction
    from famed.history_sync import reprocess_retained_history_archive
    from famed.pending_review import derive_pending_review_snapshot

    conn, archive, _source, vault, decision_file = _lab_review_fixture(tmp_path)
    processed_root = tmp_path / "data" / "processed"
    decisions = load_retained_history_decisions(
        decision_file=decision_file, processed_root=processed_root
    )
    reprocess_retained_history_archive(
        root=tmp_path,
        vault_root=vault,
        processed_root=processed_root,
        conn=conn,
        archive_path=archive,
        lab_resolution_provider=decisions,
        refresh_reports=False,
    )
    name, observed = conn.execute("SELECT test_name, observed_at FROM lab_results").fetchone()
    monkeypatch.setattr("famed.correct_fact._regenerate_lab_reports", lambda *_args: None)
    apply_lab_correction(
        conn, vault, "dummy_person", name, observed, 42.0, reason="Synthetic correction."
    )
    if authority == "user_update":
        conn.execute("UPDATE lab_results SET source_type = 'user_update'")
    if not same_identity:
        conn.execute("UPDATE lab_results SET observed_at = '2099-01-03'")
    if not same_source:
        conn.execute("UPDATE lab_results SET source_path = 'synthetic-other-source'")
    items = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
    assert any(item.kind == "lab_observation" for item in items) is not covered
    if authority == "user_correction" and same_identity:
        reprocess_retained_history_archive(
            root=tmp_path,
            vault_root=vault,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive,
            refresh_reports=False,
        )
        assert conn.execute("SELECT value, source_type FROM lab_results").fetchall() == [
            (42.0, "user_correction")
        ]
        assert not any(
            item.kind == "lab_observation"
            for item in derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
        )


@pytest.mark.parametrize("action", ["reject", "quarantine"])
def test_lab_review_respects_recorded_dispositions(tmp_path: Path, action: str) -> None:
    from famed.history_sync import reprocess_retained_history_archive
    from famed.pending_review import derive_pending_review_snapshot

    conn, archive, _source, vault, decision_file = _lab_review_fixture(tmp_path, action=action)
    decisions = load_retained_history_decisions(
        decision_file=decision_file, processed_root=tmp_path / "data" / "processed"
    )
    reprocess_retained_history_archive(
        root=tmp_path,
        vault_root=vault,
        processed_root=tmp_path / "data" / "processed",
        conn=conn,
        archive_path=archive,
        lab_resolution_provider=decisions,
        refresh_reports=False,
    )
    items = [
        item
        for item in derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
        if item.kind == "lab_observation"
    ]
    assert len(items) == (1 if action == "quarantine" else 0)
    if items:
        assert "quarantined" in items[0].reason


def test_archive_legacy_lab_decision_blocks_before_any_mutation(tmp_path: Path) -> None:
    from famed.history_sync import HistorySyncError, reprocess_retained_history_archive

    conn, archive, source, vault, _decision_file = _lab_review_fixture(tmp_path)
    conn.execute(
        """INSERT INTO audit_events
           (id,event_type,subject_type,subject_id,source_path,status,message)
           VALUES ('legacy','lab_observation_resolution_recorded','lab_result',
                   'dummy_person',?,'processed','Legacy reviewed decision summary')""",
        (source,),
    )
    conn.execute(
        """INSERT INTO lab_results
           (id,person_id,test_name,loinc_code,value_text,observed_at,source_type,source_path)
           VALUES ('accepted','dummy_person','Synthetic Urine Culture','630-4',
                   'No growth of greater than or equal to 1000 CFU/mL at 18 hours',
                   '2099-01-02','source_import',?)""",
        (source,),
    )
    original = list(conn.iterdump())
    original_notes = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(HistorySyncError, match="source-bound retained decisions"):
        reprocess_retained_history_archive(
            root=tmp_path,
            vault_root=vault,
            processed_root=tmp_path / "data" / "processed",
            conn=conn,
            archive_path=archive,
            refresh_reports=False,
        )
    assert list(conn.iterdump()) == original
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")} == original_notes


def test_archive_stale_recorded_lab_decision_blocks_before_any_mutation(tmp_path: Path) -> None:
    from famed.history_sync import HistorySyncError, reprocess_retained_history_archive
    from famed.pending_review import derive_pending_review_snapshot

    conn, archive, _source, vault, decision_file = _lab_review_fixture(tmp_path)
    decisions = load_retained_history_decisions(
        decision_file=decision_file, processed_root=tmp_path / "data" / "processed"
    )
    reprocess_retained_history_archive(
        root=tmp_path,
        vault_root=vault,
        processed_root=tmp_path / "data" / "processed",
        conn=conn,
        archive_path=archive,
        lab_resolution_provider=decisions,
        refresh_reports=False,
    )
    with zipfile.ZipFile(archive, "a") as file:
        file.writestr("unrelated.txt", "Changed archive bytes")
    original = list(conn.iterdump())
    original_notes = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises(HistorySyncError, match="checksum is stale"):
        reprocess_retained_history_archive(
            root=tmp_path,
            vault_root=vault,
            processed_root=tmp_path / "data" / "processed",
            conn=conn,
            archive_path=archive,
            refresh_reports=False,
        )
    assert list(conn.iterdump()) == original
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")} == original_notes
    items = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
    assert any("checksum is stale" in item.reason for item in items)


def test_lab_review_surfaces_unreadable_archive_evidence(tmp_path: Path) -> None:
    from famed.pending_review import derive_pending_review_snapshot

    conn, archive, _source, vault, _decision_file = _lab_review_fixture(tmp_path)
    archive.write_bytes(b"not a ZIP archive")
    items = derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
    assert len(items) == 1
    assert items[0].kind == "lab_observation"
    assert "could not be validated" in items[0].reason
    assert items[0].source_paths == (archive.relative_to(tmp_path).as_posix(),)


@pytest.mark.parametrize(
    "corruption", ["missing_archive", "missing_member", "malformed_decision", "malformed_action"]
)
def test_lab_replay_surfaces_missing_or_corrupt_review_evidence(
    tmp_path: Path, corruption: str
) -> None:
    from famed.history_sync import HistorySyncError, reprocess_retained_history_archive
    from famed.pending_review import derive_pending_review_snapshot

    conn, archive, _source, vault, decision_file = _lab_review_fixture(tmp_path)
    processed_root = tmp_path / "data" / "processed"
    decisions = load_retained_history_decisions(
        decision_file=decision_file, processed_root=processed_root
    )
    reprocess_retained_history_archive(
        root=tmp_path,
        vault_root=vault,
        processed_root=processed_root,
        conn=conn,
        archive_path=archive,
        lab_resolution_provider=decisions,
        refresh_reports=False,
    )
    if corruption == "missing_archive":
        archive.unlink()
        reason = "No such file"
    elif corruption == "missing_member":
        with zipfile.ZipFile(archive, "w") as file:
            file.writestr("metadata.txt", "Reviewed member removed")
        reason = "member is missing"
    elif corruption == "malformed_action":
        event_id, message = conn.execute(
            "SELECT id, message FROM audit_events "
            "WHERE event_type = 'lab_observation_resolution_recorded'"
        ).fetchone()
        payload = json.loads(message)
        payload["resolution"]["action"] = []
        conn.execute(
            "UPDATE audit_events SET message = ? WHERE id = ?", (json.dumps(payload), event_id)
        )
        reason = "invalid recorded Lab decision"
    else:
        conn.execute(
            """UPDATE audit_events SET message = '{"schema_version":"broken"}'
               WHERE event_type = 'lab_observation_resolution_recorded'"""
        )
        reason = "invalid recorded Lab decision"
    original = list(conn.iterdump())
    original_notes = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")}
    with pytest.raises((HistorySyncError, FileNotFoundError), match=reason):
        reprocess_retained_history_archive(
            root=tmp_path,
            vault_root=vault,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive,
            refresh_reports=False,
        )
    assert list(conn.iterdump()) == original
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*.md")} == original_notes
    assert any(
        reason in item.reason
        for item in derive_pending_review_snapshot(conn, root=tmp_path, vault_root=vault).items
    )


def test_lab_replay_renews_changed_checksum_with_exact_reviewed_bundle(tmp_path: Path) -> None:
    from famed.history_sync import reprocess_retained_history_archive

    conn, archive, source, vault, decision_file = _lab_review_fixture(tmp_path)
    processed_root = tmp_path / "data" / "processed"
    for replay_index in range(3):
        if replay_index == 1:
            with zipfile.ZipFile(archive, "a") as file:
                file.writestr("metadata.txt", "Changed bytes reviewed")
            decision_file.write_text(
                json.dumps(
                    {
                        "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                        "decisions": [
                            _lab_resolution_decision(
                                archive_path=archive,
                                canonical_source=source,
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
        decisions = (
            load_retained_history_decisions(
                decision_file=decision_file, processed_root=processed_root
            )
            if replay_index < 2
            else None
        )
        reprocess_retained_history_archive(
            root=tmp_path,
            vault_root=vault,
            processed_root=processed_root,
            conn=conn,
            archive_path=archive,
            lab_resolution_provider=decisions,
            refresh_reports=False,
        )
        assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone() == (1,)


def _write_resolvable_ccda_archive(
    processed_root: Path,
    *,
    occurrence_count: int = 1,
    member_count: int = 1,
) -> tuple[Path, str]:
    archive_path = processed_root / "dummy_person" / "HealthSummary" / "2099-01-02-summary.zip"
    archive_path.parent.mkdir(parents=True)
    member_name = "IHE_XDM/Dummy Person/DOC0001.XML"
    culture_entry = """
          <entry>
            <observation classCode="OBS" moodCode="EVN">
              <code code="630-4" codeSystem="2.16.840.1.113883.6.1"
                    displayName="Synthetic Urine Culture"/>
              <effectiveTime value="20990102"/>
              <value xsi:type="ST">No growth of greater than or equal to 1000
                CFU/mL at 18 hours</value>
            </observation>
          </entry>"""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <title>Visit Summary</title>
  <effectiveTime value="20990102"/>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <title>Results</title>
          <text>Synthetic result</text>
{"".join(culture_entry for _ in range(occurrence_count))}
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>
    """
    with zipfile.ZipFile(archive_path, "w") as archive:
        for member_index in range(1, member_count + 1):
            archive.writestr(
                member_name.replace("DOC0001", f"DOC{member_index:04d}"),
                xml,
            )
    canonical_source = (
        f"data/processed/dummy_person/HealthSummary/2099-01-02-summary.zip#{member_name}"
    )
    return archive_path, canonical_source


def _lab_resolution_decision(
    *,
    archive_path: Path,
    canonical_source: str,
    action: str = "accept_verbatim",
    expected_occurrences: int = 1,
    value_text: str = "No growth of greater than or equal to 1000 CFU/mL at 18 hours",
) -> dict[str, object]:
    return {
        "source_path": canonical_source,
        "source_sha256": source_file_checksum(archive_path),
        "resolution": {
            "category": "labs",
            "person_id": "dummy_person",
            "action": action,
            "selector": {
                "code_system": "2.16.840.1.113883.6.1",
                "loinc_code": "630-4",
                "test_name": "Synthetic Urine Culture",
                "value_text": value_text,
                "value_type": "ST",
                "unit": None,
                "observed_at": "2099-01-02",
                "reference_range_low": None,
                "reference_range_high": None,
            },
            "expected_occurrences": expected_occurrences,
            "resolved_by": "vault_owner",
            "resolved_at": "2099-01-03T12:00:00+00:00",
            "reason": "Reviewed against the retained C-CDA observation.",
        },
    }


def test_copy_retained_sources_is_isolated_and_rejects_target_extras(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_file = source / "dummy_person" / "Labs" / "panel.csv"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("source evidence", encoding="utf-8")
    source_before = source_file.read_bytes()

    copied = copy_retained_sources(source_root=source, target_root=target)

    assert copied == [target / "dummy_person" / "Labs" / "panel.csv"]
    assert copied[0].read_text(encoding="utf-8") == "source evidence"
    assert source_file.read_bytes() == source_before

    extra = target / "unexpected.txt"
    extra.write_text("not from source", encoding="utf-8")
    with pytest.raises(ReprocessError, match="outside the source corpus"):
        copy_retained_sources(source_root=source, target_root=target)

    copied = copy_retained_sources(
        source_root=source,
        target_root=target,
        allowed_target_files={Path("unexpected.txt")},
    )
    assert copied == [target / "dummy_person" / "Labs" / "panel.csv"]


def test_replay_storage_rejects_cross_overlaps_and_allows_external_target(
    tmp_path: Path,
) -> None:
    source_vault = tmp_path / "source-vault"
    source_db = tmp_path / "source.db"
    source_processed = tmp_path / "source-processed"
    target_vault = tmp_path / "target-vault"
    target_db = tmp_path / "target.db"

    with pytest.raises(ReprocessError, match="overlaps source storage"):
        validate_replay_storage_paths(
            source_vault=source_vault,
            source_db=source_db,
            source_processed=source_processed,
            target_vault=target_vault,
            target_db=target_db,
            target_processed=source_vault / "copied-sources",
        )
    with pytest.raises(ReprocessError, match="target paths overlap"):
        validate_replay_storage_paths(
            source_vault=source_vault,
            source_db=source_db,
            source_processed=source_processed,
            target_vault=target_vault,
            target_db=target_db,
            target_processed=target_vault / "copied-sources",
        )

    validate_replay_storage_paths(
        source_vault=source_vault,
        source_db=source_db,
        source_processed=source_processed,
        target_vault=target_vault,
        target_db=target_db,
        target_processed=tmp_path.parent / f"{tmp_path.name}-external-processed",
    )


@pytest.mark.parametrize(
    ("filename", "domain"),
    [
        ("2099-01-01-CT SYNTHETIC REGION.pdf", "radiology"),
        ("2099-01-01-XR SYNTHETIC REGION.pdf", "radiology"),
        ("2099-01-01-US SYNTHETIC REGION.pdf", "radiology"),
        ("2099-01-01-HUMPHREY VISUAL FIELD.pdf", "ophthalmology"),
        ("2099-01-01-OCT, SYNTHETIC REGION.pdf", "ophthalmology"),
        ("2099-01-01-ECG 12-LEAD.pdf", "cardiology"),
        ("2099-01-01-TRANSTHORACIC ECHO COMPLETE.pdf", "cardiology"),
    ],
)
def test_retained_lab_pdf_document_domain_is_bounded(
    filename: str,
    domain: str,
) -> None:
    assert retained_lab_pdf_document_domain(Path(filename)) == domain


@pytest.mark.parametrize(
    "filename",
    [
        "2099-01-01-SYNTHETIC ANALYTE.pdf",
        "2099-01-01-URINE CULTURE.pdf",
        "2099-01-01-panel.csv",
    ],
)
def test_retained_lab_pdf_document_domain_leaves_lab_sources_alone(
    filename: str,
) -> None:
    assert retained_lab_pdf_document_domain(Path(filename)) is None


def test_batch_registry_routes_bounded_lab_documents_to_history(tmp_path: Path) -> None:
    processed_root = tmp_path / "processed"
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
    )

    routed = processed_root / "dummy_person" / "Labs" / "2099-01-01-HUMPHREY VISUAL FIELD.pdf"
    lab = processed_root / "dummy_person" / "Labs" / "2099-01-01-panel.pdf"

    assert registry.handler_for(routed, processed_root).name == "history_from_labs"
    assert registry.handler_for(lab, processed_root).name == "labs"


def test_batch_replay_reports_outcomes_and_rolls_back_failed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "processed"
    for name in ["import.csv", "redundant.csv", "zero.csv", "bad.csv", "raw.dna"]:
        path = processed_root / "dummy_person" / "Labs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE scratch (value TEXT)")

    class FakeRegistry:
        def handler_for(self, source_path: Path, _processed_root: Path) -> ReprocessRegistration:
            if source_path.name == "raw.dna":
                raise ReprocessError("unsupported fixture")
            source_name = source_path.name

            def handler(source_path: Path) -> ReprocessResult:
                _ = source_path
                if source_name == "import.csv":
                    return ReprocessResult(2, {"dummy_person"})
                if source_name == "redundant.csv":
                    return ReprocessResult(0, {"dummy_person"})
                if source_name == "zero.csv":
                    return ReprocessResult(0, set())
                failed_note = tmp_path / "vault" / "Records" / "dummy_person" / "Labs" / "bad.md"
                failed_note.parent.mkdir(parents=True, exist_ok=True)
                failed_note.write_text("must roll back", encoding="utf-8")
                conn.execute("INSERT INTO scratch VALUES ('must roll back')")
                raise ValueError("malformed fixture")

            return ReprocessRegistration(
                name="labs",
                category_parts=("Labs",),
                suffixes=frozenset({".csv"}),
                handler=handler,
            )

    monkeypatch.setattr(
        "famed.retained_replay.build_retained_reprocess_registry",
        lambda **_kwargs: FakeRegistry(),
    )
    monkeypatch.setattr(
        "famed.retained_replay.refresh_lab_dependent_reports",
        lambda *_args: None,
    )

    summary = replay_retained_medical_sources(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=conn,
    )

    assert summary.count("imported") == 1
    assert summary.count("redundant") == 1
    assert summary.count("zero_fact") == 1
    assert summary.count("unsupported") == 1
    assert summary.count("failed") == 1
    assert summary.imported_rows == 2
    assert summary.failed is True
    assert conn.execute("SELECT COUNT(*) FROM scratch").fetchone()[0] == 0
    assert not (tmp_path / "vault" / "Records" / "dummy_person" / "Labs" / "bad.md").exists()


def test_batch_registry_uses_canonical_processed_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_reprocess(**kwargs: object) -> ReprocessResult:
        captured.update(kwargs)
        return ReprocessResult(1, {"dummy_person"})

    monkeypatch.setattr("famed.retained_replay.reprocess_retained_lab_source", fake_reprocess)
    processed_root = tmp_path / "isolated-copy"
    source_path = processed_root / "dummy_person" / "Labs" / "panel.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("fixture", encoding="utf-8")
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
    )

    registry.handler_for(source_path, processed_root).handler(source_path)

    assert captured["source_path_prefix"] == Path("data/processed")
    assert captured["refresh_reports"] is False


def test_routed_lab_document_preserves_labs_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_reprocess(**kwargs: object) -> ReprocessResult:
        captured.update(kwargs)
        return ReprocessResult(0, {"dummy_person"})

    monkeypatch.setattr(
        "famed.retained_replay.reprocess_retained_history_source",
        fake_reprocess,
    )
    processed_root = tmp_path / "isolated-copy"
    source_path = processed_root / "dummy_person" / "Labs" / "2099-01-01-HUMPHREY VISUAL FIELD.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"fixture")
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
    )

    registry.handler_for(source_path, processed_root).handler(source_path)

    assert captured["source_path"] == source_path
    assert captured["source_category"] == "Labs"
    assert captured["source_path_prefix"] == Path("data/processed")
    assert captured["refresh_reports"] is False


def _history_decision_payload(person_id: str) -> dict[str, object]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": 0.99},
        "category": "history",
        "category_confidence": 0.99,
        "facts": [
            {
                "description": "Synthetic follow-up finding",
                "category": "follow_up",
                "recorded_date": "2099-01-02",
                "confidence": 0.95,
                "evidence_snippet": "Synthetic follow-up finding",
            }
        ],
        "evidence": [
            {
                "field": "history",
                "snippet": "Synthetic follow-up finding",
                "confidence": 0.95,
            }
        ],
        "overall_confidence": 0.95,
        "document_date": "2099-01-02",
    }


def _lab_decision_payload(person_id: str) -> dict[str, object]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": 0.99},
        "category": "labs",
        "category_confidence": 0.99,
        "facts": [
            {
                "test_name": "Synthetic Mean Deviation OD",
                "observed_at": "2099-01-02",
                "value": "-1.25",
                "unit": "dB",
                "confidence": 0.95,
            }
        ],
        "evidence": [
            {
                "field": "labs",
                "snippet": "Synthetic Mean Deviation OD -1.25 dB",
                "confidence": 0.95,
            }
        ],
        "overall_confidence": 0.95,
        "document_date": "2099-01-02",
    }


def _medication_decision_payload(person_id: str) -> dict[str, object]:
    return {
        "schema_version": FACT_EXTRACTION_SCHEMA_VERSION,
        "person": {"person_id": person_id, "confidence": 0.99},
        "category": "medications",
        "category_confidence": 0.99,
        "facts": [
            {
                "name": "Examplemed",
                "dose": "10 mg",
                "confidence": 0.95,
            }
        ],
        "evidence": [
            {
                "field": "medications",
                "snippet": "Examplemed 10 mg",
                "confidence": 0.95,
            }
        ],
        "overall_confidence": 0.95,
        "document_date": "2099-01-02",
    }


def test_history_decisions_are_bound_to_canonical_source_and_checksum(tmp_path: Path) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "History" / "visit.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained evidence")
    canonical_source = "data/processed/dummy_person/History/visit.pdf"
    decision_file = tmp_path / "history-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "famed.history-extraction-decisions.v1",
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _history_decision_payload("dummy_person"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    interpreter = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    note = tmp_path / "visit-history-review.md"
    note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: history\n"
        f"source_path: {canonical_source}\n"
        "verification_status: needs_review\n"
        "---\n",
        encoding="utf-8",
    )

    assert interpreter.extract_facts(note) == _history_decision_payload("dummy_person")


def test_retained_semantic_decisions_allow_one_history_and_lab_extraction_per_source(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "Labs" / "2099-01-02-HUMPHREY VISUAL FIELD.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained evidence")
    canonical_source = "data/processed/dummy_person/Labs/2099-01-02-HUMPHREY VISUAL FIELD.pdf"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA,
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _history_decision_payload("dummy_person"),
                    },
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _lab_decision_payload("dummy_person"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )

    assert decisions.payload_for_source(source, "history") == _history_decision_payload(
        "dummy_person"
    )
    assert decisions.payload_for_source(source, "labs") == _lab_decision_payload("dummy_person")


def test_retained_semantic_decisions_reject_duplicate_source_category(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "History" / "visit.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained evidence")
    decision = {
        "source_path": "data/processed/dummy_person/History/visit.pdf",
        "source_sha256": source_file_checksum(source),
        "extraction": _history_decision_payload("dummy_person"),
    }
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA,
                "decisions": [decision, decision],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReprocessError, match="duplicate.*category"):
        load_retained_history_decisions(
            decision_file=decision_file,
            processed_root=processed_root,
        )


def test_retained_semantic_v3_accepts_medication_decision_for_medication_source(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "Medications" / "2099-01-02-list"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Examplemed 10 mg")
    canonical_source = "data/processed/dummy_person/Medications/2099-01-02-list"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _medication_decision_payload("dummy_person"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )

    assert decisions.payload_for_source(source, "medications") == _medication_decision_payload(
        "dummy_person"
    )


def test_retained_semantic_v4_accepts_medication_decision_for_history_visit_summary(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "History" / "2099-01-02-visitSummary.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained visit summary")
    canonical_source = "data/processed/dummy_person/History/2099-01-02-visitSummary.pdf"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _medication_decision_payload("dummy_person"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )

    assert decisions.payload_for_source(source, "medications") == _medication_decision_payload(
        "dummy_person"
    )


def test_retained_semantic_v5_preflights_exact_ccda_lab_resolution(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=canonical_source,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )

    resolutions = decisions.lab_observation_resolutions_for_source(canonical_source)
    assert len(resolutions) == 1
    assert resolutions[0].action == "accept_verbatim"
    assert resolutions[0].selector.loinc_code == "630-4"
    assert resolutions[0].resolved_by == "vault_owner"
    with pytest.raises(ReprocessError, match="checksum became stale"):
        decisions.validate_lab_observation_resolution_archive(
            canonical_source.split("#", 1)[0],
            "0" * 64,
            (canonical_source,),
        )


def test_retained_semantic_v5_rechecks_checksum_before_replay_mutation(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=canonical_source,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("IHE_XDM/Dummy Person/unrelated.txt", "changed after review")

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=conn,
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    with pytest.raises(ReprocessError, match="checksum became stale"):
        registry.handler_for(archive_path, processed_root).handler(archive_path)

    assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert not (tmp_path / "vault").exists()


def test_retained_semantic_v5_rejects_deleted_archive_before_replay_mutation(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=canonical_source,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    vault_root = tmp_path / "vault"
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        batch_mode=True,
        history_fact_interpreter=decisions,
    )
    handler = registry.handler_for(archive_path, processed_root).handler
    archive_path.unlink()

    with pytest.raises(ReprocessError, match="archive no longer exists"):
        handler(archive_path)

    assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert not vault_root.exists()


@pytest.mark.parametrize("replacement_name", [None, "IHE_XDM/Dummy Person/RENAMED.XML"])
def test_retained_semantic_v5_rejects_removed_or_renamed_member_before_replay_mutation(
    tmp_path: Path,
    replacement_name: str | None,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=canonical_source,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    with zipfile.ZipFile(archive_path) as archive:
        reviewed_xml = archive.read(canonical_source.split("#", 1)[1])
    with zipfile.ZipFile(archive_path, "w") as archive:
        if replacement_name is not None:
            archive.writestr(replacement_name, reviewed_xml)

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=conn,
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    with pytest.raises(ReprocessError, match="checksum became stale|member became stale"):
        registry.handler_for(archive_path, processed_root).handler(archive_path)

    assert conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert not (tmp_path / "vault").exists()


@pytest.mark.parametrize(
    ("decision_change", "match"),
    [
        ({"expected_occurrences": 2}, "expected 2 exact match"),
        ({"value_text": "Changed text"}, "expected 1 exact match"),
    ],
)
def test_retained_semantic_v5_rejects_stale_selector_before_replay(
    tmp_path: Path,
    decision_change: dict[str, object],
    match: str,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    changed_occurrences = decision_change.get("expected_occurrences", 1)
    assert isinstance(changed_occurrences, int)
    decision = _lab_resolution_decision(
        archive_path=archive_path,
        canonical_source=canonical_source,
        expected_occurrences=changed_occurrences,
        value_text=str(
            decision_change.get(
                "value_text",
                "No growth of greater than or equal to 1000 CFU/mL at 18 hours",
            )
        ),
    )
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [decision],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReprocessError, match=match):
        load_retained_history_decisions(
            decision_file=decision_file,
            processed_root=processed_root,
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("action", "action is unsupported"),
        ("person", "person does not match"),
        ("resolved_at", "must include a timezone"),
        ("code_system", "source LOINC code"),
        ("value_type", "C-CDA ST value"),
        ("missing_reason", "resolution fields must be exactly"),
        ("category", "category must be labs"),
        ("count_zero", "positive integer"),
        ("count_bool", "positive integer"),
        ("count_text", "positive integer"),
        ("top_level_value", "unsupported top-level fields"),
    ],
)
def test_retained_semantic_v5_rejects_malformed_resolution_contract(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision = _lab_resolution_decision(
        archive_path=archive_path,
        canonical_source=canonical_source,
    )
    resolution = decision["resolution"]
    assert isinstance(resolution, dict)
    selector = resolution["selector"]
    assert isinstance(selector, dict)
    if mutation == "action":
        resolution["action"] = "edit"
    elif mutation == "person":
        resolution["person_id"] = "other_person"
    elif mutation == "resolved_at":
        resolution["resolved_at"] = "2099-01-03T12:00:00"
    elif mutation == "code_system":
        selector["code_system"] = "2.16.840.1.113883.6.96"
    elif mutation == "value_type":
        selector["value_type"] = "PQ"
    elif mutation == "missing_reason":
        del resolution["reason"]
    elif mutation == "category":
        resolution["category"] = "history"
    elif mutation == "count_zero":
        resolution["expected_occurrences"] = 0
    elif mutation == "count_bool":
        resolution["expected_occurrences"] = True
    elif mutation == "count_text":
        resolution["expected_occurrences"] = "1"
    else:
        decision["value"] = "must not be accepted"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [decision],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReprocessError, match=match):
        load_retained_history_decisions(
            decision_file=decision_file,
            processed_root=processed_root,
        )


def test_retained_semantic_v5_rejects_duplicate_observation_selector(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision = _lab_resolution_decision(
        archive_path=archive_path,
        canonical_source=canonical_source,
    )
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [decision, decision],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReprocessError, match="duplicate retained observation"):
        load_retained_history_decisions(
            decision_file=decision_file,
            processed_root=processed_root,
        )


@pytest.mark.parametrize(
    (
        "action",
        "expected_occurrences",
        "expected_row_count",
        "expected_status",
        "expected_resolution",
    ),
    [
        ("accept_verbatim", 1, 1, "processed", "user_resolved"),
        ("accept_verbatim", 2, 1, "processed", "user_resolved"),
        ("reject", 1, 0, "processed", "user_resolved"),
        ("quarantine", 1, 0, "quarantined", "quarantined"),
    ],
)
def test_retained_semantic_v5_routes_exact_resolution_through_archive_replay(
    tmp_path: Path,
    action: str,
    expected_occurrences: int,
    expected_row_count: int,
    expected_status: str,
    expected_resolution: str,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(
        processed_root,
        occurrence_count=expected_occurrences,
    )
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=canonical_source,
                        action=action,
                        expected_occurrences=expected_occurrences,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    vault_root = tmp_path / "vault"
    person_note = vault_root / "Persons" / "dummy_person.md"
    person_note.parent.mkdir(parents=True)
    person_note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: person\n"
        "display_name: Dummy Person\n"
        "birth_year: 1980\n"
        "---\n"
        "# Dummy Person\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_person', 'Dummy Person')")
    conn.execute(
        """
        INSERT INTO audit_events (
            id, event_type, subject_type, subject_id, source_path, status, message
        )
        VALUES (
            'historical-rejection',
            'lab_observation_rejected',
            'lab_result',
            'dummy_person',
            ?,
            'awaiting_user_decision',
            'Historical rejection before attended resolution.'
        )
        """,
        (canonical_source,),
    )
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    for replay_index in range(2):
        if replay_index:
            registry = build_retained_reprocess_registry(
                root=tmp_path,
                vault_root=vault_root,
                processed_root=processed_root,
                conn=conn,
                batch_mode=True,
            )
        result = registry.handler_for(archive_path, processed_root).handler(archive_path)
        assert result.imported_rows == 1

    rows = conn.execute(
        """
        SELECT value, value_text, loinc_code, loinc_code_origin, source_type, source_path
        FROM lab_results
        WHERE test_name = 'Synthetic Urine Culture'
        """
    ).fetchall()
    assert len(rows) == expected_row_count
    if action == "accept_verbatim":
        assert rows == [
            (
                None,
                "No growth of greater than or equal to 1000 CFU/mL at 18 hours",
                "630-4",
                "source_provided",
                "source_import",
                canonical_source,
            )
        ]
        bundle, observation_count, *_ = build_fhir_bundle(
            conn,
            "dummy_person",
            generated_at=datetime(2099, 1, 3, tzinfo=UTC),
        )
        observations = [
            entry["resource"]
            for entry in bundle["entry"]
            if entry["resource"]["resourceType"] == "Observation"
        ]
        assert observation_count == 1
        assert observations[0]["valueString"] == rows[0][1]
        assert observations[0]["code"]["coding"][0]["code"] == "630-4"
        assert "valueQuantity" not in observations[0]
        assert "dataAbsentReason" not in observations[0]
        assert validate_fhir_bundle(bundle) == []

        lab_notes = [
            path
            for path in (vault_root / "Records" / "dummy_person" / "Labs").glob("*.md")
            if path.name != "_index.md"
        ]
        assert len(lab_notes) == 1
        note_content = lab_notes[0].read_text(encoding="utf-8")
        front_matter = parse_front_matter(note_content)
        assert front_matter["source_path"] == canonical_source
        note_rows = load_labs_from_note(lab_notes[0])
        assert note_rows[0]["value"] is None
        assert note_rows[0]["value_text"] == rows[0][1]
        assert note_rows[0]["loinc_code"] == "630-4"
        assert "[[_index|← Labs]]" in note_content
        index_content = (lab_notes[0].parent / "_index.md").read_text(encoding="utf-8")
        assert f"[[{lab_notes[0].stem}]] — source evidence" in index_content
    else:
        bundle, observation_count, *_ = build_fhir_bundle(
            conn,
            "dummy_person",
            generated_at=datetime(2099, 1, 3, tzinfo=UTC),
        )
        assert observation_count == 0
        assert all(entry["resource"]["resourceType"] != "Observation" for entry in bundle["entry"])
        lab_dir = vault_root / "Records" / "dummy_person" / "Labs"
        assert not lab_dir.exists() or not [
            path for path in lab_dir.glob("*.md") if path.name != "_index.md"
        ]

    resolution_events = conn.execute(
        """
        SELECT status, resolution, message
        FROM audit_events
        WHERE event_type = 'lab_observation_resolution_recorded'
        ORDER BY occurred_at, id
        """
    ).fetchall()
    assert len(resolution_events) == 2
    assert all(
        status == expected_status and resolution == expected_resolution
        for status, resolution, _ in resolution_events
    )
    assert all(
        all(
            expected in message
            for expected in (
                action,
                "No growth of greater than or equal to 1000 CFU/mL at 18 hours",
                "630-4",
                "vault_owner",
                "2099-01-03T12:00:00+00:00",
                "Reviewed against the retained C-CDA observation.",
                f"{expected_occurrences} exact rejected",
            )
        )
        for _, _, message in resolution_events
    )
    unresolved_events = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type = 'lab_observation_rejected'
          AND source_path = ?
        """,
        (canonical_source,),
    ).fetchone()[0]
    assert unresolved_events == 1


def test_retained_semantic_v5_cross_member_facts_converge_with_source_evidence(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    archive_path, first_source = _write_resolvable_ccda_archive(
        processed_root,
        member_count=2,
    )
    second_source = first_source.replace("DOC0001.XML", "DOC0002.XML")
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=source_path,
                    )
                    for source_path in (first_source, second_source)
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    vault_root = tmp_path / "vault"
    person_note = vault_root / "Persons" / "dummy_person.md"
    person_note.parent.mkdir(parents=True)
    person_note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: person\n"
        "display_name: Dummy Person\n"
        "birth_year: 1980\n"
        "---\n"
        "# Dummy Person\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
    conn.execute("INSERT INTO persons (id, display_name) VALUES ('dummy_person', 'Dummy Person')")
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=vault_root,
        processed_root=processed_root,
        conn=conn,
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    for _ in range(2):
        registry.handler_for(archive_path, processed_root).handler(archive_path)

    assert (
        conn.execute(
            """
        SELECT COUNT(*)
        FROM lab_results
        WHERE test_name = 'Synthetic Urine Culture'
        """
        ).fetchone()[0]
        == 1
    )
    dedup_rows = conn.execute(
        """
        SELECT suppressed_source_path, owner_source_path, payload
        FROM dedup_links
        WHERE subject_type = 'lab_result'
        """
    ).fetchall()
    assert len(dedup_rows) == 1
    assert set(dedup_rows[0][:2]) == {first_source, second_source}
    dedup_payload = json.loads(dedup_rows[0][2])
    assert dedup_payload["value_text"] == (
        "No growth of greater than or equal to 1000 CFU/mL at 18 hours"
    )
    assert dedup_payload["loinc_code"] == "630-4"

    evidence_sources = {
        str(parse_front_matter(path.read_text(encoding="utf-8"))["source_path"])
        for path in (vault_root / "Records" / "dummy_person" / "Labs").glob("*.md")
        if path.name != "_index.md"
    }
    assert evidence_sources == {first_source, second_source}


@pytest.mark.parametrize("decision_flag", ["--retained-decisions", "--history-decisions"])
def test_reprocess_source_cli_applies_v5_with_canonical_and_compatibility_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision_flag: str,
) -> None:
    reprocess_source_module = importlib.import_module("scripts.reprocess_source")
    processed_root = tmp_path / "data" / "processed"
    archive_path, canonical_source = _write_resolvable_ccda_archive(processed_root)
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V5,
                "decisions": [
                    _lab_resolution_decision(
                        archive_path=archive_path,
                        canonical_source=canonical_source,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "famed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL + AUDIT_EVENTS_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO persons (id, display_name) VALUES ('dummy_person', 'Dummy Person')"
        )
    vault_root = tmp_path / "famed-vault"
    person_note = vault_root / "Persons" / "dummy_person.md"
    person_note.parent.mkdir(parents=True)
    person_note.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: person\n"
        "display_name: Dummy Person\n"
        "birth_year: 1980\n"
        "---\n"
        "# Dummy Person\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(reprocess_source_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reprocess_source",
            str(archive_path.relative_to(tmp_path)),
            decision_flag,
            str(decision_file),
        ],
    )

    reprocess_source_module.main()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT value, value_text, loinc_code, loinc_code_origin, source_path
            FROM lab_results
            WHERE test_name = 'Synthetic Urine Culture'
            """
        ).fetchone()
    assert row == (
        None,
        "No growth of greater than or equal to 1000 CFU/mL at 18 hours",
        "630-4",
        "source_provided",
        canonical_source,
    )


@pytest.mark.parametrize(
    ("schema_version", "source_category", "filename", "extraction", "match"),
    [
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA,
            "History",
            "visit.pdf",
            _lab_decision_payload("dummy_person"),
            "bounded Labs study",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA,
            "History",
            "visit.pdf",
            _medication_decision_payload("dummy_person"),
            "unsupported",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA,
            "Labs",
            "ordinary-panel.pdf",
            _history_decision_payload("dummy_person"),
            "bounded domain-study",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
            "History",
            "visit.pdf",
            _medication_decision_payload("dummy_person"),
            "requires a retained Medications source",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
            "Medications",
            "list",
            _history_decision_payload("dummy_person"),
            "only supports medication extraction",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
            "Medications",
            "list.pdf",
            _medication_decision_payload("dummy_person"),
            "extensionless Medications list",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
            "History",
            "ordinary-history.pdf",
            _medication_decision_payload("dummy_person"),
            "visit-summary PDF",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
            "History",
            "visitSummary.pdf",
            _medication_decision_payload("dummy_person"),
            "visit-summary PDF",
        ),
        (
            RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
            "History",
            "2099-01-02-visitSummary.xml",
            _medication_decision_payload("dummy_person"),
            "visit-summary PDF",
        ),
    ],
)
def test_retained_semantic_decisions_reject_out_of_scope_categories(
    tmp_path: Path,
    schema_version: str,
    source_category: str,
    filename: str,
    extraction: dict[str, object],
    match: str,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / source_category / filename
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained evidence")
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "decisions": [
                    {
                        "source_path": (
                            f"data/processed/dummy_person/{source_category}/{filename}"
                        ),
                        "source_sha256": source_file_checksum(source),
                        "extraction": extraction,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReprocessError, match=match):
        load_retained_history_decisions(
            decision_file=decision_file,
            processed_root=processed_root,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("source_sha256", "0" * 64, "checksum"),
        ("person_id", "other_person", "person"),
    ],
)
def test_history_decisions_reject_wrong_source_or_person(
    tmp_path: Path,
    field: str,
    value: str,
    match: str,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "History" / "visit.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained evidence")
    extraction = _history_decision_payload("dummy_person")
    decision: dict[str, object] = {
        "source_path": "data/processed/dummy_person/History/visit.pdf",
        "source_sha256": source_file_checksum(source),
        "extraction": extraction,
    }
    if field == "person_id":
        extraction["person"] = {"person_id": value, "confidence": 0.99}
    else:
        decision[field] = value
    decision_file = tmp_path / "history-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "famed.history-extraction-decisions.v1",
                "decisions": [decision],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReprocessError, match=match):
        load_retained_history_decisions(
            decision_file=decision_file,
            processed_root=processed_root,
        )


def test_batch_registry_passes_history_decisions_to_history_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_reprocess(**kwargs: object) -> ReprocessResult:
        captured.update(kwargs)
        return ReprocessResult(1, {"dummy_person"})

    class FixtureInterpreter:
        def extract_facts(self, source_path: Path) -> Mapping[str, Any]:
            _ = source_path
            return {}

    interpreter = FixtureInterpreter()
    monkeypatch.setattr(
        "famed.retained_replay.reprocess_retained_history_source",
        fake_reprocess,
    )
    processed_root = tmp_path / "isolated-copy"
    source_path = processed_root / "dummy_person" / "History" / "visit.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"fixture")
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
        history_fact_interpreter=interpreter,
    )

    registry.handler_for(source_path, processed_root).handler(source_path)

    assert captured["fact_interpreter"] is interpreter


def test_bounded_study_replay_dispatches_attended_labs_through_canonicalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "Labs" / "2099-01-02-HUMPHREY VISUAL FIELD.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained evidence")
    canonical_source = "data/processed/dummy_person/Labs/2099-01-02-HUMPHREY VISUAL FIELD.pdf"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA,
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _lab_decision_payload("dummy_person"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "famed.retained_replay.reprocess_retained_history_source",
        lambda **_kwargs: ReprocessResult(0, {"dummy_person"}),
    )

    def capture_persist(**kwargs: object) -> list[Path]:
        captured.update(kwargs)
        dedup_stats = kwargs["dedup_stats"]
        assert isinstance(dedup_stats, dict)
        dedup_stats["inserted"] = 1
        return []

    monkeypatch.setattr("famed.retained_replay.persist_lab_rows", capture_persist)
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    result = registry.handler_for(source, processed_root).handler(source)

    assert result.imported_rows == 1
    assert result.affected_person_ids == {"dummy_person"}
    assert captured["processed_source_path"] == canonical_source
    assert captured["retained_source"] is True
    assert captured["refresh_reports"] is False
    rows = captured["rows"]
    assert isinstance(rows, list)
    assert rows[0]["test_name"] == "Synthetic Mean Deviation OD"
    assert rows[0]["value"] == -1.25
    assert rows[0]["unit"] == "dB"


def test_medication_source_replay_dispatches_attended_decision_through_canonicalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "Medications" / "2099-01-02-list"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Examplemed 10 mg")
    canonical_source = "data/processed/dummy_person/Medications/2099-01-02-list"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V3,
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _medication_decision_payload("dummy_person"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "famed.retained_replay.reprocess_retained_supplement_source",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("attended decision must replace the lossy parser")
        ),
    )

    def capture_persist(**kwargs: object) -> Path:
        captured.update(kwargs)
        return tmp_path / "vault" / "Records" / "dummy_person" / "Medications" / "list.md"

    monkeypatch.setattr("famed.retained_replay.persist_medication_rows", capture_persist)
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    result = registry.handler_for(source, processed_root).handler(source)

    assert result.imported_rows == 1
    assert result.affected_person_ids == {"dummy_person"}
    assert captured["processed_source_path"] == canonical_source
    assert captured["retained_source"] is True
    assert captured["refresh_reports"] is False
    rows = captured["rows"]
    assert isinstance(rows, list)
    assert rows[0]["medication_name"] == "Examplemed"
    assert rows[0]["dose"] == "10 mg"
    assert rows[0]["medication_type"] == "prescription"


def test_history_visit_summary_replay_dispatches_attended_medications_through_canonicalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "History" / "2099-01-02-visitSummary.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic retained visit summary")
    canonical_source = "data/processed/dummy_person/History/2099-01-02-visitSummary.pdf"
    decision_file = tmp_path / "retained-decisions.json"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": RETAINED_SEMANTIC_DECISIONS_SCHEMA_V4,
                "decisions": [
                    {
                        "source_path": canonical_source,
                        "source_sha256": source_file_checksum(source),
                        "extraction": _medication_decision_payload("dummy_person"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    decisions = load_retained_history_decisions(
        decision_file=decision_file,
        processed_root=processed_root,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "famed.retained_replay.reprocess_retained_history_source",
        lambda **_kwargs: ReprocessResult(1, {"dummy_person"}),
    )

    def capture_persist(**kwargs: object) -> Path:
        captured.update(kwargs)
        return tmp_path / "medication-note.md"

    monkeypatch.setattr(
        "famed.retained_replay.persist_medication_rows",
        capture_persist,
    )
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
        history_fact_interpreter=decisions,
    )

    result = registry.handler_for(source, processed_root).handler(source)

    assert result.imported_rows == 2
    assert result.affected_person_ids == {"dummy_person"}
    assert captured["processed_source_path"] == canonical_source
    assert captured["source_type"] == "ai_extracted"
    assert captured["retained_source"] is True
    assert captured["refresh_reports"] is False
    rows = captured["rows"]
    assert isinstance(rows, list)
    assert rows[0]["medication_name"] == "Examplemed"
    assert rows[0]["dose"] == "10 mg"
    assert rows[0]["medication_type"] == "prescription"


def test_medication_source_replay_uses_bounded_fallback_without_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "isolated-processed"
    source = processed_root / "dummy_person" / "Medications" / "2099-01-02-list"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Examplemed 10 mg")
    calls: list[Path] = []

    def capture_fallback(**kwargs: object) -> ReprocessResult:
        source_path = kwargs["source_path"]
        assert isinstance(source_path, Path)
        calls.append(source_path)
        return ReprocessResult(1, {"dummy_person"})

    monkeypatch.setattr(
        "famed.retained_replay.reprocess_retained_supplement_source",
        capture_fallback,
    )
    registry = build_retained_reprocess_registry(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
        batch_mode=True,
    )

    result = registry.handler_for(source, processed_root).handler(source)

    assert result.imported_rows == 1
    assert calls == [source]


def test_batch_medication_refreshes_fhir_once_per_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "processed"
    for name in ["first", "second"]:
        source = processed_root / "dummy_person" / "Medications" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("fixture", encoding="utf-8")

    class MedicationRegistry:
        def handler_for(self, source_path: Path, _processed_root: Path) -> ReprocessRegistration:
            return ReprocessRegistration(
                name="supplement_list",
                category_parts=("Medications",),
                suffixes=frozenset({""}),
                handler=lambda source_path: ReprocessResult(1, {"dummy_person"}),
            )

    calls: list[str] = []
    monkeypatch.setattr(
        "famed.retained_replay.build_retained_reprocess_registry",
        lambda **_kwargs: MedicationRegistry(),
    )
    monkeypatch.setattr(
        "famed.retained_replay.write_current_medications_report", lambda *_args: None
    )
    monkeypatch.setattr(
        "famed.retained_replay.write_medication_conflicts_report", lambda *_args: None
    )
    monkeypatch.setattr(
        "famed.retained_replay.refresh_medication_dependent_reports", lambda *_args: None
    )
    monkeypatch.setattr(
        "famed.retained_replay.refresh_fhir_dependent_export",
        lambda _conn, _vault, person_id: calls.append(person_id),
    )

    replay_retained_medical_sources(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
    )

    assert calls == ["dummy_person"]


def test_batch_defers_user_update_snapshots_to_authority_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "processed"
    source = processed_root / "dummy_person" / "Medications" / "dated-update"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: 2099-01-01\n"
        "---\n",
        encoding="utf-8",
    )

    class UnexpectedRegistry:
        def handler_for(self, *_args: object) -> ReprocessRegistration:
            raise AssertionError("authority overlay source must not reach medical handlers")

    monkeypatch.setattr(
        "famed.retained_replay.build_retained_reprocess_registry",
        lambda **_kwargs: UnexpectedRegistry(),
    )

    summary = replay_retained_medical_sources(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
    )

    assert summary.count("unsupported") == 1
    assert (
        summary.items[0].message == "user-update snapshot deferred to the authority-overlay stage"
    )


def test_authority_backed_medication_narrative_requires_exact_snapshot_match(
    tmp_path: Path,
) -> None:
    processed_root = tmp_path / "processed"
    medications = processed_root / "dummy_person" / "Medications"
    medications.mkdir(parents=True)
    narrative = medications / "2099-01-01 Synthetic Medication Updates"
    narrative.write_text(
        "Synthetic Medication Updates\n\nCurrent:\n\n"
        "Examplemed 25 mg from 2099-01-02; 50 mg from 2099-01-05; "
        "100 mg from 2099-01-09 (daily)\n",
        encoding="utf-8",
    )
    snapshot = medications / "2099-01-01-dummy_person-medications"
    snapshot.write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: '2099-01-01'\n"
        "---\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency "
        "| status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Examplemed | prescription |  | 25 mg | daily | active | 2099-01-02 "
        "| 2099-01-04 |  |\n"
        "| Examplemed | prescription |  | 50 mg | daily | active | 2099-01-05 "
        "| 2099-01-08 |  |\n"
        "| Examplemed | prescription |  | 100 mg | daily | active | 2099-01-09 |  |  |\n",
        encoding="utf-8",
    )

    assert authority_backed_medication_narrative_matches_snapshot(
        narrative,
        processed_root,
    )

    snapshot_content = snapshot.read_text(encoding="utf-8")
    snapshot.write_text(
        snapshot_content.replace("date: '2099-01-01'", "date: '2099-01-02'"),
        encoding="utf-8",
    )
    assert not authority_backed_medication_narrative_matches_snapshot(
        narrative,
        processed_root,
    )

    snapshot.write_text(
        snapshot_content.replace("100 mg", "75 mg"),
        encoding="utf-8",
    )
    assert not authority_backed_medication_narrative_matches_snapshot(
        narrative,
        processed_root,
    )


def test_batch_defers_exact_authority_backed_medication_narrative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "processed"
    medications = processed_root / "dummy_person" / "Medications"
    medications.mkdir(parents=True)
    (medications / "2099-01-01 Synthetic Medication Updates").write_text(
        "Synthetic Medication Updates\n\nCurrent:\n\n"
        "Examplemed 25 mg from 2099-01-02; 50 mg from 2099-01-05 (daily)\n",
        encoding="utf-8",
    )
    (medications / "2099-01-01-dummy_person-medications").write_text(
        "---\n"
        "person_id: dummy_person\n"
        "record_type: medication\n"
        "category: medications\n"
        "date: '2099-01-01'\n"
        "---\n\n"
        "| medication_name | medication_type | rxnorm_cui | dose | frequency "
        "| status | start_date | end_date | evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Examplemed | prescription |  | 25 mg | daily | active | 2099-01-02 "
        "| 2099-01-04 |  |\n"
        "| Examplemed | prescription |  | 50 mg | daily | active | 2099-01-05 |  |  |\n",
        encoding="utf-8",
    )

    class UnexpectedRegistry:
        def handler_for(self, *_args: object) -> ReprocessRegistration:
            raise AssertionError("authority-backed narrative must not reach medical handlers")

    monkeypatch.setattr(
        "famed.retained_replay.build_retained_reprocess_registry",
        lambda **_kwargs: UnexpectedRegistry(),
    )

    summary = replay_retained_medical_sources(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
    )

    assert summary.count("unsupported") == 2
    narrative_item = next(
        item for item in summary.items if "Medication Updates" in item.source_path.name
    )
    assert (
        narrative_item.message
        == "authority-backed medication narrative deferred to the authority-overlay stage"
    )


def test_batch_deduplicates_fhir_refresh_for_mixed_lab_and_medication_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "processed"
    sources = (
        processed_root / "dummy_person" / "Labs" / "panel.csv",
        processed_root / "dummy_person" / "Medications" / "list",
    )
    for source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("fixture", encoding="utf-8")

    class MixedRegistry:
        def handler_for(self, source_path: Path, _processed_root: Path) -> ReprocessRegistration:
            name = "labs" if "Labs" in source_path.parts else "supplement_list"
            return ReprocessRegistration(
                name=name,
                category_parts=(source_path.parent.name,),
                suffixes=frozenset({source_path.suffix}),
                handler=lambda source_path: ReprocessResult(1, {"dummy_person"}),
            )

    calls: list[str] = []
    monkeypatch.setattr(
        "famed.retained_replay.build_retained_reprocess_registry",
        lambda **_kwargs: MixedRegistry(),
    )
    monkeypatch.setattr(
        "famed.retained_replay.refresh_lab_dependent_reports",
        lambda _conn, _vault, person_id: calls.append(person_id),
    )
    monkeypatch.setattr(
        "famed.retained_replay.write_current_medications_report", lambda *_args: None
    )
    monkeypatch.setattr(
        "famed.retained_replay.write_medication_conflicts_report", lambda *_args: None
    )
    monkeypatch.setattr(
        "famed.retained_replay.refresh_medication_dependent_reports", lambda *_args: None
    )
    monkeypatch.setattr(
        "famed.retained_replay.refresh_fhir_dependent_export",
        lambda _conn, _vault, person_id: calls.append(person_id),
    )

    replay_retained_medical_sources(
        root=tmp_path,
        vault_root=tmp_path / "vault",
        processed_root=processed_root,
        conn=sqlite3.connect(":memory:"),
    )

    assert calls == ["dummy_person"]


def test_batch_refresh_failure_restores_report_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_root = tmp_path / "processed"
    source = processed_root / "dummy_person" / "Labs" / "panel.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("fixture", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE scratch (value TEXT)")
    vault_root = tmp_path / "vault"

    class LabRegistry:
        def handler_for(self, source_path: Path, _processed_root: Path) -> ReprocessRegistration:
            def handler(source_path: Path) -> ReprocessResult:
                _ = source_path
                note = vault_root / "Records" / "dummy_person" / "Labs" / "panel.md"
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text("canonical source note", encoding="utf-8")
                conn.execute("INSERT INTO scratch VALUES ('persisted')")
                return ReprocessResult(1, {"dummy_person"})

            return ReprocessRegistration(
                name="labs",
                category_parts=("Labs",),
                suffixes=frozenset({".csv"}),
                handler=handler,
            )

    def fail_refresh(_conn: sqlite3.Connection, vault: Path, person_id: str) -> None:
        report = vault / "Records" / person_id / "Reports" / "partial.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("partial report", encoding="utf-8")
        raise ValueError("refresh failed")

    monkeypatch.setattr(
        "famed.retained_replay.build_retained_reprocess_registry",
        lambda **_kwargs: LabRegistry(),
    )
    monkeypatch.setattr("famed.retained_replay.refresh_lab_dependent_reports", fail_refresh)

    with pytest.raises(ValueError, match="refresh failed"):
        replay_retained_medical_sources(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
        )

    assert conn.execute("SELECT value FROM scratch").fetchall() == [("persisted",)]
    assert (vault_root / "Records" / "dummy_person" / "Labs" / "panel.md").exists()
    assert not (vault_root / "Records" / "dummy_person" / "Reports" / "partial.md").exists()
