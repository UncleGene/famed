"""Validation-only PRS model/input compatibility reporting."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from famed.imputation_readiness import ImputationReadinessError, validate_imputation_person_id
from famed.lab_sync import write_category_index_note
from famed.person_sync import load_person_display_name
from famed.report_rendering import markdown_table_cell, render_yaml_front_matter

PRS_VALIDATION_FILENAME = "prs-validation.md"

ValidationState = Literal[
    "no DNA data",
    "no build-compatible DNA",
    "model/input needs review",
    "model/input validation complete",
]


class PRSValidationError(ValueError):
    """Raised when a PRS model package or report target is invalid."""


@dataclass(frozen=True)
class PRSModelMetadata:
    score_id: str
    source: str
    release: str
    genome_build: str
    trait: str
    development_population: str
    validation_population: str
    performance_metrics: str
    license: str


@dataclass(frozen=True)
class PRSModelVariant:
    rsid: str
    chromosome: str
    position: int
    effect_allele: str
    other_allele: str
    weight: float


@dataclass(frozen=True)
class PRSModel:
    metadata: PRSModelMetadata
    variants: tuple[PRSModelVariant, ...]


@dataclass(frozen=True)
class PRSValidation:
    state: ValidationState
    model: PRSModelMetadata
    model_variants: int
    local_variants: int
    build_compatible_local_variants: int
    matched_variants: int
    missing_variants: int
    coordinate_mismatches: int
    allele_identity_matches: int
    allele_identity_conflicts: int
    effect_allele_observed: int
    effect_allele_absent: int

    @property
    def coverage_percent(self) -> float:
        if self.model_variants == 0:
            return 0.0
        return self.matched_variants / self.model_variants * 100


@dataclass(frozen=True)
class _LocalGenotype:
    rsid: str
    chromosome: str
    position: int | None
    allele1: str
    allele2: str
    source_kind: str
    quality: float | None
    genome_build: str | None


def _text_field(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PRSValidationError(f"PRS model metadata field is required: {name}")
    return value.strip()


def _allele(value: Any, name: str) -> str:
    text = _text_field(value, name).upper()
    if any(char.isspace() for char in text):
        raise PRSValidationError(f"PRS model allele field must be compact text: {name}")
    return text


def _variant_from_payload(payload: Any, index: int) -> PRSModelVariant:
    if not isinstance(payload, dict):
        raise PRSValidationError(f"PRS model variant {index} must be an object")
    try:
        position = int(payload["position"])
        weight = float(payload["weight"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PRSValidationError(
            f"PRS model variant {index} requires numeric position and weight"
        ) from exc
    if position <= 0:
        raise PRSValidationError(f"PRS model variant {index} position must be positive")
    effect_allele = _allele(payload.get("effect_allele"), f"variants[{index}].effect_allele")
    other_allele = _allele(payload.get("other_allele"), f"variants[{index}].other_allele")
    if effect_allele == other_allele:
        raise PRSValidationError(f"PRS model variant {index} effect and other alleles differ")
    return PRSModelVariant(
        rsid=_text_field(payload.get("rsid"), f"variants[{index}].rsid"),
        chromosome=_text_field(payload.get("chromosome"), f"variants[{index}].chromosome"),
        position=position,
        effect_allele=effect_allele,
        other_allele=other_allele,
        weight=weight,
    )


def load_prs_model(model_path: Path) -> PRSModel:
    """Load and validate the explicit local PRS model package format."""
    if not model_path.is_file():
        raise PRSValidationError(f"PRS model file not found: {model_path}")
    try:
        payload = json.loads(model_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PRSValidationError(f"PRS model file is not valid JSON: {model_path}") from exc
    if not isinstance(payload, dict):
        raise PRSValidationError("PRS model package must be a JSON object")
    metadata_payload = payload.get("metadata")
    variants_payload = payload.get("variants")
    if not isinstance(metadata_payload, dict):
        raise PRSValidationError("PRS model package requires metadata object")
    if not isinstance(variants_payload, list) or not variants_payload:
        raise PRSValidationError("PRS model package requires at least one variant")

    metadata = PRSModelMetadata(
        score_id=_text_field(metadata_payload.get("score_id"), "score_id"),
        source=_text_field(metadata_payload.get("source"), "source"),
        release=_text_field(metadata_payload.get("release"), "release"),
        genome_build=_text_field(metadata_payload.get("genome_build"), "genome_build"),
        trait=_text_field(metadata_payload.get("trait"), "trait"),
        development_population=_text_field(
            metadata_payload.get("development_population"), "development_population"
        ),
        validation_population=_text_field(
            metadata_payload.get("validation_population"), "validation_population"
        ),
        performance_metrics=_text_field(
            metadata_payload.get("performance_metrics"), "performance_metrics"
        ),
        license=_text_field(metadata_payload.get("license"), "license"),
    )
    variants = tuple(
        _variant_from_payload(variant_payload, index)
        for index, variant_payload in enumerate(variants_payload, start=1)
    )
    rsids = [variant.rsid for variant in variants]
    if len(set(rsids)) != len(rsids):
        raise PRSValidationError("PRS model package contains duplicate rsIDs")
    return PRSModel(metadata=metadata, variants=variants)


def validate_prs_person_id(person_id: str) -> None:
    try:
        validate_imputation_person_id(person_id)
    except ImputationReadinessError as exc:
        raise PRSValidationError(str(exc)) from exc


def _reports_dir(vault_root: Path, person_id: str) -> Path:
    validate_prs_person_id(person_id)
    records_root = (vault_root / "Records").resolve()
    person_root = vault_root / "Records" / person_id
    if person_root.exists() and not person_root.resolve().is_relative_to(records_root):
        raise PRSValidationError("report output must stay under Records/<person_id>/Reports")
    reports_dir = person_root / "Reports"
    if reports_dir.exists() and not reports_dir.resolve().is_relative_to(records_root):
        raise PRSValidationError("report output must stay under Records/<person_id>/Reports")
    return reports_dir


def _local_genotype_rows(conn: sqlite3.Connection, person_id: str) -> list[_LocalGenotype]:
    rows = conn.execute(
        """
        SELECT rsid, chromosome, position, allele1, allele2, source_kind, quality,
               genome_build
        FROM genotypes
        WHERE person_id = ?
        """,
        (person_id,),
    ).fetchall()
    return [
        _LocalGenotype(
            rsid=str(row[0]),
            chromosome=str(row[1]),
            position=int(row[2]) if row[2] is not None else None,
            allele1=str(row[3]),
            allele2=str(row[4]),
            source_kind=str(row[5] or "genotyped"),
            quality=float(row[6]) if row[6] is not None else None,
            genome_build=str(row[7]).strip() if row[7] is not None else None,
        )
        for row in rows
    ]


def _select_best_row(rows: list[_LocalGenotype]) -> _LocalGenotype:
    return min(
        rows,
        key=lambda row: (
            0 if row.source_kind == "genotyped" else 1,
            -(row.quality if row.quality is not None else -1.0),
        ),
    )


def build_prs_validation(
    conn: sqlite3.Connection,
    person_id: str,
    model: PRSModel,
) -> PRSValidation:
    """Validate local aggregate PRS input compatibility without calculating a score."""
    validate_prs_person_id(person_id)
    rows = _local_genotype_rows(conn, person_id)
    local_variants = len(rows)
    compatible_rows = [row for row in rows if row.genome_build == model.metadata.genome_build]
    compatible_by_rsid: dict[str, list[_LocalGenotype]] = {}
    for row in compatible_rows:
        compatible_by_rsid.setdefault(row.rsid, []).append(row)

    matched_variants = 0
    coordinate_mismatches = 0
    allele_identity_matches = 0
    allele_identity_conflicts = 0
    effect_allele_observed = 0
    effect_allele_absent = 0
    for variant in model.variants:
        candidate_rows = compatible_by_rsid.get(variant.rsid)
        if not candidate_rows:
            continue
        row = _select_best_row(candidate_rows)
        matched_variants += 1
        if row.chromosome != variant.chromosome or row.position != variant.position:
            coordinate_mismatches += 1
        local_alleles = {row.allele1.upper(), row.allele2.upper()}
        model_alleles = {variant.effect_allele, variant.other_allele}
        if local_alleles.issubset(model_alleles):
            allele_identity_matches += 1
        else:
            allele_identity_conflicts += 1
        if variant.effect_allele in local_alleles:
            effect_allele_observed += 1
        else:
            effect_allele_absent += 1

    missing_variants = len(model.variants) - matched_variants
    if local_variants == 0:
        state: ValidationState = "no DNA data"
    elif not compatible_rows:
        state = "no build-compatible DNA"
    elif coordinate_mismatches or allele_identity_conflicts:
        state = "model/input needs review"
    else:
        state = "model/input validation complete"

    return PRSValidation(
        state=state,
        model=model.metadata,
        model_variants=len(model.variants),
        local_variants=local_variants,
        build_compatible_local_variants=len(compatible_rows),
        matched_variants=matched_variants,
        missing_variants=missing_variants,
        coordinate_mismatches=coordinate_mismatches,
        allele_identity_matches=allele_identity_matches,
        allele_identity_conflicts=allele_identity_conflicts,
        effect_allele_observed=effect_allele_observed,
        effect_allele_absent=effect_allele_absent,
    )


def _next_step(validation: PRSValidation) -> str:
    if validation.state == "no DNA data":
        return "Import DNA data before validating PRS model/input compatibility."
    if validation.state == "no build-compatible DNA":
        return "Use a model matching the imported genome build, or import compatible DNA data."
    if validation.state == "model/input needs review":
        return "Review model coordinates and allele identity before any later scoring decision."
    return "Validation is complete; score rendering still requires a separate source-specific ADR."


def render_prs_validation_report(
    person_id: str,
    validation: PRSValidation,
    *,
    display_name: str | None = None,
) -> str:
    """Render aggregate PRS validation state without exposing genotype rows or weights."""
    name = display_name or person_id
    front_matter: dict[str, Any] = {
        "person_id": person_id,
        "record_type": "report",
        "report_type": "prs_validation",
        "generated_at": datetime.now(UTC).date().isoformat(),
        "model_score_id": validation.model.score_id,
        "model_source": validation.model.source,
        "model_release": validation.model.release,
        "model_genome_build": validation.model.genome_build,
        "source_reports": [f"Records/{person_id}/Reports/imputation-readiness.md"],
    }
    lines: list[str] = [
        f"---\n{render_yaml_front_matter(front_matter)}\n---\n",
        f"# PRS Model/Input Validation — {name}\n",
        f"[[{person_id}|← {name}]]\n",
        "> [!warning]\n"
        "> This report validates aggregate PRS input readiness only. "
        "It does not calculate a score, estimate disease probability, infer ancestry, "
        "or recommend clinical action.\n",
        "## Validation State\n",
        f"- State: {validation.state}.",
        f"- Next step: {_next_step(validation)}",
        "",
        "## Pinned Model\n",
        "| Field | Value |",
        "| --- | --- |",
        f"| Score ID | {markdown_table_cell(validation.model.score_id)} |",
        f"| Source | {markdown_table_cell(validation.model.source)} |",
        f"| Release | {markdown_table_cell(validation.model.release)} |",
        f"| Trait | {markdown_table_cell(validation.model.trait)} |",
        f"| Genome build | {markdown_table_cell(validation.model.genome_build)} |",
        f"| Development population | "
        f"{markdown_table_cell(validation.model.development_population)} |",
        f"| Validation population | "
        f"{markdown_table_cell(validation.model.validation_population)} |",
        f"| Published metrics | {markdown_table_cell(validation.model.performance_metrics)} |",
        f"| License | {markdown_table_cell(validation.model.license)} |",
        "",
        "## Aggregate Input Compatibility\n",
        "| Measure | Count |",
        "| --- | ---: |",
        f"| Model variants | {validation.model_variants:,} |",
        f"| Local DNA variants | {validation.local_variants:,} |",
        f"| Build-compatible local variants | {validation.build_compatible_local_variants:,} |",
        f"| Model variants found locally | {validation.matched_variants:,} |",
        f"| Model variants missing locally | {validation.missing_variants:,} |",
        f"| Coordinate mismatches among matched variants | {validation.coordinate_mismatches:,} |",
        f"| Allele identity matches | {validation.allele_identity_matches:,} |",
        f"| Allele identity conflicts | {validation.allele_identity_conflicts:,} |",
        f"| Effect allele observed locally | {validation.effect_allele_observed:,} |",
        f"| Effect allele absent locally | {validation.effect_allele_absent:,} |",
        "",
        f"Coverage: {validation.coverage_percent:.1f}% of model variants found locally.",
        "",
        "## PRS Boundary\n",
        "- Missing-variant and coverage findings are descriptive unless the model source "
        "supplies thresholds.",
        "- This report does not expose rsIDs, genotype calls, variant weights, source paths, "
        "or bulk DNA rows.",
        "- A later scoring report still requires a source-specific ADR that accepts scoring, "
        "percentile/calibration, confidence-interval, uncertainty, and reporting rules.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_prs_validation_report(
    conn: sqlite3.Connection,
    vault_root: Path,
    person_id: str,
    model_path: Path,
) -> tuple[Path, PRSValidation]:
    """Write Records/<person_id>/Reports/prs-validation.md and refresh the index."""
    model = load_prs_model(model_path)
    validation = build_prs_validation(conn, person_id, model)
    display_name = load_person_display_name(vault_root, person_id)
    person_reports_dir = _reports_dir(vault_root, person_id)
    person_reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = person_reports_dir / PRS_VALIDATION_FILENAME
    report_path.write_text(
        render_prs_validation_report(
            person_id,
            validation,
            display_name=display_name,
        ),
        encoding="utf-8",
    )
    write_category_index_note(
        person_reports_dir,
        person_id,
        "Reports",
        note_label="report",
        display_name=display_name,
    )
    return report_path, validation
