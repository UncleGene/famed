from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from famed import dependent_reports
from famed.setup import SCHEMA_SQL

# Full DNA-triggered (and household, which runs the DNA cascade per person)
# refresh order. The medication-triggered order is this minus DNA-only reports —
# medications don't change imputation readiness or carrier status.
_FULL_CASCADE_ORDER: list[str] = [
    "genetics",
    "alerts",
    "health_advisory",
]

_LAB_CASCADE_ORDER: list[str] = [
    "lab_results",
    "lab_conflicts",
    "flagged_labs",
    "fhir",
    "alerts",
    "health_advisory",
]

# Filenames the patched writers return, keyed by the same call-order names.
_CASCADE_FILENAMES: dict[str, str] = {
    "genetics": "genetics.md",
    "alerts": "medication-safety-alerts.md",
    "health_advisory": "health-advisory.md",
    "lab_results": "lab-results.md",
    "lab_conflicts": "lab-conflicts.md",
    "flagged_labs": "flagged-for-discussion.md",
    "fhir": "fhir-export.json",
}
_PENDING_REVIEW_FILENAME = "pending-review.md"


def _write_person_note(vault_root: Path, person_id: str = "dummy_alice") -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    display_name = person_id.title()
    person_note.write_text(
        "---\n"
        f"person_id: {person_id}\n"
        "record_type: person\n"
        f"display_name: {display_name}\n"
        "---\n"
        f"# {display_name}\n",
        encoding="utf-8",
    )


def _patch_first_tier_writers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conn: sqlite3.Connection,
    vault_root: Path,
    ref_db: Path,
    ledger: Path,
    calls: list[str],
) -> None:
    def genetics_writer(passed_conn, passed_vault, person_id, ref_db_path, *, findings_ledger_path):
        assert passed_conn is conn
        assert passed_vault == vault_root
        assert person_id == "dummy_alice"
        assert ref_db_path == ref_db
        # Genetics owns the PGx ledger findings (ADR-031).
        assert findings_ledger_path == ledger
        calls.append("genetics")
        return tmp_path / _CASCADE_FILENAMES["genetics"]

    monkeypatch.setattr(dependent_reports, "write_genetics_report", genetics_writer)


def _patch_composite_writers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_root: Path,
    calls: list[str],
) -> None:
    """Patch every second- and third-tier composite the cascade reaches.

    Each fake records its name into the shared `calls` list so a single
    ordering assertion covers the whole graph.
    """

    def make_writer(name: str):
        def writer(passed_vault, person_id, **kwargs):
            assert passed_vault == vault_root
            assert person_id == "dummy_alice"
            calls.append(name)
            return tmp_path / _CASCADE_FILENAMES[name], 0

        return writer

    monkeypatch.setattr(dependent_reports, "write_health_advisory", make_writer("health_advisory"))
    # write_medication_safety_alerts is imported lazily inside the wrapper (to
    # break a circular import), so it must be patched at its source module.
    monkeypatch.setattr(
        "famed.medication_safety_alerts.write_medication_safety_alerts",
        make_writer("alerts"),
    )
    # These cascade-ordering tests use a schema-less in-memory DB and mock all
    # writers; patch the data-presence guard so composites aren't skipped.
    monkeypatch.setattr(dependent_reports, "_person_has_medical_data", lambda *a, **k: True)


def test_refresh_dna_dependent_reports_writes_full_cascade_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    vault_root = tmp_path / "vault"
    ref_db = tmp_path / "reference.db"
    ledger = tmp_path / "ledger.jsonl"
    calls: list[str] = []
    _write_person_note(vault_root)

    _patch_first_tier_writers(monkeypatch, tmp_path, conn, vault_root, ref_db, ledger, calls)
    _patch_composite_writers(monkeypatch, tmp_path, vault_root, calls)

    paths = dependent_reports.refresh_dna_dependent_reports(
        conn,
        vault_root,
        "dummy_alice",
        ref_db_path=ref_db,
        findings_ledger_path=ledger,
    )

    assert calls == _FULL_CASCADE_ORDER
    assert [path.name for path in paths] == [
        _CASCADE_FILENAMES[name] for name in _FULL_CASCADE_ORDER
    ]


