#!/usr/bin/env python3
"""Fetch the application manifests the template generator reads, without a clone.

infra-ci never checks out the app submodules (#506): they are large and the CI only
needs a handful of small files from them. The git index records the exact commit each
submodule is pinned to, so the manifests are fetched from GitHub at that commit — the
same bytes a full checkout would give, a few kilobytes instead of a repository.

    uv run python tools/fetch_app_manifests.py          # fills any missing manifest
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GITHUB_OWNER = "wangzitian0"

Fetcher = Callable[[str], bytes]


def _github_raw(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def submodule_commit(root: Path, submodule: str) -> str:
    """The commit the git index pins ``repos/<name>`` to (works without a checkout)."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "HEAD", submodule],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if len(out) < 3 or out[1] != "commit":
        raise RuntimeError(f"{submodule} is not a submodule gitlink in HEAD")
    return out[2]


def fetch_missing(
    paths: list[str], *, root: Path = ROOT, fetch: Fetcher = _github_raw
) -> list[str]:
    """Download every ``repos/<sub>/<file>`` in ``paths`` that is absent; returns what was fetched."""
    fetched: list[str] = []
    for path in paths:
        target = root / path
        parts = Path(path).parts
        if len(parts) < 3 or parts[0] != "repos" or target.exists():
            continue
        submodule = f"{parts[0]}/{parts[1]}"
        sha = submodule_commit(root, submodule)
        url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{parts[1]}/{sha}/{'/'.join(parts[2:])}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fetch(url))
        fetched.append(f"{path} @ {sha[:7]}")
    return fetched


def main() -> int:
    sys.path.insert(
        0, str(ROOT)
    )  # runnable as a script and as `python -m tools.fetch_app_manifests`
    from tools.secrets_render import SERVICES

    paths = sorted(
        {p for service in SERVICES for p in service.manifests if p.startswith("repos/")}
    )
    for line in fetch_missing(paths):
        print(f"fetched {line}")
    print(f"app manifests present: {len(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
