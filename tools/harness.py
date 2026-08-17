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

from libs.harness_manifest import check_workspace, load_manifest  # noqa: E402
from libs.harness_status import WorkspaceStatus, workspace_status  # noqa: E402


def _shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--no-submodules-expected",
        action="store_false",
        dest="submodules_expected",
        help="this environment never checks out submodules (e.g. CI) — validate "
        "their absence as expected before reporting status",
    )


def _manifest_path(root: Path, requested: Path | None) -> Path:
    if requested is None:
        return root / "harness" / "repos.yaml"
    return requested if requested.is_absolute() else root / requested


def _print_status(result: WorkspaceStatus) -> None:
    print(f"harness status: {'CURRENT' if result.current else 'DRIFT'}")
    for item in result.repositories:
        pin = (
            "root"
            if item.pin_matches is None
            else ("pin=ok" if item.pin_matches else "pin=DRIFT")
        )
        relation = (
            f"ahead={item.ahead} behind={item.behind}"
            if item.ahead is not None and item.behind is not None
            else "remote=unknown"
        )
        clean = (
            f"dirty={item.dirty_paths}" if item.dirty_paths is not None else "dirty=?"
        )
        release = item.checkout_release or "unknown"
        state = "CURRENT" if item.current else "DRIFT"
        print(
            f"{state:7} {item.repository_id:18} {pin:9} {relation:18} "
            f"{clean:9} release={release}"
        )
        checkout_head = (item.checkout_head or "unknown")[:12]
        parent_pin = (item.parent_pin or "root")[:12]
        remote_head = (item.remote_head or "unknown")[:12]
        print(
            f"        checkout={checkout_head} parent={parent_pin} "
            f"remote={item.remote_ref or 'unknown'}@{remote_head}"
        )
        if item.error:
            print(f"        error: {item.error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate the workspace inventory")
    _shared_arguments(check)
    status = subparsers.add_parser(
        "status", help="show checkout pin, remote, cleanliness, and release identity"
    )
    _shared_arguments(status)
    status.add_argument(
        "--fetch",
        action="store_true",
        help="refresh origin refs/tags before observation; never changes a checkout",
    )
    status.add_argument(
        "--require-current",
        action="store_true",
        help="exit nonzero when any checkout is behind, ahead, dirty, or off its parent pin",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifest_path = _manifest_path(root, args.manifest)
    if args.command == "check":
        result = check_workspace(
            root, manifest_path, submodules_expected=args.submodules_expected
        )
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

    manifest_check = check_workspace(
        root, manifest_path, submodules_expected=args.submodules_expected
    )
    if not manifest_check.ok:
        if args.json_output:
            print(json.dumps(manifest_check.to_dict(), indent=2, ensure_ascii=False))
        else:
            print("harness status: FAIL (invalid workspace manifest)")
            for finding in manifest_check.errors:
                print(f"error: [{finding.code}] {finding.message}")
        return 1
    result = workspace_status(root, load_manifest(manifest_path), fetch=args.fetch)
    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_status(result)
    return 0 if result.ok and (result.current or not args.require_current) else 1


if __name__ == "__main__":
    raise SystemExit(main())
