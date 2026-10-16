"""Cross-report consistency gate for derived PGx outputs (ADR-025 decision #4).

A rendered genetics.md can go stale relative to canonical computed state when
the underlying genotype DB or reference cache changes without triggering a report
regeneration. This module compares the gene results rendered in the report with
freshly recomputed calls and raises ReportConsistencyError when they disagree.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from famed.pgx_coverage import build_gene_results
from famed.report_rendering import split_front_matter


@dataclass(frozen=True)
class PhenotypeContradiction:
    """A single disagreement between a rendered report and canonical computed state."""

    person_id: str
    medication_name: str
    gene: str
    rendered_state: str
    canonical_state: str

    def __str__(self) -> str:
        return (
            f"{self.person_id}/{self.medication_name}/{self.gene}: "
            f"rendered={self.rendered_state!r} canonical={self.canonical_state!r}"
        )


class ReportConsistencyError(RuntimeError):
    """Raised when a rendered report's phenotype assertions contradict canonical state."""


def find_genetics_contradictions(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    ref_db_path: Path,
) -> list[PhenotypeContradiction]:
    """Compare genetics.md gene results against freshly computed calls (ADR-031)."""
    report_path = vault_root / "Records" / person_id / "Reports" / "genetics.md"
    if not report_path.is_file():
        return []
    _, body = split_front_matter(report_path.read_text(encoding="utf-8"))
    rendered: dict[str, str] = {}
    for line in body.splitlines():
        cells = [cell.strip() for cell in line.strip().split("|")[1:-1]]
        if len(cells) == 3 and cells[0] not in ("Gene", "") and not set(cells[0]) <= {"-", ":"}:
            rendered[cells[0]] = cells[1]
    canonical = build_gene_results(conn, ref_db_path, person_id)
    return [
        PhenotypeContradiction(
            person_id=person_id,
            medication_name="(gene card)",
            gene=gene,
            rendered_state=state,
            canonical_state=canonical[gene].interpretation_state,
        )
        for gene, state in rendered.items()
        if gene in canonical and canonical[gene].interpretation_state != state
    ]


def assert_report_consistency(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    ref_db_path: Path,
) -> None:
    """Raise ReportConsistencyError if genetics.md contradicts canonical gene results."""
    contradictions = find_genetics_contradictions(conn, vault_root, person_id, ref_db_path)
    if not contradictions:
        return
    details = "; ".join(str(c) for c in contradictions)
    raise ReportConsistencyError(
        f"{len(contradictions)} phenotype contradiction(s) after refresh: {details}"
    )
