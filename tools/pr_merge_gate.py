#!/usr/bin/env python3
"""The session-scoped merge authority of AGENTS.md, as a check instead of a habit.

An agent merging under session authority must see, for one head, that every blocking
check is green, every review thread is resolved, the head has been quiet for twelve
minutes since its last push, and that the change neither touches a protected file nor
triggers a deploy on merge — the last two need the owner's approval of that exact head.
On 2026-09-15 three of five merges landed 21–95 s before the quiet period had elapsed,
each time because the timing was judged by eye between other work. This tool judges it.

    python -m tools.pr_merge_gate 704            # verdict, exit 0 when mergeable by rule
    python -m tools.pr_merge_gate 704 --merge    # squash-merge only when the verdict is ready

Exit codes: 0 ready (or merged), 1 not yet (a check pending, a thread open, the head
still settling), 2 needs the owner (protected file, deploy-triggering path, wrong base).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_REPO = "wangzitian0/infra2"
QUIET_MINUTES = 12
# AGENTS.md: protected files need the owner's approval of the head that changes them.
PROTECTED_FILES = ("AGENTS.md", "CLAUDE.md")
# A push to main under these paths deploys (deploy.yml: the runner rebuild) — a merge
# must not be what triggers it under session authority.
DEPLOY_TRIGGERING_GLOBS = (
    "bootstrap/06.iac_runner/*",
    "bootstrap/06.iac_runner/**/*",
    "scripts/deploy_iac_runner_bootstrap.sh",
    ".github/workflows/deploy.yml",
)
# gh's own classification of a check (`bucket`): pass / fail / pending / skipping /
# cancel. `state` (SUCCESS, SKIPPED, IN_PROGRESS, …) is kept as the fallback for a gh
# build without buckets.
GREEN_BUCKETS = frozenset({"pass", "skipping"})
GREEN_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
MAX_REVIEW_THREADS = 100

Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class HeadFacts:
    number: int
    state: str
    draft: bool
    base: str
    head_sha: str
    files: tuple[str, ...]
    last_push_at: float  # epoch seconds of the newest commit on the head
    checks: tuple[tuple[str, str], ...]  # (name, gh bucket or state)
    unresolved_threads: int
    review_threads_total: int = 0


@dataclass
class Verdict:
    ready: bool
    owner_required: bool
    reasons: list[str] = field(default_factory=list)
    quiet_remaining_seconds: int = 0

    @property
    def exit_code(self) -> int:
        if self.ready:
            return 0
        return 2 if self.owner_required else 1


def _gh(argv: Sequence[str]) -> str:
    result = subprocess.run(["gh", *argv], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)}: {result.stderr.strip() or 'failed'}")
    return result.stdout


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def collect(number: int, *, repo: str = DEFAULT_REPO, gh: Runner = _gh) -> HeadFacts:
    """Read the head's facts through `gh`; nothing here decides."""
    view = json.loads(
        gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,state,isDraft,baseRefName,headRefOid,files,commits",
            ]
        )
    )
    checks = json.loads(
        gh(
            [
                "pr",
                "checks",
                str(number),
                "--repo",
                repo,
                "--json",
                "name,state,bucket",
            ]
        )
        or "[]"
    )
    owner, name = repo.split("/", 1)
    threads = json.loads(
        gh(
            [
                "api",
                "graphql",
                "-f",
                "query=query{repository(owner:%s,name:%s){pullRequest(number:%d)"
                "{reviewThreads(first:%d){totalCount nodes{isResolved}}}}}"
                % (json.dumps(owner), json.dumps(name), number, MAX_REVIEW_THREADS),
            ]
        )
    )
    review_threads = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
    nodes = review_threads["nodes"]
    commits = view.get("commits") or []
    last_push = max((_epoch(c["committedDate"]) for c in commits), default=0.0)
    return HeadFacts(
        number=int(view["number"]),
        state=str(view.get("state") or ""),
        draft=bool(view.get("isDraft")),
        base=str(view.get("baseRefName") or ""),
        head_sha=str(view.get("headRefOid") or ""),
        files=tuple(str(f["path"]) for f in view.get("files") or []),
        last_push_at=last_push,
        checks=tuple(
            (str(c["name"]), str(c.get("bucket") or c.get("state") or ""))
            for c in checks
        ),
        unresolved_threads=sum(1 for n in nodes if not n.get("isResolved")),
        review_threads_total=int(review_threads.get("totalCount") or len(nodes)),
    )


