#!/usr/bin/env python3
"""Record the production marker tag after a green production promotion (#1511).

GitHub rejects a marker push by the workflow token when the tagged commit's workflow
files differ from main. The agent loop records the marker with the owner's git identity
instead. Run `git fetch --tags origin` first: the marker order check reads local tags.

The tool checks the preconditions in order. The first failure prints `::error::` and
exits 3. It never moves or deletes a tag. It never starts, reruns or cancels a run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

from common.core.constants import REPO_ROOT
from tools.reconcile_iac_inputs import (
    assert_production_marker_advances,
    production_marker_tag,
)

WORKFLOW = "reconcile-iac-inputs.yml"
BUSY_STATUSES = ("waiting", "pending", "in_progress", "queued")
RUN_LIMIT = 30
COMMAND_TIMEOUT_SECONDS = 120
EXIT_REFUSED = 3

Runner = Callable[..., subprocess.CompletedProcess]


class Refused(Exception):
    """A precondition failed. The text is `<which one>: <detail>`."""

    def __init__(self, which: str, detail: str) -> None:
        super().__init__(f"{which}: {detail}")


def _run(runner: Runner, argv: list[str], root: Path) -> subprocess.CompletedProcess:
    try:
        return runner(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _remote_marker_sha(runner: Runner, root: Path, marker: str, which: str) -> str:
    ref = f"refs/tags/{marker}"
    listed = _run(runner, ["git", "ls-remote", "--tags", "origin", ref], root)
    if listed.returncode != 0:
        raise Refused(which, f"git ls-remote failed: {listed.stderr.strip()}")
    shas = {}
    for line in listed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        shas[name.strip()] = sha.strip().lower()
    return shas.get(f"{ref}^{{}}") or shas.get(ref, "")


def _gh_runs(
    runner: Runner, root: Path, which: str, label: str, *flags: str
) -> list[dict]:
    argv = ["gh", "run", "list", "--workflow", WORKFLOW, *flags]
    listed = _run(runner, argv, root)
    if listed.returncode != 0:
        raise Refused(which, f"gh run list {label} failed: {listed.stderr.strip()}")
    try:
        rows = json.loads(listed.stdout)
    except ValueError:
        rows = None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise Refused(which, f"gh run list {label} returned unreadable JSON")
    return rows


def record(tag: str, root: Path, *, runner: Runner, dry_run: bool) -> str:
    """Check every precondition, then record the marker. Return the line to print."""
    try:
        marker = production_marker_tag(tag)
    except ValueError as exc:
        raise Refused("tag format", str(exc)) from exc

    resolved = _run(runner, ["git", "rev-parse", "--verify", f"{tag}^{{commit}}"], root)
    if resolved.returncode != 0:
        raise Refused(
            "tag resolves",
            f"{tag} does not resolve to a commit: {resolved.stderr.strip()}",
        )
    sha = resolved.stdout.strip().lower()
    ancestor = _run(
        runner, ["git", "merge-base", "--is-ancestor", sha, "origin/main"], root
    )
    if ancestor.returncode != 0:
        why = f": {ancestor.stderr.strip()}" if ancestor.stderr.strip() else ""
        raise Refused(
            "tag on main", f"{sha[:12]} is not an ancestor of origin/main{why}"
        )

    remote = _remote_marker_sha(runner, root, marker, "remote marker")
    if remote == sha:
        return f"already recorded {marker} at {sha}"
    if remote:
        raise Refused(
            "remote marker",
            f"{marker} points to {remote[:12]}, not {sha[:12]}; a marker is immutable",
        )

    try:
        assert_production_marker_advances(
            tag, root, runner=lambda argv, **_kw: _run(runner, argv, root)
        )
    except SystemExit as exc:
        raise Refused(
            "marker advances", str(exc.code).removeprefix("::error::")
        ) from exc

    busy = []
    for status in BUSY_STATUSES:
        rows = _gh_runs(
            runner,
            root,
            "reconcile busy",
            f"--status {status}",
            "--status",
            status,
            "--json",
            "databaseId",
        )
        if rows:
            busy.append(
                f"{status}: {', '.join(str(row.get('databaseId')) for row in rows)}"
            )
    if busy:
        raise Refused("reconcile busy", "; ".join(busy))

    needle = f"(PROD {tag})"
    rows = _gh_runs(
        runner,
        root,
        "promotion run",
        "--event workflow_dispatch",
        "--event",
        "workflow_dispatch",
        "--limit",
        str(RUN_LIMIT),
        "--json",
        "displayTitle,conclusion,createdAt",
    )
    promotions = [row for row in rows if needle in str(row.get("displayTitle", ""))]
    if not promotions:
        raise Refused(
            "promotion run",
            f"no workflow_dispatch run titled '{needle}' in the newest {RUN_LIMIT}",
        )
    newest = max(promotions, key=lambda row: str(row.get("createdAt", "")))
    if newest.get("conclusion") != "success":
        raise Refused(
            "promotion run",
            f"newest run titled '{needle}' ended {newest.get('conclusion')!r}, "
            "not 'success'",
        )

    local = _run(
        runner, ["git", "rev-parse", "--verify", f"refs/tags/{marker}^{{commit}}"], root
    )
    local_sha = local.stdout.strip().lower() if local.returncode == 0 else ""
    if local_sha and local_sha != sha:
        raise Refused(
            "local marker", f"{marker} points to {local_sha[:12]}, not {sha[:12]}"
        )
    if dry_run:
        return f"would record {marker} at {sha}"

    if not local_sha:
        tagged = _run(runner, ["git", "tag", marker, sha], root)
        if tagged.returncode != 0:
            raise Refused("tag create", tagged.stderr.strip())
    pushed = _run(runner, ["git", "push", "origin", f"refs/tags/{marker}"], root)
    if pushed.returncode != 0:
        raise Refused("push", pushed.stderr.strip())
    seen = _remote_marker_sha(runner, root, marker, "verify")
    if seen != sha:
        raise Refused(
            "verify", f"remote {marker} is {seen!r} after push, expected {sha}"
        )
    return f"recorded {marker} at {sha}"


def main(argv: list[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tag", help="release tag whose promotion succeeded (vX.Y.Z)")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--dry-run", action="store_true", help="check the preconditions, change nothing"
    )
    args = parser.parse_args(argv)
    try:
        print(
            record(args.tag, Path(args.repo_root), runner=runner, dry_run=args.dry_run)
        )
    except Refused as exc:
        print(f"::error::{exc}")
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
