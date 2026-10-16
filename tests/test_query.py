"""Tests for famed.query — vault context assembly and question answering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from famed.query import (
    VaultNote,
    answer_vault_question,
    collect_person_vault_notes,
    format_query_context,
    list_person_ids,
)


def test_fm_query_skill_states_its_evidence_contract() -> None:
    """The skill must keep telling the executing agent how to answer safely."""
    skill_path = (
        Path(__file__).resolve().parents[1] / ".agents" / "skills" / "fm-query" / "SKILL.md"
    )
    skill = skill_path.read_text(encoding="utf-8")
    procedure = skill.split("# Procedure", maxsplit=1)[1].split("# Skeptic pass", maxsplit=1)[0]

    assert "does not invoke a separately configured model" in skill
    assert "Exclude stale artifacts from evidence" in procedure
    assert "include confidence metadata or verification status" in procedure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_note(path: Path, front_matter: dict[str, Any], body: str = "") -> None:
    fm = yaml.dump(front_matter, default_flow_style=False)
    path.write_text(f"---\n{fm}---\n{body}", encoding="utf-8")


def _make_person_dirs(vault: Path, person_id: str, *categories: str) -> Path:
    person_dir = vault / "Records" / person_id
    for cat in categories:
        (person_dir / cat).mkdir(parents=True, exist_ok=True)
    return person_dir


# ---------------------------------------------------------------------------
# collect_person_vault_notes
# ---------------------------------------------------------------------------


class TestCollectPersonVaultNotes:
    def test_reports_come_before_labs(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Reports", "Labs")
        _write_note(
            p / "Labs" / "2024-cbc.md", {"person_id": "dummy_alice", "record_type": "lab_panel"}
        )
        _write_note(
            p / "Reports" / "lab-results.md",
            {"person_id": "dummy_alice", "record_type": "report"},
        )

        notes = collect_person_vault_notes(vault, "dummy_alice")
        categories = [n.relative_name.split("/")[0] for n in notes]
        assert categories.index("Reports") < categories.index("Labs")

    def test_labs_before_medications(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Labs", "Medications")
        _write_note(p / "Medications" / "med-note.md", {"person_id": "dummy_alice"})
        _write_note(p / "Labs" / "2024-cbc.md", {"person_id": "dummy_alice"})

        notes = collect_person_vault_notes(vault, "dummy_alice")
        categories = [n.relative_name.split("/")[0] for n in notes]
        assert categories.index("Labs") < categories.index("Medications")

    def test_skips_underscore_files(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Reports")
        _write_note(p / "Reports" / "_index.md", {"person_id": "dummy_alice"})
        _write_note(p / "Reports" / "_template.md", {"person_id": "dummy_alice"})
        _write_note(p / "Reports" / "lab-results.md", {"person_id": "dummy_alice"})

        names = [n.relative_name for n in collect_person_vault_notes(vault, "dummy_alice")]
        assert "Reports/_index.md" not in names
        assert "Reports/_template.md" not in names
        assert "Reports/lab-results.md" in names

    def test_needs_review_sets_verified_false(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "History")
        _write_note(
            p / "History" / "visit.md",
            {"person_id": "dummy_alice", "verification_status": "needs_review"},
        )
        notes = collect_person_vault_notes(vault, "dummy_alice")
        assert len(notes) == 1
        assert notes[0].verified is False

    def test_accepted_status_is_verified(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "History")
        _write_note(
            p / "History" / "visit.md",
            {"person_id": "dummy_alice", "verification_status": "accepted"},
        )
        notes = collect_person_vault_notes(vault, "dummy_alice")
        assert notes[0].verified is True

    def test_no_verification_status_is_verified(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Labs")
        _write_note(p / "Labs" / "2024-cbc.md", {"person_id": "dummy_alice"})
        notes = collect_person_vault_notes(vault, "dummy_alice")
        assert notes[0].verified is True

    def test_empty_person_returns_empty_list(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        (vault / "Records" / "dummy_alice").mkdir(parents=True)
        assert collect_person_vault_notes(vault, "dummy_alice") == []

    def test_missing_person_returns_empty_list(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        assert collect_person_vault_notes(vault, "nobody") == []

    def test_missing_category_dir_skipped(self, tmp_path: Path) -> None:
        # Only Reports exists; Labs/Medications/History dirs absent — no error
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Reports")
        _write_note(p / "Reports" / "lab-results.md", {"person_id": "dummy_alice"})
        notes = collect_person_vault_notes(vault, "dummy_alice")
        assert len(notes) == 1

    def test_note_content_populated(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Reports")
        _write_note(
            p / "Reports" / "lab-results.md",
            {"person_id": "dummy_alice"},
            body="Glucose: 95",
        )
        notes = collect_person_vault_notes(vault, "dummy_alice")
        assert "Glucose: 95" in notes[0].content

    def test_sorted_within_category(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Labs")
        _write_note(p / "Labs" / "2021-02-01-b.md", {"person_id": "dummy_alice"})
        _write_note(p / "Labs" / "2021-01-01-a.md", {"person_id": "dummy_alice"})
        notes = collect_person_vault_notes(vault, "dummy_alice")
        names = [n.relative_name for n in notes]
        assert names.index("Labs/2021-01-01-a.md") < names.index("Labs/2021-02-01-b.md")


# ---------------------------------------------------------------------------
# format_query_context
# ---------------------------------------------------------------------------


class TestFormatQueryContext:
    def test_person_id_in_header(self) -> None:
        ctx = format_query_context([], "dummy_alice")
        assert "dummy_alice" in ctx

    def test_all_expected_reports_listed_when_missing(self) -> None:
        ctx = format_query_context([], "dummy_alice")
        assert "lab-results.md" in ctx
        assert "current-medications.md" in ctx
        assert "medical-history-summary.md" in ctx

    def test_no_missing_line_when_all_reports_present(self) -> None:
        notes = [
            VaultNote(Path("r"), "Reports/lab-results.md", "", True),
            VaultNote(Path("r"), "Reports/current-medications.md", "", True),
            VaultNote(Path("r"), "Reports/medical-history-summary.md", "", True),
        ]
        ctx = format_query_context(notes, "dummy_alice")
        assert "Missing expected reports" not in ctx

    def test_unverified_label_for_needs_review_note(self) -> None:
        notes = [VaultNote(Path("h"), "History/visit.md", "body text", False)]
        ctx = format_query_context(notes, "dummy_alice")
        assert "[UNVERIFIED" in ctx
        assert "History/visit.md" in ctx

    def test_no_unverified_label_for_verified_note(self) -> None:
        notes = [VaultNote(Path("r"), "Reports/lab-results.md", "labs", True)]
        ctx = format_query_context(notes, "dummy_alice")
        assert "[UNVERIFIED" not in ctx

    def test_note_content_included(self) -> None:
        notes = [VaultNote(Path("r"), "Reports/lab-results.md", "Glucose: 95", True)]
        ctx = format_query_context(notes, "dummy_alice")
        assert "Glucose: 95" in ctx

    def test_category_section_headers_present(self) -> None:
        notes = [
            VaultNote(Path("r"), "Reports/lab-results.md", "", True),
            VaultNote(Path("l"), "Labs/2024-cbc.md", "", True),
        ]
        ctx = format_query_context(notes, "dummy_alice")
        assert "Reports" in ctx
        assert "Labs" in ctx

    def test_partial_missing_reports_listed(self) -> None:
        notes = [VaultNote(Path("r"), "Reports/lab-results.md", "", True)]
        ctx = format_query_context(notes, "dummy_alice")
        missing_line = next(
            (line for line in ctx.splitlines() if "Missing expected reports" in line), ""
        )
        assert "current-medications.md" in missing_line
        assert "medical-history-summary.md" in missing_line
        assert "lab-results.md" not in missing_line


# ---------------------------------------------------------------------------
# list_person_ids
# ---------------------------------------------------------------------------


class TestListPersonIds:
    def test_returns_sorted_person_stems(self, tmp_path: Path) -> None:
        persons = tmp_path / "vault" / "Persons"
        persons.mkdir(parents=True)
        (persons / "dummy_charlie.md").write_text("", encoding="utf-8")
        (persons / "dummy_alice.md").write_text("", encoding="utf-8")
        (persons / "dummy_bob.md").write_text("", encoding="utf-8")
        result = list_person_ids(tmp_path / "vault")
        assert result == ["dummy_alice", "dummy_bob", "dummy_charlie"]

    def test_excludes_underscore_files(self, tmp_path: Path) -> None:
        persons = tmp_path / "vault" / "Persons"
        persons.mkdir(parents=True)
        (persons / "_index.md").write_text("", encoding="utf-8")
        (persons / "_template.md").write_text("", encoding="utf-8")
        (persons / "dummy_alice.md").write_text("", encoding="utf-8")
        result = list_person_ids(tmp_path / "vault")
        assert result == ["dummy_alice"]

    def test_empty_when_no_persons_dir(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        assert list_person_ids(vault) == []

    def test_empty_when_vault_missing(self, tmp_path: Path) -> None:
        assert list_person_ids(tmp_path / "vault") == []


# ---------------------------------------------------------------------------
# answer_vault_question
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self) -> None:
        self.received_context: str = ""
        self.received_question: str = ""

    def answer(self, context: str, question: str) -> str:
        self.received_context = context
        self.received_question = question
        return f"answer to: {question}"


class TestAnswerVaultQuestion:
    def test_returns_engine_answer(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        engine = _FakeEngine()
        result = answer_vault_question(vault, "nobody", "What?", engine)
        assert result == "answer to: What?"

    def test_question_forwarded(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        engine = _FakeEngine()
        answer_vault_question(vault, "nobody", "Medications?", engine)
        assert engine.received_question == "Medications?"

    def test_context_contains_note_content(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        p = _make_person_dirs(vault, "dummy_alice", "Reports")
        _write_note(
            p / "Reports" / "lab-results.md",
            {"person_id": "dummy_alice"},
            body="Glucose: 95 mg/dL",
        )
        engine = _FakeEngine()
        answer_vault_question(vault, "dummy_alice", "glucose?", engine)
        assert "Glucose: 95 mg/dL" in engine.received_context

    def test_context_contains_person_id(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        engine = _FakeEngine()
        answer_vault_question(vault, "dummy_alice", "anything?", engine)
        assert "dummy_alice" in engine.received_context
