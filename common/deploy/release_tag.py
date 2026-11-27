"""Cut the next infra2 release tag on ``origin/main`` when no tag train runs (#1644 Q3).

A release tag starts the Staging train. On 2026-11-27 the repository got 23 release tags in
9.5 hours, one tag after each fix. Tag v3.2.60 was pushed 4 minutes after v3.2.59, waited
behind it, and was refused for the red Canary check of v3.2.59. This tool cuts no tag:

* when the newest release tag is the head of ``origin/main``;
* while a Staging run of the train workflow runs or waits (a tag push, a dispatch or the
  schedule: they share one concurrency group);
* while the train of the newest release tag is not in the run list (a dispatch replay of
  that tag counts as its train);
* in the 30 minutes after the start of the newest tag train.

The pull requests that merge in that time ride the next tag. Without ``--push`` the tool
prints the decision and changes nothing. Exit code 0: ``cut`` or ``none``. Exit code 1:
``wait``. Exit code 4: a source is not readable, or the tag or the push failed.
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
#: A production promotion has its own concurrency group; it does not hold a Staging train.
PROD_TITLE_PREFIX = "Reconcile IaC Inputs (PROD"
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


def is_tag_train(run: Mapping[str, object]) -> bool:
    """True for a run that a release tag push started."""
    return run.get("event") == "push" and bool(
        RELEASE_TAG_RE.fullmatch(str(run.get("head_branch") or ""))
    )


def decide(
    main_sha: str,
    tags: Mapping[str, str],
    runs: Sequence[Mapping[str, object]],
    now: datetime,
) -> Decision:
    """The decision from the head of ``origin/main``, ``{release tag: commit}`` and the runs.

    ``runs`` are the newest runs of the train workflow: each event, in any order.
    """
    if not main_sha or not tags:
        raise ValueError("origin has no main branch or no release tag")
    newest = max(tags, key=version_key)
    if tags[newest] == main_sha:
        return Decision("none", f"{newest} is the head of origin/main")
    busy = [
        run
        for run in runs
        if run.get("status") != "completed"
        and not str(run.get("display_title") or "").startswith(PROD_TITLE_PREFIX)
    ]
    if busy:
        run = busy[0]
        return Decision(
            "wait",
            f"the {run.get('event')} run of {run.get('head_branch')} is "
            f"{run.get('status')} ({run.get('html_url')})",
        )
    replays = [
        run
        for run in runs
        if str(run.get("display_title") or "").endswith(f"(staging {newest})")
    ]
    trains = [run for run in runs if is_tag_train(run)] + replays
    if not replays and not any(run.get("head_branch") == newest for run in trains):
        return Decision(
            "wait",
            f"the train of {newest} is not in the run list; when it stays out, "
            f"dispatch the train workflow with after={newest}",
        )
    starts = [str(run.get("run_started_at") or "") for run in trains]
    if not all(starts):
        raise ValueError("a tag train of the run list has no run_started_at")
    started = max(_when(start) for start in starts)
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


def remote_refs(
    root: Path, runner: Runner = subprocess.run
) -> tuple[str, dict[str, str]]:
    """``(main commit, {release tag: commit})`` of ``origin``, from one ``git ls-remote``.

    An annotated tag has a peeled row ``<tag>^{}``; that row holds its commit.
    """
    done = runner(
        ["git", "ls-remote", ORIGIN, "refs/heads/main", "refs/tags/v*"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {(done.stderr or '').strip()}")
    refs = dict(
        reversed(line.split()) for line in (done.stdout or "").splitlines() if line
    )
    tags = {
        name: refs.get(f"refs/tags/{name}^{{}}", sha)
        for ref, sha in refs.items()
        if RELEASE_TAG_RE.fullmatch(name := ref.removeprefix("refs/tags/"))
    }
    return refs.get("refs/heads/main", ""), tags


def train_runs(repo: str, runner: Runner = subprocess.run) -> list[dict]:
    """The newest runs of the train workflow: 30 of each event and the 20 newest tag pushes.

    ``RuntimeError`` when a list is not readable.
    """
    found: dict[object, dict] = {}
    for query in ("per_page=30", "event=push&per_page=20"):
        done = runner(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/workflows/{TRAIN_WORKFLOW}/runs?{query}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"cannot list the train runs: {(done.stderr or '').strip()}"
            )
        runs = json.loads(done.stdout or "{}").get("workflow_runs")
        if not isinstance(runs, list):
            raise RuntimeError("the train run list holds no workflow_runs")
        found |= {run.get("id"): run for run in runs if isinstance(run, dict)}
    return list(found.values())


def cut(decision: Decision, message: str, root: Path, runner: Runner) -> None:
    """Create the annotated tag on the commit of ``origin/main`` and push it.

    A failed push deletes the local tag of this run, so the next run can make it again.
    """

    def git(*argv: str) -> subprocess.CompletedProcess:
        return runner(
            ["git", *argv], cwd=root, capture_output=True, text=True, timeout=120
        )

    def must(*argv: str) -> None:
        done = git(*argv)
        if done.returncode != 0:
            raise RuntimeError(f"git {argv[0]} failed: {(done.stderr or '').strip()}")

    must("fetch", "--quiet", ORIGIN, decision.sha)
    must("tag", "-a", decision.tag, "-m", message, decision.sha)
    try:
        must("push", ORIGIN, f"refs/tags/{decision.tag}")
    except (
        BaseException
    ):  # also an interrupt: a local tag left behind stops each later run
        git("tag", "-d", decision.tag)
        raise


def main(
    argv: list[str] | None = None,
    *,
    root: Path = REPO_ROOT,
    runner: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--push", action="store_true", help="create and push the tag")
    parser.add_argument("-m", "--message", default="", help="the tag message")
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "wangzitian0/infra2")
    )
    args = parser.parse_args(argv)
    try:
        main_sha, tags = remote_refs(root, runner)
        released = bool(tags) and tags[max(tags, key=version_key)] == main_sha
        # `none` needs no run list: each REST call counts on a shared hourly budget.
        runs = [] if released else train_runs(args.repo, runner)
        decision = decide(main_sha, tags, runs, now())
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"release tag: unreadable: {exc}")
        return EXIT_UNREADABLE
    if decision.action == "cut" and args.push:
        try:
            cut(decision, args.message or f"Release {decision.tag}", root, runner)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"release tag: failed {decision.tag}: {exc}")
            return EXIT_UNREADABLE
    done = "pushed" if decision.action == "cut" and args.push else decision.action
    target = f" {decision.tag} at {decision.sha[:8]}" if decision.tag else ""
    print(f"release tag: {done}{target}: {decision.reason}")
    return EXIT_WAIT if decision.action == "wait" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
