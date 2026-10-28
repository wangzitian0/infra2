"""CLI entrypoint for 8-class dry-run recall drill.

Simulates all 8 watchdog failure classes and verifies pager layers and alert fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.recall_drill import run_recall_drill  # noqa: E402


def main() -> int:
    """Execute 8-class dry-run recall drill and print structured report."""
    parser = argparse.ArgumentParser(
        description="Run deterministic dry-run recall drill for all 8 failure classes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run drill in dry-run mode (default: True).",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_false",
        dest="dry_run",
        help="Disable dry-run mode (refused to protect ambient services).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output full report in JSON format.",
    )
    args = parser.parse_args()

    try:
        report = run_recall_drill(dry_run=args.dry_run)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    else:
        passed = sum(1 for r in report.results.values() if r.get("status") == "pass")
        print(
            f"Recall drill: {report.status} "
            f"({passed}/{report.total_classes} classes verified)"
        )
        for class_name, res in sorted(report.results.items()):
            print(f"  [{res['status']}] {class_name} -> pager: {res['actual_layer']}")
    return 0 if report.status == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
