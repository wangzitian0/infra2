#!/usr/bin/env python3
"""Fetch the application manifests the registry reads, without a clone (see libs/app_manifests.py).

uv run python tools/fetch_app_manifests.py          # fills any missing manifest into .cache/
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(
    0, str(ROOT)
)  # runnable as a script and as `python -m tools.fetch_app_manifests`

from libs.app_manifests import CACHE_DIR, fetch_missing  # noqa: E402  (re-exported for tests)

__all__ = ["CACHE_DIR", "fetch_missing", "main"]


def main() -> int:
    from libs.secrets_registry import SERVICES

    paths = sorted(
        {p for service in SERVICES for p in service.manifests if p.startswith("repos/")}
    )
    for line in fetch_missing(paths):
        print(f"fetched {line}")
    print(f"app manifests present: {len(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
