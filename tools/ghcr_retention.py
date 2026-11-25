#!/usr/bin/env python3
"""Plan, and with --apply run, the deletion of old GHCR App image versions.

Logic and policy: `common/deploy/ghcr_retention.py`.

    GH_TOKEN=... python tools/ghcr_retention.py [--package NAME] [--apply]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.ghcr_retention import (  # noqa: E402
    KEEP_DAYS,
    PACKAGES,
    Ghcr,
    apply_plan,
    build_plan,
    token_from_env,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--package", action="append", choices=PACKAGES, help="default: every package"
    )
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete; without it the tool only prints the plan",
    )
    args = parser.parse_args(argv)
    ghcr = Ghcr(token_from_env())
    now = datetime.now(UTC)
    total = 0
    for package in args.package or PACKAGES:
        plan = build_plan(ghcr, package, now, args.keep_days)
        oldest = min((v.created_at for v in plan.delete), default=None)
        newest = max((v.created_at for v in plan.delete), default=None)
        print(
            f"{package}: keep {len(plan.keep)}, delete {len(plan.delete)}"
            + (
                f" (created {oldest:%Y-%m-%d} to {newest:%Y-%m-%d} UTC)"
                if oldest and newest
                else ""
            )
        )
        total += len(plan.delete)
        if args.apply:
            deleted = apply_plan(plan, ghcr.delete)
            print(f"{package}: deleted {deleted} of {len(plan.delete)}")
            if deleted < len(plan.delete):
                print(
                    "stopped at the API call reserve; run again after the hourly reset"
                )
                return 3
    print(f"total {'deleted' if args.apply else 'to delete'}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
