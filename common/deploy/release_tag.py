"""Cut the next infra2 release tag on ``origin/main`` when no tag train runs (#1644 Q3).

A release tag starts the Staging train. On 2026-11-27 the repository got 23 release tags in
9.5 hours, one tag after each fix. Tag v3.2.60 was pushed 4 minutes after v3.2.59, waited
behind it, and was refused for the red Canary check of v3.2.59. This tool cuts no tag:

* when the newest release tag is the head of ``origin/main``;
* while a tag train runs or waits;
* in the 30 minutes after the start of the newest tag train.

The pull requests that merge in that time ride the next tag. Without ``--push`` the tool
prints the decision and changes nothing. Exit code 0: ``cut`` or ``none``. Exit code 1:
``wait``. Exit code 4: a source is not readable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.core.constants import REPO_ROOT
from common.deploy.release_markers import RELEASE_TAG_RE, version_key

TRAIN_WORKFLOW = "reconcile-iac-inputs.yml"
MIN_INTERVAL = timedelta(minutes=30)
ORIGIN = "origin"
EXIT_OK, EXIT_WAIT, EXIT_UNREADABLE = 0, 1, 4

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Decision:
    """``action`` is ``cut``, ``wait`` or ``none``; ``tag`` and ``sha`` are set for ``cut``."""

    action: str
    reason: str
    tag: str = ""
    sha: str = ""


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def decide(
    main_sha: str,
    tags: Mapping[str, str],
    runs: Sequence[Mapping[str, object]],
    now: datetime,
) -> Decision:
    """The decision from the head of ``origin/main``, ``{release tag: commit}`` and the tag trains.

    ``runs`` are the tag-push runs of the train workflow, in any order.
    """
    if not main_sha or not tags:
        raise ValueError("origin has no main branch or no release tag")
    newest = max(tags, key=version_key)
    if tags[newest] == main_sha:
        return Decision("none", f"{newest} is the head of origin/main")
    active = [run for run in runs if run.get("status") != "completed"]
    if active:
        run = active[0]
        return Decision(
            "wait",
            f"the train of {run.get('head_branch')} is {run.get('status')} "
            f"({run.get('html_url')})",
        )
    if runs:
        started = max(_when(str(run["run_started_at"])) for run in runs)
        free = started + MIN_INTERVAL
        if now < free:
            return Decision(
                "wait",
                f"the newest tag train started at {_utc(started)}; "
                f"the next tag is free at {_utc(free)}",
            )
    major, minor, patch = version_key(newest)
    return Decision(
        "cut",
        f"origin/main is ahead of {newest}",
        tag=f"v{major}.{minor}.{patch + 1}",
        sha=main_sha,
    )


def tag_trains(repo: str, runner: Runner = subprocess.run) -> list[dict]:
    """The newest 20 tag-push runs of the train workflow. ``RuntimeError`` when unreadable."""
    done = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/{TRAIN_WORKFLOW}/runs?event=push&per_page=20",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(f"cannot list the train runs: {(done.stderr or '').strip()}")
    runs = json.loads(done.stdout or "{}").get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("the train run list holds no workflow_runs")
    return [
        run
        for run in runs
        if isinstance(run, dict)
        and RELEASE_TAG_RE.fullmatch(str(run.get("head_branch") or ""))
    ]


def cut(decision: Decision, message: str, root: Path, runner: Runner) -> None:
    """Create the annotated tag on the commit of ``origin/main`` and push it."""
    for argv in (
        ["git", "fetch", "--quiet", ORIGIN, decision.sha],
        ["git", "tag", "-a", decision.tag, "-m", message, decision.sha],
        ["git", "push", ORIGIN, f"refs/tags/{decision.tag}"],
    ):
        done = runner(argv, cwd=root, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv[:3])} failed: {(done.stderr or '').strip()}"
            )


def main(
    argv: list[str] | None = None,
    *,
    root: Path = REPO_ROOT,
    runner: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    from common.deploy.reconcile_iac_inputs import _query_remote_app_refs

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--push", action="store_true", help="create and push the tag")
    parser.add_argument("-m", "--message", default="", help="the tag message")
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "wangzitian0/infra2")
    )
    args = parser.parse_args(argv)
    try:
        main_sha, tags = _query_remote_app_refs(ORIGIN, runner=_in(root, runner))
        decision = decide(main_sha, tags, tag_trains(args.repo, runner), now())
        if decision.action == "cut" and args.push:
            cut(decision, args.message or f"Release {decision.tag}", root, runner)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"release tag: unreadable: {exc}")
        return EXIT_UNREADABLE
    done = "pushed" if decision.action == "cut" and args.push else decision.action
    target = f" {decision.tag} at {decision.sha[:8]}" if decision.tag else ""
    print(f"release tag: {done}{target}: {decision.reason}")
    return EXIT_WAIT if decision.action == "wait" else EXIT_OK


def _in(root: Path, runner: Runner) -> Runner:
    """``runner`` with ``cwd=root``: ``origin`` is a remote name of that checkout."""

    def run(argv, **kwargs):
        return runner(argv, cwd=root, **kwargs)

    return run


if __name__ == "__main__":
    sys.exit(main())
