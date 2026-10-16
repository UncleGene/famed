"""Tests for the reference data cache manager."""

from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from famed.annotation_cache import open_annotation_db
from famed.reference_cache import (
    STATUS_MISSING,
    STATUS_OK,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    SUPPORTED_DATASETS,
    _dataset_has_files,
    _strip_rxnorm_dose_form,
    cache_status,
    check_stale,
    download_dataset,
    download_rxnorm,
    load_loinc_index,
    load_rxnorm_index,
    lookup_loinc_code,
    lookup_rxnorm_cui,
    main,
    parse_loinc_csv,
    read_manifest,
    refresh_cache,
    write_manifest,
)


@pytest.mark.parametrize(
    ("test_name", "unit", "expected_code"),
    [
        ("Immature Granulocytes", "%", "71695-1"),
        ("% Immature Granulocytes", "%", "71695-1"),
        ("Immature Granulocytes", "K/uL", "53115-2"),
        ("Immature Granulocytes, Absolute", "10*3/uL", "53115-2"),
        ("NRBC Auto", "%", "58413-6"),
        ("nRBC(auto)%", "/100 WBC", "58413-6"),
        ("nRBC per 100 Cells", "%", "19048-8"),
        ("NRBC% Auto", "K/uL", "771-6"),
        ("nRBC#", "10*3/uL", "771-6"),
        ("RDW", "%", "788-0"),
        ("RDW-CV", "%", "788-0"),
        ("RDW", "fL", "21000-5"),
        ("RDW-SD", "fL", "21000-5"),
    ],
)
def test_lookup_loinc_disambiguates_unit_sensitive_cbc_identities(
    test_name: str,
    unit: str,
    expected_code: str,
) -> None:
    assert lookup_loinc_code(test_name, {"rdw": "21000-5"}, unit) == expected_code


@pytest.mark.parametrize(
    ("test_name", "unit", "wrong_code", "expected_code"),
    [
        ("Free T4", "ng/dL", "14920-3", "3024-7"),
        ("HDL-C", "mg/dL", "12772-0", "2085-9"),
        ("LDL Calculated", "mg/dL", "108018-3", "13457-7"),
        ("Triglycerides", "mg/dL", "12951-0", "2571-8"),
        ("% Free PSA", "%", "10886-0", "12841-3"),
        ("Hepatitis C Ab", "s/co ratio", "13955-0", "104741-4"),
        ("TSH", "uIU/mL", "11580-8", "3016-3"),
    ],
)
def test_lookup_loinc_prefers_unit_compatible_method_neutral_result(
    test_name: str,
    unit: str,
    wrong_code: str,
    expected_code: str,
) -> None:
    normalized_name = " ".join(
        part for part in test_name.casefold().replace("%", "").replace("-", " ").split()
    )
    assert lookup_loinc_code(test_name, {normalized_name: wrong_code}, unit) == expected_code


class TestManifest:
    def test_read_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            manifest = read_manifest(cache_dir)
            assert manifest == {"datasets": {}}

    def test_write_and_read_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            manifest = {
                "datasets": {
                    "rxnorm": {
                        "version": "20220106",
                        "downloaded_at": "2022-01-06T12:00:00Z",
                    }
                }
            }
            write_manifest(cache_dir, manifest)
            result = read_manifest(cache_dir)
            assert result == manifest

    def test_write_manifest_creates_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "nested" / "path"
            manifest = {"datasets": {}}
            write_manifest(cache_dir, manifest)
            assert (cache_dir / "manifest.json").exists()

    def test_manifest_preserves_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            manifest = {"datasets": {"loinc": {"display_name": "L\u00d6INC"}}}
            write_manifest(cache_dir, manifest)
            result = read_manifest(cache_dir)
            assert result == manifest


