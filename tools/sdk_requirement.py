#!/usr/bin/env python3
"""Print the pip requirement for the infra2-sdk wheel that uv.lock pins (#1115).

uv.lock is the only pin. pyproject.toml names the release wheel URL, and `uv lock`
records that URL with the wheel's sha256. Every install outside uv (the image
builds, the deploy workflows) reads the requirement here. pip then verifies the
same hash that CI verifies, and a release bump edits pyproject.toml and uv.lock
only.

Usage: python tools/sdk_requirement.py [LOCK]

Stdlib only: the image builds run it before pip installs anything.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

PACKAGE = "infra2-sdk"


def sdk_requirement(lock: Path) -> str:
    """`infra2-sdk @ <wheel url>#sha256=<digest>` from the lock.

    Raises ValueError when the lock does not pin exactly one direct wheel with a
    sha256 hash.
    """
    packages = [
        package
        for package in tomllib.loads(lock.read_text(encoding="utf-8")).get(
            "package", []
        )
        if package.get("name") == PACKAGE
    ]
    if len(packages) != 1:
        raise ValueError(
            f"{lock}: expected one {PACKAGE} package, found {len(packages)}"
        )
    (package,) = packages
    url = package.get("source", {}).get("url")
    wheels = package.get("wheels", [])
    if not url or len(wheels) != 1 or wheels[0].get("url") != url:
        raise ValueError(f"{lock}: {PACKAGE} is not locked to one direct wheel URL")
    algorithm, _, digest = str(wheels[0].get("hash", "")).partition(":")
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{lock}: the {PACKAGE} wheel has no sha256 hash")
    return f"{PACKAGE} @ {url}#sha256={digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lock", nargs="?", default="uv.lock", type=Path)
    args = parser.parse_args(argv)
    try:
        print(sdk_requirement(args.lock))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"sdk_requirement: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
