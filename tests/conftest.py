from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def disable_interactive_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from blocking on interactive prompts."""

    def _fail_input(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("Interactive input is disabled during pytest runs")

    monkeypatch.setattr(builtins, "input", _fail_input)
