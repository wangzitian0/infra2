#!/usr/bin/env python3
"""Run tests affected by current git changes.

This script inspects git status and git diff.
It maps changed source files to matching test files.
Then it runs only the affected tests with -x.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]


def get_changed_files(base_ref: str = "origin/main") -> list[str]:
    """Return list of modified or untracked python files."""
    changed: set[str] = set()

    # 1. Uncommitted changes (staged and unstaged)
    status_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if status_proc.returncode == 0:
        for line in status_proc.stdout.splitlines():
            if len(line) > 3:
                file_path = line[3:].strip()
                if file_path.endswith(".py"):
                    changed.add(file_path)

    # 2. Committed changes compared to base branch
    diff_proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_proc.returncode == 0:
        for line in diff_proc.stdout.splitlines():
            line = line.strip()
            if line.endswith(".py"):
                changed.add(line)

    return sorted(changed)


def resolve_affected_tests(changed_files: list[str]) -> list[str]:
    """Map changed python files to test files in libs/tests/."""
    tests: set[str] = set()
    test_dir = ROOT_DIR / "libs" / "tests"

    for file_rel in changed_files:
        path = Path(file_rel)
        # If the file is itself a test file
        if (
            path.parts
            and path.parts[0] == "libs"
            and len(path.parts) > 1
            and path.parts[1] == "tests"
        ):
            if (ROOT_DIR / path).is_file():
                tests.add(str(path))
            continue

        stem = path.stem
        # Direct pattern match: test_<stem>.py
        direct_match = test_dir / f"test_{stem}.py"
        if direct_match.is_file():
            tests.add(str(direct_match.relative_to(ROOT_DIR)))
            continue

        # Prefix pattern match: test_<parent>_<stem>.py
        parent_name = path.parent.name
        if parent_name and parent_name not in (".", "libs", "src"):
            prefixed_match = test_dir / f"test_{parent_name}_{stem}.py"
            if prefixed_match.is_file():
                tests.add(str(prefixed_match.relative_to(ROOT_DIR)))
                continue

    return sorted(tests)


def main(argv: list[str] | None = None) -> int:
    """Execute affected tests."""
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
        print("ℹ No changed Python files detected.")
        return 0

    affected = resolve_affected_tests(changed)
    if not affected:
        print(
            f"ℹ Found {len(changed)} changed file(s), but no matching unit tests in libs/tests/."
        )
        return 0

    print(f"🎯 Found {len(affected)} affected test file(s):")
    for t in affected:
        print(f"  - {t}")

    if args.dry_run:
        return 0

    cmd = ["uv", "run", "pytest", *affected, "-x", *extra_args]
    print(f"▶ Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT_DIR)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
