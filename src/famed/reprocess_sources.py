#!/usr/bin/env python3
"""Registry for retained-source reprocessing handlers."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ReprocessError(ValueError):
    """Raised when no retained-source reprocess handler can safely run."""


def retained_source_reference(
    *,
    root: Path,
    processed_root: Path,
    source_path: Path,
    source_path_prefix: Path | None = None,
) -> str:
    """Return the canonical provenance key for a retained source."""
    prefix = source_path_prefix or processed_root.relative_to(root)
    return str(prefix / source_path.relative_to(processed_root))


def snapshot_person_vault_tree(vault_root: Path, person_id: str) -> dict[Path, bytes]:
    """Capture one person's mutable vault tree for paired DB/filesystem rollback."""
    person_root = vault_root / "Records" / person_id
    if not person_root.exists():
        return {}
    return {
        path.relative_to(person_root): path.read_bytes()
        for path in person_root.rglob("*")
        if path.is_file()
    }


def restore_person_vault_tree(
    vault_root: Path,
    person_id: str,
    snapshot: dict[Path, bytes],
) -> None:
    """Restore a person tree captured by ``snapshot_person_vault_tree``."""
    person_root = vault_root / "Records" / person_id
    if person_root.exists():
        shutil.rmtree(person_root)
    for relative_path, content in snapshot.items():
        target = person_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@dataclass(frozen=True)
class ReprocessResult:
    imported_rows: int
    affected_person_ids: set[str]


class ReprocessHandler(Protocol):
    def __call__(self, source_path: Path) -> ReprocessResult:
        """Reprocess *source_path* and return the affected records summary."""


@dataclass(frozen=True)
class ReprocessRegistration:
    name: str
    category_parts: tuple[str, ...]
    suffixes: frozenset[str]
    handler: ReprocessHandler
    path_predicate: Callable[[Path], bool] | None = None

    def matches(self, source_path: Path, processed_root: Path) -> bool:
        try:
            relative_parts = source_path.resolve().relative_to(processed_root.resolve()).parts
        except ValueError:
            return False

        if len(relative_parts) < len(self.category_parts) + 2:
            return False
        if tuple(relative_parts[1 : 1 + len(self.category_parts)]) != self.category_parts:
            return False
        if source_path.suffix.lower() not in self.suffixes:
            return False
        return self.path_predicate is None or self.path_predicate(source_path)


class ReprocessRegistry:
    def __init__(self) -> None:
        self._registrations: list[ReprocessRegistration] = []

    def register(
        self,
        *,
        name: str,
        category_parts: tuple[str, ...],
        suffixes: set[str],
        handler: ReprocessHandler,
        path_predicate: Callable[[Path], bool] | None = None,
    ) -> None:
        normalized_suffixes = frozenset(
            suffix if suffix == "" or suffix.startswith(".") else f".{suffix}"
            for suffix in suffixes
        )
        self._registrations.append(
            ReprocessRegistration(
                name=name,
                category_parts=category_parts,
                suffixes=normalized_suffixes,
                handler=handler,
                path_predicate=path_predicate,
            )
        )

    def handler_for(self, source_path: Path, processed_root: Path) -> ReprocessRegistration:
        matches = [
            registration
            for registration in self._registrations
            if registration.matches(source_path, processed_root)
        ]
        if not matches:
            raise ReprocessError(
                "retained source has no registered reprocess handler; "
                "expected a supported path under data/processed"
            )
        if len(matches) > 1:
            names = ", ".join(registration.name for registration in matches)
            raise ReprocessError(f"retained source matched multiple handlers: {names}")
        return matches[0]

    def reprocess(self, source_path: Path, processed_root: Path) -> ReprocessResult:
        registration = self.handler_for(source_path, processed_root)
        return registration.handler(source_path)
