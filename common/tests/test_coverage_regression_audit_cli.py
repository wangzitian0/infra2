"""CLI-level tests for tools/coverage_regression_audit.py (argv/exit-code wiring)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.core.coverage_regression_audit import main

_COBERTURA = (
    '<?xml version="1.0" ?>\n'
    '<coverage lines-valid="{valid}" lines-covered="{covered}" line-rate="0">\n'
    "  <packages/>\n"
    "</coverage>\n"
)


def _cobertura(tmp_path: Path, *, covered: int, valid: int) -> Path:
    xml_path = tmp_path / "coverage.xml"
    xml_path.write_text(
        _COBERTURA.format(covered=covered, valid=valid), encoding="utf-8"
    )
    return xml_path


def _baseline(tmp_path: Path, *, covered: int, valid: int) -> Path:
    baseline_path = tmp_path / "coverage-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "line_rate": covered / valid,
                "lines_covered": covered,
                "lines_valid": valid,
            }
        ),
        encoding="utf-8",
    )
    return baseline_path


def _argv(xml_path: Path, baseline_path: Path, *extra: str) -> list[str]:
    return ["--coverage-xml", str(xml_path), "--baseline", str(baseline_path), *extra]


def _lines_covered(path: Path) -> int:
    return json.loads(path.read_text(encoding="utf-8"))["lines_covered"]


def test_a_missing_input_or_a_regression_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path / "missing.xml", tmp_path / "b.json")) == 1
    assert "required coverage artifact is missing" in capsys.readouterr().err
    # A missing baseline means it was deleted or not checked out, not a fresh repo:
    # docs/ssot/coverage-baseline.json is committed, so this must fail loud.
    xml_path = _cobertura(tmp_path, covered=50, valid=100)
    assert main(_argv(xml_path, tmp_path / "b.json")) == 1
    assert "no baseline found" in capsys.readouterr().err
    xml_path = _cobertura(tmp_path, covered=40, valid=100)
    baseline_path = _baseline(tmp_path, covered=70, valid=100)
    assert main(_argv(xml_path, baseline_path)) == 1
    assert "regressed" in capsys.readouterr().out


def test_update_baseline_sets_the_floor_and_refuses_a_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _cobertura(tmp_path, covered=50, valid=100)
    fresh = tmp_path / "fresh.json"
    assert main(_argv(xml_path, fresh, "--update-baseline")) == 0
    assert _lines_covered(fresh) == 50
    baseline_path = _baseline(tmp_path, covered=70, valid=100)
    xml_path = _cobertura(tmp_path, covered=80, valid=100)
    assert main(_argv(xml_path, baseline_path, "--update-baseline")) == 0
    assert _lines_covered(baseline_path) == 80
    xml_path = _cobertura(tmp_path, covered=40, valid=100)
    assert main(_argv(xml_path, baseline_path, "--update-baseline")) == 1
    assert "refusing --update-baseline" in capsys.readouterr().err
    assert _lines_covered(baseline_path) == 80
