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
    check.add_argument(
        "--no-submodules-expected",
        action="store_false",
        dest="submodules_expected",
        help="this environment never checks out submodules (e.g. CI, which "
        "deliberately skips `submodules: true` to keep app repos out of infra "
        "CI's fetch) — treat every submodule checkout as expected-absent "
        "instead of erroring on it",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifest = args.manifest
    if manifest is not None and not manifest.is_absolute():
        manifest = root / manifest
    result = check_workspace(root, manifest, submodules_expected=args.submodules_expected)
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
