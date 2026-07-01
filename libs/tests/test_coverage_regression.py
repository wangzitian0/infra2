"""Tests for the line-coverage no-regression gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from libs.coverage_regression import (
    CoverageArtifactMissing,
    CoverageBaselineInvalid,
    check_no_regression,
    load_baseline,
    read_coverage_summary,
    write_baseline,
)

_COBERTURA_TEMPLATE = (
    '<?xml version="1.0" ?>\n'
    '<coverage lines-valid="{valid}" lines-covered="{covered}" line-rate="0" '
    'branches-covered="0" branches-valid="0" branch-rate="0" complexity="0">\n'
    "  <packages/>\n"
    "</coverage>\n"
)


def _write_cobertura(path: Path, *, covered: int, valid: int) -> None:
    path.write_text(
        _COBERTURA_TEMPLATE.format(covered=covered, valid=valid), encoding="utf-8"
    )


class TestReadCoverageSummary:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CoverageArtifactMissing):
            read_coverage_summary(tmp_path / "does-not-exist.xml")

    def test_parses_valid_cobertura(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "coverage.xml"
        _write_cobertura(xml_path, covered=5281, valid=7486)

        summary = read_coverage_summary(xml_path)

        assert summary["lines_covered"] == 5281
        assert summary["lines_valid"] == 7486
        assert summary["line_rate"] == pytest.approx(5281 / 7486)

    def test_malformed_xml_raises(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "coverage.xml"
        xml_path.write_text("not xml at all", encoding="utf-8")

        with pytest.raises(CoverageArtifactMissing):
            read_coverage_summary(xml_path)

    def test_missing_required_attribute_raises(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "coverage.xml"
        xml_path.write_text('<coverage lines-valid="10"></coverage>', encoding="utf-8")

        with pytest.raises(CoverageArtifactMissing):
            read_coverage_summary(xml_path)


class TestLoadBaseline:
    def test_missing_baseline_returns_none(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path / "coverage-baseline.json") is None

    def test_loads_valid_baseline(self, tmp_path: Path) -> None:
        baseline_path = tmp_path / "coverage-baseline.json"
        baseline_path.write_text(
            json.dumps(
                {"line_rate": 0.7055, "lines_covered": 5281, "lines_valid": 7486}
            ),
            encoding="utf-8",
        )

        baseline = load_baseline(baseline_path)

        assert baseline == {
            "line_rate": 0.7055,
            "lines_covered": 5281,
            "lines_valid": 7486,
        }

    def test_malformed_baseline_raises(self, tmp_path: Path) -> None:
        baseline_path = tmp_path / "coverage-baseline.json"
        baseline_path.write_text("{not json", encoding="utf-8")

        with pytest.raises(CoverageBaselineInvalid):
            load_baseline(baseline_path)

    def test_baseline_missing_field_raises(self, tmp_path: Path) -> None:
        baseline_path = tmp_path / "coverage-baseline.json"
        baseline_path.write_text(json.dumps({"line_rate": 0.5}), encoding="utf-8")

        with pytest.raises(CoverageBaselineInvalid):
            load_baseline(baseline_path)


class TestWriteBaseline:
    def test_writes_reloadable_baseline(self, tmp_path: Path) -> None:
        baseline_path = tmp_path / "nested" / "coverage-baseline.json"
        summary = {"line_rate": 5281 / 7486, "lines_covered": 5281, "lines_valid": 7486}

        write_baseline(baseline_path, summary)

        assert load_baseline(baseline_path) == pytest.approx(
            {"line_rate": 5281 / 7486, "lines_covered": 5281, "lines_valid": 7486}
        )


class TestCheckNoRegression:
    def test_no_baseline_is_ok(self) -> None:
        current = {"line_rate": 0.5, "lines_covered": 50, "lines_valid": 100}

        ok, message = check_no_regression(current, None)

        assert ok is True
        assert "no committed baseline" in message

    def test_equal_rate_is_ok(self) -> None:
        current = {"line_rate": 0.70, "lines_covered": 70, "lines_valid": 100}
        baseline = {"line_rate": 0.70, "lines_covered": 70, "lines_valid": 100}

        ok, _ = check_no_regression(current, baseline)

        assert ok is True

    def test_improved_rate_is_ok(self) -> None:
        current = {"line_rate": 0.75, "lines_covered": 75, "lines_valid": 100}
        baseline = {"line_rate": 0.70, "lines_covered": 70, "lines_valid": 100}

        ok, _ = check_no_regression(current, baseline)

        assert ok is True

    def test_regressed_rate_fails_with_actionable_message(self) -> None:
        current = {"line_rate": 0.65, "lines_covered": 65, "lines_valid": 100}
        baseline = {"line_rate": 0.70, "lines_covered": 70, "lines_valid": 100}

        ok, message = check_no_regression(current, baseline)

        assert ok is False
        assert "regressed" in message
        assert "--update-baseline" in message

    def test_tiny_float_noise_does_not_fail(self) -> None:
        current = {"line_rate": 0.699999995, "lines_covered": 70, "lines_valid": 100}
        baseline = {"line_rate": 0.70, "lines_covered": 70, "lines_valid": 100}

        ok, _ = check_no_regression(current, baseline)

        assert ok is True