class TestReferenceCacheCli:
    def test_help_lists_all_supported_dataset_families(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.argv", ["cache.py", "--help"])

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 0
        output = capsys.readouterr().out
        normalized = " ".join(output.split())
        for config in SUPPORTED_DATASETS.values():
            assert config["display_name"] in normalized
        for dataset_id in SUPPORTED_DATASETS:
            assert dataset_id in output

    def test_status_prints_last_failed_refresh_detail(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("sys.argv", ["cache.py", "status"])
        monkeypatch.setattr("famed.reference_cache._find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            "famed.reference_cache.cache_status",
            lambda _cache_dir: {
                "gnomad": {
                    "status": STATUS_UNAVAILABLE,
                    "version": None,
                    "display_name": "gnomAD",
                    "last_attempted_at": "2023-08-05T00:00:00+00:00",
                    "last_attempt_status": "failed",
                    "last_error": "release drift",
                }
            },
        )

        assert main() == 0
        output = capsys.readouterr().out
        assert "UNAVAILABLE gnomad" in output
        assert "last refresh failed at 2023-08-05T00:00:00+00:00: release drift" in output

    @pytest.mark.parametrize("selected_dataset", ["rxnorm", "gnomad"])
    def test_refresh_triggers_household_derived_report_refresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        selected_dataset: str,
    ) -> None:
        root = tmp_path
        cache_dir = root / "data" / "reference"
        calls: list[tuple[Path, Path]] = []

        monkeypatch.setattr("sys.argv", ["cache.py", "--dataset", selected_dataset, "refresh"])
        monkeypatch.setattr("famed.reference_cache._find_repo_root", lambda: root)
        monkeypatch.setattr("famed.reference_cache._cache_root", lambda repo_root: cache_dir)
        monkeypatch.setattr(
            "famed.reference_cache.refresh_cache",
            lambda passed_cache, dataset_id=None: {selected_dataset: STATUS_OK},
        )

        def refresh_hook(passed_root: Path, passed_cache: Path) -> int:
            calls.append((passed_root, passed_cache))
            return 2

        monkeypatch.setattr(
            "famed.reference_cache._refresh_derived_outputs_after_cache_change",
            refresh_hook,
        )

        assert main() == 0

        assert calls == [(root, cache_dir)]
        assert "derived reports refreshed for 2 person(s)" in capsys.readouterr().out

    def test_download_triggers_household_derived_report_refresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path
        cache_dir = root / "data" / "reference"
        calls: list[tuple[Path, Path]] = []

        monkeypatch.setattr("sys.argv", ["cache.py", "--dataset", "rxnorm", "download"])
        monkeypatch.setattr("famed.reference_cache._find_repo_root", lambda: root)
        monkeypatch.setattr("famed.reference_cache._cache_root", lambda repo_root: cache_dir)
        monkeypatch.setattr("famed.reference_cache.download_dataset", lambda *_args: True)

        def refresh_hook(passed_root: Path, passed_cache: Path) -> int:
            calls.append((passed_root, passed_cache))
            return 1

        monkeypatch.setattr(
            "famed.reference_cache._refresh_derived_outputs_after_cache_change",
            refresh_hook,
        )

        assert main() == 0

        assert calls == [(root, cache_dir)]
        output = capsys.readouterr().out
        assert "rxnorm: downloaded" in output
        assert "derived reports refreshed for 1 person(s)" in output

    def test_failed_refresh_does_not_trigger_household_derived_report_refresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        root = tmp_path
        cache_dir = root / "data" / "reference"

        monkeypatch.setattr("sys.argv", ["cache.py", "--dataset", "rxnorm", "refresh"])
        monkeypatch.setattr("famed.reference_cache._find_repo_root", lambda: root)
        monkeypatch.setattr("famed.reference_cache._cache_root", lambda repo_root: cache_dir)
        monkeypatch.setattr(
            "famed.reference_cache.refresh_cache",
            lambda passed_cache, dataset_id=None: {"rxnorm": "UNAVAILABLE: timeout"},
        )

        def fail_if_called(*_args: object) -> int:
            raise AssertionError("derived reports must refresh only after successful cache changes")

        monkeypatch.setattr(
            "famed.reference_cache._refresh_derived_outputs_after_cache_change",
            fail_if_called,
        )

        assert main() == 0


class TestCacheStatus:
    def test_status_all_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            status = cache_status(cache_dir)
            for ds_id in SUPPORTED_DATASETS:
                assert status[ds_id]["status"] == STATUS_MISSING
                assert status[ds_id]["version"] is None

    def test_status_surfaces_failed_gnomad_attempt_with_last_good_rows(
        self, tmp_path: Path
    ) -> None:
        conn = open_annotation_db(tmp_path / "famed-reference.db")
        conn.execute(
            """
            INSERT INTO annotation_meta (
                dataset, refreshed_at, row_count, source_url, source_version,
                last_attempted_at, last_attempt_status, last_error
            ) VALUES (
                'gnomad', '2023-08-01T00:00:00+00:00', 10, 'https://example.com',
                '4.1.1', '2023-08-05T00:00:00+00:00', 'failed', 'network unavailable'
            )
            """
        )
        conn.commit()
        conn.close()

        status = cache_status(tmp_path)

        assert status["gnomad"]["status"] == STATUS_STALE
        assert status["gnomad"]["version"] == "4.1.1"
        assert status["gnomad"]["last_attempt_status"] == "failed"
        assert status["gnomad"]["last_error"] == "network unavailable"

    def test_status_does_not_invent_provenance_for_first_failed_gnomad_attempt(
        self, tmp_path: Path
    ) -> None:
        conn = open_annotation_db(tmp_path / "famed-reference.db")
        conn.execute(
            """
            INSERT INTO annotation_meta (
                dataset, refreshed_at, row_count, source_url, source_version,
                last_attempted_at, last_attempt_status, last_error
            ) VALUES (
                'gnomad', '2023-08-05T00:00:00+00:00', 0, 'https://example.com',
                NULL, '2023-08-05T00:00:00+00:00', 'failed', 'release drift'
            )
            """
        )
        conn.commit()
        conn.close()

        status = cache_status(tmp_path)["gnomad"]

        assert status["status"] == "UNAVAILABLE"
        assert status["version"] is None
        assert status["downloaded_at"] is None
        assert status["last_attempted_at"] == "2023-08-05T00:00:00+00:00"
        assert status["last_attempt_status"] == "failed"
        assert status["last_error"] == "release drift"

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106")
    def test_status_with_cached_dataset(self, _mock_ver: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(
                cache_dir,
                {
                    "datasets": {
                        "rxnorm": {
                            "version": "20220106",
                            "downloaded_at": "2022-01-06T12:00:00Z",
                            "source_url": "https://example.com/rx",
                        }
                    }
                },
            )
            status = cache_status(cache_dir)
            assert status["rxnorm"]["status"] == STATUS_OK
            assert status["rxnorm"]["version"] == "20220106"
            assert status["rxnorm"]["display_name"] == "RxNorm"
            assert status["loinc"]["status"] == STATUS_MISSING

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220201")
    def test_status_reports_stale_rxnorm(self, _mock_ver: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(
                cache_dir,
                {
                    "datasets": {
                        "rxnorm": {
                            "version": "20220106",
                            "downloaded_at": "2022-01-06T12:00:00Z",
                            "source_url": "https://example.com/rx",
                        }
                    }
                },
            )
            status = cache_status(cache_dir)
            assert status["rxnorm"]["status"] == STATUS_STALE
            assert status["rxnorm"]["version"] == "20220106"
            assert status["loinc"]["status"] == STATUS_MISSING

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106")
    def test_status_stays_ok_when_version_match(self, _mock_ver: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(
                cache_dir,
                {
                    "datasets": {
                        "rxnorm": {
                            "version": "20220106",
                            "downloaded_at": "2022-01-06T12:00:00Z",
                            "source_url": "https://example.com/rx",
                        }
                    }
                },
            )
            status = cache_status(cache_dir)
            assert status["rxnorm"]["status"] == STATUS_OK

    @mock.patch("famed.reference_cache._rxnorm_version", return_value=None)
    def test_status_stays_ok_when_network_error(self, _mock_ver: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(
                cache_dir,
                {
                    "datasets": {
                        "rxnorm": {
                            "version": "20220106",
                            "downloaded_at": "2022-01-06T12:00:00Z",
                            "source_url": "https://example.com/rx",
                        }
                    }
                },
            )
            status = cache_status(cache_dir)
            assert status["rxnorm"]["status"] == STATUS_OK

    def test_status_loinc_not_staled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(
                cache_dir,
                {
                    "datasets": {
                        "loinc": {
                            "version": "2.78",
                            "downloaded_at": "2022-01-06T12:00:00Z",
                            "source_url": "https://example.com/loinc",
                        }
                    }
                },
            )
            status = cache_status(cache_dir)
            assert status["rxnorm"]["status"] == STATUS_MISSING
            assert status["loinc"]["status"] == STATUS_OK

    def test_status_detects_filesystem_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "loinc").mkdir(parents=True)
            (cache_dir / "loinc" / "loinc.csv").write_text("LOINC_NUM,COMPONENT\n1234-5,Glucose\n")

            status = cache_status(cache_dir)
            assert status["loinc"]["status"] == STATUS_OK
            assert status["loinc"]["version"] is None
            assert status["rxnorm"]["status"] == STATUS_MISSING

    def test_status_empty_directory_stays_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "loinc").mkdir(parents=True)

            status = cache_status(cache_dir)
            assert status["loinc"]["status"] == STATUS_MISSING

    def test_status_manifest_entries_take_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "rxnorm").mkdir(parents=True)
            (cache_dir / "rxnorm" / "rxnorm_concepts.jsonl").write_text(
                '{"rxcui":"1","name":"A","tty":"IN"}\n'
            )
            write_manifest(
                cache_dir,
                {
                    "datasets": {
                        "rxnorm": {
                            "version": "20220106",
                            "downloaded_at": "2022-01-06T12:00:00Z",
                            "source_url": "https://example.com/rx",
                        }
                    }
                },
            )

            with mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106"):
                status = cache_status(cache_dir)
            assert status["rxnorm"]["status"] == STATUS_OK


class TestLoadRxnormIndex:
    def test_reviewed_overrides_add_alias_not_in_downloaded_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_concepts.jsonl").write_text(
                '{"rxcui":"6809","name":"Metformin","tty":"IN"}\n',
                encoding="utf-8",
            )
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Glucophage XR","rxcui":"6809",'
                '"reviewed_by":"local_manual_review","reviewed_at":"2023-05-07"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Glucophage XR", index) == "6809"
            assert lookup_rxnorm_cui("Metformin", index) == "6809"

    def test_reviewed_overrides_take_priority_over_downloaded_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_concepts.jsonl").write_text(
                '{"rxcui":"111","name":"Metformin","tty":"IN"}\n',
                encoding="utf-8",
            )
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Metformin","rxcui":"6809","reviewed_by":"local_manual_review","reviewed_at":"2023-05-07"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Metformin", index) == "6809"

    def test_invalid_reviewed_override_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_concepts.jsonl").write_text(
                '{"rxcui":"6809","name":"Metformin","tty":"IN"}\n',
                encoding="utf-8",
            )
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Metformin","rxcui":"999","reviewed_by":"local_manual_review","reviewed_at":"05/07/2023"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Metformin", index) == "6809"

    def test_reviewed_overrides_work_without_downloaded_concepts_file(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Zoloft","rxcui":"82728",'
                '"reviewed_by":"local_manual_review","reviewed_at":"2023-05-30"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Zoloft", index) == "82728"

    def test_reviewed_overrides_can_carry_cpic_drug_alias(self) -> None:
        from famed.reference_cache import (
            load_reviewed_rxnorm_cpic_aliases,
            lookup_reviewed_rxnorm_cpic_alias,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Zoloft","rxcui":"82728","cpic_drug":"sertraline",'
                '"reviewed_by":"local_manual_review","reviewed_at":"2023-05-30"}\n',
                encoding="utf-8",
            )

            aliases = load_reviewed_rxnorm_cpic_aliases(cache_dir)

            assert lookup_reviewed_rxnorm_cpic_alias("Zoloft", aliases) == "sertraline"


class TestStripRxnormDoseForm:
    def test_strips_trailing_strength_and_form_words(self) -> None:
        assert _strip_rxnorm_dose_form("omeprazole 20 mg delay release capsule") == "omeprazole"
        assert _strip_rxnorm_dose_form("sertraline 12.5 mg tabs") == "sertraline"
        assert _strip_rxnorm_dose_form("fluoxetine 5 mg tablet") == "fluoxetine"
        assert _strip_rxnorm_dose_form("magnesium 250 mg caps") == "magnesium"

    def test_strips_trailing_route_words(self) -> None:
        assert _strip_rxnorm_dose_form("ergocalciferol po") == "ergocalciferol"
        assert _strip_rxnorm_dose_form("turmeric oral") == "turmeric"
        assert _strip_rxnorm_dose_form("psyllium sugar free oral powder") == "psyllium"

    def test_no_op_when_no_trailing_dose_form_tokens(self) -> None:
        assert _strip_rxnorm_dose_form("vitamin d") == "vitamin d"
        assert _strip_rxnorm_dose_form("magnesium glycinate") == "magnesium glycinate"


class TestLookupRxnormCuiDoseFormFallback:
    def test_exact_match_is_preferred_over_stripped_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Magnesium","rxcui":"6574",'
                '"reviewed_by":"local_manual_review","reviewed_at":"2023-06-11"}\n'
                '{"name":"Magnesium Citrate","rxcui":"52356",'
                '"reviewed_by":"local_manual_review","reviewed_at":"2023-06-11"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Magnesium Citrate PO", index) == "52356"

    def test_dose_form_suffix_falls_back_to_bare_ingredient_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Omeprazole","rxcui":"7646","reviewed_by":"local_manual_review","reviewed_at":"2023-05-30"}\n'
                '{"name":"sertraline","rxcui":"36437","reviewed_by":"local_manual_review","reviewed_at":"2023-05-30"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Omeprazole 20 mg. delay release capsule", index) == "7646"
            assert lookup_rxnorm_cui("sertraline (ZOLOFT) 12.5 mg TABS", index) == "36437"

    def test_unresolvable_name_with_dose_form_suffix_stays_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir(parents=True)
            (rxnorm_dir / "rxnorm_review_overrides.jsonl").write_text(
                '{"name":"Omeprazole","rxcui":"7646","reviewed_by":"local_manual_review","reviewed_at":"2023-05-30"}\n',
                encoding="utf-8",
            )

            index = load_rxnorm_index(cache_dir)

            assert lookup_rxnorm_cui("Multivitamin 50 mg tablet", index) == ""


class TestDatasetHasFiles:
    def test_returns_false_for_nonexistent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert _dataset_has_files(Path(tmp), "rxnorm") is False

    def test_returns_false_for_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rxnorm").mkdir()
            assert _dataset_has_files(Path(tmp), "rxnorm") is False

    def test_returns_true_for_directory_with_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rxnorm").mkdir()
            (Path(tmp) / "rxnorm" / "rxnorm_concepts.jsonl").write_text("data")
            assert _dataset_has_files(Path(tmp), "rxnorm") is True


class TestCheckStale:
    def test_check_stale_unknown_dataset(self) -> None:
        assert check_stale("nonexistent", {"datasets": {}}) is False

    def test_check_stale_no_version_url(self) -> None:
        assert check_stale("loinc", {"datasets": {"loinc": {"version": "1.0"}}}) is False

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220201")
    def test_check_stale_rxnorm_version_match(self, _mock_ver: mock.Mock) -> None:
        manifest = {"datasets": {"rxnorm": {"version": "20220201"}}}
        assert check_stale("rxnorm", manifest) is False

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220201")
    def test_check_stale_rxnorm_version_mismatch(self, _mock_ver: mock.Mock) -> None:
        manifest = {"datasets": {"rxnorm": {"version": "20220106"}}}
        assert check_stale("rxnorm", manifest) is True

    @mock.patch("famed.reference_cache._rxnorm_version", return_value=None)
    def test_check_stale_rxnorm_network_error(self, _mock_ver: mock.Mock) -> None:
        manifest = {"datasets": {"rxnorm": {"version": "20220106"}}}
        assert check_stale("rxnorm", manifest) is False


class TestDownloadRxnorm:
    def _build_concept_page(self, concepts: list[dict], page_num: int, total_pages: int) -> dict:
        return {
            "allConcepts": {
                "allConceptGroup": {"allConceptProperties": concepts},
                "pageInfo": {"totalPages": total_pages, "pageNum": page_num},
            }
        }

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106")
    def test_download_rxnorm_single_page(self, _mock_ver: mock.Mock) -> None:
        concepts = [
            {"rxcui": "1191", "name": "Aspirin", "tty": "IN"},
            {"rxcui": "161", "name": "Acetaminophen", "tty": "IN"},
        ]
        in_page = self._build_concept_page(concepts, 1, 1)
        empty_page = self._build_concept_page([], 1, 1)

        def fetch_tty(tty: str, page_num: int) -> dict:
            if tty == "IN":
                return in_page
            return empty_page

        with mock.patch(
            "famed.reference_cache._rxnorm_fetch_tty_page", side_effect=fetch_tty
        ) as mock_fetch:
            with tempfile.TemporaryDirectory() as tmp:
                cache_dir = Path(tmp)
                version = download_rxnorm(cache_dir)
                assert version == "20220106"
                rxnorm_file = cache_dir / "rxnorm" / "rxnorm_concepts.jsonl"
                assert rxnorm_file.exists()
                lines = rxnorm_file.read_text().strip().split("\n")
                assert len(lines) == 2
                parsed = [json.loads(line) for line in lines]
                assert parsed[0]["rxcui"] == "1191"
                assert parsed[1]["rxcui"] == "161"
        assert ("IN", 2) not in [call.args for call in mock_fetch.call_args_list]

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106")
    def test_download_rxnorm_multiple_pages(self, _mock_ver: mock.Mock) -> None:
        page1_concepts = [{"rxcui": "p1", "name": "A", "tty": "IN"}]
        page2_concepts = [{"rxcui": "p2", "name": "B", "tty": "IN"}]
        empty_page = self._build_concept_page([], 1, 1)

        responses = {
            ("IN", 1): self._build_concept_page(page1_concepts, 1, 2),
            ("IN", 2): self._build_concept_page(page2_concepts, 2, 2),
            ("SCD", 1): empty_page,
            ("SBD", 1): empty_page,
        }

        def side_effect(tty: str, page_num: int) -> dict:
            return responses.get((tty, page_num), empty_page)

        with mock.patch(
            "famed.reference_cache._rxnorm_fetch_tty_page", side_effect=side_effect
        ) as mock_fetch:
            with tempfile.TemporaryDirectory() as tmp:
                cache_dir = Path(tmp)
                version = download_rxnorm(cache_dir)
                assert version == "20220106"
                lines = (
                    (cache_dir / "rxnorm" / "rxnorm_concepts.jsonl").read_text().strip().split("\n")
                )
                assert len(lines) == 2
        assert ("IN", 3) not in [call.args for call in mock_fetch.call_args_list]

    @mock.patch("famed.reference_cache._rxnorm_version", return_value=None)
    @mock.patch("famed.reference_cache.time.sleep", return_value=None)
    def test_download_rxnorm_no_version_fallback(
        self, _mock_sleep: mock.Mock, _mock_ver: mock.Mock
    ) -> None:
        empty_page = {
            "allConcepts": {
                "allConceptGroup": {"allConceptProperties": []},
                "pageInfo": {"totalPages": 1, "pageNum": 1},
            }
        }
        with mock.patch("famed.reference_cache._rxnorm_fetch_tty_page", return_value=empty_page):
            with tempfile.TemporaryDirectory() as tmp:
                cache_dir = Path(tmp)
                version = download_rxnorm(cache_dir)
                assert len(version) == 8
                assert version.isdigit()


class TestDownloadDataset:
    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106")
    @mock.patch("famed.reference_cache.time.sleep", return_value=None)
    def test_download_rxnorm_dataset(self, _mock_sleep: mock.Mock, _mock_ver: mock.Mock) -> None:
        concept = {"rxcui": "1191", "name": "Aspirin", "tty": "IN"}
        page = {
            "allConcepts": {
                "allConceptGroup": {"allConceptProperties": [concept]},
                "pageInfo": {"totalPages": 1, "pageNum": 1},
            }
        }
        with mock.patch("famed.reference_cache._rxnorm_fetch_tty_page", return_value=page):
            with tempfile.TemporaryDirectory() as tmp:
                cache_dir = Path(tmp)
                result = download_dataset(cache_dir, "rxnorm")
                assert result is True
                manifest = read_manifest(cache_dir)
                assert "rxnorm" in manifest["datasets"]
                assert manifest["datasets"]["rxnorm"]["version"] == "20220106"
                assert "checksum_sha256" in manifest["datasets"]["rxnorm"]

    def test_download_unknown_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError, match="Unknown dataset"):
                download_dataset(Path(tmp), "nonexistent")

    def test_download_loinc_requires_manual_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError, match="manual"):
                download_dataset(Path(tmp), "loinc")

    @mock.patch("famed.reference_cache._rxnorm_version", return_value="20220106")
    @mock.patch("famed.reference_cache.time.sleep", return_value=None)
    def test_download_idempotent(self, _mock_sleep: mock.Mock, _mock_ver: mock.Mock) -> None:
        concept = {"rxcui": "1191", "name": "Aspirin", "tty": "IN"}
        page = {
            "allConcepts": {
                "allConceptGroup": {"allConceptProperties": [concept]},
                "pageInfo": {"totalPages": 1, "pageNum": 1},
            }
        }
        with mock.patch("famed.reference_cache._rxnorm_fetch_tty_page", return_value=page):
            with tempfile.TemporaryDirectory() as tmp:
                cache_dir = Path(tmp)
                download_dataset(cache_dir, "rxnorm")
                first_manifest = read_manifest(cache_dir)
                download_dataset(cache_dir, "rxnorm")
                second_manifest = read_manifest(cache_dir)
                assert (
                    first_manifest["datasets"]["rxnorm"]["version"]
                    == second_manifest["datasets"]["rxnorm"]["version"]
                )


