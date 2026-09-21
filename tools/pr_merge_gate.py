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

Two policies for the settling condition:

- ``clock`` (default, AGENTS.md as written): twelve minutes since the last push.
- ``event``: an automated review has been submitted on the *current* head and three
  minutes have passed since that review — the thing the clock was waiting for, measured.
  On 2026-09-15 Copilot reviewed each first push within 2–3 minutes and never re-reviewed
  a fix-up push on its own, so the clock waited on nothing; ``--request-review`` asks
  Copilot for a review of the head when none exists.
- ``either``: whichever of the two is satisfied first — the review lets a head go early,
  the clock stays the upper bound when no automated review ever arrives.
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import yaml
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "wangzitian0/infra2"
QUIET_MINUTES = 12
SETTLE_MINUTES = 3  # event policy: after the review of the head, not after the push
# GitHub's Copilot pull-request reviewer (a global bot id, the same in every repository).
COPILOT_BOT_ID = "BOT_kgDOCnlnWA"
AUTOMATED_REVIEWERS = frozenset({"copilot-pull-request-reviewer"})
# AGENTS.md: protected files need the owner's approval of the head that changes them.
PROTECTED_FILES = ("AGENTS.md", "CLAUDE.md")
# A push to main under these paths deploys (deploy.yml: the runner rebuild;
# deploy-cloudflare-watchdog.yml: `wrangler deploy` of the out-of-band worker, #718) —
# a merge must not be what triggers it under session authority.
# Paths whose merge sets something running that a revert does not undo. These
# are the ones no workflow declares for itself -- a runner rebuild, a bootstrap
# self-update -- so they stay written down.
DEPLOY_TRIGGERING_GLOBS = (
    "bootstrap/06.iac_runner/*",
    "bootstrap/06.iac_runner/**/*",
    "scripts/deploy_iac_runner_bootstrap.sh",
    ".github/workflows/deploy.yml",
    "cloudflare/infra-watchdog/*",
    "cloudflare/infra-watchdog/**/*",
)

# A workflow that pushes to main and does one of these deploys. Deliberately a
# short, explicit list: the judgement "this actually deploys" stays here, while
# the paths that reach it are derived, because it is the paths that drift.
DEPLOY_MARKERS = ("deploy_v2", "wrangler deploy", "iac_runner", "repository_dispatch")
WORKFLOW_DIR = ROOT / ".github" / "workflows"


@functools.lru_cache(maxsize=1)
def _declared_deploy_globs() -> tuple[tuple[str, str], ...]:
    """`on.push.paths` of every workflow that pushes to main and deploys.

    The hand-written list above missed `ops-checks.yml`, whose 21 push paths
    each start a live `deploy_v2` canary on merge -- its own comment says so:
    "Same-repo PRs, main pushes, schedules, and manual runs all mutate the same
    reserved ephemeral slot". A PR touching any of them therefore needs owner
    approval of that head under AGENTS.md's 高风险例外, and this gate would have
    said session authority sufficed.

    Deriving is the point. A second hand-written list would drift the same way
    the first one did; the workflows already declare which paths wake them.

    Reads only local files, so there is no network, no TTY and nothing to race.
    A workflow that cannot be parsed contributes nothing rather than raising --
    a malformed file must not take the gate offline -- and the written list
    above still stands on its own.
    """
    globs: list[tuple[str, str]] = []
    try:
        names = sorted(WORKFLOW_DIR.glob("*.yml"))
    except OSError:
        return ()
    for path in names:
        try:
            text = path.read_text(encoding="utf-8")
            doc = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        # PyYAML resolves a bare `on:` key to the boolean True.
        triggers = doc.get("on") if isinstance(doc.get("on"), dict) else doc.get(True)
        push = (triggers or {}).get("push") if isinstance(triggers, dict) else None
        if not isinstance(push, dict) or "main" not in (push.get("branches") or []):
            continue
        if not any(marker in text for marker in DEPLOY_MARKERS):
            continue
        globs.extend((str(p), path.name) for p in (push.get("paths") or []))
    return tuple(dict.fromkeys(globs))
