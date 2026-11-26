"""Tests for the line-coverage no-regression gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.core.ci_contract import (
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


def test_read_coverage_summary_parses_cobertura_and_rejects_bad_input(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "coverage.xml"
    _write_cobertura(xml_path, covered=5281, valid=7486)
    summary = read_coverage_summary(xml_path)
    assert summary["lines_covered"] == 5281
    assert summary["lines_valid"] == 7486
    assert summary["line_rate"] == pytest.approx(5281 / 7486)
    with pytest.raises(CoverageArtifactMissing):
        read_coverage_summary(tmp_path / "does-not-exist.xml")
    for text in ("not xml at all", '<coverage lines-valid="10"></coverage>'):
        xml_path.write_text(text, encoding="utf-8")
        with pytest.raises(CoverageArtifactMissing):
            read_coverage_summary(xml_path)


def test_baseline_round_trips_and_rejects_bad_input(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "coverage-baseline.json"
    assert load_baseline(path) is None
    summary = {"line_rate": 5281 / 7486, "lines_covered": 5281, "lines_valid": 7486}
    write_baseline(path, summary)
    assert load_baseline(path) == pytest.approx(summary)
    for text in ("{not json", json.dumps({"line_rate": 0.5})):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(CoverageBaselineInvalid):
            load_baseline(path)


def _summary(rate: float, covered: int) -> dict:
    return {"line_rate": rate, "lines_covered": covered, "lines_valid": 100}


def test_check_no_regression_fails_only_on_a_real_drop() -> None:
    ok, message = check_no_regression(_summary(0.5, 50), None)
    assert ok is True and "no committed baseline" in message
    baseline = _summary(0.70, 70)
    # equal, better, and float noise pass
    for current in (_summary(0.70, 70), _summary(0.75, 75), _summary(0.699999995, 70)):
        assert check_no_regression(current, baseline)[0] is True, current
    ok, message = check_no_regression(_summary(0.65, 65), baseline)
    assert ok is False
    assert "regressed" in message
    assert "65.00%" in message and "70.00%" in message
