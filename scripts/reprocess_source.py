#!/usr/bin/env python3
"""Internal entry point for retained-source reprocessing. Invoked by fm-reprocess-source skill."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reprocess a retained source file under data/processed/."
    )
    parser.add_argument(
        "source_path",
        nargs="?",
        help="Path to the retained source file to reprocess",
    )
    parser.add_argument(
        "--all-healthsummary-archives",
        action="store_true",
        help=(
            "Reprocess every retained HealthSummary archive under data/processed/ and "
            "regenerate the per-person lab trends and health advisory reports."
        ),
    )
    parser.add_argument(
        "--db",
        default="data/famed.db",
        help="Path to SQLite database (default: data/famed.db)",
    )
    parser.add_argument(
        "--vault",
        default="famed-vault",
        help="Path to vault root (default: famed-vault)",
    )
    parser.add_argument(
        "--retained-decisions",
        "--history-decisions",
        dest="retained_decisions",
        help=(
            "Apply a source-bound retained semantic decision bundle while reprocessing; "
            "--history-decisions remains a compatibility alias."
        ),
    )
    args = parser.parse_args()

    if bool(args.source_path) == bool(args.all_healthsummary_archives):
        parser.error("provide either source_path or --all-healthsummary-archives")

    root = repo_root()

    target_path: Path | None = None
    if args.source_path is not None:
        target_path = Path(args.source_path)
        if not target_path.is_absolute():
            target_path = (root / target_path).resolve()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (root / db_path).resolve()

    vault_root = Path(args.vault)
    if not vault_root.is_absolute():
        vault_root = (root / vault_root).resolve()

    retained_decisions = (
        Path(args.retained_decisions) if args.retained_decisions is not None else None
    )
    if retained_decisions is not None and not retained_decisions.is_absolute():
        retained_decisions = (root / retained_decisions).resolve()

    from famed.annotation_cache import REFERENCE_DB_FILENAME
    from famed.dependent_reports import has_composite_report_surface
    from famed.health_advisory import write_health_advisory_unattended
    from famed.lab_sync import write_lab_results_report
    from famed.reprocess_sources import ReprocessError
    from famed.retained_replay import (
        build_retained_reprocess_registry,
        load_retained_history_decisions,
    )
    from famed.storage_contract import configured_storage_roots

    storage_roots = configured_storage_roots(root)
    reference_db_path = root / "data" / "reference" / REFERENCE_DB_FILENAME

    def refresh_healthsummary_reports(
        conn: sqlite3.Connection,
        *,
        person_id: str,
    ) -> tuple[Path | None, Path | None]:
        trend_path, _ = write_lab_results_report(conn, vault_root, person_id)
        if not has_composite_report_surface(vault_root, person_id):
            return trend_path, None
        advisory_path, _ = write_health_advisory_unattended(
            vault_root=vault_root,
            person_id=person_id,
            conn=conn,
            ref_db_path=reference_db_path,
        )
        return trend_path, advisory_path

    def iter_healthsummary_archives() -> list[Path]:
        return sorted(storage_roots.processed.glob("*/HealthSummary/*.zip"))

    try:
        decision_interpreter = (
            load_retained_history_decisions(
                decision_file=retained_decisions,
                processed_root=storage_roots.processed,
            )
            if retained_decisions is not None
            else None
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            registry = build_retained_reprocess_registry(
                root=root,
                vault_root=vault_root,
                processed_root=storage_roots.processed,
                conn=conn,
                history_fact_interpreter=decision_interpreter,
            )
            if args.all_healthsummary_archives:
                archives = iter_healthsummary_archives()
                if not archives:
                    print(
                        "No retained HealthSummary archives found under data/processed/.",
                        flush=True,
                    )
                    return
                total_rows = 0
                affected_person_ids: set[str] = set()
                refreshed_advisories = 0
                for archive_path in archives:
                    result = registry.reprocess(archive_path, storage_roots.processed)
                    total_rows += result.imported_rows
                    affected_person_ids.update(result.affected_person_ids)
                    print(
                        f"Reprocessed {archive_path.relative_to(root)!s}: "
                        f"{result.imported_rows} rows",
                        flush=True,
                    )
                for person_id in sorted(affected_person_ids):
                    _trend_path, advisory_path = refresh_healthsummary_reports(
                        conn,
                        person_id=person_id,
                    )
                    if advisory_path is not None:
                        refreshed_advisories += 1
                print(
                    "HealthSummary archive reprocessing completed: "
                    f"{len(archives)} archive(s), {total_rows} rows, "
                    f"persons: {sorted(affected_person_ids)}, "
                    f"health advisories refreshed: {refreshed_advisories}",
                    flush=True,
                )
                return

            assert target_path is not None
            result = registry.reprocess(target_path, storage_roots.processed)
            print(
                f"Reprocessed {target_path.name!r}: {result.imported_rows} rows, "
                f"persons: {sorted(result.affected_person_ids)}",
                flush=True,
            )
    except ReprocessError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
