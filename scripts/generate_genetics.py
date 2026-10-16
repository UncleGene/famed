#!/usr/bin/env python3
"""Compatibility wrapper for the packaged famed module."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from famed.generate_genetics import *  # noqa: F403
from famed.generate_genetics import main

if __name__ == "__main__":
    raise SystemExit(main())
