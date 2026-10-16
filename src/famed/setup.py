#!/usr/bin/env python3
"""Bootstrap a local famed vault and database."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from famed.storage_contract import (
    AUDIT_EVENTS_SCHEMA_SQL,
    DEDUP_LINKS_SCHEMA_SQL,
    configured_storage_roots,
)

SCHEMA_SQL = (
    """
CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    birth_year INTEGER,
    sex TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    vault_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS family_links (
    id TEXT PRIMARY KEY,
    source_person_id TEXT NOT NULL,
    target_person_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_person_id) REFERENCES persons(id),
    FOREIGN KEY(target_person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS lab_results (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    loinc_code TEXT,
    loinc_code_origin TEXT,
    test_name TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    unit TEXT,
    reference_range_low REAL,
    reference_range_high REAL,
    interpretation_code TEXT,
    observed_at TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS medications (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    rxnorm_cui TEXT,
    medication_name TEXT NOT NULL,
    medication_type TEXT NOT NULL DEFAULT 'prescription',
    dose TEXT,
    frequency TEXT,
    start_date TEXT,
    end_date TEXT,
    authorized_through TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS immunizations (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    vaccine_name TEXT NOT NULL,
    cvx_code TEXT,
    administered_date TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    lot_number TEXT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_path TEXT,
    source_paths TEXT,
    evidence_snippet TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_checksum TEXT,
    status TEXT NOT NULL,
    message TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_conflicts (
    id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    field_name TEXT,
    vault_value TEXT,
    db_value TEXT,
    detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    resolution TEXT,
    resolved_by TEXT
);

"""
    + AUDIT_EVENTS_SCHEMA_SQL
    + DEDUP_LINKS_SCHEMA_SQL
    + """
CREATE TABLE IF NOT EXISTS genotypes (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    rsid TEXT NOT NULL,
    chromosome TEXT NOT NULL,
    position INTEGER,
    allele1 TEXT NOT NULL,
    allele2 TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_path TEXT,
    source_kind TEXT NOT NULL DEFAULT 'genotyped',
    quality REAL,
    genome_build TEXT,
    FOREIGN KEY(person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS dna_imports (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    file_checksum TEXT NOT NULL,
    source_path TEXT NOT NULL,
    variant_count INTEGER NOT NULL DEFAULT 0,
    chip_version TEXT,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(person_id) REFERENCES persons(id)
);

"""
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up a famed vault and local SQLite index.")
    parser.add_argument(
        "--vault-path",
        default="famed-vault",
        help="Path to the vault directory to create or update (default: famed-vault).",
    )
    parser.add_argument(
        "--db-path",
        default="data/famed.db",
        help="Path to the SQLite database file (default: data/famed.db).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run dependency/plugin readiness checks only.",
    )
    parser.add_argument(
        "--skip-keychain",
        action="store_true",
        help="Skip creating a placeholder API key entry in macOS Keychain.",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def copy_template_tree(
    template_root: Path,
    vault_root: Path,
    *,
    overwrite: bool = False,
) -> None:
    vault_root.mkdir(parents=True, exist_ok=True)

    for source in template_root.rglob("*"):
        relative = source.relative_to(template_root)
        destination = vault_root / relative

        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not destination.exists():
            shutil.copy2(source, destination)


def ensure_data_directories(root: Path) -> None:
    storage_roots = configured_storage_roots(root)
    paths = [
        root / "tmp",
        root / "data/raw",
        storage_roots.quarantine,
        storage_roots.inbox,
        storage_roots.inbox / "persons",
        storage_roots.processed,  # retained source files, post-ingest; gitignored
        storage_roots.reference,  # refreshable external reference data; gitignored
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def initialize_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Migration: add vault_synced_at to persons for existing databases.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(persons)")}
        if "vault_synced_at" not in columns:
            conn.execute("ALTER TABLE persons ADD COLUMN vault_synced_at TEXT")
        # Migration: drop vestigial family_role column.
        if "family_role" in columns:
            conn.execute("ALTER TABLE persons DROP COLUMN family_role")
        # Migration: add medication_type to medications for existing databases.
        med_columns = {row[1] for row in conn.execute("PRAGMA table_info(medications)")}
        if "medication_type" not in med_columns:
            conn.execute(
                "ALTER TABLE medications ADD COLUMN medication_type TEXT NOT NULL "
                "DEFAULT 'prescription'"
            )
        if "source_type" not in med_columns:
            conn.execute(
                "ALTER TABLE medications ADD COLUMN source_type TEXT NOT NULL DEFAULT 'manual'"
            )
        # Migration: separate prescription authorization window from therapy end (ADR-041).
        if "authorized_through" not in med_columns:
            conn.execute("ALTER TABLE medications ADD COLUMN authorized_through TEXT")
        immunization_columns = {row[1] for row in conn.execute("PRAGMA table_info(immunizations)")}
        if not immunization_columns:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS immunizations (
                    id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL,
                    vaccine_name TEXT NOT NULL,
                    cvx_code TEXT,
                    administered_date TEXT,
                    status TEXT NOT NULL DEFAULT 'completed',
                    lot_number TEXT,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    source_path TEXT,
                    source_paths TEXT,
                    evidence_snippet TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(person_id) REFERENCES persons(id)
                )
                """
            )
        elif "source_paths" not in immunization_columns:
            conn.execute("ALTER TABLE immunizations ADD COLUMN source_paths TEXT")
        # Migration: add explicit LOINC code provenance to lab_results (ADR-034).
        # Backfill deterministically from source_type: codes from structured
        # imports or user-authored corrections are source-provided; any other
        # non-blank code was filled by lookup_loinc_code inference.
        lab_columns = {row[1] for row in conn.execute("PRAGMA table_info(lab_results)")}
        if "value_text" not in lab_columns:
            conn.execute("ALTER TABLE lab_results ADD COLUMN value_text TEXT")
        if "interpretation_code" not in lab_columns:
            # The source's own per-observation abnormality flag (HL7 v3
            # ObservationInterpretation: H high, L low, A abnormal, AA
            # critically abnormal). Previously discarded, which lost the lab's
            # judgement on individual results -- including flags on results
            # sitting inside their own stated reference range. No backfill is
            # possible from stored data; existing rows stay NULL until their
            # source is reprocessed.
            conn.execute("ALTER TABLE lab_results ADD COLUMN interpretation_code TEXT")
            conn.execute("ALTER TABLE lab_results ADD COLUMN interpretation_code_origin TEXT")
        if "loinc_code_origin" not in lab_columns:
            conn.execute("ALTER TABLE lab_results ADD COLUMN loinc_code_origin TEXT")
            conn.execute(
                "UPDATE lab_results SET loinc_code_origin = 'source_provided' "
                "WHERE loinc_code IS NOT NULL AND TRIM(loinc_code) != '' "
                "AND source_type IN ('source_import', 'user_correction', 'user_update')"
            )
            conn.execute(
                "UPDATE lab_results SET loinc_code_origin = 'inferred' "
                "WHERE loinc_code IS NOT NULL AND TRIM(loinc_code) != '' "
                "AND (loinc_code_origin IS NULL OR loinc_code_origin = '')"
            )
        genotype_columns = {row[1] for row in conn.execute("PRAGMA table_info(genotypes)")}
        if "source_kind" not in genotype_columns:
            conn.execute(
                "ALTER TABLE genotypes ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'genotyped'"
            )
        if "quality" not in genotype_columns:
            conn.execute("ALTER TABLE genotypes ADD COLUMN quality REAL")
        if "genome_build" not in genotype_columns:
            conn.execute("ALTER TABLE genotypes ADD COLUMN genome_build TEXT")
        conn.commit()


def ensure_keychain_placeholder(service: str = "famed", account: str = "FAMED_API_KEY") -> str:
    if sys.platform != "darwin":
        return "Skipped keychain setup (not macOS)."

    find_cmd = ["security", "find-generic-password", "-s", service, "-a", account]
    find_result = subprocess.run(find_cmd, capture_output=True, text=True, check=False)
    if find_result.returncode == 0:
        return f"Keychain slot already exists for {account}."

    add_cmd = [
        "security",
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        account,
        "-w",
        "PLACEHOLDER_SET_REAL_KEY",
    ]
    add_result = subprocess.run(add_cmd, capture_output=True, text=True, check=False)
    if add_result.returncode == 0:
        return f"Created placeholder keychain slot for {account}."

    stderr = add_result.stderr.strip() or "unknown error"
    return f"Warning: could not create keychain slot for {account}: {stderr}"


def _version_tuple(raw_version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", raw_version)
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts[:3])


def _python_pkg_status(dist_name: str, min_version: str) -> tuple[str, str]:
    try:
        installed = importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return "MISSING", f"not installed; run: python -m pip install '{dist_name}>={min_version}'"

    if _version_tuple(installed) < _version_tuple(min_version):
        return (
            "OUTDATED",
            f"installed {installed}, requires >= {min_version}; "
            f"run: python -m pip install -U '{dist_name}>={min_version}'",
        )

    return "OK", f"installed {installed}"


def _dataview_status(vault_root: Path) -> tuple[str, str]:
    plugin_dir = vault_root / ".obsidian" / "plugins" / "dataview"
    if plugin_dir.exists():
        return "OK", f"found at {plugin_dir}"
    return "MISSING", "install Obsidian Dataview plugin in this vault"


def _keychain_status(skip_keychain: bool) -> tuple[str, str]:
    if skip_keychain:
        return "OK", "skipped by --skip-keychain"

    if sys.platform != "darwin":
        return "MISSING", "macOS Keychain is required on this project baseline"

    cmd = ["security", "find-generic-password", "-s", "famed", "-a", "FAMED_API_KEY"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return "OK", "keychain slot FAMED_API_KEY exists"
    return "MISSING", "run: python scripts/setup.py"


def _plink_status() -> tuple[str, str]:
    path = shutil.which("plink")
    if path:
        return "OK", f"found at {path}"
    return "MISSING", "optional; install plink only for advanced genetics workflows"


def _tesseract_status() -> tuple[str, str]:
    try:
        from famed.ocr import is_ocr_available

        if is_ocr_available():
            import pytesseract

            version = pytesseract.get_tesseract_version()
            return "OK", f"tesseract {version}"
    except Exception:
        pass
    return "MISSING", "optional TIF OCR; install: brew install tesseract && pip install pytesseract"


def _annotation_cache_status() -> tuple[str, str]:
    try:
        from famed.annotation_cache import _reference_db_path, get_annotation_status
        from famed.reference_cache import _cache_root

        cache_dir = _cache_root()
        ref_db = _reference_db_path(cache_dir)
        status = get_annotation_status(ref_db)
        ok = [k for k, v in status.items() if v["status"] == "OK"]
        missing = [k for k, v in status.items() if v["status"] != "OK"]
        if not missing:
            counts = ", ".join(f"{k}: {status[k]['row_count']:,} rows" for k in ok)
            return "OK", f"annotation cache ready — {counts}"
        msg = (
            f"missing: {', '.join(missing)}. Run: python scripts/cache.py --dataset <name> refresh"
        )
        return "MISSING", msg
    except Exception as exc:
        return "MISSING", f"could not determine annotation cache status: {exc}"


def _reference_cache_status() -> tuple[str, str]:
    try:
        from famed.reference_cache import (
            STATUS_MISSING,
            STATUS_OK,
            STATUS_STALE,
            _cache_root,
            cache_status,
        )

        cache_dir = _cache_root()
        status = cache_status(cache_dir)
        ok = [k for k, v in status.items() if v["status"] == STATUS_OK]
        stale = [k for k, v in status.items() if v["status"] == STATUS_STALE]
        missing = [k for k, v in status.items() if v["status"] == STATUS_MISSING]
        if not stale and not missing:
            return "OK", f"all datasets cached: {', '.join(ok)}"
        parts = []
        if stale:
            parts.append(f"stale: {', '.join(stale)}")
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        msg = "; ".join(parts)
        if stale:
            return "STALE", f"{msg}. Run: python scripts/cache.py refresh"
        return "MISSING", (
            f"{msg}. Run: python scripts/cache.py download for downloadable datasets; "
            "place LOINC files under data/reference/loinc/ manually"
        )
    except Exception as exc:
        return "MISSING", f"could not determine cache status: {exc}"


def run_dependency_check(vault_root: Path, skip_keychain: bool) -> int:
    checks: list[dict[str, Any]] = [
        {
            "name": "PyYAML",
            "required_for": "All workflows",
            "failure_mode": "Block",
            "status": _python_pkg_status("PyYAML", "6.0.1"),
        },
        {
            "name": "pypdf",
            "required_for": "PDF import",
            "failure_mode": "Block",
            "status": _python_pkg_status("pypdf", "4.3.0"),
        },
        {
            "name": "Obsidian Dataview plugin",
            "required_for": "Vault browsing",
            "failure_mode": "Block",
            "status": _dataview_status(vault_root),
        },
        {
            "name": "macOS Keychain",
            "required_for": "All workflows",
            "failure_mode": "Block",
            "status": _keychain_status(skip_keychain),
        },
        {
            "name": "Tesseract OCR",
            "required_for": "TIF history documents (OCR)",
            "failure_mode": "Warn",
            "status": _tesseract_status(),
        },
        {
            "name": "PLINK binary",
            "required_for": "Advanced genetics (optional)",
            "failure_mode": "Warn",
            "status": _plink_status(),
        },
        {
            "name": "Reference data cache",
            "required_for": "LOINC and RxNorm coding",
            "failure_mode": "Warn",
            "status": _reference_cache_status(),
        },
        {
            "name": "Annotation cache (CPIC, ClinVar, GenCC)",
            "required_for": "DNA reports",
            "failure_mode": "Warn",
            "status": _annotation_cache_status(),
        },
    ]

    print("Dependency check results:")
    has_blocking_failure = False
    for check in checks:
        status, detail = check["status"]  # pyright: ignore[reportGeneralTypeIssues]
        print(f"- {status:<8} {check['name']}: {detail}")
        if check["failure_mode"] == "Block" and status in {"MISSING", "OUTDATED"}:
            has_blocking_failure = True

    return 1 if has_blocking_failure else 0


def main() -> int:
    args = parse_args()
    root = repo_root()
    template_root = root / "vault-template"
    vault_root = (root / args.vault_path).resolve()
    db_path = (root / args.db_path).resolve()

    if args.check:
        return run_dependency_check(vault_root, skip_keychain=args.skip_keychain)

    if not template_root.exists():
        print(f"Missing template directory: {template_root}")
        return 1

    copy_template_tree(template_root, vault_root)
    ensure_data_directories(root)
    initialize_database(db_path)

    keychain_status = "Skipped keychain setup by request."
    if not args.skip_keychain:
        keychain_status = ensure_keychain_placeholder()

    print("famed setup complete")
    print(f"Vault: {vault_root}")
    print(f"Database: {db_path}")
    print(keychain_status)
    return run_dependency_check(vault_root, skip_keychain=args.skip_keychain)


if __name__ == "__main__":
    raise SystemExit(main())
