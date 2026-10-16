from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from famed.allergies import render_allergy_section


def _write_history(
    vault_root: Path, name: str, event_date: str, facts: list[dict[str, Any]]
) -> None:
    history_dir = vault_root / "Records" / "dummy_alice" / "History"
    history_dir.mkdir(parents=True, exist_ok=True)
    fm = {
        "person_id": "dummy_alice",
        "record_type": "history",
        "event_date": event_date,
        "verification_status": "accepted",
        "accepted_facts": facts,
    }
    block = yaml.safe_dump(fm, sort_keys=False).strip()
    (history_dir / name).write_text(f"---\n{block}\n---\n", encoding="utf-8")


def test_documented_allergy_is_listed_with_date_and_source(tmp_path: Path) -> None:
    vault_root = tmp_path / "famed-vault"
    _write_history(
        vault_root,
        "2020-05-01-visit-history-review.md",
        "2020-05-01",
        [{"description": "Example-drug allergy documented", "category": "allergy"}],
    )

    section = "\n".join(render_allergy_section(vault_root, "dummy_alice"))

    assert section.startswith("## Allergies")
    assert "Example-drug allergy documented" in section
    assert "2020-05-01" in section
    assert "[[Records/dummy_alice/History/2020-05-01-visit-history-review" in section


def test_later_no_known_allergies_never_hides_a_documented_allergy(tmp_path: Path) -> None:
    vault_root = tmp_path / "famed-vault"
    _write_history(
        vault_root,
        "2020-05-01-a-history-review.md",
        "2020-05-01",
        [{"description": "Example-drug allergy documented", "category": "allergy"}],
    )
    _write_history(
        vault_root,
        "2022-01-01-b-history-review.md",
        "2022-01-01",
        [{"description": "No known allergies documented", "category": "allergy"}],
    )

    section = "\n".join(render_allergy_section(vault_root, "dummy_alice"))

    assert "Example-drug allergy documented" in section
    assert "2022-01-01" in section
    assert "confirm" in section.casefold()


def test_no_known_allergies_and_unknown_states_are_distinct(tmp_path: Path) -> None:
    vault_root = tmp_path / "famed-vault"
    unknown = "\n".join(render_allergy_section(vault_root, "dummy_alice"))
    _write_history(
        vault_root,
        "2021-02-02-visit-history-review.md",
        "2021-02-02",
        [{"description": "No known allergies documented", "category": "allergy"}],
    )
    nka = "\n".join(render_allergy_section(vault_root, "dummy_alice"))

    assert "No allergy information recorded" in unknown
    assert "No known allergies documented" in nka
    assert "2021-02-02" in nka