def test_refresh_medication_dependent_reports_writes_pgx_and_composite_cascade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    vault_root = tmp_path / "vault"
    ref_db = tmp_path / "reference.db"
    calls: list[str] = []
    _write_person_note(vault_root)

    def carrier_writer(*args, **kwargs):
        raise AssertionError("medication changes must not regenerate carrier screening")

    monkeypatch.setattr(
        dependent_reports,
        "write_genetics_report",
        lambda *_a, **_k: calls.append("genetics") or tmp_path / "genetics.md",
    )
    _patch_composite_writers(monkeypatch, tmp_path, vault_root, calls)

    paths = dependent_reports.refresh_medication_dependent_reports(
        conn,
        vault_root,
        "dummy_alice",
        ref_db_path=ref_db,
        findings_ledger_path=None,
    )

    expected_order = _FULL_CASCADE_ORDER
    assert calls == expected_order
    assert [path.name for path in paths] == [
        *[_CASCADE_FILENAMES[name] for name in expected_order[:1]],
        _PENDING_REVIEW_FILENAME,
        *[_CASCADE_FILENAMES[name] for name in expected_order[1:]],
    ]


def test_refresh_lab_dependent_reports_writes_full_cascade_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    vault_root = tmp_path / "vault"
    ref_db = tmp_path / "reference.db"
    calls: list[str] = []
    _write_person_note(vault_root)

    def make_lab_writer(name: str):
        def writer(*args, **kwargs):
            calls.append(name)
            return tmp_path / _CASCADE_FILENAMES[name], 0

        return writer

    monkeypatch.setattr(
        "famed.lab_sync.write_lab_results_report",
        make_lab_writer("lab_results"),
    )
    monkeypatch.setattr(
        "famed.lab_sync.write_lab_conflicts_report",
        make_lab_writer("lab_conflicts"),
    )
    monkeypatch.setattr(
        "famed.lab_sync.write_flagged_for_discussion_report",
        make_lab_writer("flagged_labs"),
    )

    def fhir_writer(*args, **kwargs):
        calls.append("fhir")
        return tmp_path / _CASCADE_FILENAMES["fhir"]

    monkeypatch.setattr(dependent_reports, "refresh_fhir_dependent_export", fhir_writer)
    _patch_composite_writers(monkeypatch, tmp_path, vault_root, calls)

    paths = dependent_reports.refresh_lab_dependent_reports(
        conn,
        vault_root,
        "dummy_alice",
        ref_db_path=ref_db,
        findings_ledger_path=None,
    )

    assert calls == _LAB_CASCADE_ORDER
    assert [path.name for path in paths] == [
        *[_CASCADE_FILENAMES[name] for name in _LAB_CASCADE_ORDER[:3]],
        _PENDING_REVIEW_FILENAME,
        *[_CASCADE_FILENAMES[name] for name in _LAB_CASCADE_ORDER[3:]],
    ]


def test_refresh_lab_dependent_reports_surfaces_first_tier_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    vault_root = tmp_path / "vault"

    monkeypatch.setattr(
        "famed.lab_sync.write_lab_results_report",
        lambda *args, **kwargs: (tmp_path / "lab-results.md", 1),
    )

    def fail_conflicts(*args, **kwargs):
        raise OSError("conflict report write failed")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the cascade must stop after the first failed writer")

    monkeypatch.setattr("famed.lab_sync.write_lab_conflicts_report", fail_conflicts)
    monkeypatch.setattr("famed.lab_sync.write_flagged_for_discussion_report", fail_if_called)

    with pytest.raises(OSError, match="conflict report write failed"):
        dependent_reports.refresh_lab_dependent_reports(
            conn,
            vault_root,
            "dummy_alice",
            findings_ledger_path=None,
        )


