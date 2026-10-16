"""One Genetics report per person with DNA (ADR-031 section 3).

Replaces the DNA profile, PGx coverage, PGx wallet card, and carrier-screening
notes. The PGx table is gene-centric so it serves any future prescriber, not only
today's medications; carrier screening appears only when a carrier is found.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from famed.carrier_screening import CarrierGeneResult, build_carrier_screening_results
from famed.dna_sync import provider_label
from famed.findings_ledger import mark_orphaned_findings_stale
from famed.lab_sync import write_category_index_note
from famed.person_sync import load_person_display_name
from famed.pgx_coverage import (
    GeneResult,
    build_gene_results,
    build_pgx_coverage_rows,
    record_pgx_coverage_findings,
)
from famed.report_rendering import markdown_table_cell, render_yaml_front_matter

GENETICS_FILENAME = "genetics.md"


@dataclass(frozen=True)
class DnaImport:
    provider: str
    variant_count: int
    chip_version: str | None
    imported_on: str


def _load_imports(conn: sqlite3.Connection, person_id: str) -> list[DnaImport]:
    rows = conn.execute(
        "SELECT provider, variant_count, chip_version, imported_at "
        "FROM dna_imports WHERE person_id = ? ORDER BY imported_at DESC",
        (person_id,),
    ).fetchall()
    return [
        DnaImport(
            provider_label(str(provider)),
            int(variant_count or 0),
            chip_version,
            str(imported_at or "")[:10],
        )
        for provider, variant_count, chip_version, imported_at in rows
    ]


def _medications_by_gene(rows: list[Any]) -> dict[str, list[str]]:
    by_gene: dict[str, list[str]] = {}
    for row in rows:
        if row.gene and row.medication_name not in by_gene.setdefault(row.gene, []):
            by_gene[row.gene].append(row.medication_name)
    return by_gene


def render_genetics_report(
    *,
    person_id: str,
    display_name: str,
    imports: list[DnaImport],
    gene_results: dict[str, GeneResult],
    medications_by_gene: dict[str, list[str]],
    carriers: list[CarrierGeneResult],
    generated_on: str | None = None,
) -> str:
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "genetics",
        "generated_at": generated_on or datetime.now(UTC).date().isoformat(),
    }
    lines = [
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n",
        f"# Genetics — {display_name}\n",
        f"[[{person_id}|← {display_name}]]\n",
        "> [!warning] Consumer genotyping\n"
        "> These results come from a consumer DNA chip, not clinical-grade testing. "
        "Confirm any result before acting on it.\n",
        "## DNA Source\n",
        "| Provider | Variants | Chip | Imported |",
        "| --- | --- | --- | --- |",
    ]
    for item in imports:
        lines.append(
            f"| {markdown_table_cell(item.provider)} | {item.variant_count:,} "
            f"| {markdown_table_cell(item.chip_version or '—')} | {item.imported_on} |"
        )

    interpreted = sorted(
        (result for result in gene_results.values() if result.interpreted),
        key=lambda result: result.gene,
    )
    uninterpreted = sorted(r.gene for r in gene_results.values() if not r.interpreted)
    lines.extend(
        [
            "",
            "## Medication Genes (wallet card)\n",
            "Show this to a prescriber or pharmacist before starting a new medication.\n",
        ]
    )
    if interpreted:
        lines.extend(["| Gene | Result | Affects current medications |", "| --- | --- | --- |"])
        for result in interpreted:
            medications = ", ".join(medications_by_gene.get(result.gene, [])) or "—"
            lines.append(
                f"| {result.gene} | {markdown_table_cell(result.interpretation_state)} "
                f"| {markdown_table_cell(medications)} |"
            )
    else:
        lines.append("_No medication gene could be interpreted from this chip._")
    if uninterpreted:
        lines.append(
            f"\n_Genes measured only partially and not interpreted from this chip: "
            f"{', '.join(uninterpreted)}._"
        )

    lines.extend(["", "## Carrier Screening\n"])
    found = [result for result in carriers if result.state == "carrier"]
    if found:
        lines.extend(["| Gene | Condition | State |", "| --- | --- | --- |"])
        for result in found:
            lines.append(
                f"| {result.gene} | {markdown_table_cell(result.condition)} | {result.state} |"
            )
        lines.append("\n_Carrier status matters mainly for family planning; confirm clinically._")
    else:
        lines.append(
            "_No carrier variant observed in the screened genes. A consumer chip cannot "
            "rule carrier status out._"
        )

    lines.extend(
        [
            "",
            "## Genetic Risk Flags\n",
            f"See [[Records/{person_id}/Reports/health-advisory#Genetic Risk Flags"
            "|Health advisory: genetic risk flags]].",
            "",
        ]
    )
    return "\n".join(lines)


def write_genetics_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    ref_db_path: Path,
    *,
    findings_ledger_path: Path | None = None,
) -> Path | None:
    """Write genetics.md and its PGx ledger findings, or remove both when there is no DNA.

    The report owns the PGx findings the retired coverage and wallet-card notes
    used to record; finding identities do not depend on the cited report, so
    existing findings carry over (ADR-025 section 3, ADR-031).
    """
    reports_dir = vault_root / "Records" / person_id / "Reports"
    report_path = reports_dir / GENETICS_FILENAME
    relative_path = f"Records/{person_id}/Reports/{GENETICS_FILENAME}"
    imports = _load_imports(conn, person_id)
    if not imports:
        report_path.unlink(missing_ok=True)
        if findings_ledger_path is not None:
            mark_orphaned_findings_stale(
                findings_ledger_path,
                person_id=person_id,
                domain="pgx_coverage",
                current_finding_ids=set(),
                reason="PGx coverage is unavailable because no genotype data remains",
            )
        return None
    coverage_rows = build_pgx_coverage_rows(conn, ref_db_path, person_id)
    display_name = load_person_display_name(vault_root, person_id)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_genetics_report(
            person_id=person_id,
            display_name=display_name,
            imports=imports,
            gene_results=build_gene_results(conn, ref_db_path, person_id),
            medications_by_gene=_medications_by_gene(coverage_rows),
            carriers=build_carrier_screening_results(conn, ref_db_path, person_id),
        ),
        encoding="utf-8",
    )
    write_category_index_note(
        reports_dir, person_id, "Reports", note_label="report", display_name=display_name
    )
    if findings_ledger_path is not None:
        record_pgx_coverage_findings(
            findings_ledger_path,
            person_id=person_id,
            rows=coverage_rows,
            source_reports=[relative_path],
            report_relative_path=relative_path,
            reference_db_path=ref_db_path,
        )
    return report_path
