#!/usr/bin/env python3
"""Read-only workspace harness commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.harness_manifest import check_workspace  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate the workspace inventory")
    check.add_argument("--root", type=Path, default=ROOT)
    check.add_argument("--manifest", type=Path)
    check.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifest = args.manifest
    if manifest is not None and not manifest.is_absolute():
        manifest = root / manifest
    result = check_workspace(root, manifest)
    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        state = "PASS" if result.ok else "FAIL"
        print(
            f"harness check: {state} ({result.repository_count} repositories, "
            f"{len(result.errors)} errors, {len(result.warnings)} warnings)"
        )
        for finding in result.findings:
            print(f"{finding.level}: [{finding.code}] {finding.message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
