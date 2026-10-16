"""Deterministic DNA replay from an isolated retained-source tree."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from famed.dependent_reports import refresh_dna_dependent_reports
from famed.dna_sync import (
    DnaSyncError,
    detect_dna_file,
    persist_dna_source,
)
from famed.import_labs import slugify_filename
from famed.reprocess_sources import restore_person_vault_tree, snapshot_person_vault_tree

_RETAINED_DATE_PREFIX = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<name>.+)$")


@dataclass(frozen=True)
class DnaReplaySummary:
    source_files: int
    import_rows: int
    genotype_rows: int
    affected_person_ids: frozenset[str]


@dataclass(frozen=True)
class _ReplaySource:
    path: Path
    person_id: str
    canonical_source_path: str
    source_file_name: str
    note_name: str
    imported_at: str
    provider: str


def _discover_sources(processed_root: Path, known_person_ids: set[str]) -> list[_ReplaySource]:
    sources: list[_ReplaySource] = []
    for genes_dir in sorted(processed_root.glob("*/Genes")):
        person_id = genes_dir.parent.name
        paths = sorted(path for path in genes_dir.iterdir() if path.is_file())
        if paths and person_id not in known_person_ids:
            raise DnaSyncError(f"retained DNA source belongs to unknown person: {person_id}")
        for path in paths:
            detection = detect_dna_file(path)
            if detection is None:
                raise DnaSyncError(f"retained Genes source has unsupported DNA format: {path}")
            match = _RETAINED_DATE_PREFIX.match(path.name)
            if match is None:
                raise DnaSyncError(f"retained DNA filename lacks import-date prefix: {path}")
            imported_at = match.group("date")
            try:
                date.fromisoformat(imported_at)
            except ValueError as exc:
                raise DnaSyncError(
                    f"retained DNA filename has invalid import-date prefix: {path}"
                ) from exc
            sources.append(
                _ReplaySource(
                    path=path,
                    person_id=person_id,
                    canonical_source_path=str(
                        Path("data/processed") / path.relative_to(processed_root)
                    ),
                    source_file_name=match.group("name"),
                    note_name=f"{slugify_filename(path.stem)}-dna.md",
                    imported_at=imported_at,
                    provider=detection.provider,
                )
            )
    return sources


def replay_retained_dna_sources(
    *,
    root: Path,
    vault_root: Path,
    processed_root: Path,
    conn: sqlite3.Connection,
) -> DnaReplaySummary:
    """Replay every retained Genes source as one DB/filesystem transaction."""
    known_person_ids = {str(row[0]) for row in conn.execute("SELECT id FROM persons")}
    sources = _discover_sources(processed_root, known_person_ids)
    affected_person_ids = frozenset(source.person_id for source in sources)
    snapshots = {
        person_id: snapshot_person_vault_tree(vault_root, person_id)
        for person_id in affected_person_ids
    }

    conn.execute("SAVEPOINT dna_replay")
    try:
        genotype_rows = 0
        for source in sources:
            variant_count = persist_dna_source(
                conn,
                vault_root,
                person_id=source.person_id,
                source_path=source.path,
                canonical_source_path=source.canonical_source_path,
                source_file_name=source.source_file_name,
                note_name=source.note_name,
                imported_at=source.imported_at,
                provider=source.provider,
            )
            genotype_rows += variant_count

        for person_id in sorted(affected_person_ids):
            refresh_dna_dependent_reports(
                conn,
                vault_root,
                person_id,
                ref_db_path=root / "data" / "reference" / "famed-reference.db",
                findings_ledger_path=None,
            )
        conn.execute("RELEASE SAVEPOINT dna_replay")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT dna_replay")
        conn.execute("RELEASE SAVEPOINT dna_replay")
        for person_id, snapshot in snapshots.items():
            restore_person_vault_tree(vault_root, person_id, snapshot)
        raise

    return DnaReplaySummary(
        source_files=len(sources),
        import_rows=len(sources),
        genotype_rows=genotype_rows,
        affected_person_ids=affected_person_ids,
    )