# gh's own classification of a check (`bucket`): pass / fail / pending / skipping /
# cancel. `state` (SUCCESS, SKIPPED, IN_PROGRESS, …) is kept as the fallback for a gh
# build without buckets.
# Merge states this gate accepts. Everything else is named by the
# mergeStateStatus reason itself -- including BLOCKED and BEHIND, which have no
# separate reason of their own. An earlier version of this comment claimed they
# did; adding a state here silently stops it blocking, so the list is the rule.
#
# HAS_HOOKS and UNSTABLE are accepted deliberately: the first is a repository
# configured with pre-receive hooks, the second means a non-required check is
# red. Required checks are already judged by name above, so treating UNSTABLE as
# a blocker here would duplicate that judgement and also block on checks the
# repository has decided do not block.
MERGE_STATES_OK = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})

GREEN_BUCKETS = frozenset({"pass", "skipping"})
GREEN_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
MAX_REVIEW_THREADS = 100
# gh's stderr when a pull request has no check registered yet (it exits 1, no JSON).
NO_CHECKS_REPORTED = "no checks reported"

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
    node_id: str = ""
    # GitHub's own merge computation. "" means it was not read; "UNKNOWN" means
    # GitHub is still computing a test merge and the caller should re-poll.
    mergeable: str = ""
    merge_state: str = ""
    # Files the base branch has changed since this head diverged from it. Their
    # intersection with `files` is what makes a green check stale.
    base_changed_files: tuple[str, ...] = ()
    # (reviewer login, commit reviewed, submitted epoch) — a review pins a head
    reviews: tuple[tuple[str, str, float], ...] = ()

    def reviews_on_head(self) -> tuple[tuple[str, str, float], ...]:
        return tuple(r for r in self.reviews if r[1] == self.head_sha)


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


def _read_checks(number: int, *, repo: str, gh: Runner) -> str:
    """`gh pr checks --json`, with gh's "nothing registered yet" read as zero checks.

    Before the first check registers, gh exits 1 with "no checks reported on the
    '<branch>' branch" instead of printing `[]`. Raised as an error, that crashed the
    gate with a traceback (also exit 1), so the "no checks reported yet" reason below
    could never be reported. Any other failure still raises.
    """
    try:
        return gh(
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
    except RuntimeError as exc:
        if NO_CHECKS_REPORTED not in str(exc):
            raise
        return "[]"


def _base_changed_files(
    repo: str, head_sha: str, base: str, *, gh: Runner = _gh
) -> tuple[str, ...]:
    """Files the base branch has changed since this head diverged from it.

    `compare/<head>...<base>` is computed from the merge base, so it answers
    exactly "what has base gained that this head has not seen" -- not "what does
    the head change", which `files` already covers.

    This exists because a green check proves the tree it ran on, not the tree a
    merge would produce. A sibling PR that lands between the run and the merge
    can rewrite the very files under test, and every check stays green because
    none of them re-ran. finance_report's AGENTS.md states the same hazard for
    the adjacent field: `mergeStateStatus` "can flip from CLEAN to DIRTY/BEHIND
    the instant a sibling PR merges to main -- re-check it fresh before every
    'ready' report, never trust an earlier snapshot".

    Best-effort: a comparison that cannot be read yields no files, so this can
    only ever fail to raise a concern, never invent one. The hard blockers
    (`mergeable`, checks, threads) do not depend on it.
    """
    if not head_sha or not base:
        return ()
    try:
        payload = json.loads(
            gh(["api", f"repos/{repo}/compare/{head_sha}...{base}", "--jq", "{files:[.files[]?.filename]}"])
        )
    except (RuntimeError, json.JSONDecodeError):
        return ()
    return tuple(str(f) for f in payload.get("files") or [])


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
                "number,state,isDraft,baseRefName,headRefOid,files,commits,id,reviews,"
                "mergeable,mergeStateStatus",
            ]
        )
    )
    checks = json.loads(_read_checks(number, repo=repo, gh=gh) or "[]")
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
    # Only an open pull request can be updated, so the comparison that feeds the
    # stale-green reason is worth a round trip only then. Auditing a run of
    # merged PRs would otherwise pay one extra API call each for an answer
    # `evaluate` discards.
    base_changed = (
        _base_changed_files(
            repo,
            str(view.get("headRefOid") or ""),
            str(view.get("baseRefName") or ""),
            gh=gh,
        )
        if str(view.get("state") or "") == "OPEN"
        else ()
    )
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
        node_id=str(view.get("id") or ""),
        mergeable=str(view.get("mergeable") or ""),
        merge_state=str(view.get("mergeStateStatus") or ""),
        base_changed_files=base_changed,
        reviews=tuple(
            (
                str((r.get("author") or {}).get("login") or ""),
                str((r.get("commit") or {}).get("oid") or ""),
                _epoch(r["submittedAt"]) if r.get("submittedAt") else 0.0,
            )
            for r in view.get("reviews") or []
        ),
    )


