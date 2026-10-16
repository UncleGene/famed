from __future__ import annotations

import json
from pathlib import Path

import pytest

from famed.findings_ledger import (
    FindingsLedgerError,
    active_findings,
    current_findings,
    load_events,
    main,
    mark_findings_stale_for_source,
    mark_orphaned_findings_stale,
    record_finding,
    supersede_finding,
    supersede_stale_variants,
)


def test_record_finding_appends_jsonl_with_sources(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "findings.jsonl"

    event = record_finding(
        ledger,
        finding_id="finding-1",
        person_id="dummy_alice",
        domain="Medication Safety",
        statement="Clopidogrel has CPIC coverage for CYP2C19.",
        evidence_tier="E1",
        confidence=0.95,
        evidence_check="verified",
        source_paths=[
            "Records/dummy_alice/Reports/pgx-coverage.md",
            "Records/dummy_alice/Reports/pgx-coverage.md",
        ],
    )

    lines = ledger.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    assert event.finding_id == "finding-1"
    assert payload["event_type"] == "finding_recorded"
    assert payload["source_paths"] == ["Records/dummy_alice/Reports/pgx-coverage.md"]
    assert payload["status"] == "active"
    assert active_findings(ledger)[0].statement == "Clopidogrel has CPIC coverage for CYP2C19."


def test_record_finding_requires_sources_and_verified_high_evidence(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "findings.jsonl"

    with pytest.raises(FindingsLedgerError, match="at least one source path"):
        record_finding(
            ledger,
            person_id="dummy_alice",
            domain="Medication Safety",
            statement="No source.",
            evidence_tier="E3",
            confidence=0.5,
            source_paths=[],
        )

    with pytest.raises(FindingsLedgerError, match="E1/E2"):
        record_finding(
            ledger,
            person_id="dummy_alice",
            domain="Medication Safety",
            statement="High evidence but unchecked.",
            evidence_tier="E1",
            confidence=0.9,
            source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
        )


def test_supersede_appends_without_mutating_original(tmp_path: Path) -> None:
    ledger = tmp_path / "findings.jsonl"
    record_finding(
        ledger,
        finding_id="old",
        person_id="dummy_alice",
        domain="Medication Safety",
        statement="Old finding.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )
    record_finding(
        ledger,
        finding_id="new",
        person_id="dummy_alice",
        domain="Medication Safety",
        statement="New finding.",
        evidence_tier="E3",
        confidence=0.7,
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )

    supersede_finding(
        ledger,
        finding_id="old",
        superseded_by="new",
        reason="evidence rule updated",
    )

    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    current = current_findings(load_events(ledger))
    assert current["old"].status == "superseded"
    assert current["old"].superseded_by == "new"
    assert current["new"].status == "active"


def test_supersede_requires_existing_active_findings(tmp_path: Path) -> None:
    ledger = tmp_path / "findings.jsonl"
    record_finding(
        ledger,
        finding_id="old",
        person_id="dummy_alice",
        domain="Medication Safety",
        statement="Old finding.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )

    with pytest.raises(FindingsLedgerError, match="finding not found: new"):
        supersede_finding(
            ledger,
            finding_id="old",
            superseded_by="new",
            reason="missing replacement",
        )


def test_mark_findings_stale_for_source_only_marks_matching_active_findings(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "findings.jsonl"
    record_finding(
        ledger,
        finding_id="match",
        person_id="dummy_alice",
        domain="Medication Safety",
        statement="Uses corrected source.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/current-medications.md"],
    )
    record_finding(
        ledger,
        finding_id="other-person",
        person_id="dummy_bob",
        domain="Medication Safety",
        statement="Same source path but different person.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/current-medications.md"],
    )
    record_finding(
        ledger,
        finding_id="other-source",
        person_id="dummy_alice",
        domain="Medication Safety",
        statement="Different source.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )

    events = mark_findings_stale_for_source(
        ledger,
        person_id="dummy_alice",
        source_path="Records/dummy_alice/Reports/current-medications.md",
        reason="medication correction",
    )

    current = current_findings(load_events(ledger))
    assert [event.finding_id for event in events] == ["match"]
    assert current["match"].status == "stale"
    assert current["other-person"].status == "active"
    assert current["other-source"].status == "active"


def test_mark_orphaned_findings_stale_is_scoped_to_person_and_domain(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "findings.jsonl"
    _record(ledger, "retained")
    _record(ledger, "orphaned")
    record_finding(
        ledger,
        finding_id="other-domain",
        person_id="dummy_alice",
        domain="carrier_screening",
        statement="Other domain.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_alice/Reports/carrier-screening.md"],
    )
    record_finding(
        ledger,
        finding_id="other-person",
        person_id="dummy_bob",
        domain="pgx_coverage",
        statement="Other person.",
        evidence_tier="E3",
        confidence=0.6,
        source_paths=["Records/dummy_bob/Reports/pgx-coverage.md"],
    )

    events = mark_orphaned_findings_stale(
        ledger,
        person_id="dummy_alice",
        domain="pgx_coverage",
        current_finding_ids={"retained"},
        reason="producer recomputed current identities",
    )

    assert [event.finding_id for event in events] == ["orphaned"]
    active_ids = {finding.finding_id for finding in active_findings(ledger)}
    assert active_ids == {"retained", "other-domain", "other-person"}


def test_cli_records_and_lists_current_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "findings.jsonl"

    assert (
        main(
            [
                "--ledger-path",
                str(ledger),
                "record",
                "--finding-id",
                "finding-1",
                "--person-id",
                "dummy_alice",
                "--domain",
                "Medication Safety",
                "--statement",
                "Clopidogrel has CPIC coverage.",
                "--evidence-tier",
                "E3",
                "--confidence",
                "0.7",
                "--source-path",
                "Records/dummy_alice/Reports/pgx-coverage.md",
            ]
        )
        == 0
    )
    assert main(["--ledger-path", str(ledger), "list-current"]) == 0

    output = capsys.readouterr().out
    assert "recorded finding finding-1" in output
    assert "finding-1\tdummy_alice\tE3\tClopidogrel has CPIC coverage." in output


def _record(ledger_path: Path, finding_id: str) -> None:
    record_finding(
        ledger_path,
        finding_id=finding_id,
        person_id="dummy_alice",
        domain="pgx_coverage",
        statement=f"Finding {finding_id}.",
        evidence_tier="E1",
        confidence=0.85,
        evidence_check="verified",
        source_paths=["Records/dummy_alice/Reports/pgx-coverage.md"],
    )


def test_supersede_stale_variants_supersedes_active_candidates(tmp_path: Path) -> None:
    ledger_path = tmp_path / "findings.jsonl"
    _record(ledger_path, "old-finding")
    _record(ledger_path, "new-finding")

    superseded = supersede_stale_variants(
        ledger_path,
        new_finding_id="new-finding",
        stale_candidate_ids=["old-finding", "absent-finding"],
        reason="state updated",
    )

    assert superseded == ["old-finding"]
    active_ids = {f.finding_id for f in active_findings(ledger_path)}
    assert active_ids == {"new-finding"}


def test_supersede_stale_variants_noop_when_no_active_candidates(tmp_path: Path) -> None:
    ledger_path = tmp_path / "findings.jsonl"
    _record(ledger_path, "new-finding")

    superseded = supersede_stale_variants(
        ledger_path,
        new_finding_id="new-finding",
        stale_candidate_ids=["absent-1", "absent-2"],
        reason="state updated",
    )

    assert superseded == []
    assert len(active_findings(ledger_path)) == 1


def test_supersede_stale_variants_skips_new_finding_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "findings.jsonl"
    _record(ledger_path, "current-finding")

    superseded = supersede_stale_variants(
        ledger_path,
        new_finding_id="current-finding",
        stale_candidate_ids=["current-finding"],
        reason="state updated",
    )

    assert superseded == []
    assert len(active_findings(ledger_path)) == 1


def test_supersede_stale_variants_accepts_pre_built_active_ids(tmp_path: Path) -> None:
    ledger_path = tmp_path / "findings.jsonl"
    _record(ledger_path, "old-finding")
    _record(ledger_path, "new-finding")
    active_ids = {f.finding_id for f in active_findings(ledger_path)}

    superseded = supersede_stale_variants(
        ledger_path,
        new_finding_id="new-finding",
        stale_candidate_ids=["old-finding"],
        reason="state updated",
        active_ids=active_ids,
    )

    assert superseded == ["old-finding"]
    assert "old-finding" not in active_ids
