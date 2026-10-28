#!/usr/bin/env python3
"""Guard against vendored App environment manifests in infra2 (#1320).

App environment manifests are fetched from the deployed release commit at deploy
time to render the Vault Agent template. Vendored copies under ``contracts/`` and
``contracts/apps.lock.json`` are forbidden.

    uv run python tools/fetch_app_manifests.py                 # offline guard check (CI)
"""

from __future__ import annotations

import argparse
import http.client
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from common.security.registry import SERVICES

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = Path("contracts")
LOCK = "contracts/apps.lock.json"


def check(root: Path = ROOT) -> list[str]:
    """Return one problem string per vendored App manifest or forbidden registry path."""
    problems: list[str] = []
    contracts_dir = root / CONTRACTS
    if contracts_dir.is_symlink():
        problems.append("contracts: symlink is forbidden (#1320)")
    elif contracts_dir.exists():
        for path in sorted(contracts_dir.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                problems.append(f"{rel}: symlink is forbidden (#1320)")
            elif path.is_file() and path.suffix == ".json":
                problems.append(
                    f"{rel}: vendored app manifest is forbidden (#1320); "
                    "render Vault templates from the deployed release manifest"
                )
    for service in SERVICES:
        for manifest_path in service.manifests:
            if manifest_path.startswith(("contracts/", "repos/")):
                problems.append(
                    f"{service.id}: {manifest_path} references forbidden path (#1320)"
                )
            target = root / manifest_path
            if target.is_symlink():
                problems.append(f"{manifest_path}: symlink is forbidden (#1320)")
    return problems


def http_get(url, retries=2, sleep=time.sleep, opener=urllib.request.urlopen) -> bytes:
    """GET ``url``. Retry a connection error, 5xx, 408, 429 (#824). Other errors fail at once."""
    for attempt in range(retries + 1):
        try:
            with opener(url, timeout=30) as response:
                return response.read()
        except (OSError, http.client.HTTPException) as exc:
            code = exc.code if isinstance(exc, urllib.error.HTTPError) else 0
            cert = isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError)
            permanent = cert or (400 <= code < 500 and code not in (408, 429))
            if attempt == retries or permanent:
                raise SystemExit(f"fetching {url} failed: {exc}") from exc
            sleep(2 ** (attempt + 1))


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Run the offline guard.")
    parser.add_argument("--sync", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.sync is not None:
        print(
            "vendoring app manifests in contracts/ was removed in #1320; "
            "Vault templates render from the deployed release manifest",
            file=sys.stderr,
        )
        return 2
    errors = check(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("app manifest guard: ok (no vendored app manifests in contracts/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