def request_copilot_review(facts: HeadFacts, *, gh: Runner = _gh) -> None:
    """Ask Copilot to review the current head (it re-reviews a fix-up push only on request)."""
    if not facts.node_id:
        raise RuntimeError("pull request node id unknown; cannot request a review")
    gh(
        [
            "api",
            "graphql",
            "-f",
            "query=mutation{requestReviews(input:{pullRequestId:%s,botIds:[%s],union:true})"
            "{pullRequest{number}}}"
            % (json.dumps(facts.node_id), json.dumps(COPILOT_BOT_ID)),
        ]
    )


def _deploy_triggering(path: str) -> str:
    """The workflow a merge of `path` would start, or "" for none.

    Returns the name rather than a bool so the verdict can say *what* fires. An
    owner asked to approve "tools/deploy_v2.py triggers a deploy" has to go and
    find out which one; "...starts ops-checks.yml" is a judgement they can make
    from the line itself.
    """
    for glob in DEPLOY_TRIGGERING_GLOBS:
        if fnmatch.fnmatch(path, _normalise(glob)):
            return "on merge"
    for glob, workflow in _declared_deploy_globs():
        if fnmatch.fnmatch(path, _normalise(glob)):
            return workflow
    return ""


def _normalise(glob: str) -> str:
    """`fnmatch` has no `**`; a workflow's `a/**` means "anything under a/"."""
    return glob[:-1] + "*" if glob.endswith("/**") else glob


