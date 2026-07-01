"""CLI-level tests for tools/coverage_regression_audit.py (argv/exit-code wiring)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.coverage_regression_audit import main

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


def test_missing_coverage_xml_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--coverage-xml",
            str(tmp_path / "missing.xml"),
            "--baseline",
            str(tmp_path / "b.json"),
        ]
    )

    assert exit_code == 1
    assert "required coverage artifact is missing" in capsys.readouterr().err


def test_missing_baseline_without_update_flag_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing baseline means it was deleted/not checked out, not a fresh repo —
    docs/ssot/coverage-baseline.json is committed, so this must fail loud rather
    than silently disable the gate."""
    xml_path = _cobertura(tmp_path, covered=50, valid=100)

    exit_code = main(
        ["--coverage-xml", str(xml_path), "--baseline", str(tmp_path / "b.json")]
    )

    assert exit_code == 1
    assert "no baseline found" in capsys.readouterr().err


def test_update_baseline_establishes_baseline_when_none_exists(
    tmp_path: Path,
) -> None:
    xml_path = _cobertura(tmp_path, covered=50, valid=100)
    baseline_path = tmp_path / "b.json"

    exit_code = main(
        [
            "--coverage-xml",
            str(xml_path),
            "--baseline",
            str(baseline_path),
            "--update-baseline",
        ]
    )

    assert exit_code == 0
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["lines_covered"] == 50


def test_regression_against_baseline_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _cobertura(tmp_path, covered=40, valid=100)
    baseline_path = _baseline(tmp_path, covered=70, valid=100)

    exit_code = main(
        ["--coverage-xml", str(xml_path), "--baseline", str(baseline_path)]
    )

    assert exit_code == 1
    assert "regressed" in capsys.readouterr().out


def test_update_baseline_writes_new_floor(tmp_path: Path) -> None:
    xml_path = _cobertura(tmp_path, covered=80, valid=100)
    baseline_path = _baseline(tmp_path, covered=70, valid=100)

    exit_code = main(
        [
            "--coverage-xml",
            str(xml_path),
            "--baseline",
            str(baseline_path),
            "--update-baseline",
        ]
    )

    assert exit_code == 0
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["lines_covered"] == 80


def test_update_baseline_refuses_on_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _cobertura(tmp_path, covered=40, valid=100)
    baseline_path = _baseline(tmp_path, covered=70, valid=100)

    exit_code = main(
        [
            "--coverage-xml",
            str(xml_path),
            "--baseline",
            str(baseline_path),
            "--update-baseline",
        ]
    )

    assert exit_code == 1
    assert "refusing --update-baseline" in capsys.readouterr().err
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["lines_covered"] == 70
