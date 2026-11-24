#!/usr/bin/env python3
"""Run the imports of each infra2-owned App entrypoint inside the pinned App image (#1823).

Logic and docs: `common/deploy/app_image_imports.py`.

    python tools/app_image_imports.py [--runtime docker|podman] [--platform linux/amd64]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.app_image_imports import ENTRYPOINT_IMAGES, check, release_tag  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", default="docker", choices=("docker", "podman"))
    parser.add_argument("--platform", default=None, help="for example linux/amd64")
    args = parser.parse_args(argv)
    problems: list[str] = []
    for target in ENTRYPOINT_IMAGES:
        tag = release_tag(target.repo_id)
        script_text = (ROOT / target.script).read_text(encoding="utf-8")
        found = check(target, tag, script_text, args.runtime, args.platform)
        print(f"{target.image}:{tag} {target.script}: {'FAIL' if found else 'ok'}")
        problems += found
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
