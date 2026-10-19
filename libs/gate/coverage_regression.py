"""Line-coverage no-regression check against a committed baseline.

Canonical implementation lives in ``infra2_sdk.ci`` (#1270).
"""

from __future__ import annotations

from infra2_sdk.ci import (
    EPSILON,
    CoverageArtifactMissing,
    CoverageBaselineInvalid,
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

