#!/usr/bin/env python3
"""CLI of the production promotion guard (common/deploy/promotion_guard.py).

``python -m tools.promotion_soak_guard --after v1.2.3 --before v1.2.0 --release v1.2.1
--release v1.2.3 --require finance_report/app=v0.2.61`` runs ``require_upstream_post`` and
exits nonzero on any refusal. The release train does not call this entry: its `pre` job
calls the same function through ``tools/reconcile_iac_inputs.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.promotion_guard import require_upstream_post  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after", required=True, help="release tag to promote")
    parser.add_argument("--before", default="", help="last production release tag")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--release", action="append", default=[], help="release tag since the marker"
    )
    parser.add_argument(
        "--require", action="append", default=[], help="SERVICE=TAG of an App release"
    )
    args = parser.parse_args(argv)
    required = dict(item.split("=", 1) for item in args.require)
    try:
        require_upstream_post(
            args.after,
            Path(args.repo_root),
            before=args.before,
            required=required,
            releases=lambda *_: list(args.release),
        )
    except SystemExit as refused:
        print(refused, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
