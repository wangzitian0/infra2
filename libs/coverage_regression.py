"""Line-coverage no-regression check against a committed baseline.

Reads the Cobertura XML produced by `pytest --cov=libs --cov=tools
--cov-report=xml:...` and compares the aggregate line rate against
docs/ssot/coverage-baseline.json. infra2 is a single Python tree, so one
Cobertura file vs. one baseline is enough here — no LCOV merge across
components like the app repo's unified coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

# Absolute per-run float noise is near-zero for a deterministic suite; this
# only absorbs rounding, not a real drop.
EPSILON = 1e-6


class CoverageArtifactMissing(RuntimeError):
    """The expected Cobertura XML does not exist or fails to parse."""


class CoverageBaselineInvalid(RuntimeError):
    """The committed baseline file exists but is not well-formed."""


def read_coverage_summary(xml_path: Path) -> dict[str, float | int]:
    """Extract aggregate line coverage from a Cobertura XML file's root element."""
    if not xml_path.is_file():
        raise CoverageArtifactMissing(
            f"required coverage artifact is missing (expected {xml_path}); "
            "run `pytest --cov=libs --cov=tools --cov-report=xml:...` first"
        )
    try:
        root = ElementTree.parse(xml_path).getroot()
        lines_valid = int(root.attrib["lines-valid"])
        lines_covered = int(root.attrib["lines-covered"])
    except (ElementTree.ParseError, KeyError, ValueError) as exc:
        raise CoverageArtifactMissing(
            f"{xml_path} is not a valid Cobertura report: {exc}"
        ) from exc
    line_rate = lines_covered / lines_valid if lines_valid else 0.0
    return {
        "lines_valid": lines_valid,
        "lines_covered": lines_covered,
        "line_rate": line_rate,
    }


def load_baseline(baseline_path: Path) -> dict[str, float | int] | None:
    """Load the committed baseline, or None if it does not exist yet (first run)."""
    if not baseline_path.is_file():
        return None
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        return {
            "lines_valid": int(data["lines_valid"]),
            "lines_covered": int(data["lines_covered"]),
            "line_rate": float(data["line_rate"]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CoverageBaselineInvalid(
            f"{baseline_path} is not a valid baseline: {exc}"
        ) from exc


def write_baseline(baseline_path: Path, summary: dict[str, float | int]) -> None:
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "line_rate": round(summary["line_rate"], 6),
                "lines_covered": summary["lines_covered"],
                "lines_valid": summary["lines_valid"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def check_no_regression(
    current: dict[str, float | int], baseline: dict[str, float | int] | None
) -> tuple[bool, str]:
    """Return (ok, message). No baseline yet is always ok (first run establishes it)."""
    current_pct = current["line_rate"] * 100
    if baseline is None:
        return (
            True,
            f"no committed baseline yet; current line coverage {current_pct:.2f}%",
        )
    baseline_pct = baseline["line_rate"] * 100
    if current["line_rate"] + EPSILON < baseline["line_rate"]:
        return False, (
            f"line coverage regressed: {current_pct:.2f}% "
            f"(covered {current['lines_covered']}/{current['lines_valid']}) < "
            f"baseline {baseline_pct:.2f}% "
            f"(covered {baseline['lines_covered']}/{baseline['lines_valid']}). "
            "Add tests to recover the floor, or if this drop is deliberate and "
            "reviewed, re-run with --update-baseline."
        )
    return True, f"line coverage {current_pct:.2f}% >= baseline {baseline_pct:.2f}%"