class TestParseLoincCsv:
    def test_parse_loinc_csv_writes_jsonl_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc" / "Loinc_2.82" / "LoincTable"
            loinc_dir.mkdir(parents=True)
            (loinc_dir / "Loinc.csv").write_text(
                "LOINC_NUM,COMPONENT,LONG_COMMON_NAME,CLASS,SYSTEM,SCALE_TYP,METHOD_TYP,STATUS\n"
                "2345-7,Glucose,Glucose [Mass/volume] in Serum or Plasma,CHEM,Ser/Plas,"
                "Qn,,ACTIVE\n",
                encoding="utf-8",
            )

            version = parse_loinc_csv(cache_dir)

            parsed = cache_dir / "loinc" / "loinc_concepts.jsonl"
            assert version == "2.82"
            assert parsed.exists()
            record = json.loads(parsed.read_text(encoding="utf-8").strip())
            assert record["loinc_num"] == "2345-7"
            assert record["long_common_name"] == "Glucose [Mass/volume] in Serum or Plasma"

            manifest = read_manifest(cache_dir)
            assert manifest["datasets"]["loinc"]["version"] == "2.82"
            assert manifest["datasets"]["loinc"]["row_count"] == 1

    def test_parse_loinc_csv_requires_loinc_num_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "Loinc.csv").write_text("BAD,COMPONENT\nx,Glucose\n", encoding="utf-8")

            with pytest.raises(ValueError, match="LOINC table CSV"):
                parse_loinc_csv(cache_dir)

    def test_parse_loinc_csv_requires_manual_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(FileNotFoundError, match="LOINC CSV not found"):
                parse_loinc_csv(Path(tmp))


