"""Read-only probe: would a production promotion pass the upstream guard now? (#1646, #1830)

``require_upstream_post`` runs once, in the ``pre`` job of a production promotion, after the
owner asks for it. A refusal there (``missing staging_gate for truealpha/app@v0.2.137``) comes
three times too late. This probe builds the same inputs that the ``pre`` job builds for the
next production candidate (the newest release tag newer than the newest production marker)
and calls the same ``require_upstream_post`` against the real GitHub stage records. It
changes nothing: no marker, no deploy, no dispatch.

It does not decide anything itself. The verdict is the guard's verdict.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from common.core.constants import REPO_ROOT
from common.deploy.promotion_guard import require_upstream_post, required_staging_gates
from common.deploy.release_markers import (
    PRODUCTION_MARKER_PREFIX,
    RELEASE_TAG_RE,
    version_key,
)

#: A pull request that changes one of these files runs the probe.
WATCHED_PATHS: tuple[str, ...] = (
    "common/deploy/promotion_guard.py",
    "common/deploy/stage_record_reader.py",
    "common/deploy/reconcile_iac_inputs.py",
)

Guard = Callable[..., None]


@dataclass(frozen=True)
class Verdict:
    """What the probe found: ``ok`` when the guard passes, else the guard's refusal text."""

    ok: bool
    after: str
    before: str
    required: Mapping[str, str]
    detail: str


def touches_guard(changed_files: Sequence[str]) -> bool:
    """True when any changed file is one of ``WATCHED_PATHS``."""
    return any(path.strip() in WATCHED_PATHS for path in changed_files)


def _tags(root: Path, pattern: str) -> list[str]:
    listed = subprocess.run(
        ["git", "tag", "--list", pattern],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return listed.stdout.split()


def next_candidate(root: Path) -> str:
    """The newest release tag that is newer than the newest production marker, or ``""``."""
    releases = [t for t in _tags(root, "v*.*.*") if RELEASE_TAG_RE.fullmatch(t)]
    markers = [
        m.removeprefix(PRODUCTION_MARKER_PREFIX)
        for m in _tags(root, f"{PRODUCTION_MARKER_PREFIX}v*.*.*")
    ]
    markers = [m for m in markers if RELEASE_TAG_RE.fullmatch(m)]
    newest = max(releases, key=version_key, default="")
    if not newest:
        return ""
    if markers and version_key(newest) <= version_key(max(markers, key=version_key)):
        return ""
    return newest


def probe(
    after: str,
    root: Path = REPO_ROOT,
    *,
    env: Mapping[str, str] = os.environ,
    guard: Guard = require_upstream_post,
) -> Verdict:
    """Run ``guard`` with the inputs of the ``pre`` job of a production run of ``after``."""
    from common.deploy.reconcile_iac_inputs import (
        build_plan,
        changed_files_from_git,
        production_before,
        releases_between,
    )

    before = production_before(after, root) or ""
    changed = changed_files_from_git(root, before or None, after)
    plan = build_plan(changed, repo_root=root, before=before or None, after=after)
    required = required_staging_gates(plan)
    try:
        guard(
            after,
            root,
            before=before,
            required=required,
            releases=releases_between,
            env=env,
        )
    except SystemExit as exc:
        detail = str(exc.code).removeprefix("::error::")
        return Verdict(False, after, before, required, detail)
    return Verdict(True, after, before, required, "the guard passes")


def main(argv: list[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    """Run the probe. With ``--report-only`` any crash is a warning and the exit code is 0."""
    try:
        return _run(argv, root)
    except Exception as exc:  # noqa: BLE001 - report-only must never fail a job
        if "--report-only" not in (argv if argv is not None else sys.argv[1:]):
            raise
        print(f"::warning::promotion probe: crashed: {exc!r}")
        return 0


def _run(argv: list[str] | None, root: Path) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--after", default="", help="release tag to probe; default: next candidate"
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="print a refusal as a warning and exit 0 (scheduled runs)",
    )
    parser.add_argument(
        "--only-if-changed",
        metavar="BASE..HEAD",
        default="",
        help="skip unless a guard file differs in this git range (pull requests)",
    )
    args = parser.parse_args(argv)
    if args.only_if_changed:
        diff = subprocess.run(
            ["git", "diff", "--name-only", *args.only_if_changed.split("..", 1)],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not touches_guard(diff):
            print("promotion probe: skipped, no promotion guard file changed")
            return 0
    after = args.after or next_candidate(root)
    if not after:
        print("promotion probe: no release tag is newer than the production marker")
        return 0
    try:
        verdict = probe(after, root)
    except (SystemExit, subprocess.CalledProcessError, OSError, ValueError) as exc:
        # An unreadable marker or a git failure is a failed probe, not a crash.
        prefix = "::warning::" if args.report_only else "::error::"
        detail = str(getattr(exc, "code", exc)).removeprefix("::error::")
        print(
            f"{prefix}promotion probe: could not build the promotion of {after}: {detail}"
        )
        return 0 if args.report_only else 1
    required = ", ".join(f"{s}@{t}" for s, t in sorted(verdict.required.items()))
    print(
        f"promotion probe: {after} (production before: {verdict.before or 'none'}; "
        f"required gates: {required or 'none'})"
    )
    if verdict.ok:
        print(f"promotion probe: a promotion of {after} would pass")
        return 0
    prefix = "::warning::" if args.report_only else "::error::"
    print(
        f"{prefix}promotion probe: a promotion of {after} would be refused: {verdict.detail}"
    )
    return 0 if args.report_only else 1


if __name__ == "__main__":
    sys.exit(main())
