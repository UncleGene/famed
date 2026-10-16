#!/usr/bin/env python3
"""ADR-032 enforcement: fail if any tracked file names a real household member.

Run before committing changes to docs, session checkpoints, or any other
tracked file. famed-vault/ and data/ are gitignored, but the person IDs and
display names stored there are real first names (PII) and must not appear in
tracked documentation -- use generic phrasing ("a household member") or the
literal `<person_id>` placeholder instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

from famed.doc_privacy import find_violations_in_tracked_files, household_name_pattern


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    root = repo_root()
    pattern = household_name_pattern(root / "famed-vault")
    if pattern is None:
        print("No household members found in famed-vault/Persons; nothing to check.")
        return 0

    violations = find_violations_in_tracked_files(root, pattern)
    if violations:
        print("ADR-032 violation: real household-member name(s) in tracked files:")
        for path, lineno, names in violations:
            print(f"  {path}:{lineno}: {', '.join(names)}")
        return 1

    print("No real household-member names found in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
