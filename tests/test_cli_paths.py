from __future__ import annotations

from pathlib import Path

from famed import (
    add_family_member,
    import_labs,
    import_person,
    prepare_doctor_visit,
    repair_family_links,
    setup,
)


def test_packaged_cli_helpers_resolve_project_root() -> None:
    expected_root = Path(__file__).resolve().parents[1]

    assert import_labs.repo_root() == expected_root
    assert import_person.repo_root() == expected_root
    assert add_family_member.repo_root() == expected_root
    assert prepare_doctor_visit.repo_root() == expected_root
    assert repair_family_links.repo_root() == expected_root
    assert setup.repo_root() == expected_root
