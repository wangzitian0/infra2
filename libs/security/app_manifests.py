"""Application manifests without an app checkout.

Neither infra-ci (#506) nor the iac-runner checks out the app submodules: they are large
and only a few small files are needed from them. The git index records the exact commit
each submodule is pinned to, so those files are fetched from GitHub at that commit — the
same bytes a full checkout would give — into ``.cache/app-manifests/<path>`` (never under
``repos/<sub>/``: writing inside an un-checked-out submodule path makes every later
``git diff`` fail with "Could not access submodule").
"""

from __future__ import annotations

import http.client
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

from libs.core.constants import REPO_ROOT

ROOT = REPO_ROOT
GITHUB_OWNER = "wangzitian0"
CACHE_DIR = ".cache/app-manifests"

Fetcher = Callable[[str], bytes]


# #824: one "Connection reset by peer" on raw.githubusercontent.com failed a required
# check 1.3 s in, before any test ran. Same blip class as #810 / #813 (see
# ``libs.security.supply.retrying_transport``), one layer up: reuse its budget
# (2 retries, ``2**attempt`` seconds) rather than inventing a second backoff policy.
# This module cannot import ``supply``: that module needs infra2-sdk, and the manifest
# prefetch runs before any dependency beyond the standard library is guaranteed (#1016).
_TRANSIENT_RETRIES = 2
_TRANSIENT_HTTP_STATUS = frozenset({408, 429})  # plus every 5xx, see below
_TRANSIENT_TYPES = (
    ConnectionError,  # reset, aborted, refused, broken pipe (RemoteDisconnected too)
    TimeoutError,
    socket.gaierror,  # a DNS blip
    ssl.SSLError,  # TLS EOF; a certificate failure is excluded in the classifier
    http.client.HTTPException,  # IncompleteRead, BadStatusLine: the body died mid-way
)


class ManifestFetchError(RuntimeError):
    """A transient blip survived the #824 retry budget. The cause is chained."""


def _is_transient_fetch_error(exc: BaseException) -> bool:
    """A blip that a second request can fix: 5xx, 408, 429, a reset or timed-out
    connection, a TLS EOF. A 404 (the pinned commit lacks the file), any other 4xx, a
    certificate failure, a malformed URL, and a programming error never are."""
    if isinstance(exc, urllib.error.HTTPError):  # before URLError: it is a subclass
        return exc.code >= 500 or exc.code in _TRANSIENT_HTTP_STATUS
    if isinstance(exc, urllib.error.URLError):
        # ``urlopen`` wraps an ``OSError`` in ``reason``; a text reason is a usage error
        return isinstance(exc.reason, BaseException) and _is_transient_fetch_error(
            exc.reason
        )
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    return isinstance(exc, _TRANSIENT_TYPES)


def _github_raw(
    url: str,
    *,
    _retries: int = _TRANSIENT_RETRIES,
    _sleep: Callable[[float], None] | None = None,
) -> bytes:
    """GET ``url`` with a bounded retry for transient transport errors only (#824).

    The retry unit is the whole request-and-read: a connection can die in ``read()``
    after ``urlopen`` returned. A GET is idempotent, so a blind retry is safe. A
    permanent error (404, 4xx, bad certificate) propagates on the first attempt.
    """
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                return response.read()
        except Exception as exc:  # noqa: BLE001 - reclassified below, or re-raised as-is
            if not _is_transient_fetch_error(exc):
                raise
            if attempt >= _retries:
                attempts = attempt + 1
                raise ManifestFetchError(
                    f"fetching {url} failed after {attempts} "
                    f"attempt{'s' if attempts != 1 else ''}: {exc}"
                ) from exc
            attempt += 1
            print(
                f"transient error fetching {url} ({exc}); "
                f"retry {attempt}/{_retries} in {2**attempt}s",
                file=sys.stderr,
            )
            (_sleep or time.sleep)(2**attempt)


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
