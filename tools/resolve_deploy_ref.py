#!/usr/bin/env python3
"""CLI front door for commit-addressed app ref resolution (deploy_v2 ``version_ref``).

    main          -> finance_report main branch HEAD
    vX.Y.Z        -> that tag
    <sha>         -> itself (used verbatim)

The resolution logic is library code and lives in :mod:`libs.deploy.refs` (#955: libs/
must not import tools/). This module is only the CLI plus a re-export of the public
names that ``tools/deploy_v2.py`` and the tests still import from here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.deploy.refs import (  # noqa: E402
    FINANCE_REPORT_REPO,
    CommandRunner,
    ResolvedRef,
    classify_ref,
    resolve_branch_to_sha,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)

__all__ = [
    "CommandRunner",
    "FINANCE_REPORT_REPO",
    "ResolvedRef",
    "classify_ref",
    "main",
    "resolve_branch_to_sha",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
]


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