class TestRefreshCache:
    @mock.patch("famed.reference_cache.download_dataset")
    def test_refresh_single_dataset(self, mock_download: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(cache_dir, {"datasets": {}})
            results = refresh_cache(cache_dir, dataset_id="rxnorm")
            assert results["rxnorm"] == STATUS_OK
            mock_download.assert_called_once()

    def test_refresh_unknown_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = refresh_cache(Path(tmp), dataset_id="nonexistent")
            assert "Unknown dataset" in results["nonexistent"]

    def test_refresh_loinc_without_local_files_reports_manual_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(cache_dir, {"datasets": {}})
            results = refresh_cache(cache_dir, dataset_id="loinc")
            assert "manual download required" in results["loinc"]

    def test_refresh_loinc_with_local_files_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "loinc").mkdir(parents=True)
            (cache_dir / "loinc" / "Loinc.csv").write_text(
                "LOINC_NUM,COMPONENT,LONG_COMMON_NAME\n1234-5,Glucose,Glucose test\n",
                encoding="utf-8",
            )
            write_manifest(cache_dir, {"datasets": {}})
            results = refresh_cache(cache_dir, dataset_id="loinc")
            assert results["loinc"] == STATUS_OK
            assert (cache_dir / "loinc" / "loinc_concepts.jsonl").exists()

    @mock.patch("famed.reference_cache.download_dataset")
    def test_refresh_all_datasets(self, mock_download: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            write_manifest(cache_dir, {"datasets": {}})
            results = refresh_cache(cache_dir)
            assert results["rxnorm"] == STATUS_OK
            dataset_order = [call.args[1] for call in mock_download.call_args_list]
            assert dataset_order.index("clinvar") < dataset_order.index("gnomad")

    def test_refresh_skill_exposes_gnomad_privacy_boundary(self) -> None:
        skill_path = (
            Path(__file__).resolve().parents[1]
            / ".agents"
            / "skills"
            / "fm-refresh-reference-cache"
            / "SKILL.md"
        )
        content = skill_path.read_text(encoding="utf-8")
        normalized = " ".join(content.split())

        assert "public ClinVar-derived target contract" in normalized
        assert "Never construct or narrow the gnomAD target list from household data" in normalized
        assert "python scripts/cache.py --dataset <dataset> refresh" in normalized


class TestErrorHandling:
    def test_fetch_json_retry_then_success(self) -> None:
        from io import BytesIO

        from famed.reference_cache import MAX_RETRIES, _fetch_json

        call_count = 0

        def urlopen_fn(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count < MAX_RETRIES:
                raise urllib.error.URLError("timeout")
            return BytesIO(b'{"version": "123"}')

        with mock.patch("famed.reference_cache.urllib.request.urlopen", side_effect=urlopen_fn):
            with mock.patch("famed.reference_cache.time.sleep", return_value=None):
                result = _fetch_json("https://example.com/api")
                assert result == {"version": "123"}
                assert call_count == MAX_RETRIES

    def test_fetch_json_all_retries_exhausted(self) -> None:
        from famed.reference_cache import MAX_RETRIES, _fetch_json

        call_count = 0

        def urlopen_fn(req, timeout=None):
            nonlocal call_count
            call_count += 1
            raise urllib.error.URLError("timeout")

        with mock.patch("famed.reference_cache.urllib.request.urlopen", side_effect=urlopen_fn):
            with mock.patch("famed.reference_cache.time.sleep", return_value=None):
                with pytest.raises(RuntimeError, match="Failed to fetch"):
                    _fetch_json("https://example.com/api")
                assert call_count == MAX_RETRIES

    def test_manifest_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "manifest.json").write_text("not json", encoding="utf-8")
            with pytest.raises(json.JSONDecodeError):
                read_manifest(cache_dir)


_SAMPLE_LOINC_JSONL = (
    '{"loinc_num": "2345-7", "component": "Glucose", '
    '"long_common_name": "Glucose [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "6690-2", "component": "WBC", '
    '"long_common_name": "Leukocytes [#/volume] in Blood by Automated count", '
    '"class": "HEM/BC", "system": "Bld", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "1742-6", "component": "ALT", '
    '"long_common_name": "Alanine aminotransferase [Enzymatic activity/volume] '
    'in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
)


class TestLoadLoincIndex:
    def test_returns_empty_when_file_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            assert load_loinc_index(cache_dir) == {}

    def test_loads_component_and_long_name_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(_SAMPLE_LOINC_JSONL, encoding="utf-8")

            index = load_loinc_index(cache_dir)

            assert index["glucose"] == "2345-7"
            assert "glucose mass volume in serum or plasma" in index
            assert index["glucose mass volume in serum or plasma"] == "2345-7"
            assert index["wbc"] == "6690-2"
            assert index["alt"] == "1742-6"

    def test_reviewed_override_resolves_equation_tied_gfr_family(self) -> None:
        """Every "Glomerular filtration rate" concept is ACTIVE, serum/plasma and
        quantitative, so they tie on every field the canonical rank can see and
        differ only by estimating equation. Insertion order then picked the CKiD
        equation -- pediatric, creatinine + cystatin C + urea -- which was
        inferred onto adult records. The reviewed override (ADR-034) must win,
        and the abbreviation must inherit it through the synonym map.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            rows = [
                {
                    "loinc_num": "102097-3",
                    "component": "Glomerular filtration rate",
                    "long_common_name": "GFR by CKiD",
                    "system": "Ser/Plas/Bld",
                    "scale": "Qn",
                    "status": "ACTIVE",
                    "method": "Creatinine, Cystatin C and Urea-based formula (CKiD)/1.73 sq M",
                },
                {
                    "loinc_num": "69405-9",
                    "component": "Glomerular filtration rate",
                    "long_common_name": "GFR based on 1.73 sq M",
                    "system": "Ser/Plas/Bld",
                    "scale": "Qn",
                    "status": "ACTIVE",
                    "method": "based on 1.73 sq M",
                },
            ]
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )

            index = load_loinc_index(cache_dir)

            assert index["glomerular filtration rate"] == "69405-9"
            assert index["egfr"] == "69405-9"

    def test_skips_blank_loinc_num(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                '{"loinc_num": "", "component": "Orphan", "long_common_name": ""}\n',
                encoding="utf-8",
            )
            index = load_loinc_index(cache_dir)
            assert "orphan" not in index

    def test_equal_rank_keeps_first_occurrence(self) -> None:
        # ADR-034: when two concepts share a key and rank identically (here both
        # carry no status/system/scale), the first record seen wins for
        # determinism. Uses a non-overridden component name.
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                '{"loinc_num": "1111-1", "component": "Magnesium", "long_common_name": ""}\n'
                '{"loinc_num": "2222-2", "component": "Magnesium", "long_common_name": ""}\n',
                encoding="utf-8",
            )
            index = load_loinc_index(cache_dir)
            assert index["magnesium"] == "1111-1"

    def test_canonical_preference_beats_file_order(self) -> None:
        # ADR-034: a more canonical concept (ACTIVE, serum/plasma, quantitative)
        # wins over an earlier non-canonical one for the same key, even though
        # first-record-wins would have chosen the earlier row. Uses a
        # non-overridden component so the ranking heuristic, not the override map,
        # is exercised.
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                '{"loinc_num": "9001-0", "component": "Potassium", "long_common_name": "", '
                '"system": "Stool", "scale": "-", "status": "TRIAL"}\n'
                '{"loinc_num": "9002-0", "component": "Potassium", "long_common_name": "", '
                '"system": "RBC", "scale": "Qn", "status": "ACTIVE"}\n'
                '{"loinc_num": "2823-3", "component": "Potassium", "long_common_name": "", '
                '"system": "Ser/Plas", "scale": "Qn", "status": "ACTIVE"}\n',
                encoding="utf-8",
            )
            index = load_loinc_index(cache_dir)
            assert index["potassium"] == "2823-3"

    def test_reviewed_override_wins_over_heuristic(self) -> None:
        # ADR-034: a reviewed override is authoritative and pins the code even
        # when the ranking heuristic (or file order) would select another.
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                '{"loinc_num": "111263-0", "component": "Sodium", "long_common_name": "", '
                '"system": "Stool", "scale": "-", "status": "TRIAL"}\n'
                '{"loinc_num": "9999-9", "component": "Sodium", "long_common_name": "", '
                '"system": "Ser/Plas", "scale": "Qn", "status": "ACTIVE"}\n',
                encoding="utf-8",
            )
            index = load_loinc_index(cache_dir)
            # Heuristic alone would pick 9999-9; the reviewed override pins 2951-2.
            assert index["sodium"] == "2951-2"

    def test_returns_empty_on_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text("not json\n", encoding="utf-8")
            assert load_loinc_index(cache_dir) == {}


class TestLookupLoincCode:
    def _make_index(self) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(_SAMPLE_LOINC_JSONL, encoding="utf-8")
            return load_loinc_index(cache_dir)

    def test_exact_match(self) -> None:
        index = self._make_index()
        assert lookup_loinc_code("Glucose", index) == "2345-7"

    def test_case_insensitive(self) -> None:
        index = self._make_index()
        assert lookup_loinc_code("GLUCOSE", index) == "2345-7"
        assert lookup_loinc_code("glucose", index) == "2345-7"

    def test_punctuation_normalized(self) -> None:
        index = self._make_index()
        assert lookup_loinc_code("Glucose [Mass/volume] in Serum or Plasma", index) == "2345-7"

    def test_no_match_returns_empty_string(self) -> None:
        index = self._make_index()
        assert lookup_loinc_code("Unobtainium", index) == ""

    def test_empty_index_returns_empty_string(self) -> None:
        assert lookup_loinc_code("Glucose", {}) == ""

    def test_empty_test_name_returns_empty_string(self) -> None:
        index = self._make_index()
        assert lookup_loinc_code("", index) == ""

    def test_unit_disambiguation_prefers_serum_albumin_over_urine_microalbumin(self) -> None:
        index = {
            "albumin": "100158-5",
            "albumin mass volume in serum or plasma": "1751-7",
        }
        assert lookup_loinc_code("Albumin", index, "g/dL") == "1751-7"

    def test_unit_disambiguation_prefers_serum_psa_over_dbs_variant(self) -> None:
        index = {
            "prostate specific antigen": "100716-0",
            "psa": "100716-0",
        }
        assert lookup_loinc_code("PSA", index, "ng/mL") == "2857-1"


# LOINC sample using full component names so synonyms are exercised
_SAMPLE_LOINC_JSONL_FULL_NAMES = (
    '{"loinc_num": "2345-7", "component": "Glucose", '
    '"long_common_name": "Glucose [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "11580-8", "component": "Thyrotropin", '
    '"long_common_name": "Thyrotropin [Units/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "100849-9", "component": "Leukocytes", '
    '"long_common_name": "Leukocytes [#/volume] in Blood", '
    '"class": "HEM/BC", "system": "Bld", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "41995-2", "component": "Hemoglobin A1c", '
    '"long_common_name": "Hemoglobin A1c/Hemoglobin.total in Blood", '
    '"class": "CHEM", "system": "Bld", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2823-3", "component": "Potassium", '
    '"long_common_name": "Potassium [Moles/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
)


class TestLoincSynonyms:
    def _make_cache(self) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
        tmp = tempfile.TemporaryDirectory()
        cache_dir = Path(tmp.name)
        loinc_dir = cache_dir / "loinc"
        loinc_dir.mkdir()
        (loinc_dir / "loinc_concepts.jsonl").write_text(
            _SAMPLE_LOINC_JSONL_FULL_NAMES, encoding="utf-8"
        )
        return cache_dir, tmp

    def test_abbreviation_resolves_via_synonym(self) -> None:
        """TSH maps to Thyrotropin via the synonym table."""
        cache_dir, tmp = self._make_cache()
        with tmp:
            index = load_loinc_index(cache_dir)
            assert lookup_loinc_code("TSH", index) == "11580-8"
            assert lookup_loinc_code("tsh", index) == "11580-8"

    def test_wbc_resolves_via_synonym(self) -> None:
        """WBC maps to Leukocytes via the synonym table."""
        cache_dir, tmp = self._make_cache()
        with tmp:
            index = load_loinc_index(cache_dir)
            assert lookup_loinc_code("WBC", index) == "100849-9"

    def test_multiple_aliases_for_same_target(self) -> None:
        """HbA1c, A1C, and HgbA1c all resolve to the same Hemoglobin A1c code --
        the reviewed %-of-total-hemoglobin code (ADR-034), not the fixture's
        mass/volume concept."""
        cache_dir, tmp = self._make_cache()
        with tmp:
            index = load_loinc_index(cache_dir)
            assert lookup_loinc_code("HbA1c", index) == "4548-4"
            assert lookup_loinc_code("A1C", index) == "4548-4"
            assert lookup_loinc_code("HgbA1c", index) == "4548-4"
            # Exact component name also resolves
            assert lookup_loinc_code("Hemoglobin A1c", index) == "4548-4"

    def test_exact_match_not_overwritten_by_synonym(self) -> None:
        """When the component name IS the abbreviation, its direct code is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            # 'WBC' is already the component name → key 'wbc' = '6690-2'
            # 'Leukocytes' absent → synonym target absent → no injection
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                '{"loinc_num": "6690-2", "component": "WBC", '
                '"long_common_name": "WBC [#/volume] in Blood", '
                '"class": "HEM/BC", "system": "Bld", "scale": "Qn", '
                '"method": "", "status": "ACTIVE"}\n',
                encoding="utf-8",
            )
            index = load_loinc_index(cache_dir)
            assert index.get("wbc") == "6690-2"

    def test_synonym_not_injected_when_target_absent(self) -> None:
        """If the synonym target key is absent from the index, the alias is not added."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            # Only Glucose — 'thyrotropin' key absent, so 'tsh' must not appear
            (loinc_dir / "loinc_concepts.jsonl").write_text(
                '{"loinc_num": "2345-7", "component": "Glucose", '
                '"long_common_name": "Glucose [Mass/volume] in Serum or Plasma", '
                '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", '
                '"method": "", "status": "ACTIVE"}\n',
                encoding="utf-8",
            )
            index = load_loinc_index(cache_dir)
            assert "tsh" not in index
            assert "thyrotropin" not in index

    def test_synonym_injection_skipped_for_absent_file(self) -> None:
        """No errors or spurious entries when the LOINC file is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            assert load_loinc_index(cache_dir) == {}

    def test_exact_name_match_unaffected(self) -> None:
        """Exact component/long-name matches continue to work alongside synonyms."""
        cache_dir, tmp = self._make_cache()
        with tmp:
            index = load_loinc_index(cache_dir)
            assert lookup_loinc_code("Glucose", index) == "2345-7"
            assert lookup_loinc_code("Thyrotropin", index) == "11580-8"
            assert lookup_loinc_code("Potassium", index) == "2823-3"


