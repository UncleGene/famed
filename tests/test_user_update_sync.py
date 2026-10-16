"""Tests for user_update_sync — REQ-021 isolation and REQ-024 update notes.

Covers: medication snapshot semantics, lab user-updates, history user-updates,
multi-category note rendering, and category-specific note routing.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from famed.medication_sync import upsert_medication, write_medication_conflicts_report
from famed.person_sync import validate_note_front_matter
from famed.reprocess_sources import ReprocessError
from famed.setup import SCHEMA_SQL
from famed.user_update_sync import (
    record_user_update_history_facts,
    record_user_update_lab_facts,
    record_user_update_medications,
    reprocess_retained_user_update_history_source,
    reprocess_retained_user_update_lab_source,
    reprocess_retained_user_update_medication_source,
)


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "famed.db")
    conn.executescript(SCHEMA_SQL)
    return conn


def _scaffold_person(vault_root: Path, person_id: str = "dummy_alice") -> None:
    person_note = vault_root / "Persons" / f"{person_id}.md"
    person_note.parent.mkdir(parents=True, exist_ok=True)
    person_note.write_text(
        f"---\nperson_id: {person_id}\ndisplay_name: Dummy Alice\n---\n",
        encoding="utf-8",
    )


def _insert_db_person(conn: sqlite3.Connection, person_id: str = "dummy_alice") -> None:
    conn.execute("INSERT INTO persons (id, display_name) VALUES (?, ?)", (person_id, "Dummy Alice"))


def _fhir_resources(vault_root: Path, resource_type: str) -> list[dict]:
    export_path = vault_root / "Records" / "dummy_alice" / "Reports" / "fhir-export.json"
    bundle = json.loads(export_path.read_text(encoding="utf-8"))
    return [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == resource_type
    ]


def _med(name: str, **kwargs: object) -> dict:
    return {
        "medication_name": name,
        "status": "active",
        "medication_type": "prescription",
        **kwargs,
    }


def _stored_med(
    name: str,
    *,
    source_type: str,
    source_path: str,
    row_index: int = 0,
    **kwargs: object,
) -> dict:
    return {
        **_med(name, **kwargs),
        "person_id": "dummy_alice",
        "rxnorm_cui": None,
        "dose": kwargs.get("dose"),
        "frequency": kwargs.get("frequency"),
        "start_date": kwargs.get("start_date"),
        "end_date": kwargs.get("end_date"),
        "source_type": source_type,
        "source_path": source_path,
        "row_index": row_index,
    }


def _lab(test_name: str, observed_at: str, **kwargs: object) -> dict:
    return {"test_name": test_name, "observed_at": observed_at, **kwargs}


def _hist(description: str, **kwargs: object) -> dict:
    return {"description": description, **kwargs}


def _write_rxnorm_record(
    reference_root: Path,
    *,
    name: str,
    rxcui: str,
    reviewed: bool = False,
) -> None:
    rxnorm_dir = reference_root / "rxnorm"
    rxnorm_dir.mkdir(parents=True, exist_ok=True)
    filename = "rxnorm_review_overrides.jsonl" if reviewed else "rxnorm_concepts.jsonl"
    record: dict[str, str] = {"name": name, "rxcui": rxcui}
    if reviewed:
        record.update(
            reviewed_by="synthetic_fixture",
            reviewed_at="2099-01-01",
        )
    else:
        record["tty"] = "IN"
    (rxnorm_dir / filename).write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )


def _med_note_path(
    vault_root: Path, person_id: str = "dummy_alice", date_str: str = "2023-05-04"
) -> Path:
    """Return the expected category-specific note path for a medications update."""
    return (
        vault_root
        / "Records"
        / person_id
        / "Medications"
        / f"{date_str}-{person_id}-medications.md"
    )


def _lab_note_path(
    vault_root: Path, person_id: str = "dummy_alice", date_str: str = "2023-05-04"
) -> Path:
    """Return the expected category-specific note path for a labs update."""
    return vault_root / "Records" / person_id / "Labs" / f"{date_str}-{person_id}-labs.md"


def _hist_note_path(
    vault_root: Path, person_id: str = "dummy_alice", date_str: str = "2023-05-04"
) -> Path:
    """Return the expected category-specific note path for a history update."""
    return vault_root / "Records" / person_id / "History" / f"{date_str}-{person_id}-history.md"


def _copy_record_user_update_script(tmp_path: Path) -> tuple[Path, Path]:
    fake_repo_root = tmp_path / "fake-repo"
    script_path = fake_repo_root / "scripts" / "record_user_update.py"
    script_path.parent.mkdir(parents=True)
    source_path = Path(__file__).resolve().parents[1] / "scripts" / "record_user_update.py"
    shutil.copy2(source_path, script_path)
    return script_path, fake_repo_root


# ---------------------------------------------------------------------------
# retained-source storage isolation
# ---------------------------------------------------------------------------


class TestRetainedSourceStorageIsolation:
    def test_default_root_follows_vault_not_working_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        unrelated_cwd = tmp_path / "unrelated-cwd"
        unrelated_cwd.mkdir()
        monkeypatch.chdir(unrelated_cwd)

        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95.0)],
            vault_root,
            conn,
        )
        record_user_update_history_facts(
            "dummy_alice",
            "2023-05-04",
            [_hist("Synthetic history fact")],
            vault_root,
            conn,
        )

        processed_root = tmp_path / "data" / "processed" / "dummy_alice"
        assert (processed_root / "Medications" / "2023-05-04-dummy_alice-medications").exists()
        assert (processed_root / "Labs" / "2023-05-04-dummy_alice-labs").exists()
        assert (processed_root / "History" / "2023-05-04-dummy_alice-history").exists()
        assert not (unrelated_cwd / "data" / "processed").exists()

    def test_explicit_processed_root_overrides_vault_default(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        processed_root = tmp_path / "retained-sources"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia")],
            vault_root,
            conn,
            processed_root=processed_root,
        )

        expected = (
            processed_root / "dummy_alice" / "Medications" / "2023-05-04-dummy_alice-medications"
        )
        assert expected.exists()
        assert not (tmp_path / "data" / "processed").exists()


class TestRecordUserUpdateCliStorageIsolation:
    def test_custom_vault_defaults_to_its_isolated_processed_root(
        self,
        tmp_path: Path,
    ) -> None:
        isolated_root = tmp_path / "isolated"
        vault_root = isolated_root / "vault"
        db_path = isolated_root / "famed.db"
        unrelated_cwd = tmp_path / "unrelated-cwd"
        script_path, fake_repo_root = _copy_record_user_update_script(tmp_path)
        unrelated_cwd.mkdir()
        isolated_root.mkdir()
        _scaffold_person(vault_root)
        _write_rxnorm_record(
            isolated_root / "data" / "reference",
            name="Synthetic Medicine",
            rxcui="100001",
            reviewed=True,
        )
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA_SQL)
        conn.close()

        subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--person-id",
                "dummy_alice",
                "--date",
                "2023-05-04",
                "--vault-root",
                str(vault_root),
                "--db",
                str(db_path),
                "--medications-json",
                json.dumps([_med("Synthetic Medicine")]),
                "--labs-json",
                json.dumps([_lab("Glucose", "2023-05-04", value=95.0)]),
                "--history-json",
                json.dumps([_hist("Synthetic history fact")]),
            ],
            cwd=unrelated_cwd,
            check=True,
            capture_output=True,
            text=True,
        )

        processed_root = isolated_root / "data" / "processed" / "dummy_alice"
        assert (processed_root / "Medications" / "2023-05-04-dummy_alice-medications").exists()
        assert (processed_root / "Labs" / "2023-05-04-dummy_alice-labs").exists()
        assert (processed_root / "History" / "2023-05-04-dummy_alice-history").exists()
        assert not (fake_repo_root / "data" / "processed").exists()
        assert not (unrelated_cwd / "data" / "processed").exists()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
            ).fetchone() == ("100001",)

    def test_explicit_storage_roots_are_forwarded(
        self,
        tmp_path: Path,
    ) -> None:
        vault_root = tmp_path / "vault"
        db_path = tmp_path / "famed.db"
        processed_root = tmp_path / "retained-sources"
        reference_root = tmp_path / "local-reference"
        _scaffold_person(vault_root)
        _write_rxnorm_record(
            reference_root,
            name="Synthetic Medicine",
            rxcui="100001",
            reviewed=True,
        )
        conn = _db(tmp_path)
        conn.close()
        script_path, _fake_repo_root = _copy_record_user_update_script(tmp_path)
        subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--person-id",
                "dummy_alice",
                "--date",
                "2023-05-04",
                "--vault-root",
                str(vault_root),
                "--db",
                str(db_path),
                "--processed-root",
                str(processed_root),
                "--reference-root",
                str(reference_root),
                "--medications-json",
                json.dumps([_med("Synthetic Medicine")]),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        expected = (
            processed_root / "dummy_alice" / "Medications" / "2023-05-04-dummy_alice-medications"
        )
        assert expected.exists()
        assert not (tmp_path / "data" / "processed").exists()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
            ).fetchone() == ("100001",)


# ---------------------------------------------------------------------------
# record_user_update_medications — with snapshot semantics
# ---------------------------------------------------------------------------


class TestRecordUserUpdateMedications:
    def test_reviewed_rxnorm_enrichment_reaches_all_persisted_surfaces(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "vault"
        reference_root = tmp_path / "reference"
        processed_root = tmp_path / "processed"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        _insert_db_person(conn)
        _write_rxnorm_record(
            reference_root,
            name="Synthetic Medicine",
            rxcui="100001",
            reviewed=True,
        )

        first = record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [_med("Synthetic Medicine", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
            processed_root=processed_root,
            reference_root=reference_root,
        )
        second = record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [_med("Synthetic Medicine", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
            processed_root=processed_root,
            reference_root=reference_root,
        )

        assert (first, second) == (1, 0)
        assert conn.execute(
            "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone() == ("100001",)
        note = _med_note_path(vault_root, date_str="2099-01-01")
        retained = (
            processed_root / "dummy_alice" / "Medications" / "2099-01-01-dummy_alice-medications"
        )
        assert "| Synthetic Medicine | prescription | 100001 |" in note.read_text(encoding="utf-8")
        assert validate_note_front_matter(note) == []
        assert "[[_index|← Medications]]" in note.read_text(encoding="utf-8")
        assert retained.read_bytes() == note.read_bytes()
        current = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        ).read_text(encoding="utf-8")
        assert "| 100001 |" in current
        statements = _fhir_resources(vault_root, "MedicationStatement")
        assert statements[0]["medicationCodeableConcept"]["coding"][0]["code"] == "100001"

    def test_downloaded_rxnorm_enrichment_and_explicit_cui_precedence(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        reference_root = tmp_path / "reference"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        _write_rxnorm_record(
            reference_root,
            name="Synthetic Medicine",
            rxcui="100001",
        )

        record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [
                _med("Synthetic Medicine"),
                _med("Synthetic Medicine Explicit", rxnorm_cui="200002"),
            ],
            vault_root,
            conn,
            reference_root=reference_root,
        )

        rows = conn.execute(
            """
            SELECT medication_name, rxnorm_cui
            FROM medications
            WHERE person_id = 'dummy_alice'
            ORDER BY medication_name
            """
        ).fetchall()
        assert rows == [
            ("Synthetic Medicine", "100001"),
            ("Synthetic Medicine Explicit", "200002"),
        ]

    def test_later_update_without_cui_preserves_existing_explicit_cui(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        reference_root = tmp_path / "reference"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        _write_rxnorm_record(
            reference_root,
            name="Synthetic Medicine",
            rxcui="100001",
        )

        record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [_med("Synthetic Medicine", rxnorm_cui="200002", dose="10 mg")],
            vault_root,
            conn,
            reference_root=reference_root,
        )
        record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [_med("Synthetic Medicine", dose="20 mg")],
            vault_root,
            conn,
            reference_root=reference_root,
        )

        assert conn.execute(
            "SELECT rxnorm_cui, dose FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone() == ("200002", "20 mg")

    def test_whitespace_only_cui_is_enriched_as_blank(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        reference_root = tmp_path / "reference"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        _write_rxnorm_record(
            reference_root,
            name="Synthetic Medicine",
            rxcui="100001",
        )

        record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [_med("Synthetic Medicine", rxnorm_cui="   ")],
            vault_root,
            conn,
            reference_root=reference_root,
        )

        assert conn.execute(
            "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone() == ("100001",)

    def test_missing_rxnorm_mapping_stays_unresolved_without_network(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_network(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("dated updates must not make live RxNorm requests")

        monkeypatch.setattr("famed.reference_cache._http_request", fail_network)
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_medications(
            "dummy_alice",
            "2099-01-01",
            [_med("Unmapped Synthetic Medicine")],
            vault_root,
            conn,
        )

        assert conn.execute(
            "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone() == (None,)
        current = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "current-medications.md"
        ).read_text(encoding="utf-8")
        assert "| unresolved |" in current

    def test_retained_replay_enriches_from_root_reference_cache_not_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "isolated-root"
        vault_root = root / "vault"
        processed_root = root / "data" / "processed"
        source_path = (
            processed_root / "dummy_alice" / "Medications" / "2099-01-01-dummy_alice-medications"
        )
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            "---\n"
            "person_id: dummy_alice\n"
            "record_type: medication\n"
            "category: medications\n"
            "date: 2099-01-01\n"
            "---\n\n"
            "## Medications\n\n"
            "| medication_name | medication_type | rxnorm_cui | dose | frequency | "
            "status | start_date | end_date | evidence |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| Synthetic Medicine | prescription |  | 10 mg | daily | active | "
            "2099-01-01 |  | fixture |\n",
            encoding="utf-8",
        )
        _write_rxnorm_record(
            root / "data" / "reference",
            name="Synthetic Medicine",
            rxcui="100001",
            reviewed=True,
        )
        _scaffold_person(vault_root)
        conn = _db(root)
        unrelated_cwd = tmp_path / "unrelated-cwd"
        unrelated_cwd.mkdir()
        monkeypatch.chdir(unrelated_cwd)

        result = reprocess_retained_user_update_medication_source(
            root=root,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 1
        assert conn.execute(
            "SELECT rxnorm_cui FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone() == ("100001",)
        assert not (unrelated_cwd / "data" / "reference").exists()

    def test_authoritative_update_clears_pending_medication_conflict_report(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        for row_index, (source_path, dose) in enumerate(
            (
                ("processed/dummy_alice/Medications/example-a.json", "10 mg"),
                ("processed/dummy_alice/Medications/example-b.json", "20 mg"),
            )
        ):
            upsert_medication(
                conn,
                _stored_med(
                    "Examplemed",
                    dose=dose,
                    frequency="daily",
                    source_type="manual",
                    source_path=source_path,
                    row_index=row_index,
                ),
            )
        report_path, _ = write_medication_conflicts_report(conn, vault_root, "dummy_alice")
        assert "verification_status: needs_review" in report_path.read_text(encoding="utf-8")

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Examplemed", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
        )

        report = report_path.read_text(encoding="utf-8")
        assert "verification_status: accepted" in report
        assert "pending_review_count: 0" in report
        assert "_No conflicting medication entries found._" in report
        events = conn.execute(
            """
            SELECT event_type, subject_id, status, resolution, message
            FROM audit_events
            WHERE subject_type = 'medication_conflict'
            ORDER BY rowid
            """
        ).fetchall()
        assert [event[0] for event in events] == [
            "medication_conflict_detected",
            "medication_conflict_resolution_recorded",
        ]
        assert events[0][1] == events[1][1]
        assert events[1][2:4] == ("processed", "user_resolved")
        resolution = json.loads(events[1][4])["resolution"]
        assert resolution["actor"] == "user"
        assert resolution["reason"] == "dated authoritative medication update on 2023-05-04"

    def test_conflicted_medication_rejects_multiple_authoritative_periods(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        for row_index, (source_path, dose) in enumerate(
            (
                ("processed/dummy_alice/Medications/example-a.json", "10 mg"),
                ("processed/dummy_alice/Medications/example-b.json", "20 mg"),
            )
        ):
            upsert_medication(
                conn,
                _stored_med(
                    "Examplemed",
                    dose=dose,
                    frequency="daily",
                    source_type="manual",
                    source_path=source_path,
                    row_index=row_index,
                ),
            )

        with pytest.raises(
            ReprocessError,
            match="requires exactly one authoritative row",
        ):
            record_user_update_medications(
                "dummy_alice",
                "2023-05-04",
                [
                    _med(
                        "Examplemed 10 mg tablet",
                        dose="10 mg",
                        frequency="daily",
                        start_date="2023-05-01",
                    ),
                    _med(
                        "Examplemed 20 mg tablet",
                        dose="20 mg",
                        frequency="daily",
                        start_date="2023-05-02",
                    ),
                ],
                vault_root,
                conn,
            )

        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE event_type = 'medication_conflict_resolution_recorded'
            """
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM medications WHERE source_type = 'user_update'"
        ).fetchone() == (0,)

    @pytest.mark.parametrize("unstated_dose", ["", "unknown"])
    def test_conflicted_medication_update_rejects_unstated_value(
        self, tmp_path: Path, unstated_dose: str
    ) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        for row_index, (source_path, dose) in enumerate(
            (
                ("processed/dummy_alice/Medications/example-a.json", "10 mg"),
                ("processed/dummy_alice/Medications/example-b.json", "20 mg"),
            )
        ):
            upsert_medication(
                conn,
                _stored_med(
                    "Examplemed",
                    dose=dose,
                    frequency="daily",
                    source_type="manual",
                    source_path=source_path,
                    row_index=row_index,
                ),
            )

        with pytest.raises(ReprocessError, match="missing: dose"):
            record_user_update_medications(
                "dummy_alice",
                "2023-05-04",
                [_med("Examplemed", dose=unstated_dose, frequency="daily")],
                vault_root,
                conn,
            )

        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE event_type = 'medication_conflict_resolution_recorded'
            """
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM medications WHERE source_type = 'user_update'"
        ).fetchone() == (0,)

    def test_exact_user_update_replaces_lower_authority_import(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        for source_path in (
            "processed/dummy_alice/Medications/example-a.json",
            "processed/dummy_alice/Medications/example-b.json",
        ):
            upsert_medication(
                conn,
                _stored_med(
                    "Examplemed",
                    dose="10 mg",
                    frequency="daily",
                    source_type="manual",
                    source_path=source_path,
                ),
            )

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Examplemed", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
        )

        rows = conn.execute(
            """
            SELECT medication_name, dose, frequency, source_type
            FROM medications
            WHERE person_id = 'dummy_alice'
            """
        ).fetchall()
        assert rows == [("Examplemed", "10 mg", "daily", "user_update")]
        audit_rows = conn.execute(
            """
            SELECT subject_id, source_path, message
            FROM audit_events
            WHERE event_type = 'medication_redundant_row_superseded'
            ORDER BY source_path
            """
        ).fetchall()
        assert len(audit_rows) == 2
        authoritative_id = conn.execute(
            "SELECT id FROM medications WHERE source_type = 'user_update'"
        ).fetchone()[0]
        for subject_id, source_path, message in audit_rows:
            payload = json.loads(message)
            assert subject_id == authoritative_id
            assert payload["authoritative_medication_id"] == authoritative_id
            assert payload["removed_medication"]["source_path"] == source_path
            assert payload["removed_medication"]["dose"] == "10 mg"

    def test_distinct_import_period_remains_after_user_update(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        upsert_medication(
            conn,
            _stored_med(
                "Examplemed",
                dose="10 mg",
                frequency="daily",
                start_date="2023-04-01",
                end_date="2023-04-30",
                status="discontinued",
                source_type="manual",
                source_path="processed/dummy_alice/Medications/example.json",
            ),
        )

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [
                _med(
                    "Examplemed",
                    dose="10 mg",
                    frequency="daily",
                    start_date="2023-05-01",
                )
            ],
            vault_root,
            conn,
        )

        rows = conn.execute(
            """
            SELECT start_date, end_date, status, source_type
            FROM medications
            WHERE person_id = 'dummy_alice'
            ORDER BY start_date
            """
        ).fetchall()
        assert rows == [
            ("2023-04-01", "2023-04-30", "discontinued", "manual"),
            ("2023-05-01", None, "active", "user_update"),
        ]

    def test_exact_convergence_preserves_other_user_authored_rows(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        for source_type, source_path in (
            ("user_correction", "Records/dummy_alice/Medications/correction.md"),
            ("user_update", "Records/dummy_alice/Medications/earlier-update.md"),
        ):
            upsert_medication(
                conn,
                _stored_med(
                    "Examplemed",
                    dose="10 mg",
                    frequency="daily",
                    source_type=source_type,
                    source_path=source_path,
                ),
            )

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Examplemed", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
        )

        rows = conn.execute(
            """
            SELECT source_type, source_path
            FROM medications
            WHERE person_id = 'dummy_alice'
            ORDER BY source_path
            """
        ).fetchall()
        assert len(rows) == 3
        assert {row[0] for row in rows} == {"user_correction", "user_update"}
        assert conn.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE event_type = 'medication_redundant_row_superseded'
            """
        ).fetchone() == (0,)

    def test_existing_update_field_clear_converges_and_renders_current_value(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        upsert_medication(
            conn,
            _stored_med(
                "Examplemed",
                dose=None,
                frequency="daily",
                source_type="manual",
                source_path="processed/dummy_alice/Medications/example.json",
            ),
        )
        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Examplemed", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
        )

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Examplemed", dose=None, frequency="daily")],
            vault_root,
            conn,
        )

        rows = conn.execute(
            """
            SELECT dose, frequency, source_type
            FROM medications
            WHERE person_id = 'dummy_alice'
            """
        ).fetchall()
        assert rows == [(None, "daily", "user_update")]
        assert "10 mg" not in _med_note_path(vault_root).read_text(encoding="utf-8")
        assert conn.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE event_type = 'medication_redundant_row_superseded'
            """
        ).fetchone() == (1,)

    def test_creates_note_and_db_row(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        count = record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia", dose="10 mg")], vault_root, conn
        )
        assert count == 1
        note_path = _med_note_path(vault_root)
        assert note_path.exists()
        rows = conn.execute(
            "SELECT medication_name, source_type FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("Zetia", "user_update")

        reports_dir = vault_root / "Records" / "dummy_alice" / "Reports"
        assert not (reports_dir / "pgx-coverage.md").exists()
        assert not (reports_dir / "pgx-wallet-card.md").exists()

    def test_refreshes_fhir_export_when_person_is_exportable(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        _insert_db_person(conn)

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia", dose="10 mg", frequency="daily")],
            vault_root,
            conn,
        )

        statements = _fhir_resources(vault_root, "MedicationStatement")
        assert len(statements) == 1
        assert statements[0]["medicationCodeableConcept"] == {"text": "Zetia"}
        assert statements[0]["dosage"] == [{"text": "10 mg daily"}]

    def test_source_path_points_to_note(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        row = conn.execute(
            "SELECT source_path FROM medications WHERE person_id = 'dummy_alice' LIMIT 1"
        ).fetchone()
        assert row[0] == "Records/dummy_alice/Medications/2023-05-04-dummy_alice-medications.md"

    def test_same_date_merge_adds_new_rows(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        count = record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Metformin")], vault_root, conn
        )
        assert count == 1
        rows = conn.execute(
            "SELECT medication_name FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert names == {"Zetia", "Metformin"}

    def test_same_date_snapshot_updates_existing_medication(self, tmp_path: Path) -> None:
        """Second session same day: update existing medication instead of skipping."""
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia", dose="10 mg", status="discontinued")],
            vault_root,
            conn,
        )
        count = record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia", dose="20 mg", status="active")],
            vault_root,
            conn,
        )
        assert count == 1  # updated, not skipped
        rows = conn.execute(
            "SELECT medication_name, dose, status FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("Zetia", "20 mg", "active")

    def test_same_date_allows_multiple_dated_periods_for_same_medication(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        count = record_user_update_medications(
            "dummy_alice",
            "2023-05-30",
            [
                _med(
                    "Zoloft",
                    dose="25 mg",
                    frequency="daily",
                    start_date="2023-05-28",
                    end_date="2023-06-01",
                ),
                _med(
                    "Zoloft",
                    dose="50 mg",
                    frequency="daily",
                    start_date="2023-06-02",
                    end_date="2023-06-08",
                ),
                _med("Zoloft", dose="100 mg", frequency="daily", start_date="2023-06-09"),
            ],
            vault_root,
            conn,
        )
        assert count == 3
        rows = conn.execute(
            """
            SELECT medication_name, dose, frequency, start_date, end_date
            FROM medications
            WHERE person_id = 'dummy_alice'
            ORDER BY start_date
            """
        ).fetchall()
        assert rows == [
            ("Zoloft", "25 mg", "daily", "2023-05-28", "2023-06-01"),
            ("Zoloft", "50 mg", "daily", "2023-06-02", "2023-06-08"),
            ("Zoloft", "100 mg", "daily", "2023-06-09", None),
        ]

    def test_dated_schedule_replaces_prior_same_day_single_medication_row(
        self, tmp_path: Path
    ) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice",
            "2023-05-30",
            [
                _med(
                    "Zoloft",
                    dose="25 mg from 2023-05-28; 50 mg from 2023-06-02; 100 mg from 2023-06-09",
                    frequency="daily",
                    start_date="2023-05-28",
                )
            ],
            vault_root,
            conn,
        )
        record_user_update_medications(
            "dummy_alice",
            "2023-05-30",
            [
                _med(
                    "Zoloft",
                    dose="25 mg",
                    frequency="daily",
                    start_date="2023-05-28",
                    end_date="2023-06-01",
                ),
                _med(
                    "Zoloft",
                    dose="50 mg",
                    frequency="daily",
                    start_date="2023-06-02",
                    end_date="2023-06-08",
                ),
                _med("Zoloft", dose="100 mg", frequency="daily", start_date="2023-06-09"),
            ],
            vault_root,
            conn,
        )
        rows = conn.execute(
            """
            SELECT dose, start_date, end_date
            FROM medications
            WHERE person_id = 'dummy_alice' AND medication_name = 'Zoloft'
            ORDER BY start_date, end_date
            """
        ).fetchall()
        assert rows == [
            ("25 mg", "2023-05-28", "2023-06-01"),
            ("50 mg", "2023-06-02", "2023-06-08"),
            ("100 mg", "2023-06-09", None),
        ]

    def test_same_date_snapshot_preserves_other_fields(self, tmp_path: Path) -> None:
        """Updating one field does not wipe other populated fields."""
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia", dose="10 mg", frequency="daily", medication_type="prescription")],
            vault_root,
            conn,
        )
        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia", status="discontinued")],  # only status changed
            vault_root,
            conn,
        )
        row = conn.execute(
            "SELECT dose, frequency, status FROM medications WHERE person_id = 'dummy_alice'"
        ).fetchone()
        assert row == ("10 mg", "daily", "discontinued")

    def test_note_contains_all_merged_facts(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Metformin")], vault_root, conn
        )
        content = _med_note_path(vault_root).read_text()
        assert "Zetia" in content
        assert "Metformin" in content

    def test_different_dates_produce_separate_notes(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        record_user_update_medications(
            "dummy_alice", "2023-05-05", [_med("Metformin")], vault_root, conn
        )
        note1 = _med_note_path(vault_root, date_str="2023-05-04")
        note2 = _med_note_path(vault_root, date_str="2023-05-05")
        assert note1.exists()
        assert note2.exists()
        assert "Zetia" in note1.read_text()
        assert "Metformin" in note2.read_text()

    def test_medications_index_exists(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        index_path = vault_root / "Records" / "dummy_alice" / "Medications" / "_index.md"
        assert index_path.exists()

        note_path = _med_note_path(vault_root)
        assert validate_note_front_matter(note_path) == []
        index = index_path.read_text(encoding="utf-8")
        assert f"[[{note_path.stem}]]" in index

    def test_audit_event_logged(self, tmp_path: Path) -> None:
        from famed.storage_contract import ensure_audit_events_table

        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        ensure_audit_events_table(conn)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        events = conn.execute(
            "SELECT event_type FROM audit_events WHERE subject_id = 'dummy_alice'"
        ).fetchall()
        event_types = [e[0] for e in events]
        assert "user_update_recorded" in event_types


# ---------------------------------------------------------------------------
# record_user_update_lab_facts
# ---------------------------------------------------------------------------


class TestRecordUserUpdateLabFacts:
    def test_persists_lab_row(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        count = record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [
                _lab(
                    "Glucose",
                    "2023-05-04",
                    value=95,
                    unit="mg/dL",
                    reference_range_low=70,
                    reference_range_high=90,
                )
            ],
            vault_root,
            conn,
        )
        assert count == 1
        rows = conn.execute(
            "SELECT test_name, value, unit, source_type FR"
            "OM lab_results WHERE person_id = 'dummy_alice'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("Glucose", 95.0, "mg/dL", "user_update")
        flagged = (
            vault_root / "Records" / "dummy_alice" / "Reports" / "flagged-for-discussion.md"
        ).read_text(encoding="utf-8")
        assert "Glucose" in flagged
        assert "2023-05-04" in flagged

    def test_refreshes_fhir_export_when_person_is_exportable(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        _insert_db_person(conn)

        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95, unit="mg/dL")],
            vault_root,
            conn,
        )

        observations = _fhir_resources(vault_root, "Observation")
        assert len(observations) == 1
        assert observations[0]["code"]["text"] == "Glucose"
        assert observations[0]["valueQuantity"] == {"value": 95.0, "unit": "mg/dL"}

    def test_note_contains_lab_section(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95)],
            vault_root,
            conn,
        )
        content = _lab_note_path(vault_root).read_text()
        assert "## Results" in content
        assert "Glucose" in content
        assert "95" in content

    def test_note_satisfies_vault_contract_and_is_indexed(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95)],
            vault_root,
            conn,
        )
        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95)],
            vault_root,
            conn,
        )

        note_path = _lab_note_path(vault_root)
        assert validate_note_front_matter(note_path) == []
        index = note_path.with_name("_index.md").read_text(encoding="utf-8")
        assert index.count(f"[[{note_path.stem}]]") == 1

    def test_same_day_lab_dedup(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95)],
            vault_root,
            conn,
        )
        count = record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=100)],
            vault_root,
            conn,
        )
        assert count == 0  # skipped, same test + date
        rows = conn.execute(
            "SELECT value FROM lab_results WHERE person_id = 'dummy_alice'"
        ).fetchall()
        assert len(rows) == 1

    def test_no_combined_note_is_written_at_person_root(self, tmp_path: Path) -> None:
        """Each category writes only to its own subfolder note.

        Guards against reintroducing the retired combined
        ``Records/{person_id}/{date}-{person_id}-updates.md`` layout.
        """
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        record_user_update_lab_facts(
            "dummy_alice",
            "2023-05-04",
            [_lab("Glucose", "2023-05-04", value=95)],
            vault_root,
            conn,
        )
        med_note = _med_note_path(vault_root)
        content = med_note.read_text()
        assert "Zetia" in content
        assert "Glucose" not in content
        combined = vault_root / "Records" / "dummy_alice" / "2023-05-04-dummy_alice-updates.md"
        assert not combined.exists()


# ---------------------------------------------------------------------------
# record_user_update_history_facts
# ---------------------------------------------------------------------------


class TestRecordUserUpdateHistoryFacts:
    def test_persists_history_in_note(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        count = record_user_update_history_facts(
            "dummy_alice",
            "2023-05-04",
            [_hist("Seasonal allergies", category="diagnosis", date="2023-05-01")],
            vault_root,
            conn,
        )
        assert count == 1
        content = _hist_note_path(vault_root).read_text()
        assert "## History" in content
        assert "Seasonal allergies" in content
        assert "diagnosis" in content

    def test_note_is_linked_from_history_index(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_history_facts(
            "dummy_alice",
            "2023-05-04",
            [_hist("Seasonal allergies")],
            vault_root,
            conn,
        )

        note_path = _hist_note_path(vault_root)
        assert validate_note_front_matter(note_path) == []
        index = note_path.with_name("_index.md").read_text(encoding="utf-8")
        assert f"[[{note_path.stem}]]" in index

    def test_same_day_history_dedup(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        record_user_update_history_facts(
            "dummy_alice",
            "2023-05-04",
            [_hist("Seasonal allergies")],
            vault_root,
            conn,
        )
        count = record_user_update_history_facts(
            "dummy_alice",
            "2023-05-04",
            [_hist("Seasonal allergies"), _hist("Broken arm")],
            vault_root,
            conn,
        )
        assert count == 1  # "Seasonal allergies" skipped, "Broken arm" new


# ---------------------------------------------------------------------------
# Re-import protection
# ---------------------------------------------------------------------------


class TestReimportProtection:
    def test_reimport_of_originating_source_skips_user_update_row(self, tmp_path: Path) -> None:
        from famed.medication_sync import replace_medications_for_source_path

        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_medications(
            "dummy_alice",
            "2023-05-04",
            [_med("Zetia", dose="10 mg", medication_type="prescription", status="discontinued")],
            vault_root,
            conn,
        )

        import_source = "Records/dummy_alice/Medications/import-2025.md"
        _, skipped = replace_medications_for_source_path(
            conn,
            import_source,
            [
                {
                    "person_id": "dummy_alice",
                    "medication_name": "Zetia",
                    "medication_type": "prescription",
                    "dose": "10 mg",
                    "frequency": "daily",
                    "status": "active",
                    "start_date": None,
                    "end_date": None,
                    "rxnorm_cui": None,
                }
            ],
            non_interactive=True,
        )
        skipped_after = conn.execute(
            """
            SELECT COUNT(*)
            FROM medications
            WHERE person_id = 'dummy_alice' AND source_type = 'user_update'
            """
        ).fetchone()[0]
        assert skipped_after == 1

    def test_delete_guard_does_not_delete_user_update_rows(self, tmp_path: Path) -> None:
        from famed.medication_sync import replace_medications_for_source_path

        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)

        record_user_update_medications(
            "dummy_alice", "2023-05-04", [_med("Zetia")], vault_root, conn
        )
        note_relative = "Records/dummy_alice/Medications/2023-05-04-dummy_alice-medications.md"

        replace_medications_for_source_path(conn, note_relative, [], non_interactive=True)

        rows = conn.execute(
            "SELECT medication_name FROM medications WHERE source_type = 'user_update'"
        ).fetchall()
        assert len(rows) == 1  # user_update row was NOT deleted


# ---------------------------------------------------------------------------
# reprocess_retained_user_update_lab_source / _history_source
# ---------------------------------------------------------------------------


_LAB_NOTE_FRONT_MATTER = (
    "---\n"
    "person_id: {person_id}\n"
    "record_type: lab_panel\n"
    "observed_at: '{date_str}'\n"
    "source_type: user_update\n"
    "---\n\n"
    "# Lab Panel — {date_str}\n\n"
    "[[_index|← Labs]]\n\n"
    "## Results\n\n"
    "| test_name | loinc_code | value | unit | reference_range_low "
    "| reference_range_high | observed_at |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
)

_HISTORY_NOTE_FRONT_MATTER = (
    "---\n"
    "person_id: {person_id}\n"
    "record_type: user_update\n"
    "category: history\n"
    "date: '{date_str}'\n"
    "---\n\n"
    "# History Update — {date_str}\n\n"
    "[[_index|← History]]\n\n"
    "## History\n\n"
    "| Description | Category | Date | Details |\n"
    "| --- | --- | --- | --- |\n"
)


class TestReprocessRetainedUserUpdateLabSource:
    def test_multiple_rows_round_trip(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "Labs" / "2023-03-01-dummy_alice-labs"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            _LAB_NOTE_FRONT_MATTER.format(person_id="dummy_alice", date_str="2023-03-01")
            + "| Glucose | 2345-7 | 95 | mg/dL | 70 | 100 | 2023-03-01 |\n"
            "| Hemoglobin A1c |  | 5.4 | % |  | 5.7 | 2023-03-01 |\n"
            "| Synthetic screen |  | Negative |  |  |  | 2023-03-01 |\n",
            encoding="utf-8",
        )

        result = reprocess_retained_user_update_lab_source(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 3
        assert result.affected_person_ids == {"dummy_alice"}
        rows = conn.execute(
            "SELECT test_name, value, value_text, unit, reference_range_low, "
            "reference_range_high "
            "FROM lab_results WHERE person_id = 'dummy_alice' ORDER BY test_name"
        ).fetchall()
        assert rows == [
            ("Glucose", 95.0, "95", "mg/dL", 70.0, 100.0),
            ("Hemoglobin A1c", 5.4, "5.4", "%", None, 5.7),
            ("Synthetic screen", None, "Negative", None, None, None),
        ]
        note_path = _lab_note_path(vault_root, date_str="2023-03-01")
        assert note_path.exists()

    def test_empty_results_table_is_a_no_op_not_an_error(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "Labs" / "2023-09-01-dummy_alice-labs"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            _LAB_NOTE_FRONT_MATTER.format(person_id="dummy_alice", date_str="2023-09-01"),
            encoding="utf-8",
        )

        result = reprocess_retained_user_update_lab_source(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 0
        assert result.affected_person_ids == {"dummy_alice"}

    def test_rejects_path_outside_processed_labs(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = (
            processed_root / "dummy_alice" / "Medications" / "2023-03-01-dummy_alice-medications"
        )
        source_path.parent.mkdir(parents=True)
        source_path.write_text("not a labs file", encoding="utf-8")

        with pytest.raises(ReprocessError, match="must be under data/processed"):
            reprocess_retained_user_update_lab_source(
                root=tmp_path,
                vault_root=tmp_path / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )

    def test_rejects_missing_front_matter(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "Labs" / "2023-03-01-dummy_alice-labs"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("no front matter here", encoding="utf-8")

        with pytest.raises(ReprocessError, match="front matter"):
            reprocess_retained_user_update_lab_source(
                root=tmp_path,
                vault_root=tmp_path / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )

    def test_rejects_person_id_mismatch(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "Labs" / "2023-03-01-dummy_alice-labs"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            _LAB_NOTE_FRONT_MATTER.format(person_id="dummy_bob", date_str="2023-03-01"),
            encoding="utf-8",
        )

        with pytest.raises(ReprocessError, match="person_id mismatch"):
            reprocess_retained_user_update_lab_source(
                root=tmp_path,
                vault_root=tmp_path / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )


class TestReprocessRetainedUserUpdateHistorySource:
    def test_multiple_facts_round_trip(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "History" / "2023-03-01-dummy_alice-history"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            _HISTORY_NOTE_FRONT_MATTER.format(person_id="dummy_alice", date_str="2023-03-01")
            + "| Annual physical exam | screening | 2023-03-01 | Routine checkup |\n",
            encoding="utf-8",
        )

        result = reprocess_retained_user_update_history_source(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 1
        assert result.affected_person_ids == {"dummy_alice"}
        note_path = _hist_note_path(vault_root, date_str="2023-03-01")
        content = note_path.read_text(encoding="utf-8")
        assert "Annual physical exam" in content

    def test_empty_history_table_is_a_no_op_not_an_error(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        _scaffold_person(vault_root)
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "History" / "2023-09-01-dummy_alice-history"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            _HISTORY_NOTE_FRONT_MATTER.format(person_id="dummy_alice", date_str="2023-09-01"),
            encoding="utf-8",
        )

        result = reprocess_retained_user_update_history_source(
            root=tmp_path,
            vault_root=vault_root,
            processed_root=processed_root,
            conn=conn,
            source_path=source_path,
        )

        assert result.imported_rows == 0
        assert result.affected_person_ids == {"dummy_alice"}

    def test_rejects_path_outside_processed_history(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "Labs" / "2023-03-01-dummy_alice-labs"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("not a history file", encoding="utf-8")

        with pytest.raises(ReprocessError, match="must be under data/processed"):
            reprocess_retained_user_update_history_source(
                root=tmp_path,
                vault_root=tmp_path / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )

    def test_rejects_missing_front_matter(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "History" / "2023-03-01-dummy_alice-history"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("no front matter here", encoding="utf-8")

        with pytest.raises(ReprocessError, match="front matter"):
            reprocess_retained_user_update_history_source(
                root=tmp_path,
                vault_root=tmp_path / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )

    def test_rejects_person_id_mismatch(self, tmp_path: Path) -> None:
        conn = _db(tmp_path)
        processed_root = tmp_path / "processed"
        source_path = processed_root / "dummy_alice" / "History" / "2023-03-01-dummy_alice-history"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            _HISTORY_NOTE_FRONT_MATTER.format(person_id="dummy_bob", date_str="2023-03-01"),
            encoding="utf-8",
        )

        with pytest.raises(ReprocessError, match="person_id mismatch"):
            reprocess_retained_user_update_history_source(
                root=tmp_path,
                vault_root=tmp_path / "vault",
                processed_root=processed_root,
                conn=conn,
                source_path=source_path,
            )
