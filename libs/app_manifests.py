"""Application manifests without an app checkout.

Neither infra-ci (#506) nor the iac-runner checks out the app submodules: they are large
and only a few small files are needed from them. The git index records the exact commit
each submodule is pinned to, so those files are fetched from GitHub at that commit — the
same bytes a full checkout would give — into ``.cache/app-manifests/<path>`` (never under
``repos/<sub>/``: writing inside an un-checked-out submodule path makes every later
``git diff`` fail with "Could not access submodule").
"""

from __future__ import annotations

import subprocess
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GITHUB_OWNER = "wangzitian0"
CACHE_DIR = ".cache/app-manifests"

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


def cached_path(path: str, *, root: Path = ROOT) -> Path:
    return root / CACHE_DIR / path


def manifest_file(path: str, *, root: Path = ROOT) -> Path:
    """The checkout when present, else the cache location for a ``repos/`` path."""
    candidate = root / path
    if candidate.exists() or not path.startswith("repos/"):
        return candidate
    return cached_path(path, root=root)


def fetch_missing(
    paths: Iterable[str], *, root: Path = ROOT, fetch: Fetcher = _github_raw
) -> list[str]:
    """Download every ``repos/<sub>/<file>`` in ``paths`` that neither the checkout nor
    the cache holds; returns ``"<path> @ <sha7>"`` for each file fetched."""
    fetched: list[str] = []
    for path in paths:
        parts = Path(path).parts
        if len(parts) < 3 or parts[0] != "repos" or (root / path).exists():
            continue
        target = cached_path(path, root=root)
        if target.exists():
            continue
        sha = submodule_commit(root, f"{parts[0]}/{parts[1]}")
        url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{parts[1]}/{sha}/{'/'.join(parts[2:])}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fetch(url))
        fetched.append(f"{path} @ {sha[:7]}")
    return fetched


def ensure_present(
    paths: Iterable[str], *, root: Path = ROOT, fetch: Fetcher = _github_raw
) -> None:
    """Make every manifest readable through ``manifest_file`` — fetching on demand, so the
    iac-runner (no submodules) resolves app manifests at deploy time exactly as CI does."""
    fetch_missing(paths, root=root, fetch=fetch)