def test_refresh_medication_dependent_reports_skips_composites_before_person_onboarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Medication data can arrive before the person note is written (path-based
    inbox routing does not require pre-registration). First-tier reports must
    still regenerate; composites need identity context they don't have yet and
    are skipped — not failed — until onboarding writes the note (ADR-025's
    `has_fhir_export_surface` gating pattern, applied to composites).
    """
    conn = sqlite3.connect(":memory:")
    vault_root = tmp_path / "vault"
    ref_db = tmp_path / "reference.db"
    calls: list[str] = []

    def fail_if_called(*args, **kwargs):
        raise AssertionError("composite reports must not run before the person note exists")

    monkeypatch.setattr(
        dependent_reports,
        "write_genetics_report",
        lambda *_a, **_k: calls.append("genetics") or tmp_path / "genetics.md",
    )
    monkeypatch.setattr(dependent_reports, "write_health_advisory", fail_if_called)
    monkeypatch.setattr(
        "famed.medication_safety_alerts.write_medication_safety_alerts", fail_if_called
    )

    assert not dependent_reports.has_composite_report_surface(vault_root, "dummy_alice")

    paths = dependent_reports.refresh_medication_dependent_reports(
        conn, vault_root, "dummy_alice", ref_db_path=ref_db, findings_ledger_path=None
    )

    assert calls == ["genetics"]
    assert [path.name for path in paths] == [_CASCADE_FILENAMES["genetics"]]


def test_refresh_derived_reports_for_household_runs_full_cascade_per_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    vault_root = tmp_path / "vault"
    persons_dir = vault_root / "Persons"
    persons_dir.mkdir(parents=True)
    for person_id in ("dummy_alice", "dummy_bob"):
        (persons_dir / f"{person_id}.md").write_text(
            f"---\nperson_id: {person_id}\n---\n# {person_id.title()}\n", encoding="utf-8"
        )
    ref_db = tmp_path / "reference.db"
    calls: list[tuple[str, str]] = []

    def make_writer(name: str, returns_rows: bool):
        def writer(*args, **kwargs):
            person_id = args[2] if len(args) > 2 else kwargs["person_id"]
            calls.append((person_id, name))
            path = tmp_path / person_id / _CASCADE_FILENAMES[name]
            return (path, []) if returns_rows else (path, 0)

        return writer

    def genetics_writer(*args, **kwargs):
        calls.append((args[2], "genetics"))
        return tmp_path / args[2] / _CASCADE_FILENAMES["genetics"]

    monkeypatch.setattr(dependent_reports, "write_genetics_report", genetics_writer)

    def make_composite_writer(name: str):
        def writer(passed_vault, person_id, **kwargs):
            calls.append((person_id, name))
            return tmp_path / person_id / _CASCADE_FILENAMES[name], 0

        return writer

    monkeypatch.setattr(
        dependent_reports,
        "write_health_advisory",
        make_composite_writer("health_advisory"),
    )
    monkeypatch.setattr(
        "famed.medication_safety_alerts.write_medication_safety_alerts",
        make_composite_writer("alerts"),
    )
    monkeypatch.setattr(dependent_reports, "_person_has_medical_data", lambda *a, **k: True)

    result = dependent_reports.refresh_derived_reports_for_household(
        conn, vault_root, ref_db_path=ref_db
    )

    assert set(result) == {"dummy_alice", "dummy_bob"}
    for person_id in ("dummy_alice", "dummy_bob"):
        assert [path.name for path in result[person_id]] == [
            *[_CASCADE_FILENAMES[name] for name in _FULL_CASCADE_ORDER],
            _PENDING_REVIEW_FILENAME,
        ]
    # Each person's whole cascade completes before the next person's begins.
    assert calls == [
        (person_id, name)
        for person_id in ("dummy_alice", "dummy_bob")
        for name in _FULL_CASCADE_ORDER
    ]


def test_refresh_fhir_dependent_export_writes_when_person_note_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE persons (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO persons (id) VALUES ('dummy_alice')")
    vault_root = tmp_path / "vault"
    person_note = vault_root / "Persons" / "dummy_alice.md"
    person_note.parent.mkdir(parents=True)
    person_note.write_text("---\nperson_id: dummy_alice\n---\n# Dummy Alice\n", encoding="utf-8")
    calls: list[tuple[sqlite3.Connection, Path, str]] = []

    def fhir_writer(
        passed_conn: sqlite3.Connection,
        passed_vault: Path,
        person_id: str,
    ):
        calls.append((passed_conn, passed_vault, person_id))
        return tmp_path / "fhir-export.json", 1, 2, 3, 4

    monkeypatch.setattr(dependent_reports, "write_fhir_export", fhir_writer)

    path = dependent_reports.refresh_fhir_dependent_export(conn, vault_root, "dummy_alice")

    assert path == tmp_path / "fhir-export.json"
    assert calls == [(conn, vault_root, "dummy_alice")]


def test_refresh_fhir_dependent_export_skips_unscaffolded_person_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")

    def fhir_writer(
        passed_conn: sqlite3.Connection,
        passed_vault: Path,
        person_id: str,
    ):
        raise AssertionError("FHIR export should not run without a person note")

    monkeypatch.setattr(dependent_reports, "write_fhir_export", fhir_writer)

    assert (
        dependent_reports.refresh_fhir_dependent_export(conn, tmp_path / "vault", "dummy_alice")
        is None
    )


def test_composite_refresh_removes_retired_report_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notes for report types retired by ADR-031 disappear on the next refresh."""
    vault_root = tmp_path / "famed-vault"
    _write_person_note(vault_root)
    reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    for filename in dependent_reports.RETIRED_REPORT_FILENAMES:
        (reports_dir / filename).write_text("old\n", encoding="utf-8")
    calls: list[str] = []
    _patch_composite_writers(monkeypatch, tmp_path, vault_root, calls)

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    dependent_reports.refresh_history_dependent_reports(conn, vault_root, "dummy_alice")

    assert not [f for f in dependent_reports.RETIRED_REPORT_FILENAMES if (reports_dir / f).exists()]
    assert calls == ["alerts", "health_advisory"]