def _deploy_triggering(path: str) -> bool:
    return any(fnmatch.fnmatch(path, glob) for glob in DEPLOY_TRIGGERING_GLOBS)


def evaluate(
    facts: HeadFacts, *, now: float, quiet_minutes: int = QUIET_MINUTES
) -> Verdict:
    """The rule, in one place. Every failed condition is a reason; owner-only
    conditions are named as such so a caller never waits on something time cannot fix."""
    reasons: list[str] = []
    owner = False
    if facts.state != "OPEN":
        reasons.append(f"pull request is {facts.state or 'unknown'}, not OPEN")
    if facts.draft:
        reasons.append("pull request is a draft")
    if facts.base != "main":
        reasons.append(f"base branch is {facts.base!r}, not main")
        owner = True
    protected = sorted(f for f in facts.files if f in PROTECTED_FILES)
    if protected:
        reasons.append(
            f"touches protected file(s) {', '.join(protected)}: owner approval of "
            f"head {facts.head_sha[:7]} required"
        )
        owner = True
    deploying = sorted(f for f in facts.files if _deploy_triggering(f))
    if deploying:
        reasons.append(
            f"merging would trigger a deploy ({', '.join(deploying)}): owner approval "
            f"of head {facts.head_sha[:7]} required"
        )
        owner = True
    not_green = sorted(
        name
        for name, verdict in facts.checks
        if verdict not in GREEN_BUCKETS and verdict not in GREEN_STATES
    )
    if not facts.checks:
        reasons.append("no checks reported yet")
    if not_green:
        reasons.append(f"check(s) not green: {', '.join(not_green)}")
    if facts.unresolved_threads:
        reasons.append(f"{facts.unresolved_threads} review thread(s) unresolved")
    if facts.review_threads_total > MAX_REVIEW_THREADS:
        reasons.append(
            f"{facts.review_threads_total} review threads, only the first "
            f"{MAX_REVIEW_THREADS} were read: resolve or evaluate by hand"
        )
    # Round up: a fractional second still inside the window is inside the window.
    remaining = math.ceil(facts.last_push_at + quiet_minutes * 60 - now)
    if remaining > 0:
        reasons.append(
            f"head pushed {int(now - facts.last_push_at)}s ago; quiet period has "
            f"{remaining}s to run"
        )
    return Verdict(
        ready=not reasons,
        owner_required=owner,
        reasons=reasons,
        quiet_remaining_seconds=max(0, remaining),
    )


def render(facts: HeadFacts, verdict: Verdict) -> str:
    head = f"#{facts.number} @ {facts.head_sha[:7]}"
    if verdict.ready:
        return (
            f"{head}: mergeable under session authority — checks green "
            f"({len(facts.checks)}), threads resolved, quiet period elapsed, "
            "no protected or deploy-triggering paths"
        )
    kind = "needs the owner" if verdict.owner_required else "not yet"
    return f"{head}: {kind}\n" + "\n".join(f"  - {r}" for r in verdict.reasons)


def main(argv: list[str] | None = None, *, gh: Runner = _gh, now=time.time) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--quiet-minutes", type=int, default=QUIET_MINUTES)
    parser.add_argument(
        "--merge", action="store_true", help="squash-merge when the verdict is ready"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = parser.parse_args(argv)

    from libs.console import error, success, warning

    facts = collect(args.number, repo=args.repo, gh=gh)
    verdict = evaluate(facts, now=now(), quiet_minutes=args.quiet_minutes)
    if args.json:
        print(
            json.dumps(
                {
                    "number": facts.number,
                    "head": facts.head_sha,
                    "ready": verdict.ready,
                    "owner_required": verdict.owner_required,
                    "reasons": verdict.reasons,
                    "quiet_remaining_seconds": verdict.quiet_remaining_seconds,
                }
            )
        )
    else:
        text = render(facts, verdict)
        if verdict.ready:
            success(text)
        elif verdict.owner_required:
            error(text)
        else:
            warning(text)
    if verdict.ready and args.merge:
        gh(
            [
                "pr",
                "merge",
                str(facts.number),
                "--repo",
                args.repo,
                "--squash",
                "--delete-branch",
                "--match-head-commit",
                facts.head_sha,
            ]
        )
        success(f"merged #{facts.number} at {facts.head_sha[:7]}")
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
