#!/usr/bin/env python3
"""Read-only workspace harness commands."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs import harness_sweep  # noqa: E402
from libs.harness_manifest import check_workspace, load_manifest  # noqa: E402
from libs.harness_status import WorkspaceStatus, workspace_status  # noqa: E402

SWEEP_DESCRIPTION = """Classify every watched agent, PR, release log, workflow run and
worktree into one state: WAITING, DONE, ACTION, STALL or UNKNOWN. Read-only; merge
gates are judged by exit code (0 ready, 2 owner) and gate commands carrying a mutating
flag are refused. The watch list is JSON: {"items": [{"kind": "pr", ...}, ...]}."""


class _SweepParser(argparse.ArgumentParser):
    """A usage error exits 4 (could not evaluate): exit 2 means progress for a sweep."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(harness_sweep.EXIT_UNKNOWN, f"{self.prog}: error: {message}\n")


def _sweep_parser() -> argparse.ArgumentParser:
    codes = "\n".join(
        f"  {code}  {meaning}" for code, meaning in harness_sweep.EXIT_CODES.items()
    )
    parser = _SweepParser(
        prog="python -m tools.harness sweep",
        description=SWEEP_DESCRIPTION,
        epilog=f"exit codes:\n{codes}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", type=Path, help="watch list JSON file")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="print only transitions and heartbeats; exit as soon as an item leaves "
        "WAITING (run it under a persistent Monitor)",
    )
    parser.add_argument(
        "--interval", type=float, default=90, help="seconds between sweeps (90)"
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=240,
        help="print a heartbeat after this many quiet seconds (240)",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=0,
        help="end the watch with exit 5 after this long (0 = never)",
    )
    parser.add_argument(
        "--unknown-tolerance",
        type=int,
        default=1,
        help="consecutive sweeps with an UNKNOWN item tolerated before exiting (1)",
    )
    return parser


def _sweep(argv: list[str]) -> int:
    args = _sweep_parser().parse_args(argv)
    try:
        items = harness_sweep.load_items(args.config)
    except harness_sweep.SweepConfigError as exc:
        print(f"sweep: {exc}", file=sys.stderr)
        return harness_sweep.EXIT_UNKNOWN

    def emit(text: str) -> None:
        print(text, flush=True)

    env = harness_sweep.Env()
    if not args.watch:
        return harness_sweep.sweep_once(env, items, emit)
    return harness_sweep.watch(
        env,
        items,
        interval=args.interval,
        heartbeat=args.heartbeat,
        max_minutes=args.max_minutes,
        unknown_tolerance=args.unknown_tolerance,
        emit=emit,
        sleep=time.sleep,
    )


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
    argv = sys.argv[1:] if argv is None else list(argv)
    # `sweep` has its own parser so that no usage error can exit 2, which a sweep
    # reserves for "an item finished while others still wait".
    if argv[:1] == ["sweep"]:
        return _sweep(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    # Listed for --help only; main() dispatches `sweep` before this parser runs.
    subparsers.add_parser(
        "sweep",
        help="one state per watched agent/PR/release/run (see `sweep --help`)",
        add_help=False,
    )
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
