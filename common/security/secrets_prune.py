#!/usr/bin/env python3
"""Remove orphan keys from each service Vault path (store hygiene, #649)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.security.registry import SERVICES  # noqa: E402
from common.security.prune import (  # noqa: E402
    RESERVED_PREFIXES,
    PrunePlan,
    PruneReport,
    plan_for,
    prune,
)

__all__ = [
    "RESERVED_PREFIXES",
    "SERVICES",
    "PrunePlan",
    "PruneReport",
    "plan_for",
    "prune",
]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for secrets prune."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", help="one service id, e.g. platform/alerting")
    parser.add_argument("--env", help="one environment, e.g. staging")
    parser.add_argument("--apply", action="store_true", help="rewrite the documents")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    services = tuple(SERVICES)
    if args.service:
        services = tuple(s for s in services if s.id == args.service)
        if not services:
            parser.error(f"unknown service {args.service!r}")
    environments = (args.env,) if args.env else None
    report = prune(services=services, environments=environments, apply=args.apply)
    print(json.dumps(report.to_dict(), indent=1) if args.json else report.render())
    failed = [plan for plan in report.plans if plan.error]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
