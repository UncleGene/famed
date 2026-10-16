#!/usr/bin/env python3
"""Internal helper for doctor-visit packet generation."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from famed.doctor_visit_packet import DoctorVisitPacketError, write_doctor_visit_packet
from famed.person_sync import normalize_person_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic doctor-visit packet from existing vault reports."
    )
    parser.add_argument("--person-id", required=True, help="Person identifier")
    parser.add_argument(
        "--vault-path",
        default="famed-vault",
        help="Path to vault root (default: famed-vault)",
    )
    parser.add_argument(
        "--generated-at",
        help="Optional ISO timestamp override for deterministic testing",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    args = parse_args()
    root = repo_root()
    person_id = normalize_person_id(args.person_id)
    vault_root = Path(args.vault_path)
    if not vault_root.is_absolute():
        vault_root = (root / vault_root).resolve()

    generated_at = None
    if args.generated_at:
        try:
            generated_at = datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
        except ValueError:
            print("Error: --generated-at must be an ISO timestamp", file=sys.stderr)
            return 1
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        else:
            generated_at = generated_at.astimezone(UTC)

    try:
        packet_path = write_doctor_visit_packet(
            vault_root,
            person_id,
            generated_at=generated_at,
        )
    except DoctorVisitPacketError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(packet_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
