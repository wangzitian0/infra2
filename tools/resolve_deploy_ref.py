#!/usr/bin/env python3
"""CLI front door for commit-addressed app ref resolution (deploy_v2 ``version_ref``).

    main          -> finance_report main branch HEAD
    vX.Y.Z        -> that tag
    <sha>         -> itself (used verbatim)

The resolution logic is library code and lives in :mod:`common.deploy.refs` (#955: common/
must not import tools/).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.refs import (  # noqa: E402
    FINANCE_REPORT_REPO,
    resolve_to_sha,
)

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="main | vX.Y.Z | <sha>")
    parser.add_argument("--repo", default=FINANCE_REPORT_REPO)
    args = parser.parse_args(argv)
    try:
        print(resolve_to_sha(args.ref, repo=args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