# LOINC JSONL for new synonym tests — urine (POCT), thyroid free, iron panel.
# Long_common_name strings match what a real LOINC CSV would contain so the
# normalized target keys are present in the index after load_loinc_index runs.
_SAMPLE_LOINC_JSONL_NEW_SYNONYMS = (
    # Urine / POCT targets
    '{"loinc_num": "5778-6", "component": "Color", '
    '"long_common_name": "Color of Urine", '
    '"class": "UA", "system": "Urine", "scale": "Nom", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2350-7", "component": "Glucose", '
    '"long_common_name": "Glucose [Mass/volume] in Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2965-2", "component": "Specific gravity", '
    '"long_common_name": "Specific gravity of Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2756-5", "component": "pH", '
    '"long_common_name": "pH of Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "13658-0", "component": "Urobilinogen", '
    '"long_common_name": "Urobilinogen [Mass/volume] in Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2514-8", "component": "Ketones", '
    '"long_common_name": "Ketones [Mass/volume] in Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2888-6", "component": "Protein", '
    '"long_common_name": "Protein [Mass/volume] in Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "5794-3", "component": "Hemoglobin", '
    '"long_common_name": "Hemoglobin [Presence] in Urine by Test strip", '
    '"class": "UA", "system": "Urine", "scale": "Ord", "method": "Test strip", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "32167-9", "component": "Clarity", '
    '"long_common_name": "Clarity of Urine", '
    '"class": "SPEC", "system": "Urine", "scale": "Nom", "method": "", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "5770-3", "component": "Bilirubin.total", '
    '"long_common_name": "Bilirubin.total [Mass/volume] in Urine", '
    '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "5802-4", "component": "Nitrite", '
    '"long_common_name": "Nitrite [Presence] in Urine", '
    '"class": "UA", "system": "Urine", "scale": "Ord", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "5799-2", "component": "Leukocyte esterase", '
    '"long_common_name": "Leukocyte esterase [Presence] in Urine by Test strip", '
    '"class": "UA", "system": "Urine", "scale": "Ord", "method": "Test strip", '
    '"status": "ACTIVE"}\n'
    # Thyroid free fraction targets
    '{"loinc_num": "3026-3", "component": "Thyroxine (T4).free", '
    '"long_common_name": "Thyroxine (T4) [Free] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "3053-7", "component": "Triiodothyronine (T3).free", '
    '"long_common_name": "Triiodothyronine (T3) [Free] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "11580-8", "component": "Thyrotropin", '
    '"long_common_name": "Thyrotropin [Units/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    # Iron panel targets
    '{"loinc_num": "2498-4", "component": "Iron", '
    '"long_common_name": "Iron [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2500-7", "component": "Iron binding capacity", '
    '"long_common_name": "Iron binding capacity [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2502-3", "component": "Iron saturation", '
    '"long_common_name": "Iron saturation [Mass Fraction] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    # Urate / phosphate targets
    '{"loinc_num": "3084-1", "component": "Urate", '
    '"long_common_name": "Urate [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2777-1", "component": "Phosphate", '
    '"long_common_name": "Phosphate [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    # DHEA-S target
    '{"loinc_num": "2191-5", "component": "DHEA-S", '
    '"long_common_name": "DHEA-S [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    # Real PDF miss targets
    '{"loinc_num": "42254-3", "component": "Nuclear Ab", '
    '"long_common_name": "Nuclear Ab [Presence] in Serum by Immunofluorescence", '
    '"class": "SERO", "system": "Ser", "scale": "Ord", "method": "IF", "status": "ACTIVE"}\n'
    '{"loinc_num": "771-6", "component": "Erythrocytes.nucleated", '
    '"long_common_name": "Nucleated erythrocytes [#/volume] in Blood by Automated count", '
    '"class": "HEM/BC", "system": "Bld", "scale": "Qn", "method": "Automated count", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "58413-6", "component": "Erythrocytes.nucleated/Leukocytes", '
    '"long_common_name": "Nucleated erythrocytes/Leukocytes [Ratio] in Blood by Automated count", '
    '"class": "HEM/BC", "system": "Bld", "scale": "Qn", "method": "Automated count", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "2335-8", "component": "Hemoglobin", '
    '"long_common_name": "Hemoglobin [Presence] in Stool from gastrointestinal", '
    '"class": "CHEM", "system": "Stool^Patient.gastrointestinal", "scale": "Ord", '
    '"method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "2085-9", "component": "Cholesterol in HDL", '
    '"long_common_name": "Cholesterol in HDL [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "13457-7", "component": "Cholesterol in LDL", '
    '"long_common_name": "Cholesterol in LDL [Mass/volume] in Serum or Plasma by calculation", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "Calc", "status": "ACTIVE"}\n'
    '{"loinc_num": "2857-1", "component": "Prostate specific Ag", '
    '"long_common_name": "Prostate specific Ag [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "10886-0", "component": "Prostate specific Ag.free", '
    '"long_common_name": "Prostate Specific Ag Free [Mass/volume] in Serum or Plasma", '
    '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
    '{"loinc_num": "106733-9", "component": "Troponin I.cardiac", '
    '"long_common_name": "Troponin I.cardiac [Mass/volume] in Serum, Plasma or Blood '
    'by Rapid immunoassay", '
    '"class": "CHEM", "system": "Ser/Plas/Bld", "scale": "Qn", "method": "IA.rapid", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "19261-7", "component": "Amphetamines", '
    '"long_common_name": "Amphetamines [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19270-8", "component": "Barbiturates", '
    '"long_common_name": "Barbiturates [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "94117-9", "component": "Benzodiazepines panel", '
    '"long_common_name": "Benzodiazepines panel [Presence] - Urine by Screen method", '
    '"class": "PANEL.DRUG/TOX", "system": "Urine", "scale": "Ord", "method": "Screen", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "18282-4", "component": "Cannabinoids", '
    '"long_common_name": "Cannabinoids [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "20521-1", "component": "Carboxy tetrahydrocannabinol", '
    '"long_common_name": "Carboxy tetrahydrocannabinol [Mass/volume] in Urine by '
    'Confirmatory method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Qn", "method": "Confirm", '
    '"status": "ACTIVE"}\n'
    '{"loinc_num": "42241-0", "component": "Cocaine+Benzoylecgonine", '
    '"long_common_name": "Cocaine+Benzoylecgonine [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19295-5", "component": "Opiates", '
    '"long_common_name": "Opiates [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "58430-0", "component": "oxyCODONE+Oxymorphone", '
    '"long_common_name": "oxyCODONE+oxyMORphone [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19659-2", "component": "Phencyclidine", '
    '"long_common_name": "Phencyclidine [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19550-3", "component": "Methadone", '
    '"long_common_name": "Methadone [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19429-0", "component": "Propoxyphene", '
    '"long_common_name": "Propoxyphene [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19532-1", "component": "Meperidine", '
    '"long_common_name": "Meperidine [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "19710-3", "component": "Tramadol", '
    '"long_common_name": "Tramadol [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "59673-4", "component": "Fentanyl", '
    '"long_common_name": "fentaNYL [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
    '{"loinc_num": "93494-3", "component": "Buprenorphine", '
    '"long_common_name": "Buprenorphine [Presence] in Urine by Screen method", '
    '"class": "DRUG/TOX", "system": "Urine", "scale": "Ord", '
    '"method": "Screen", "status": "ACTIVE"}\n'
)


