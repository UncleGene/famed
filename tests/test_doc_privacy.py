"""Tests for famed.doc_privacy — ADR-032 real-name enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from famed.doc_privacy import (
    find_real_name_references,
    find_violations_in_tracked_files,
    household_name_pattern,
)

# ---------------------------------------------------------------------------
# household_name_pattern / find_real_name_references
# ---------------------------------------------------------------------------


def test_household_name_pattern_none_without_person_notes(tmp_path: Path) -> None:
    assert household_name_pattern(tmp_path / "vault") is None


def test_household_name_pattern_matches_person_id_and_display_name(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    persons_dir = vault_root / "Persons"
    persons_dir.mkdir(parents=True)
    (persons_dir / "dummy_alice.md").write_text(
        "---\ndisplay_name: Dummy Alice Example\n---\n", encoding="utf-8"
    )
    (persons_dir / "_template.md").write_text("---\n---\n", encoding="utf-8")

    pattern = household_name_pattern(vault_root)
    assert pattern is not None
    assert find_real_name_references("dummy_alice's TSH is high", pattern) == ["dummy_alice"]
    assert find_real_name_references("Dummy Alice Example's TSH is high", pattern) == [
        "Dummy Alice Example"
    ]
    assert find_real_name_references("a household member's TSH is high", pattern) == []


def test_household_name_pattern_matches_archive_style_name_suffix(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    persons_dir = vault_root / "Persons"
    persons_dir.mkdir(parents=True)
    (persons_dir / "dummy_alice.md").write_text(
        "---\ndisplay_name: Dummy Alice Example\n---\n", encoding="utf-8"
    )

    pattern = household_name_pattern(vault_root)
    assert pattern is not None
    text = "archive.zip#IHE_XDM/dummy_alice1/DOC0001.XML"
    assert find_real_name_references(text, pattern) == ["dummy_alice"]


# ---------------------------------------------------------------------------
# find_violations_in_tracked_files
# ---------------------------------------------------------------------------


def test_no_real_household_names_in_tracked_files() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pattern = household_name_pattern(repo_root / "famed-vault")
    if pattern is None:
        pytest.skip("famed-vault/Persons not present locally; nothing to check")

    violations = find_violations_in_tracked_files(repo_root, pattern)
    assert not violations, violations
