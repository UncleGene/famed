from __future__ import annotations

from pathlib import Path
from unittest import mock

from famed.setup import run_dependency_check


def _patch_non_dataview_checks():
    return mock.patch.multiple(
        "famed.setup",
        _tesseract_status=mock.Mock(return_value=("OK", "mocked")),
        _plink_status=mock.Mock(return_value=("OK", "mocked")),
        _reference_cache_status=mock.Mock(return_value=("OK", "mocked")),
        _annotation_cache_status=mock.Mock(return_value=("OK", "mocked")),
    )


def test_run_dependency_check_blocks_when_dataview_missing(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir(parents=True)

    with _patch_non_dataview_checks():
        result = run_dependency_check(vault_root, skip_keychain=True)

    assert result == 1


def test_run_dependency_check_passes_when_dataview_present(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    (vault_root / ".obsidian" / "plugins" / "dataview").mkdir(parents=True)

    with _patch_non_dataview_checks():
        result = run_dependency_check(vault_root, skip_keychain=True)

    assert result == 0


def test_run_dependency_check_warns_on_stale_reference_cache(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    (vault_root / ".obsidian" / "plugins" / "dataview").mkdir(parents=True)

    with mock.patch("famed.setup._reference_cache_status", return_value=("STALE", "stale: rxnorm")):
        result = run_dependency_check(vault_root, skip_keychain=True)
        assert result == 0
