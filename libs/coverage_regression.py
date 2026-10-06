"""Backward-compatibility shim — the implementation lives in `libs.gate.coverage_regression`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.gate.coverage_regression import (
    CoverageArtifactMissing,
    CoverageBaselineInvalid,
    EPSILON,
    check_no_regression,
    load_baseline,
    read_coverage_summary,
    write_baseline,
)

__all__ = [
    "CoverageArtifactMissing",
    "CoverageBaselineInvalid",
    "EPSILON",
    "check_no_regression",
    "load_baseline",
    "read_coverage_summary",
    "write_baseline",
]