class TestLoincSynonymsExpanded:
    """Tests for synonym entries added in Next Delivery #2."""

    def _make_index(self, jsonl: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            loinc_dir = cache_dir / "loinc"
            loinc_dir.mkdir()
            (loinc_dir / "loinc_concepts.jsonl").write_text(jsonl, encoding="utf-8")
            return load_loinc_index(cache_dir)

    def test_cbc_absolute_aliases_resolve_to_blood_count_codes(self) -> None:
        jsonl = (
            '{"loinc_num": "770-8", "component": "Neutrophils/Leukocytes", '
            '"long_common_name": "Neutrophils/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "751-8", "component": "Neutrophils", '
            '"long_common_name": "Neutrophils [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "736-9", "component": "Lymphocytes/Leukocytes", '
            '"long_common_name": "Lymphocytes/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "731-0", "component": "Lymphocytes", '
            '"long_common_name": "Lymphocytes [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "5905-5", "component": "Monocytes/Leukocytes", '
            '"long_common_name": "Monocytes/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "742-7", "component": "Monocytes", '
            '"long_common_name": "Monocytes [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "713-8", "component": "Eosinophils/Leukocytes", '
            '"long_common_name": "Eosinophils/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "711-2", "component": "Eosinophils", '
            '"long_common_name": "Eosinophils [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "706-2", "component": "Basophils/Leukocytes", '
            '"long_common_name": "Basophils/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "704-7", "component": "Basophils", '
            '"long_common_name": "Basophils [#/volume] in Blood by Automated count"}\n'
        )
        index = self._make_index(jsonl)

        assert lookup_loinc_code("Absolute Neutrophils", index) == "751-8"
        assert lookup_loinc_code("Neutrophils Abs", index, "K/uL") == "751-8"
        assert lookup_loinc_code("Lymphs Abs", index) == "731-0"
        assert lookup_loinc_code("MonocytesAbs", index) == "742-7"
        assert lookup_loinc_code("Eos Abs", index) == "711-2"
        assert lookup_loinc_code("Basophils absolute", index) == "704-7"

    def test_cbc_differential_percentage_aliases_resolve_to_percent_codes(self) -> None:
        jsonl = (
            '{"loinc_num": "770-8", "component": "Neutrophils/Leukocytes", '
            '"long_common_name": "Neutrophils/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "751-8", "component": "Neutrophils", '
            '"long_common_name": "Neutrophils [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "736-9", "component": "Lymphocytes/Leukocytes", '
            '"long_common_name": "Lymphocytes/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "731-0", "component": "Lymphocytes", '
            '"long_common_name": "Lymphocytes [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "5905-5", "component": "Monocytes/Leukocytes", '
            '"long_common_name": "Monocytes/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "742-7", "component": "Monocytes", '
            '"long_common_name": "Monocytes [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "713-8", "component": "Eosinophils/Leukocytes", '
            '"long_common_name": "Eosinophils/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "711-2", "component": "Eosinophils", '
            '"long_common_name": "Eosinophils [#/volume] in Blood by Automated count"}\n'
            '{"loinc_num": "706-2", "component": "Basophils/Leukocytes", '
            '"long_common_name": "Basophils/Leukocytes in Blood by Automated count"}\n'
            '{"loinc_num": "704-7", "component": "Basophils", '
            '"long_common_name": "Basophils [#/volume] in Blood by Automated count"}\n'
        )
        index = self._make_index(jsonl)

        assert lookup_loinc_code("Neutrophils % (Auto)", index, "%") == "770-8"
        assert lookup_loinc_code("% Eosinophils", index, "") == "713-8"
        assert lookup_loinc_code("Lymphs", index, "%") == "736-9"
        assert lookup_loinc_code("Monocytes", index, "%") == "5905-5"
        assert lookup_loinc_code("Basophils", index, "%") == "706-2"

    # -- POCT urinalysis ---------------------------------------------------

    @pytest.mark.parametrize(
        "raw_name,expected_code",
        [
            ("POCT Color, UA", "5778-6"),
            ("POCT Glucose, UA", "2350-7"),
            ("POCT Spec Grav, UA", "2965-2"),
            ("POCT pH, UA", "2756-5"),
            ("POCT Urobilinogen, UA", "13658-0"),
            ("POCT Ketones, UA", "2514-8"),
            ("POCT Protein, UA", "2888-6"),
            ("POCT Blood, UA", "5794-3"),
            ("POCT Clarity, UA", "32167-9"),
            ("POCT Bilirubin, UA", "5770-3"),
            ("POCT Nitrite, UA", "5802-4"),
            ("POCT Leukocyte Esterase, UA", "5799-2"),
            ("POCT Leukocytes, UA", "5799-2"),
        ],
    )
    def test_poct_urine_names_resolve(self, raw_name: str, expected_code: str) -> None:
        index = self._make_index(_SAMPLE_LOINC_JSONL_NEW_SYNONYMS)
        assert lookup_loinc_code(raw_name, index) == expected_code

    def test_poct_glucose_resolves_to_urine_not_serum(self) -> None:
        # When both serum (2345-7) and urine (2350-7) glucose entries exist,
        # "POCT Glucose, UA" must resolve to the urine code.
        jsonl = (
            '{"loinc_num": "2345-7", "component": "Glucose", '
            '"long_common_name": "Glucose [Mass/volume] in Serum or Plasma", '
            '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", '
            '"method": "", "status": "ACTIVE"}\n'
            '{"loinc_num": "2350-7", "component": "Glucose", '
            '"long_common_name": "Glucose [Mass/volume] in Urine", '
            '"class": "UA", "system": "Urine", "scale": "Qn", "method": "", "status": "ACTIVE"}\n'
        )
        index = self._make_index(jsonl)
        assert lookup_loinc_code("POCT Glucose, UA", index) == "2350-7"
        # Plain "Glucose" still resolves to serum (first-occurrence wins on component key)
        assert lookup_loinc_code("Glucose", index) == "2345-7"

    # -- Thyroid free fractions --------------------------------------------

    @pytest.mark.parametrize(
        "raw_name,expected_code",
        [
            ("Free T4", "3026-3"),
            ("T4 Free", "3026-3"),
            # Reviewed override (ADR-034): free T3 mass concentration.
            ("Free T3", "3051-0"),
            ("T3 Free", "3051-0"),
            ("TSH 3rd generation", "11580-8"),
            ("TSH Ultrasensitive", "11580-8"),
        ],
    )
    def test_thyroid_aliases_resolve(self, raw_name: str, expected_code: str) -> None:
        index = self._make_index(_SAMPLE_LOINC_JSONL_NEW_SYNONYMS)
        assert lookup_loinc_code(raw_name, index) == expected_code

    # -- Iron panel --------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_name,expected_code",
        [
            ("Serum Iron", "2498-4"),
            ("Iron Total", "2498-4"),
            ("TIBC", "2500-7"),
            ("Transferrin Saturation", "2502-3"),
        ],
    )
    def test_iron_panel_aliases_resolve(self, raw_name: str, expected_code: str) -> None:
        index = self._make_index(_SAMPLE_LOINC_JSONL_NEW_SYNONYMS)
        assert lookup_loinc_code(raw_name, index) == expected_code

    # -- Metabolic / other -------------------------------------------------

    @pytest.mark.parametrize(
        "raw_name,expected_code",
        [
            ("Uric Acid", "3084-1"),
            ("Phosphorus", "2777-1"),
            ("DHEAS", "2191-5"),
        ],
    )
    def test_metabolic_aliases_resolve(self, raw_name: str, expected_code: str) -> None:
        index = self._make_index(_SAMPLE_LOINC_JSONL_NEW_SYNONYMS)
        assert lookup_loinc_code(raw_name, index) == expected_code

    # -- Real PDF miss aliases -------------------------------------------

    @pytest.mark.parametrize(
        "raw_name,expected_code",
        [
            ("ANA screen, IFA", "42254-3"),
            ("% nRBC", "58413-6"),
            ("nRBC(auto)%", "58413-6"),
            ("nRBC#", "771-6"),
            ("Fecal Occult Blood", "2335-8"),
            ("HDL-C - LC", "2085-9"),
            ("LDL-C (NIH Calc) - LC", "13457-7"),
            ("Prostate Specific Ag, Serum - LC", "2857-1"),
            ("PSA, Free - LC", "10886-0"),
            ("Troponin I, POC", "106733-9"),
        ],
    )
    def test_real_pdf_aliases_resolve(self, raw_name: str, expected_code: str) -> None:
        index = self._make_index(_SAMPLE_LOINC_JSONL_NEW_SYNONYMS)
        assert lookup_loinc_code(raw_name, index) == expected_code

    @pytest.mark.parametrize(
        "raw_name,expected_code",
        [
            ("Amphetamines screen, urine", "19261-7"),
            ("Barbiturates screen, urine", "19270-8"),
            ("Benzodiazepines screen, urine", "94117-9"),
            ("Cannabinoid screen, urine", "18282-4"),
            ("Carboxy-THC confirmatory, urine", "20521-1"),
            ("Cocaine metabolites screen, urine", "42241-0"),
            ("Opiates screen, urine", "19295-5"),
            ("Oxycodone/Oxymorphone screen, urine", "58430-0"),
            ("Phencyclidine screen, urine", "19659-2"),
            ("Methadone screen, urine", "19550-3"),
            ("Propoxyphene screen, urine", "19429-0"),
            ("Meperidine screen, urine", "19532-1"),
            ("Tramadol screen, urine", "19710-3"),
            ("Fentanyl, urine", "59673-4"),
            ("Buprenorphine, urine", "93494-3"),
        ],
    )
    def test_real_drug_screen_aliases_resolve(self, raw_name: str, expected_code: str) -> None:
        index = self._make_index(_SAMPLE_LOINC_JSONL_NEW_SYNONYMS)
        assert lookup_loinc_code(raw_name, index) == expected_code

    # -- Safety: absent target keeps alias out of index --------------------

    def test_synonym_not_injected_when_target_absent(self) -> None:
        # Only glucose present; urine-specific keys absent → POCT alias not injected.
        jsonl = (
            '{"loinc_num": "2345-7", "component": "Glucose", '
            '"long_common_name": "Glucose [Mass/volume] in Serum or Plasma", '
            '"class": "CHEM", "system": "Ser/Plas", "scale": "Qn", '
            '"method": "", "status": "ACTIVE"}\n'
        )
        index = self._make_index(jsonl)
        assert lookup_loinc_code("POCT Glucose, UA", index) == ""


