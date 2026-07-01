#!/usr/bin/env python3
"""Guard libs/tools line coverage against silent regression.

Compares the Cobertura XML from `pytest --cov=libs --cov=tools
--cov-report=xml:coverage/infra2-coverage.xml` against the committed baseline
(docs/ssot/coverage-baseline.json). Exits non-zero on a drop, so it serves as
a PR gate (infra-ci) run on every push/PR — independent of the Coveralls
upload, which stays main-only and non-blocking per docs/ssot/ops.pipeline.md.

Use --update-baseline to record a new floor after a deliberate, reviewed
coverage improvement (never to silently paper over a regression).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.coverage_regression import (  # noqa: E402
    CoverageArtifactMissing,
    CoverageBaselineInvalid,
    check_no_regression,
    load_baseline,
    read_coverage_summary,
    write_baseline,
)

DEFAULT_COVERAGE_XML = ROOT / "coverage" / "infra2-coverage.xml"
DEFAULT_BASELINE = ROOT / "docs" / "ssot" / "coverage-baseline.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-xml", type=Path, default=DEFAULT_COVERAGE_XML)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Record the current coverage as the new baseline (reviewed improvements only).",
    )
    args = parser.parse_args(argv)

    try:
        current = read_coverage_summary(args.coverage_xml)
        baseline = load_baseline(args.baseline)
    except (CoverageArtifactMissing, CoverageBaselineInvalid) as exc:
        print(f"coverage_regression_audit: {exc}", file=sys.stderr)
        return 1

    ok, message = check_no_regression(current, baseline)
    print(f"coverage_regression_audit: {message}")

    if args.update_baseline:
        if baseline is not None and current["line_rate"] + 1e-6 < baseline["line_rate"]:
            print(
                "coverage_regression_audit: refusing --update-baseline on a regression; "
                "fix coverage first",
                file=sys.stderr,
            )
            return 1
        write_baseline(args.baseline, current)
        print(f"coverage_regression_audit: baseline written to {args.baseline}")
        return 0

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
