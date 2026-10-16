"""Deterministic differential audit for an isolated cold rebuild."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from famed.findings_ledger import FINDINGS_LEDGER_PATH
from famed.person_sync import validate_vault_notes
from famed.rebuild_vault import RebuildError
from famed.reprocess_sources import restore_person_vault_tree, snapshot_person_vault_tree

AUDIT_SCHEMA = "rebuild-diff-v1"
SAMPLE_LIMIT = 10

CANONICAL_TABLES = (
    "persons",
    "family_links",
    "lab_results",
    "medications",
    "immunizations",
    "dna_imports",
    "genotypes",
)
OPERATIONAL_TABLES = ("audit_events", "ingestion_log", "sync_conflicts")
SEMANTIC_COLUMNS = {
    "persons": ("id", "display_name", "birth_year", "sex"),
    "family_links": ("source_person_id", "target_person_id", "relationship_type"),
    "lab_results": (
        "person_id",
        "loinc_code",
        "loinc_code_origin",
        "test_name",
        "value",
        "value_text",
        "unit",
        "reference_range_low",
        "reference_range_high",
        "interpretation_code",
        "observed_at",
    ),
    "medications": (
        "person_id",
        "rxnorm_cui",
        "medication_name",
        "medication_type",
        "dose",
        "frequency",
        "start_date",
        "end_date",
        "authorized_through",
        "status",
    ),
    "immunizations": (
        "person_id",
        "vaccine_name",
        "cvx_code",
        "administered_date",
        "status",
        "lot_number",
    ),
    "dna_imports": (
        "person_id",
        "provider",
        "file_checksum",
        "variant_count",
        "chip_version",
    ),
    "genotypes": (
        "person_id",
        "rsid",
        "chromosome",
        "position",
        "allele1",
        "allele2",
        "provider",
        "source_kind",
        "quality",
        "genome_build",
    ),
}
AUTHORITY_COLUMNS = {
    "lab_results": ("person_id", "test_name", "observed_at", "source_type"),
    "medications": (
        "person_id",
        "medication_name",
        "start_date",
        "source_type",
    ),
    "immunizations": (
        "person_id",
        "vaccine_name",
        "administered_date",
        "source_type",
    ),
}
PROVENANCE_COLUMNS = {
    "family_links": ("id",),
    "lab_results": ("id", "source_type", "source_path"),
    "medications": ("id", "source_type", "source_path"),
    "immunizations": (
        "id",
        "source_type",
        "source_path",
        "source_paths",
        "evidence_snippet",
    ),
    "dna_imports": ("id", "source_path"),
    "genotypes": ("id", "source_path"),
}
VOLATILE_COLUMNS = frozenset(
    {
        "created_at",
        "updated_at",
        "vault_synced_at",
        "imported_at",
        "occurred_at",
        "detected_at",
        "resolved_at",
    }
)
VOLATILE_FRONT_MATTER_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "generated_at",
        "imported_at",
        "vault_synced_at",
    }
)
VAULT_TRIAGE_STATUS = {
    "canonical_data": "candidate_bug",
    "source_evidence": "needs_review",
    "derived_artifact": "needs_review",
    "review_item": "needs_review",
    "operational_metadata": "contract_explained",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise RebuildError(f"audit root does not exist: {root}")
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _file_fingerprint(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return _sha256(path)


def _validate_output_path(
    output_path: Path,
    *,
    source_vault: Path,
    source_db: Path,
    source_processed: Path,
    target_vault: Path,
    target_db: Path,
    target_processed: Path,
    source_findings_ledger: Path | None,
) -> None:
    resolved = output_path.resolve()
    protected_files = {source_db.resolve(), target_db.resolve()}
    if source_findings_ledger is not None:
        protected_files.add(source_findings_ledger.resolve())
    protected_roots = (
        source_vault.resolve(),
        source_processed.resolve(),
        target_vault.resolve(),
        target_processed.resolve(),
    )
    database_sidecars = {
        Path(f"{database}{suffix}").resolve()
        for database in (source_db, target_db)
        for suffix in ("-journal", "-shm", "-wal")
    }
    protected_aliases = {*protected_files, *database_sidecars}
    if output_path.exists():
        protected_aliases.update(
            path for root in protected_roots for path in root.rglob("*") if path.is_file()
        )
    same_file = output_path.exists() and any(
        protected.exists() and os.path.samefile(output_path, protected)
        for protected in protected_aliases
    )
    if (
        resolved in protected_files
        or resolved in database_sidecars
        or same_file
        or any(root in resolved.parents for root in protected_roots)
    ):
        raise RebuildError("audit output must be outside source and target storage")


def _storage_fingerprint(
    *,
    vault: Path,
    database: Path,
    processed: Path,
    ledger: Path | None,
) -> dict[str, Any]:
    return {
        "vault": _inventory(vault),
        "database": _file_fingerprint(database),
        "processed": _inventory(processed),
        "findings_ledger": _file_fingerprint(ledger),
    }


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [
        str(row[1]) for row in conn.execute(f"PRAGMA {_quote(schema)}.table_info({_quote(table)})")
    ]


def _validate_projection_coverage(table: str, columns: list[str]) -> None:
    if table not in CANONICAL_TABLES:
        return
    stable = set(columns) - VOLATILE_COLUMNS
    covered = set(SEMANTIC_COLUMNS[table])
    covered.update(AUTHORITY_COLUMNS.get(table, ()))
    covered.update(PROVENANCE_COLUMNS.get(table, ()))
    missing = sorted(stable - covered)
    if missing:
        raise RebuildError(
            f"rebuild audit projection omits stable {table} columns: {', '.join(missing)}"
        )


def _projection(columns: Iterable[str]) -> str:
    return ", ".join(_quote(column) for column in columns)


def _multiset_difference_sql(
    *,
    left_schema: str,
    right_schema: str,
    table: str,
    columns: list[str],
    where: str | None = None,
) -> str:
    projection = _projection(columns)
    quoted_table = _quote(table)
    join = " AND ".join(
        f"left_rows.{_quote(column)} IS right_rows.{_quote(column)}" for column in columns
    )
    selected = ", ".join(f"left_rows.{_quote(column)}" for column in columns)
    predicate = f" WHERE {where}" if where is not None else ""
    return (
        f"WITH left_rows AS ("
        f"SELECT {projection}, COUNT(*) AS row_count "
        f"FROM {_quote(left_schema)}.{quoted_table}{predicate} GROUP BY {projection}"
        f"), right_rows AS ("
        f"SELECT {projection}, COUNT(*) AS row_count "
        f"FROM {_quote(right_schema)}.{quoted_table}{predicate} GROUP BY {projection}"
        f") SELECT {selected}, "
        f"left_rows.row_count - COALESCE(right_rows.row_count, 0) AS multiplicity "
        f"FROM left_rows LEFT JOIN right_rows ON {join} "
        f"WHERE left_rows.row_count > COALESCE(right_rows.row_count, 0)"
    )


def _rows_as_json(rows: Iterable[sqlite3.Row], columns: list[str]) -> list[dict[str, Any]]:
    sample_columns = [*columns, "multiplicity"]
    return [dict(zip(sample_columns, tuple(row), strict=True)) for row in rows]


def _append_difference(
    differences: list[dict[str, Any]],
    *,
    layer: str,
    category: str,
    kind: str,
    status: str,
    count: int,
    samples: list[Any],
) -> None:
    if count:
        differences.append(
            {
                "layer": layer,
                "category": category,
                "kind": kind,
                "triage_status": status,
                "count": count,
                "samples": samples,
            }
        )


def _compare_db(
    source_db: Path,
    target_db: Path,
    differences: list[dict[str, Any]],
) -> dict[str, Any]:
    if not source_db.is_file() or not target_db.is_file():
        raise RebuildError("both working and isolated databases must exist for comparison")
    source_uri = f"{source_db.resolve().as_uri()}?mode=ro"
    target_uri = f"{target_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS redriven", (target_uri,))
        source_fk = conn.execute("PRAGMA main.foreign_key_check").fetchall()
        target_fk = conn.execute("PRAGMA redriven.foreign_key_check").fetchall()
        if source_fk or target_fk:
            raise RebuildError(
                f"foreign-key validation failed: working={source_fk}, isolated={target_fk}"
            )

        for table in (*CANONICAL_TABLES, *OPERATIONAL_TABLES):
            source_columns = _table_columns(conn, "main", table)
            target_columns = _table_columns(conn, "redriven", table)
            common_columns = set(source_columns) & set(target_columns)
            if set(source_columns) != set(target_columns):
                _append_difference(
                    differences,
                    layer="database",
                    category="unresolved",
                    kind=f"{table}:schema",
                    status="candidate_bug",
                    count=1,
                    samples=[{"working": source_columns, "redriven": target_columns}],
                )
            _validate_projection_coverage(table, source_columns)
            _validate_projection_coverage(table, target_columns)

            projections: list[tuple[str, list[str], str, str, str | None]]
            if table in CANONICAL_TABLES:
                projections = [
                    (
                        "canonical_data",
                        [column for column in SEMANTIC_COLUMNS[table] if column in common_columns],
                        "",
                        "candidate_bug",
                        None,
                    )
                ]
                if table in AUTHORITY_COLUMNS and "source_type" in common_columns:
                    projections.append(
                        (
                            "authority_overlay",
                            [
                                column
                                for column in AUTHORITY_COLUMNS[table]
                                if column in common_columns
                            ],
                            "authority_",
                            "candidate_bug",
                            "source_type IN ('user_update', 'user_correction')",
                        )
                    )
                if table in PROVENANCE_COLUMNS:
                    projections.append(
                        (
                            "provenance",
                            [
                                column
                                for column in PROVENANCE_COLUMNS[table]
                                if column in common_columns
                            ],
                            "provenance_",
                            "needs_review",
                            None,
                        )
                    )
            else:
                projections = [
                    (
                        "operational_metadata",
                        [
                            column
                            for column in source_columns
                            if column in common_columns and column not in VOLATILE_COLUMNS
                        ],
                        "",
                        "contract_explained",
                        None,
                    )
                ]
            for category, columns, kind_prefix, status, where in projections:
                if not columns:
                    continue
                for left, right, direction in (
                    ("main", "redriven", "working_only"),
                    ("redriven", "main", "redriven_only"),
                ):
                    query = _multiset_difference_sql(
                        left_schema=left,
                        right_schema=right,
                        table=table,
                        columns=columns,
                        where=where,
                    )
                    count = int(
                        conn.execute(
                            f"SELECT COALESCE(SUM(multiplicity), 0) FROM ({query})"
                        ).fetchone()[0]
                    )
                    order_by = ", ".join(str(index) for index in range(1, len(columns) + 1))
                    sample_rows = conn.execute(
                        f"SELECT * FROM ({query}) ORDER BY {order_by} LIMIT ?",
                        (SAMPLE_LIMIT,),
                    ).fetchall()
                    _append_difference(
                        differences,
                        layer="database",
                        category=category,
                        kind=f"{table}:{kind_prefix}{direction}",
                        status=status,
                        count=count,
                        samples=_rows_as_json(sample_rows, columns),
                    )

            volatile_columns = [
                column
                for column in source_columns
                if column in common_columns and column in VOLATILE_COLUMNS
            ]
            if "id" not in common_columns or not volatile_columns:
                continue
            comparisons = " OR ".join(
                f"NOT (w.{_quote(column)} IS r.{_quote(column)})" for column in volatile_columns
            )
            projection = ", ".join(
                [
                    "w.id AS id",
                    *(
                        f"w.{_quote(column)} AS working_{column}, "
                        f"r.{_quote(column)} AS redriven_{column}"
                        for column in volatile_columns
                    ),
                ]
            )
            query = (
                f"SELECT {projection} FROM main.{_quote(table)} w "
                f"JOIN redriven.{_quote(table)} r ON w.id = r.id "
                f"WHERE {comparisons}"
            )
            count = int(conn.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0])
            sample_rows = [
                dict(row)
                for row in conn.execute(
                    f"{query} ORDER BY w.id LIMIT ?", (SAMPLE_LIMIT,)
                ).fetchall()
            ]
            _append_difference(
                differences,
                layer="database",
                category="operational_metadata",
                kind=f"{table}:volatile_values",
                status="contract_explained",
                count=count,
                samples=sample_rows,
            )
    return {"working_foreign_keys": 0, "redriven_foreign_keys": 0}


def _compare_processed(
    working: dict[str, str],
    redriven: dict[str, str],
    differences: list[dict[str, Any]],
    *,
    allowed_target_files: set[str],
) -> None:
    working_paths = set(working)
    redriven_paths = set(redriven)
    working_only = sorted(working_paths - redriven_paths)
    redriven_only = sorted(redriven_paths - working_paths)
    recovered = sorted(path for path in redriven_only if path in allowed_target_files)
    unexplained_target = [path for path in redriven_only if path not in allowed_target_files]
    mismatched = sorted(
        path for path in working_paths & redriven_paths if working[path] != redriven[path]
    )
    _append_difference(
        differences,
        layer="processed_sources",
        category="retained_source",
        kind="working_only",
        status="candidate_bug",
        count=len(working_only),
        samples=working_only[:SAMPLE_LIMIT],
    )
    _append_difference(
        differences,
        layer="processed_sources",
        category="authority_overlay",
        kind="redriven_recovered_authority_snapshot",
        status="contract_explained",
        count=len(recovered),
        samples=recovered[:SAMPLE_LIMIT],
    )
    _append_difference(
        differences,
        layer="processed_sources",
        category="retained_source",
        kind="redriven_only",
        status="needs_review",
        count=len(unexplained_target),
        samples=unexplained_target[:SAMPLE_LIMIT],
    )
    _append_difference(
        differences,
        layer="processed_sources",
        category="retained_source",
        kind="content_mismatch",
        status="candidate_bug",
        count=len(mismatched),
        samples=[
            {
                "path": path,
                "working_sha256": working[path],
                "redriven_sha256": redriven[path],
            }
            for path in mismatched[:SAMPLE_LIMIT]
        ],
    )


def _split_front_matter(content: bytes) -> tuple[dict[str, Any] | None, bytes]:
    text = content.decode("utf-8")
    if not text.startswith("---\n"):
        return None, content
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return None, content
    loaded = yaml.safe_load(text[4:closing]) or {}
    if not isinstance(loaded, dict):
        return None, content
    return dict(loaded), text[closing + 5 :].encode()


def _markdown_difference_kind(working: bytes, redriven: bytes) -> str:
    """Classify Markdown bytes without conflating YAML style with semantic drift."""
    working_front, working_body = _split_front_matter(working)
    redriven_front, redriven_body = _split_front_matter(redriven)
    if working_front is None or redriven_front is None or working_body != redriven_body:
        return "substantive"
    if working_front == redriven_front:
        return "format_only_front_matter"
    for key in VOLATILE_FRONT_MATTER_KEYS:
        working_front.pop(key, None)
        redriven_front.pop(key, None)
    if working_front == redriven_front:
        return "volatile_front_matter"
    return "substantive"


def _vault_category(path: str) -> str:
    if path.startswith(".obsidian/") or path in {".DS_Store", "famed.db"}:
        return "operational_metadata"
    if "/Reports/" in f"/{path}" or path.startswith("Reports/"):
        return "derived_artifact"
    if Path(path).name == "_index.md":
        return "derived_artifact"
    return "canonical_data"


def _vault_triage(path: str, note_path: Path) -> tuple[str, str]:
    category = _vault_category(path)
    if category == "canonical_data" and note_path.suffix == ".md":
        try:
            front_matter, _ = _split_front_matter(note_path.read_bytes())
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            front_matter = None
        if front_matter and (
            front_matter.get("verification_status") == "needs_review"
            or front_matter.get("extraction_mode") == "clinical_interpreter"
        ):
            category = "review_item"
        elif front_matter and (
            (
                front_matter.get("projection_role") == "source_evidence"
                and front_matter.get("source_type") not in {"user_update", "user_correction"}
            )
            or (
                front_matter.get("record_type") in {"lab_panel", "medication"}
                and bool(front_matter.get("source_path"))
                and front_matter.get("source_type") not in {"user_update", "user_correction"}
            )
        ):
            category = "source_evidence"
    return category, VAULT_TRIAGE_STATUS[category]


def _mismatched_vault_triage(
    path: str,
    working_path: Path,
    redriven_path: Path,
) -> tuple[str, str]:
    for note_path in (working_path, redriven_path):
        try:
            front_matter, _ = _split_front_matter(note_path.read_bytes())
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            front_matter = None
        if front_matter and front_matter.get("extraction_mode") == "clinical_interpreter":
            return "review_item", "needs_review"
    working = _vault_triage(path, working_path)
    redriven = _vault_triage(path, redriven_path)
    if working == redriven:
        return working
    if "source_evidence" in {working[0], redriven[0]}:
        return "source_evidence", "needs_review"
    if "candidate_bug" in {working[1], redriven[1]}:
        return "canonical_data", "candidate_bug"
    if "needs_review" in {working[1], redriven[1]}:
        return "review_item", "needs_review"
    return "operational_metadata", "contract_explained"


def _append_vault_path_differences(
    differences: list[dict[str, Any]],
    *,
    direction: str,
    paths: list[str],
    root: Path,
) -> None:
    grouped: dict[tuple[str, str], list[str]] = {}
    for path in paths:
        grouped.setdefault(_vault_triage(path, root / path), []).append(path)
    for (category, status), selected in sorted(grouped.items()):
        _append_difference(
            differences,
            layer="vault",
            category=category,
            kind=direction,
            status=status,
            count=len(selected),
            samples=selected[:SAMPLE_LIMIT],
        )


def _compare_vault(
    working_root: Path,
    redriven_root: Path,
    working: dict[str, str],
    redriven: dict[str, str],
    differences: list[dict[str, Any]],
) -> dict[str, Any]:
    working_issues = validate_vault_notes(working_root)
    redriven_issues = validate_vault_notes(redriven_root)
    if working_issues or redriven_issues:
        raise RebuildError(
            "vault validation failed: "
            f"working={len(working_issues)}, isolated={len(redriven_issues)}"
        )
    working_only = sorted(set(working) - set(redriven))
    redriven_only = sorted(set(redriven) - set(working))
    mismatched = sorted(
        path for path in set(working) & set(redriven) if working[path] != redriven[path]
    )
    _append_vault_path_differences(
        differences,
        direction="working_only",
        paths=working_only,
        root=working_root,
    )
    _append_vault_path_differences(
        differences,
        direction="redriven_only",
        paths=redriven_only,
        root=redriven_root,
    )

    format_only: list[str] = []
    volatile: list[str] = []
    substantive: list[str] = []
    for path in mismatched:
        working_bytes = (working_root / path).read_bytes()
        redriven_bytes = (redriven_root / path).read_bytes()
        difference_kind = (
            _markdown_difference_kind(working_bytes, redriven_bytes)
            if path.endswith(".md")
            else "substantive"
        )
        if difference_kind == "format_only_front_matter":
            format_only.append(path)
        elif difference_kind == "volatile_front_matter":
            volatile.append(path)
        else:
            substantive.append(path)
    _append_difference(
        differences,
        layer="vault",
        category="operational_metadata",
        kind="format_only_front_matter",
        status="contract_explained",
        count=len(format_only),
        samples=format_only[:SAMPLE_LIMIT],
    )
    _append_difference(
        differences,
        layer="vault",
        category="operational_metadata",
        kind="volatile_front_matter",
        status="contract_explained",
        count=len(volatile),
        samples=volatile[:SAMPLE_LIMIT],
    )
    grouped: dict[tuple[str, str], list[str]] = {}
    for path in substantive:
        triage = _mismatched_vault_triage(
            path,
            working_root / path,
            redriven_root / path,
        )
        grouped.setdefault(triage, []).append(path)
    for (category, status), selected in sorted(grouped.items()):
        _append_difference(
            differences,
            layer="vault",
            category=category,
            kind="content_mismatch",
            status=status,
            count=len(selected),
            samples=[
                {
                    "path": path,
                    "working_sha256": working[path],
                    "redriven_sha256": redriven[path],
                }
                for path in selected[:SAMPLE_LIMIT]
            ],
        )
    return {"working_issues": 0, "redriven_issues": 0}


def _ledger_event_count(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _classify_unsupported_source(path: str, message: str) -> tuple[str, str]:
    if (
        "deferred to the authority-overlay stage" in message
        or "/Genes/" in f"/{path}"
        or Path(path).name == ".DS_Store"
    ):
        return "contract_explained", "staged_or_operational_source"
    return "needs_review", "unsupported_source"


def _append_replay_outcomes(
    differences: list[dict[str, Any]],
    outcomes: Iterable[tuple[str, str, str]],
) -> dict[str, int]:
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    counts: dict[str, int] = {}
    for outcome, path, message in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "failed":
            status, kind = "candidate_bug", "failed_source"
        elif outcome == "zero_fact":
            status, kind = "needs_review", "zero_fact_source"
        elif outcome == "unsupported":
            status, kind = _classify_unsupported_source(path, message)
        else:
            continue
        grouped.setdefault((status, kind, outcome), []).append({"path": path, "message": message})
    for (status, kind, _outcome), items in sorted(grouped.items()):
        items.sort(key=lambda item: (item["path"], item["message"]))
        _append_difference(
            differences,
            layer="source_replay",
            category="source_processing",
            kind=kind,
            status=status,
            count=len(items),
            samples=items[:SAMPLE_LIMIT],
        )
    return dict(sorted(counts.items()))


def refresh_isolated_household(
    *,
    root: Path,
    target_vault: Path,
    conn: sqlite3.Connection,
) -> dict[str, list[Path]]:
    """Refresh target-only household reports and roll back both stores on failure."""
    from famed.dependent_reports import refresh_derived_reports_for_household

    person_ids = [
        str(row[0]) for row in conn.execute("SELECT id FROM persons ORDER BY id").fetchall()
    ]
    snapshots = {
        person_id: snapshot_person_vault_tree(target_vault, person_id) for person_id in person_ids
    }
    conn.execute("SAVEPOINT rebuild_household_refresh")
    try:
        result = refresh_derived_reports_for_household(
            conn,
            target_vault,
            ref_db_path=root / "data" / "reference" / "famed-reference.db",
            findings_ledger_path=None,
        )
        conn.execute("RELEASE SAVEPOINT rebuild_household_refresh")
        conn.commit()
        return result
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT rebuild_household_refresh")
        conn.execute("RELEASE SAVEPOINT rebuild_household_refresh")
        for person_id, snapshot in snapshots.items():
            restore_person_vault_tree(target_vault, person_id, snapshot)
        raise RebuildError(f"isolated household refresh failed: {exc}") from exc


def compare_rebuild(
    *,
    source_vault: Path,
    source_db: Path,
    source_processed: Path,
    target_vault: Path,
    target_db: Path,
    target_processed: Path,
    output_path: Path,
    source_findings_ledger: Path | None = FINDINGS_LEDGER_PATH,
    allowed_target_files: set[Path] | None = None,
    replay_outcomes: Iterable[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    """Compare working and redriven stores without mutating either side."""
    _validate_output_path(
        output_path,
        source_vault=source_vault,
        source_db=source_db,
        source_processed=source_processed,
        target_vault=target_vault,
        target_db=target_db,
        target_processed=target_processed,
        source_findings_ledger=source_findings_ledger,
    )
    working_before = _storage_fingerprint(
        vault=source_vault,
        database=source_db,
        processed=source_processed,
        ledger=source_findings_ledger,
    )
    target_processed_inventory = _inventory(target_processed)
    target_vault_inventory = _inventory(target_vault)
    differences: list[dict[str, Any]] = []
    allowed_relative = {
        (
            path.relative_to(source_processed).as_posix()
            if path.is_absolute() and path.is_relative_to(source_processed)
            else path.as_posix()
        )
        for path in (allowed_target_files or set())
    }
    _compare_processed(
        working_before["processed"],
        target_processed_inventory,
        differences,
        allowed_target_files=allowed_relative,
    )
    validations = {
        "database": _compare_db(source_db, target_db, differences),
        "vault": _compare_vault(
            source_vault,
            target_vault,
            working_before["vault"],
            target_vault_inventory,
            differences,
        ),
    }
    ledger_events = _ledger_event_count(source_findings_ledger)
    _append_difference(
        differences,
        layer="findings_ledger",
        category="ledger_input",
        kind="working_events_excluded_from_redrive",
        status="contract_explained",
        count=ledger_events,
        samples=[],
    )
    replay_outcome_counts = _append_replay_outcomes(differences, replay_outcomes)

    working_after = _storage_fingerprint(
        vault=source_vault,
        database=source_db,
        processed=source_processed,
        ledger=source_findings_ledger,
    )
    if working_before != working_after:
        raise RebuildError("comparison mutated working storage")

    differences.sort(key=lambda item: (item["layer"], item["category"], item["kind"]))
    audit = {
        "schema": AUDIT_SCHEMA,
        "working_storage_unchanged": True,
        "validations": validations,
        "replay_outcomes": replay_outcome_counts,
        "summary": {
            "difference_classes": len(differences),
            "difference_count": sum(int(item["count"]) for item in differences),
            "candidate_bug_count": sum(
                int(item["count"])
                for item in differences
                if item["triage_status"] == "candidate_bug"
            ),
            "needs_review_count": sum(
                int(item["count"])
                for item in differences
                if item["triage_status"] == "needs_review"
            ),
            "contract_explained_count": sum(
                int(item["count"])
                for item in differences
                if item["triage_status"] == "contract_explained"
            ),
        },
        "differences": differences,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return audit
