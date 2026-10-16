"""ADR-032 enforcement: tracked files must not name real household members.

``famed-vault/`` and ``data/`` are gitignored, but the person IDs and display
names stored there are real first names (PII). Tracked documentation must use
generic phrasing or the literal ``<person_id>`` placeholder instead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from famed.person_sync import load_person_display_name
from famed.query import list_person_ids


def household_name_pattern(vault_root: Path) -> re.Pattern[str] | None:
    """Return a case-insensitive whole-word pattern matching any household
    member's person ID or display name, or ``None`` if the vault has no
    person notes to check against."""
    names: set[str] = set()
    for person_id in list_person_ids(vault_root):
        names.add(person_id)
        names.add(load_person_display_name(vault_root, person_id))
    names.discard("")
    if not names:
        return None
    alternation = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    # Keep the leading boundary so we do not match inside unrelated words, and
    # only reject alphabetic continuations on the right so archive-style
    # suffixes like `<person_id>1` still match.
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z])", re.IGNORECASE)


def find_real_name_references(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Return the distinct household-member names found in *text*."""
    return sorted({match.group(0) for match in pattern.finditer(text)})


def find_violations_in_tracked_files(
    repo_root: Path, pattern: re.Pattern[str]
) -> list[tuple[str, int, list[str]]]:
    """Return ``(relative_path, line_number, names)`` for every tracked-file
    line that names a household member."""
    output = subprocess.run(
        ["git", "ls-files"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout
    violations: list[tuple[str, int, list[str]]] = []
    for rel_path in output.splitlines():
        try:
            text = (repo_root / rel_path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            names = find_real_name_references(line, pattern)
            if names:
                violations.append((rel_path, lineno, names))
    return violations
