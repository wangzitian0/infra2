#!/usr/bin/env python3
"""Fail CI when a platform compose file pins an image to a bare ``:latest`` tag.

A bare ``:latest`` drifts silently upstream and is irreproducible — the exact
class of bug behind the prefect lockup (#253/#255). Pin a digest
(``image: repo:tag@sha256:...``) or, at minimum, a specific version.

Usage: python tools/lint_platform_image_pins.py [glob ...]
Default scans platform/*/compose.yaml.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from libs.image_pins import bare_latest_violations  # noqa: E402

DEFAULT_GLOBS = ["platform/*/compose.yaml"]


def main(argv: list[str]) -> int:
    patterns = argv or DEFAULT_GLOBS
    failures: list[tuple[str, str]] = []
    for pattern in patterns:
        for path in sorted(glob.glob(str(REPO_ROOT / pattern))):
            for ref in bare_latest_violations(Path(path).read_text(encoding="utf-8")):
                rel = Path(path).relative_to(REPO_ROOT)
                failures.append((str(rel), ref))

    if failures:
        print("❌ Bare ':latest' image tags are not allowed (pin a digest):")
        for rel, ref in failures:
            print(f"   {rel}: image: {ref}")
        print(
            "\nPin with `image: <repo>:<tag>@sha256:<digest>` "
            "(get it via `docker inspect <image> --format '{{index .RepoDigests 0}}'`)."
        )
        return 1

    print("✅ No bare ':latest' platform image tags.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
