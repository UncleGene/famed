"""Offline imputation-readiness reporting for imported DNA data."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from famed.dna_sync import DEFAULT_IMPUTED_QUALITY_FLOOR
from famed.report_rendering import markdown_table_cell, render_yaml_front_matter

ReadinessState = Literal[
    "no DNA data",
    "DNA metadata needs review",
    "imputation recommended before PRS",
    "imputed data needs metadata review",
    "ready for PRS input validation",
]


class ImputationReadinessError(ValueError):
    """Raised when imputation-readiness output would violate its safety boundary."""


@dataclass(frozen=True)
class ImputationReadiness:
    state: ReadinessState
    total_variants: int
    direct_variants: int
    imputed_variants: int
    genome_builds: tuple[str, ...]
    missing_genome_build_variants: int
    imputed_with_quality: int
    imputed_missing_quality: int

    @property
    def quality_floor_label(self) -> str:
        return f"{DEFAULT_IMPUTED_QUALITY_FLOOR:g}"


def _genome_builds(
    conn: sqlite3.Connection,
    person_id: str,
) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT DISTINCT genome_build
        FROM genotypes
        WHERE person_id = ? AND genome_build IS NOT NULL AND TRIM(genome_build) != ''
        ORDER BY genome_build
        """,
        (person_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def validate_imputation_person_id(person_id: str) -> None:
    if not person_id or person_id in {".", ".."} or "/" in person_id or "\\" in person_id:
        raise ImputationReadinessError("person_id must be a single vault person identifier")


def _reports_dir(vault_root: Path, person_id: str) -> Path:
    validate_imputation_person_id(person_id)
    records_root = (vault_root / "Records").resolve()
    person_root = vault_root / "Records" / person_id
    if person_root.exists() and not person_root.resolve().is_relative_to(records_root):
        raise ImputationReadinessError("report output must stay under Records/<person_id>/Reports")
    reports_dir = person_root / "Reports"
    if reports_dir.exists() and not reports_dir.resolve().is_relative_to(records_root):
        raise ImputationReadinessError("report output must stay under Records/<person_id>/Reports")
    return reports_dir


def build_imputation_readiness(
    conn: sqlite3.Connection,
    person_id: str,
) -> ImputationReadiness:
    """Return aggregate imputation-readiness state without exposing genotype rows."""
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN COALESCE(source_kind, 'genotyped') = 'genotyped' THEN 1 ELSE 0 END)
                AS direct,
            SUM(CASE WHEN source_kind = 'imputed' THEN 1 ELSE 0 END) AS imputed,
            SUM(CASE WHEN (genome_build IS NULL OR TRIM(genome_build) = '') THEN 1 ELSE 0 END)
                AS missing_build,
            SUM(
                CASE
                    WHEN source_kind = 'imputed'
                         AND (genome_build IS NULL OR TRIM(genome_build) = '')
                    THEN 1
                    ELSE 0
                END
            )
                AS imputed_missing_build,
            SUM(CASE WHEN source_kind = 'imputed' AND quality IS NOT NULL THEN 1 ELSE 0 END)
                AS imputed_with_quality
        FROM genotypes
        WHERE person_id = ?
        """,
        (person_id,),
    ).fetchone()
    total = int(counts[0] or 0)
    direct = int(counts[1] or 0)
    imputed = int(counts[2] or 0)
    missing_build = int(counts[3] or 0)
    imputed_missing_build = int(counts[4] or 0)
    imputed_with_quality = int(counts[5] or 0)
    imputed_missing_quality = imputed - imputed_with_quality

    if total == 0:
        state: ReadinessState = "no DNA data"
    elif imputed == 0 and missing_build > 0:
        state = "DNA metadata needs review"
    elif imputed == 0:
        state = "imputation recommended before PRS"
    elif imputed_missing_build > 0 or imputed_missing_quality > 0:
        state = "imputed data needs metadata review"
    else:
        state = "ready for PRS input validation"

    return ImputationReadiness(
        state=state,
        total_variants=total,
        direct_variants=direct,
        imputed_variants=imputed,
        genome_builds=_genome_builds(conn, person_id),
        missing_genome_build_variants=missing_build,
        imputed_with_quality=imputed_with_quality,
        imputed_missing_quality=imputed_missing_quality,
    )


def _next_step(readiness: ImputationReadiness) -> str:
    if readiness.state == "no DNA data":
        return "Import a DNA file before assessing imputation or PRS readiness."
    if readiness.state == "DNA metadata needs review":
        return (
            "Review genome-build provenance before deciding whether the direct DNA file "
            "is suitable for user-initiated external imputation."
        )
    if readiness.state == "imputation recommended before PRS":
        return (
            "Use a user-initiated external imputation workflow before PRS work; "
            "famed must not upload or transmit DNA automatically."
        )
    if readiness.state == "imputed data needs metadata review":
        return (
            "Review the imported imputed VCF metadata before PRS input validation; "
            "build and quality provenance must be explicit."
        )
    return "Proceed to PRS input validation when a PRS implementation slice is approved."


def render_imputation_readiness_report(
    person_id: str,
    readiness: ImputationReadiness,
    *,
    display_name: str | None = None,
    source_reports: Sequence[str] | None = None,
) -> str:
    """Render an Obsidian report with aggregate DNA imputation-readiness state."""
    name = display_name or person_id
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "imputation_readiness",
        "generated_at": datetime.now(UTC).date().isoformat(),
    }
    if source_reports:
        front_matter["source_reports"] = list(source_reports)
    genome_builds = (
        ", ".join(readiness.genome_builds) if readiness.genome_builds else "not recorded"
    )

    lines: list[str] = [
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n",
        f"# Imputation Readiness — {name}\n",
        f"[[{person_id}|← {name}]]\n",
        "> [!warning]\n"
        "> This report does not upload, export, or score DNA. "
        "External imputation must be initiated by the user outside famed.\n",
        "## Readiness\n",
        f"- State: {readiness.state}.",
        f"- Next step: {_next_step(readiness)}",
        "",
        "## Imported DNA Coverage\n",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Total local variants | {readiness.total_variants:,} |",
        f"| Direct/genotyped variants | {readiness.direct_variants:,} |",
        f"| Imputed variants | {readiness.imputed_variants:,} |",
        f"| Genome builds | {markdown_table_cell(genome_builds)} |",
        f"| Variants missing genome build | {readiness.missing_genome_build_variants:,} |",
        f"| Imputed variants with quality metadata | {readiness.imputed_with_quality:,} |",
        f"| Imputed variants missing quality metadata | {readiness.imputed_missing_quality:,} |",
        "",
        "## PRS Boundary\n",
        "- Directly genotyped calls remain authoritative over imputed rows for the same rsID.",
        f"- Imported imputed rows below the configured local quality floor "
        f"({readiness.quality_floor_label}) are filtered before persistence.",
        "- Consumer DNA and imputed calls are not diagnoses and are not clinical-grade testing.",
    ]
    return "\n".join(lines).rstrip() + "\n"