_SAMPLE_RXNORM_JSONL = (
    '{"rxcui": "5521", "name": "Metformin", "tty": "IN"}\n'
    '{"rxcui": "860975", "name": "Metformin 500 MG Oral Tablet", "tty": "SCD"}\n'
    '{"rxcui": "860995", "name": "Glucophage 500 MG Oral Tablet", "tty": "SBD"}\n'
    '{"rxcui": "3498", "name": "Lisinopril", "tty": "IN"}\n'
    '{"rxcui": "485", "name": "Aspirin", "tty": "IN"}\n'
)


class TestLoadRxnormIndexLookup:
    def _make_cache(
        self, content: str = _SAMPLE_RXNORM_JSONL
    ) -> tuple[Path, tempfile.TemporaryDirectory]:  # type: ignore[type-arg]
        tmp = tempfile.TemporaryDirectory()
        cache_dir = Path(tmp.name)
        rxnorm_dir = cache_dir / "rxnorm"
        rxnorm_dir.mkdir()
        (rxnorm_dir / "rxnorm_concepts.jsonl").write_text(content, encoding="utf-8")
        return cache_dir, tmp

    def test_returns_empty_when_file_missing(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        with tempfile.TemporaryDirectory() as tmp:
            assert load_rxnorm_index(Path(tmp)) == {}

    def test_loads_ingredient_entries(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        cache_dir, tmp = self._make_cache()
        with tmp:
            index = load_rxnorm_index(cache_dir)
        assert index["metformin"] == "5521"
        assert index["lisinopril"] == "3498"
        assert index["aspirin"] == "485"

    def test_ingredient_takes_priority_over_scd(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        content = (
            '{"rxcui": "860975", "name": "Metformin", "tty": "SCD"}\n'
            '{"rxcui": "5521", "name": "Metformin", "tty": "IN"}\n'
        )
        cache_dir, tmp = self._make_cache(content)
        with tmp:
            index = load_rxnorm_index(cache_dir)
        assert index["metformin"] == "5521"  # IN wins over SCD via priority ordering

    def test_in_beats_scd_regardless_of_file_order(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        # IN appears after SCD in the file, but should still win via priority order
        content = (
            '{"rxcui": "111", "name": "Metformin 500 MG Tablet", "tty": "SCD"}\n'
            '{"rxcui": "5521", "name": "Metformin", "tty": "IN"}\n'
        )
        cache_dir, tmp = self._make_cache(content)
        with tmp:
            index = load_rxnorm_index(cache_dir)
        # "metformin" key only appears as IN entry; SCD key is different name
        assert index.get("metformin") == "5521"

    def test_scd_fills_names_not_in_in(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        content = (
            '{"rxcui": "860975", "name": "Metformin 500 MG Oral Tablet", "tty": "SCD"}\n'
            '{"rxcui": "5521", "name": "Metformin", "tty": "IN"}\n'
        )
        cache_dir, tmp = self._make_cache(content)
        with tmp:
            index = load_rxnorm_index(cache_dir)
        assert index["metformin"] == "5521"
        assert index["metformin 500 mg oral tablet"] == "860975"

    def test_skips_entries_missing_required_fields(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        content = (
            '{"rxcui": "", "name": "Orphan", "tty": "IN"}\n'
            '{"rxcui": "5521", "name": "", "tty": "IN"}\n'
            '{"rxcui": "5521", "name": "Metformin", "tty": "UNKNOWN"}\n'
        )
        cache_dir, tmp = self._make_cache(content)
        with tmp:
            index = load_rxnorm_index(cache_dir)
        assert index == {}

    def test_returns_empty_on_corrupt_file(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        cache_dir, tmp = self._make_cache("not json\n")
        with tmp:
            assert load_rxnorm_index(cache_dir) == {}

    def test_normalizes_punctuation_and_case(self) -> None:
        from famed.reference_cache import load_rxnorm_index

        content = '{"rxcui": "5521", "name": "Metformin HCl", "tty": "IN"}\n'
        cache_dir, tmp = self._make_cache(content)
        with tmp:
            index = load_rxnorm_index(cache_dir)
        assert "metformin hcl" in index


class TestLookupRxnormCui:
    def _make_index(self) -> dict[str, str]:
        from famed.reference_cache import load_rxnorm_index

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rxnorm_dir = cache_dir / "rxnorm"
            rxnorm_dir.mkdir()
            (rxnorm_dir / "rxnorm_concepts.jsonl").write_text(
                _SAMPLE_RXNORM_JSONL, encoding="utf-8"
            )
            return load_rxnorm_index(cache_dir)

    def test_exact_match(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        index = self._make_index()
        assert lookup_rxnorm_cui("Metformin", index) == "5521"

    def test_case_insensitive(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        index = self._make_index()
        assert lookup_rxnorm_cui("METFORMIN", index) == "5521"
        assert lookup_rxnorm_cui("metformin", index) == "5521"

    def test_punctuation_normalized(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        index = self._make_index()
        assert lookup_rxnorm_cui("Metformin 500 MG Oral Tablet", index) == "860975"

    def test_no_match_returns_empty_string(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        index = self._make_index()
        assert lookup_rxnorm_cui("Unobtainium", index) == ""

    def test_empty_index_returns_empty_string(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        assert lookup_rxnorm_cui("Metformin", {}) == ""

    def test_empty_name_returns_empty_string(self) -> None:
        from famed.reference_cache import lookup_rxnorm_cui

        index = self._make_index()
        assert lookup_rxnorm_cui("", index) == ""


@pytest.mark.parametrize(
    ("test_name", "expected_code"),
    [
        # Normalization drops "(corrected)"; QTc must not become uncorrected QT.
        ("Q-T INTERVAL (CORRECTED)", "8636-3"),
        ("QT Interval (Corrected)", "8636-3"),
        ("Q-T INTERVAL", "8634-8"),
        # A bare comment label names no analyte; never infer a code for it.
        ("Comment:,", ""),
        ("COMMENT", ""),
    ],
)
def test_lookup_loinc_reviewed_exact_names_and_non_analyte_labels(
    test_name: str, expected_code: str
) -> None:
    index = {"q t interval": "8634-8", "comment": "28816-7"}

    assert lookup_loinc_code(test_name, index, "ms") == expected_code


def test_thyroid_antibodies_resolve_to_method_neutral_codes(tmp_path: Path) -> None:
    """Lab names for TPO and thyroglobulin antibodies map to the general LOINC codes,
    not method-specific (RIA/IA) variants (user report 2023-10-08; ADR-034)."""
    loinc_dir = tmp_path / "loinc"
    loinc_dir.mkdir()
    concepts = [
        ("5382-7", "Thyroperoxidase Ab", "RIA"),
        ("8099-4", "Thyroperoxidase Ab", ""),
        ("56536-6", "Thyroglobulin Ab", "IA"),
        ("8098-6", "Thyroglobulin Ab", ""),
    ]
    (loinc_dir / "loinc_concepts.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "loinc_num": code,
                    "component": component,
                    "long_common_name": f"{component} [Units/volume] {method}".strip(),
                    "class": "SERO",
                    "system": "Ser/Plas",
                    "scale": "Qn",
                    "method": method,
                    "status": "ACTIVE",
                }
            )
            for code, component, method in concepts
        ),
        encoding="utf-8",
    )
    index = load_loinc_index(tmp_path)

    assert lookup_loinc_code("Thyroid Peroxidase (TPO) Ab", index, "IU/mL") == "8099-4"
    assert lookup_loinc_code("TPO Ab", index, "IU/mL") == "8099-4"
    assert lookup_loinc_code("Thyroid Peroxidase Antibody", index, "IU/mL") == "8099-4"
    assert lookup_loinc_code("Thyroglobulin Antibody", index, "IU/mL") == "8098-6"


def _write_concepts(tmp_path: Path, concepts: list[dict[str, str]]) -> Path:
    loinc_dir = tmp_path / "loinc"
    loinc_dir.mkdir()
    (loinc_dir / "loinc_concepts.jsonl").write_text(
        "\n".join(json.dumps(concept) for concept in concepts) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_lookup_loinc_selects_mass_or_molar_concept_by_unit(tmp_path: Path) -> None:
    """A synthetic analyte with mass and molar concepts resolves by the row's unit;
    the molar concept wins the name-only rank (ADR-034)."""
    common = {
        "component": "Analyte X",
        "long_common_name": "",
        "scale": "Qn",
        "status": "ACTIVE",
        "method": "",
    }
    cache_dir = _write_concepts(
        tmp_path,
        [
            {**common, "loinc_num": "9001-1", "property": "SCnc", "system": "Ser/Plas"},
            {**common, "loinc_num": "9002-2", "property": "MCnc", "system": "Ser"},
        ],
    )
    index = load_loinc_index(cache_dir)

    assert lookup_loinc_code("Analyte X", index, "mg/dL") == "9002-2"
    assert lookup_loinc_code("Analyte X", index, "mmol/L") == "9001-1"
    assert lookup_loinc_code("Analyte X", index, "") == "9001-1"


def test_lookup_loinc_leaves_row_uncoded_when_only_concept_contradicts_unit(
    tmp_path: Path,
) -> None:
    cache_dir = _write_concepts(
        tmp_path,
        [
            {
                "loinc_num": "9003-3",
                "component": "Analyte Y",
                "long_common_name": "",
                "property": "SCnc",
                "system": "Ser/Plas",
                "scale": "Qn",
                "status": "ACTIVE",
                "method": "",
            }
        ],
    )
    index = load_loinc_index(cache_dir)

    assert lookup_loinc_code("Analyte Y", index, "mg/dL") == ""
    assert lookup_loinc_code("Analyte Y", index, "nmol/L") == "9003-3"


@pytest.mark.parametrize("name", ["MPV", " mpv ", "Platelet mean volume"])
def test_mpv_femtoliters_uses_active_duplicate_replacement(name: str) -> None:
    index = {"mpv": "32711-4", "platelet mean volume": "32711-4"}

    assert lookup_loinc_code(name, index, "fL") == "32623-1"


def test_reviewed_overrides_outrank_unit_qualified_concepts(tmp_path: Path) -> None:
    """Audit-confirmed overrides (ADR-034) win over unit-qualified heuristics,
    including through synonyms."""
    cache_dir = _write_concepts(
        tmp_path,
        [
            {
                "loinc_num": "14635-7",
                "component": "Calcidiol",
                "long_common_name": "",
                "property": "SCnc",
                "system": "Ser/Plas",
                "scale": "Qn",
                "status": "ACTIVE",
                "method": "",
            },
            {
                "loinc_num": "1989-3",
                "component": "Calcidiol",
                "long_common_name": "",
                "property": "MCnc",
                "system": "Ser/Plas",
                "scale": "Qn",
                "status": "ACTIVE",
                "method": "",
            },
            {
                "loinc_num": "74434-2",
                "component": "Bilirubin total",
                "long_common_name": "",
                "property": "MFr",
                "system": "Calculus",
                "scale": "Qn",
                "status": "ACTIVE",
                "method": "Infrared spectroscopy",
            },
        ],
    )
    index = load_loinc_index(cache_dir)

    assert lookup_loinc_code("Vitamin D 25-hydroxy", index, "ng/mL") == "62292-8"
    assert lookup_loinc_code("Bilirubin, total", index, "mg/dL") == "1975-2"
    assert lookup_loinc_code("Calcidiol", index, "ng/mL") == "1989-3"
