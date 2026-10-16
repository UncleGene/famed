from __future__ import annotations

from pathlib import Path

import pytest

from famed.reprocess_sources import ReprocessError, ReprocessRegistry, ReprocessResult


def test_reprocess_registry_routes_person_scoped_category_source() -> None:
    processed_root = Path("/repo/data/processed")
    source_path = processed_root / "dummy_alice" / "Labs" / "2023-04-20-panel.csv"
    seen: list[Path] = []
    registry = ReprocessRegistry()
    registry.register(
        name="labs",
        category_parts=("Labs",),
        suffixes={"csv"},
        handler=lambda source_path: (
            seen.append(source_path)
            or ReprocessResult(imported_rows=2, affected_person_ids={"dummy_alice"})
        ),
    )

    result = registry.reprocess(source_path, processed_root)

    assert seen == [source_path]
    assert result == ReprocessResult(imported_rows=2, affected_person_ids={"dummy_alice"})


def test_reprocess_registry_rejects_unregistered_category() -> None:
    processed_root = Path("/repo/data/processed")
    source_path = processed_root / "dummy_alice" / "Medications" / "list.csv"
    registry = ReprocessRegistry()
    registry.register(
        name="labs",
        category_parts=("Labs",),
        suffixes={"csv"},
        handler=lambda source_path: ReprocessResult(imported_rows=1, affected_person_ids=set()),
    )

    with pytest.raises(ReprocessError, match="no registered reprocess handler"):
        registry.reprocess(source_path, processed_root)


def test_reprocess_registry_matches_extension_less_path() -> None:
    processed_root = Path("/repo/data/processed")
    source_path = processed_root / "dummy_alice" / "Medications" / "undated-dummy_alice-supplements"
    seen: list[Path] = []
    registry = ReprocessRegistry()
    registry.register(
        name="supplement_list",
        category_parts=("Medications",),
        suffixes={""},
        handler=lambda source_path: (
            seen.append(source_path)
            or ReprocessResult(imported_rows=3, affected_person_ids={"dummy_alice"})
        ),
    )

    result = registry.reprocess(source_path, processed_root)

    assert seen == [source_path]
    assert result == ReprocessResult(imported_rows=3, affected_person_ids={"dummy_alice"})


def test_reprocess_registry_rejects_ambiguous_handlers() -> None:
    processed_root = Path("/repo/data/processed")
    source_path = processed_root / "dummy_alice" / "Labs" / "panel.csv"
    registry = ReprocessRegistry()
    for name in ("labs-a", "labs-b"):
        registry.register(
            name=name,
            category_parts=("Labs",),
            suffixes={"csv"},
            handler=lambda source_path: ReprocessResult(imported_rows=1, affected_person_ids=set()),
        )

    with pytest.raises(ReprocessError, match="matched multiple handlers"):
        registry.reprocess(source_path, processed_root)


def test_reprocess_registry_path_predicate_separates_same_category_and_suffix() -> None:
    processed_root = Path("/repo/data/processed")
    study = processed_root / "dummy_alice" / "Labs" / "2099-01-01-ECG 12-LEAD.pdf"
    panel = processed_root / "dummy_alice" / "Labs" / "2099-01-01-panel.pdf"
    registry = ReprocessRegistry()
    for name, predicate in (
        ("clinical_document", lambda path: path.name.endswith("ECG 12-LEAD.pdf")),
        ("labs", lambda path: not path.name.endswith("ECG 12-LEAD.pdf")),
    ):
        registry.register(
            name=name,
            category_parts=("Labs",),
            suffixes={"pdf"},
            path_predicate=predicate,
            handler=lambda source_path: ReprocessResult(1, {"dummy_alice"}),
        )

    assert registry.handler_for(study, processed_root).name == "clinical_document"
    assert registry.handler_for(panel, processed_root).name == "labs"
