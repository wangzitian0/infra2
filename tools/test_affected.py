#!/usr/bin/env python3
"""Run tests affected by current git changes.

This CLI entry point delegates changed-file discovery, affected-test resolution,
and local worker capping to ``tools.ci_runner`` so both ``python tools/test_affected.py``
and ``python -m tools.ci_runner unit-tests --changed`` share one source of truth.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.ci_runner import (  # noqa: E402
    _collect_changed_files,
    _default_pytest_workers,
    _resolve_changed_test_targets,
)


def get_changed_files(base_ref: str = "origin/main") -> list[str]:
    """Return repository-relative paths changed relative to base_ref."""
    normalized_base = base_ref.removeprefix("origin/")
    return _collect_changed_files(base_ref=normalized_base)


def resolve_affected_tests(changed_files: Sequence[str]) -> list[str]:
    """Map changed repository-relative paths to affected tests in common/tests/."""
    return _resolve_changed_test_targets(changed_files)


def main(argv: list[str] | None = None) -> int:
    """Execute affected unit tests with fail-fast and capped local workers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Base git reference to diff against (default: origin/main).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List affected tests without executing them.",
    )
    args, extra_args = parser.parse_known_args(argv)

    changed = get_changed_files(base_ref=args.base)
    if not changed:
        print("ℹ No changed files detected.")
        return 0

    affected = resolve_affected_tests(changed)
    if not affected:
        print(
            f"ℹ Found {len(changed)} changed file(s), but no matching unit tests in common/tests/."
        )
        return 0

    print(f"🎯 Found {len(affected)} affected test file(s):")
    for target in affected:
        print(f"  - {target}")

    if args.dry_run:
        return 0

    worker_args: list[str] = []
    if (
        len(affected) > 2
        and "-n" not in extra_args
        and not any(a.startswith("--numprocesses") for a in extra_args)
    ):
        worker_args = ["-n", _default_pytest_workers()]

    cmd = ["uv", "run", "pytest", *affected, "-x", *worker_args, *extra_args]
    print(f"▶ Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT_DIR, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