def evaluate(
    facts: HeadFacts,
    *,
    now: float,
    quiet_minutes: int = QUIET_MINUTES,
    policy: str = "clock",
    settle_minutes: int = SETTLE_MINUTES,
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
    # AGENTS.md's 合流真源唯一 names three things: the right base, `mergeable`,
    # and no conflict. Only the first was ever checked here, while the green
    # verdict printed the word "mergeable" without having asked GitHub. A
    # CONFLICTING pull request reached this function and was reported solely as
    # "N review thread(s) unresolved", which sends a caller to fix the wrong
    # thing.
    # Only meaningful while the pull request is open: GitHub stops computing a
    # test merge once it closes, so a merged head reports mergeable=UNKNOWN
    # forever. Asking anyway turned a one-line "not OPEN" verdict into four
    # lines of noise about a question that no longer has an answer.
    is_open = facts.state == "OPEN"
    if is_open and facts.mergeable == "CONFLICTING":
        reasons.append("GitHub reports mergeable=CONFLICTING: rebase or merge main first")
    elif is_open and facts.mergeable == "UNKNOWN":
        reasons.append(
            "GitHub is still computing mergeable (UNKNOWN): re-run in a moment "
            "rather than treating it as clean"
        )
    elif is_open and facts.mergeable and facts.mergeable != "MERGEABLE":
        reasons.append(f"GitHub reports mergeable={facts.mergeable}, not MERGEABLE")
    if is_open and facts.merge_state and facts.merge_state not in MERGE_STATES_OK:
        if facts.merge_state == "UNKNOWN":
            # Transient for the same reason mergeable=UNKNOWN is: GitHub is
            # still computing the test merge. Saying "not CLEAN" reads like a
            # settled conflict and sends the caller to rebase something that
            # may well be fine.
            reasons.append(
                "mergeStateStatus is still UNKNOWN: re-run in a moment rather "
                "than treating it as a conflict"
            )
        else:
            reasons.append(f"mergeStateStatus is {facts.merge_state}, not CLEAN")
    protected = sorted(f for f in facts.files if f in PROTECTED_FILES)
    if protected:
        reasons.append(
            f"touches protected file(s) {', '.join(protected)}: owner approval of "
            f"head {facts.head_sha[:7]} required"
        )
        owner = True
    fired = {f: _deploy_triggering(f) for f in facts.files}
    deploying = sorted(f for f, w in fired.items() if w)
    if deploying:
        workflows = sorted({fired[f] for f in deploying if fired[f] != "on merge"})
        via = f" via {', '.join(workflows)}" if workflows else ""
        reasons.append(
            f"merging would trigger a deploy{via} ({', '.join(deploying)}): owner "
            f"approval of head {facts.head_sha[:7]} required"
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
    # Green proves the tree the checks ran on. When main has since rewritten
    # files this pull request also touches, that tree is not the one a merge
    # would produce, and nothing re-ran to notice.
    stale = sorted(set(facts.files) & set(facts.base_changed_files))
    if stale and not not_green and is_open:
        shown = ", ".join(stale[:4]) + (f" (+{len(stale) - 4} more)" if len(stale) > 4 else "")
        reasons.append(
            f"checks are green against a base that has since changed {len(stale)} of "
            f"this PR's files ({shown}): update the branch so they re-run"
        )
    if facts.unresolved_threads:
        reasons.append(f"{facts.unresolved_threads} review thread(s) unresolved")
    if facts.review_threads_total > MAX_REVIEW_THREADS:
        reasons.append(
            f"{facts.review_threads_total} review threads, only the first "
            f"{MAX_REVIEW_THREADS} were read: resolve or evaluate by hand"
        )
    if policy not in ("clock", "event", "either"):
        raise ValueError(f"unknown policy {policy!r}")
    # Round up: a fractional second still inside the window is inside the window.
    clock_remaining = math.ceil(facts.last_push_at + quiet_minutes * 60 - now)
    clock_reason = (
        f"head pushed {int(now - facts.last_push_at)}s ago; quiet period has "
        f"{clock_remaining}s to run"
    )
    # A review without a submission time is not a submitted review (r[2] == 0.0 would
    # read as "settled since the epoch"): only timestamped automated reviews count.
    automated = [
        r for r in facts.reviews_on_head() if r[0] in AUTOMATED_REVIEWERS and r[2] > 0
    ]
    if automated:
        reviewed_at = max(r[2] for r in automated)
        event_remaining = math.ceil(reviewed_at + settle_minutes * 60 - now)
        event_reason = (
            f"head reviewed {int(now - reviewed_at)}s ago; settling for "
            f"{event_remaining}s more"
        )
    else:
        event_remaining = None  # no review: the event never happened
        event_reason = (
            f"no automated review on head {facts.head_sha[:7]} yet "
            "(--request-review asks Copilot for one)"
        )
    remaining = 0
    if policy == "clock" and clock_remaining > 0:
        remaining = clock_remaining
        reasons.append(clock_reason)
    elif policy == "event" and (event_remaining is None or event_remaining > 0):
        remaining = event_remaining or 0
        reasons.append(event_reason)
    elif policy == "either":
        event_ok = event_remaining is not None and event_remaining <= 0
        if clock_remaining > 0 and not event_ok:
            remaining = (
                clock_remaining
                if event_remaining is None
                else min(clock_remaining, event_remaining)
            )
            reasons.append(f"{event_reason}; {clock_reason}")
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
            f"({len(facts.checks)}), threads resolved, settled, "
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
    parser.add_argument(
        "--policy",
        choices=("clock", "event", "either"),
        default="clock",
        help="settling rule: 12 min after the push (clock), 3 min after an automated "
        "review of the head (event), or whichever comes first (either)",
    )
    parser.add_argument(
        "--request-review",
        action="store_true",
        help="ask Copilot to review the head when no automated review of it exists",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="run OMCA fast audit and block merge on critical architectural or blindfold findings",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = parser.parse_args(argv)

    from libs.console import error, success, warning

    facts = collect(args.number, repo=args.repo, gh=gh)
    if args.request_review and not any(
        r[0] in AUTOMATED_REVIEWERS and r[2] > 0 for r in facts.reviews_on_head()
    ):
        request_copilot_review(facts, gh=gh)
        warning(f"#{facts.number}: Copilot review requested on {facts.head_sha[:7]}")
    verdict = evaluate(
        facts, now=now(), quiet_minutes=args.quiet_minutes, policy=args.policy
    )
    if args.audit:
        try:
            from tools.omca_gate_policy import evaluate_audit_report
            audit_proc = subprocess.run(
                ["omca", "audit", "--json", "--mode", "fast"],
                capture_output=True,
                text=True,
                check=False,
            )
            if audit_proc.returncode != 0 and not audit_proc.stdout.strip():
                verdict.reasons.append(
                    f"omca audit failed to run (rc={audit_proc.returncode}): {audit_proc.stderr.strip()[:100]}"
                )
                verdict.ready = False
            else:
                report = json.loads(audit_proc.stdout)
                passed, blocking, _ = evaluate_audit_report(report, expect_sha=facts.head_sha)
                if not passed:
                    for b in blocking:
                        verdict.reasons.append(f"omca audit blocked: {b}")
                    verdict.ready = False
        except Exception as exc:
            verdict.reasons.append(f"omca audit execution error: {exc}")
            verdict.ready = False
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
                    "policy": args.policy,
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
