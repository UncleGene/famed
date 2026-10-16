"""One-off migration: split prescription authorization windows out of end_date (ADR-041).

`end_date` was populated from the C-CDA `effectiveTime/high` of every
`substanceAdministration`, regardless of mood code. On an `INT`-mood entry that
boundary is when the order stops being dispensable — when refills run out — not when
the therapy stopped. Downstream that made an expired refill window look like a
completed course.

This migration is keyed on provenance, never on the shape or lapsed-ness of a date: it
re-reads each originating document and moves `end_date` to `authorized_through` only for
rows whose value demonstrably came from an `INT`-mood high boundary. User-authored end
dates (`user_update`, `manual`, `user_correction`) state a real therapy stop and are
never touched.

Only rows the source still calls `active` are migrated. Where a row's status is
`aborted` or `completed` the source itself asserts the therapy ended, so its end_date
legitimately closes that dose period — a re-prescription ladder chains through exactly
those dates — and rewriting it would destroy real dose history.

Run with --apply to write; the default is a dry run.
"""

from __future__ import annotations

import argparse
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from famed.history_sync import _HL7_NS, _ccda_date, _ccda_document_local_offsets

# Provenance that means "a person said they stopped taking this" — never an order window.
_USER_AUTHORED_SOURCE_TYPES = frozenset({"user_update", "manual", "user_correction"})


def _document_bytes(source_path: str, repo_root: Path) -> bytes | None:
    """Return the raw XML for a `<archive>.zip#<inner path>` or plain-file source."""
    if "#" in source_path:
        archive_text, inner = source_path.split("#", 1)
        archive = repo_root / archive_text
        if not archive.is_file():
            return None
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if name == inner or name.endswith(inner.split("/")[-1]):
                    return bundle.read(name)
        return None
    candidate = repo_root / source_path
    return candidate.read_bytes() if candidate.is_file() else None


def _int_mood_high_dates(document: bytes) -> dict[str, tuple[str, str]]:
    """Map medication display text -> (local date, raw truncated date) for INT-mood highs.

    Both forms are returned so provenance can be proven against a row written before the
    timezone fix (raw) or after it (local), while the value written is always the local one.
    """
    root = ET.fromstring(document)
    local_offsets = _ccda_document_local_offsets(root)
    narrative = {
        element.get("ID"): "".join(element.itertext()).strip()
        for element in root.iter()
        if element.get("ID")
    }
    highs: dict[str, tuple[str, str]] = {}
    for administration in root.iter(f"{{{_HL7_NS['hl7']}}}substanceAdministration"):
        if administration.get("moodCode") != "INT":
            continue
        high = administration.find("./hl7:effectiveTime/hl7:high", _HL7_NS)
        if high is None or not high.get("value"):
            continue
        local_date = _ccda_date(high.get("value"), local_offsets=local_offsets)
        raw_date = _ccda_date(high.get("value"))
        if not local_date or not raw_date:
            continue
        for reference in administration.iter(f"{{{_HL7_NS['hl7']}}}reference"):
            target = (reference.get("value") or "").lstrip("#")
            text = narrative.get(target)
            if text:
                highs[" ".join(text.split()).lower()] = (local_date, raw_date)
    return highs


def _name_matches(medication_name: str, narrative_text: str) -> bool:
    key = " ".join(medication_name.split()).lower()
    return key in narrative_text or narrative_text.startswith(key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/famed.db")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    conn = sqlite3.connect(repo_root / args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT id, person_id, medication_name, end_date, authorized_through,
               source_type, source_path
        FROM medications
        WHERE (end_date IS NOT NULL OR authorized_through IS NOT NULL)
          AND (status = 'active' OR status IS NULL OR TRIM(status) = '')
        """
    ).fetchall()

    migrated: list[tuple[str, str, str]] = []
    preserved: list[tuple[str, str, str]] = []
    document_cache: dict[str, dict[str, tuple[str, str]]] = {}

    for row in rows:
        stored = row["end_date"] or row["authorized_through"]
        source_type = (row["source_type"] or "").strip().lower()
        if source_type in _USER_AUTHORED_SOURCE_TYPES:
            preserved.append((row["medication_name"], stored, f"{source_type}: real end"))
            continue
        source_path = row["source_path"] or ""
        if source_path not in document_cache:
            document = _document_bytes(source_path, repo_root)
            document_cache[source_path] = _int_mood_high_dates(document) if document else {}
        highs = document_cache[source_path]
        match = next(
            (dates for text, dates in highs.items() if _name_matches(row["medication_name"], text)),
            None,
        )
        # Provenance is proven by the stored value matching this entry's high boundary in
        # either representation — raw truncation for rows written before the timezone fix,
        # local for rows written after it. The value written is always the local date.
        if match is not None and stored in match:
            local_date = match[0]
            note = "INT-mood high" if stored == local_date else f"INT-mood high (was {stored})"
            migrated.append((row["medication_name"], local_date, note))
            if args.apply:
                conn.execute(
                    "UPDATE medications SET end_date = NULL, authorized_through = ? WHERE id = ?",
                    (local_date, row["id"]),
                )
        else:
            preserved.append((row["medication_name"], stored, "no INT-mood high in source"))

    if args.apply:
        conn.commit()

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode} — {len(rows)} rows carrying an authorization or end date\n")
    print(f"migrated to authorized_through ({len(migrated)}):")
    for name, date, why in migrated:
        print(f"  {name[:46]:<46} {date}  [{why}]")
    print(f"\npreserved as therapy end ({len(preserved)}):")
    for name, date, why in preserved:
        print(f"  {name[:46]:<46} {date}  [{why}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
