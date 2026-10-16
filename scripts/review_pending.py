#!/usr/bin/env python3
"""Refresh and print the deterministic household current-review snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from famed.pending_review import refresh_pending_review_reports
from famed.person_sync import load_person_display_name
from famed.query import list_person_ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    vault_root = root / "famed-vault"
    db_path = root / "data" / "famed.db"
    if not db_path.is_file():
        parser.error(f"database does not exist: {db_path}")
    if not (vault_root / "Persons").is_dir():
        parser.error(f"configured vault Persons directory does not exist: {vault_root / 'Persons'}")
    with sqlite3.connect(db_path) as conn:
        snapshot = refresh_pending_review_reports(
            conn,
            root=root,
            vault_root=vault_root,
        )
    person_ids = list_person_ids(vault_root)
    payload = {
        "people": {
            person_id: {
                "display_name": load_person_display_name(vault_root, person_id),
                "items": [item.as_dict() for item in snapshot.items_for_person(person_id)],
            }
            for person_id in person_ids
        },
        "unresolved": [item.as_dict() for item in snapshot.unresolved_items],
        "unsupported_event_counts": dict(snapshot.unsupported_event_counts),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
