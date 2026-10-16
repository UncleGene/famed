#!/usr/bin/env python3
"""Internal entry point for an isolated, identity-first cold rebuild."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _absolute(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the person and family-graph layer into isolated targets."
    )
    parser.add_argument("--source-vault", required=True)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-vault", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument(
        "--replay-medical",
        action="store_true",
        help="After identity validation, copy and replay supported retained medical sources.",
    )
    parser.add_argument(
        "--resume-medical",
        action="store_true",
        help="Validate an existing target identity layer and rerun retained medical replay.",
    )
    parser.add_argument(
        "--replay-authority",
        action="store_true",
        help="After medical replay, apply dated user updates and active corrections.",
    )
    parser.add_argument(
        "--replay-dna",
        action="store_true",
        help="After authority replay, import retained Genes sources and refresh dependencies.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help=(
            "After DNA replay, refresh the isolated household and audit it against working stores."
        ),
    )
    parser.add_argument(
        "--audit-output",
        help="Write the rebuild-diff-v1 JSON audit here (defaults beside the target DB).",
    )
    parser.add_argument(
        "--source-findings-ledger",
        help="Working findings ledger to inventory as an intentionally excluded redrive input.",
    )
    parser.add_argument("--source-processed")
    parser.add_argument("--target-processed")
    parser.add_argument(
        "--retained-decisions",
        "--history-decisions",
        dest="retained_decisions",
        help=(
            "Apply a source-bound retained semantic decision bundle during replay "
            "(v1 History-only, v2 History+Labs, v3 retained-Medications decisions, "
            "v4 visit-summary medication decisions, and v5 exact C-CDA lab "
            "observation resolutions supported)."
        ),
    )
    args = parser.parse_args()
    if args.replay_medical and args.resume_medical:
        parser.error("--replay-medical and --resume-medical are mutually exclusive")
    medical_replay = args.replay_medical or args.resume_medical
    if args.replay_authority and not medical_replay:
        parser.error("--replay-authority requires --replay-medical or --resume-medical")
    if args.replay_dna and not args.replay_authority:
        parser.error("--replay-dna requires --replay-authority")
    if args.compare and not args.replay_dna:
        parser.error("--compare requires --replay-dna")
    if medical_replay and (not args.source_processed or not args.target_processed):
        parser.error("medical replay requires --source-processed and --target-processed")
    if args.retained_decisions and not medical_replay:
        parser.error("--retained-decisions requires --replay-medical or --resume-medical")

    root = repo_root()
    sys.path.insert(0, str(root / "src"))
    from famed.authority_replay import (
        authority_update_snapshot_paths,
        prepare_authority_update_snapshots,
        replay_authority_overlays,
    )
    from famed.dna_replay import replay_retained_dna_sources
    from famed.dna_sync import DnaSyncError
    from famed.person_sync import PersonSyncError
    from famed.rebuild_compare import compare_rebuild, refresh_isolated_household
    from famed.rebuild_vault import (
        RebuildError,
        rebuild_identity_layer,
        reset_existing_target_to_identity,
        validate_existing_identity_layer,
    )
    from famed.reprocess_sources import ReprocessError
    from famed.retained_replay import (
        copy_retained_sources,
        load_retained_history_decisions,
        replay_retained_medical_sources,
        validate_replay_storage_paths,
        validate_retained_source_copy,
    )

    source_vault = _absolute(root, args.source_vault)
    source_db = _absolute(root, args.source_db)
    target_vault = _absolute(root, args.target_vault)
    target_db = _absolute(root, args.target_db)
    source_processed = (
        _absolute(root, args.source_processed) if args.source_processed is not None else None
    )
    target_processed = (
        _absolute(root, args.target_processed) if args.target_processed is not None else None
    )
    source_findings_ledger = (
        _absolute(root, args.source_findings_ledger)
        if args.source_findings_ledger is not None
        else root / "ledger" / "findings.jsonl"
    )
    audit_output = (
        _absolute(root, args.audit_output)
        if args.audit_output is not None
        else target_db.with_suffix(".rebuild-audit.json")
    )
    retained_decisions = (
        _absolute(root, args.retained_decisions) if args.retained_decisions is not None else None
    )
    allowed_target_files: set[Path] = set()

    try:
        if args.replay_authority:
            allowed_target_files = authority_update_snapshot_paths(
                source_vault=source_vault,
                source_db=source_db,
            )
        if medical_replay:
            assert source_processed is not None
            assert target_processed is not None
            validate_replay_storage_paths(
                source_vault=source_vault,
                source_db=source_db,
                source_processed=source_processed,
                target_vault=target_vault,
                target_db=target_db,
                target_processed=target_processed,
            )
            validate_retained_source_copy(
                source_root=source_processed,
                target_root=target_processed,
                allowed_target_files=allowed_target_files,
            )
            if retained_decisions is not None:
                load_retained_history_decisions(
                    decision_file=retained_decisions,
                    processed_root=source_processed,
                )
        if args.resume_medical:
            validate_existing_identity_layer(
                source_vault=source_vault,
                source_db=source_db,
                target_vault=target_vault,
                target_db=target_db,
            )
            reset_existing_target_to_identity(
                target_vault=target_vault,
                target_db=target_db,
                template_root=root / "vault-template",
                person_ids={
                    path.stem
                    for path in (target_vault / "Persons").glob("*.md")
                    if not path.name.startswith("_")
                },
            )
        result = rebuild_identity_layer(
            source_vault=source_vault,
            source_db=source_db,
            target_vault=target_vault,
            target_db=target_db,
            template_root=root / "vault-template",
        )
    except (OSError, sqlite3.Error, PersonSyncError, RebuildError, ReprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        ("Identity target validated: " if args.resume_medical else "Identity rebuild complete: ")
        + f"{result.person_count} people, {result.family_link_count} family links."
    )
    print("Target foreign-key validation passed. Medical replay has not started.")
    if not medical_replay:
        return 0

    assert source_processed is not None
    assert target_processed is not None

    try:
        copied_sources = copy_retained_sources(
            source_root=source_processed,
            target_root=target_processed,
            allowed_target_files=allowed_target_files,
        )
        prepared_update_sources = (
            prepare_authority_update_snapshots(
                source_vault=source_vault,
                source_db=source_db,
                processed_root=target_processed,
                source_processed_root=source_processed,
            )
            if args.replay_authority
            else 0
        )
        history_fact_interpreter = (
            load_retained_history_decisions(
                decision_file=retained_decisions,
                processed_root=target_processed,
            )
            if retained_decisions is not None
            else None
        )
        with sqlite3.connect(target_db) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            summary = replay_retained_medical_sources(
                root=root,
                vault_root=target_vault,
                processed_root=target_processed,
                conn=conn,
                history_fact_interpreter=history_fact_interpreter,
            )
            foreign_key_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_issues:
            raise RebuildError(f"target foreign-key validation failed: {foreign_key_issues}")
    except (OSError, sqlite3.Error, PersonSyncError, RebuildError, ReprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        "Retained medical replay complete: "
        f"{len(copied_sources)} copied; "
        f"{summary.count('imported')} imported, "
        f"{summary.count('redundant')} redundant, "
        f"{summary.count('zero_fact')} zero-fact, "
        f"{summary.count('unsupported')} unsupported, "
        f"{summary.count('failed')} failed; "
        f"{summary.imported_rows} rows imported."
    )
    for item in summary.items:
        if item.outcome in {"failed", "unsupported", "zero_fact"}:
            detail = f": {item.message}" if item.message else ""
            print(f"[{item.outcome}] {item.source_path}{detail}")
    if args.replay_authority:
        try:
            with sqlite3.connect(target_db) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                authority_summary = replay_authority_overlays(
                    root=root,
                    source_vault=source_vault,
                    source_db=source_db,
                    target_vault=target_vault,
                    processed_root=target_processed,
                    source_processed_root=source_processed,
                    conn=conn,
                    prepared_update_sources=prepared_update_sources,
                )
                foreign_key_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_issues:
                raise RebuildError(
                    f"target foreign-key validation failed after authority replay: "
                    f"{foreign_key_issues}"
                )
        except (OSError, sqlite3.Error, PersonSyncError, RebuildError, ReprocessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(
            "Authority overlay replay complete: "
            f"{authority_summary.update_sources} update sources, "
            f"{authority_summary.recovered_update_sources} recovered from vault authority, "
            f"{authority_summary.update_rows} update rows, "
            f"{authority_summary.correction_rows} active corrections, "
            f"{len(authority_summary.affected_person_ids)} people affected."
        )
        if args.replay_dna:
            try:
                with sqlite3.connect(target_db) as conn:
                    conn.execute("PRAGMA foreign_keys = ON")
                    dna_summary = replay_retained_dna_sources(
                        root=root,
                        vault_root=target_vault,
                        processed_root=target_processed,
                        conn=conn,
                    )
                    foreign_key_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_key_issues:
                    raise RebuildError(
                        "target foreign-key validation failed after DNA replay: "
                        f"{foreign_key_issues}"
                    )
            except (
                DnaSyncError,
                OSError,
                sqlite3.Error,
                PersonSyncError,
                RebuildError,
                ReprocessError,
            ) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(
                "DNA replay complete: "
                f"{dna_summary.source_files} retained sources, "
                f"{dna_summary.import_rows} imports, "
                f"{dna_summary.genotype_rows} genotypes, "
                f"{len(dna_summary.affected_person_ids)} people affected."
            )
            if args.compare:
                try:
                    with sqlite3.connect(target_db) as conn:
                        conn.execute("PRAGMA foreign_keys = ON")
                        refreshed = refresh_isolated_household(
                            root=root,
                            target_vault=target_vault,
                            conn=conn,
                        )
                    audit = compare_rebuild(
                        source_vault=source_vault,
                        source_db=source_db,
                        source_processed=source_processed,
                        target_vault=target_vault,
                        target_db=target_db,
                        target_processed=target_processed,
                        output_path=audit_output,
                        source_findings_ledger=source_findings_ledger,
                        allowed_target_files=allowed_target_files,
                        replay_outcomes=(
                            (item.outcome, str(item.source_path), item.message or "")
                            for item in summary.items
                        ),
                    )
                except (
                    DnaSyncError,
                    OSError,
                    sqlite3.Error,
                    PersonSyncError,
                    RebuildError,
                    ReprocessError,
                ) as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 1
                audit_summary = audit["summary"]
                print(
                    "Differential rebuild audit complete: "
                    f"{len(refreshed)} people refreshed; "
                    f"{audit_summary['candidate_bug_count']} candidate-bug, "
                    f"{audit_summary['needs_review_count']} needs-review, "
                    f"{audit_summary['contract_explained_count']} contract-explained "
                    f"differences. Audit: {audit_output}"
                )
            else:
                print("Final household derived-report regeneration remains pending.")
        else:
            print("DNA replay and final derived-report regeneration remain pending.")
    else:
        print(
            "Authority overlays, DNA replay, and final derived-report regeneration remain pending."
        )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
