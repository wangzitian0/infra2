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
# A field `gh` was asked for and did not return, distinct from one never asked for.
ABSENT = "ABSENT"
DEFAULT_REPO = "wangzitian0/infra2"
QUIET_MINUTES = 12
SETTLE_MINUTES = 3  # event policy: after the review of the head, not after the push
# GitHub's Copilot pull-request reviewer (a global bot id, the same in every repository).
COPILOT_BOT_ID = "BOT_kgDOCnlnWA"
AUTOMATED_REVIEWERS = frozenset({"copilot-pull-request-reviewer"})
# AGENTS.md: protected files need the owner's approval of the head that changes them.
SELF_GOVERNING_FILES = (
    # The gate decides from the working tree, which for an agent merging its
    # own PR is that PR's branch -- so a change here is judged by the version
    # it introduces. Its tests ship in the same PR and can be edited by it.
    "tools/pr_merge_gate.py",
    "libs/tests/test_pr_merge_gate.py",
    # #765 moved the merge-authority rules out of the protected AGENTS.md and
    # into SSOT. The rules stayed protected in name only: a .md-only PR also
    # skips every required check, so an agent could rewrite its own authority
    # and merge it with nothing run and nothing asked.
    "docs/ssot/ops.merge-gate.md",
    # Flipping one gate to `blocks_merge: false` removes it from the required
    # set that judges the very PR doing the flipping, and ci_gate_audit does
    # not cross-check that field against the live ruleset.
    "docs/ssot/ci-gate-inventory.yaml",
)
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
# Workflows whose push-to-main deploy never reaches prod. Owner approval is
# scoped by environment (2026-09-21): staging, the reserved pr-0 canary slot and
# the report-branch-main preview are the agent's to merge; prod is not.
#
# This stays a written list because the question -- which environment does this
# deploy reach -- is not stated anywhere in the workflow file. It is the same
# shape as DEPLOY_MARKERS: the judgement is written down, the paths are derived.
NON_PROD_DEPLOY_WORKFLOWS = frozenset({"ops-checks.yml"})

DEPLOY_MARKERS = (
    "deploy_v2",
    "wrangler deploy",
    "iac_runner",
    "repository_dispatch",
    # apply-observability.yml pushes to main and runs
    # `invoke fr-observability.shared.apply-alerts` / `.apply-dashboard`
    # against the live SigNoz. AGENTS.md names "observability apply" as a
    # high-risk merge side effect by name, and the first version of this list
    # missed it -- the same omission it was written to stop.
    "invoke fr-observability",
    "terraform apply",
)
WORKFLOW_DIR = ROOT / ".github" / "workflows"


@functools.lru_cache(maxsize=1)
def _declared_deploy_globs() -> tuple[tuple[tuple[str, str], ...], bool]:
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
        names = sorted(WORKFLOW_DIR.glob("*.y*ml"))  # Actions accepts .yaml too
    except OSError:
        return (), False
    if not names:
        # Path.glob swallows scandir errors, so a missing or unreadable
        # directory arrives here indistinguishable from an empty one -- which
        # is why the OSError branch above is effectively unreachable. An
        # existing-but-empty directory is a real repository state; one that is
        # not there at all is a broken read, and reporting it healthy silently
        # retired every derived deploy path.
        return (), WORKFLOW_DIR.is_dir()
    parsed = 0
    for path in names:
        try:
            text = path.read_text(encoding="utf-8")
            doc = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        parsed += 1
        if not isinstance(doc, dict):
            continue
        # PyYAML resolves a bare `on:` key to the boolean True.
        triggers = doc.get("on") if isinstance(doc.get("on"), dict) else doc.get(True)
        push = (triggers or {}).get("push") if isinstance(triggers, dict) else None
        if not isinstance(push, dict):
            continue
        # Actions accepts a bare string wherever it accepts a list, and an
        # omitted `branches` means every branch -- which includes main. Reading
        # only the list form silently skipped such a workflow, under-detecting
        # in the one direction that matters.
        # Actions: `tags` without `branches` means tag pushes only, so a merge
        # to main cannot start it. Reading an absent `branches` as "every
        # branch" is right in general and wrong here, and it was masked until
        # the `paths` case below stopped defaulting to nothing.
        if "tags" in push and "branches" not in push:
            continue
        if "branches" in push and "main" not in _as_list(push["branches"]):
            continue
        if not any(marker in text for marker in DEPLOY_MARKERS):
            continue
        paths = _as_list(push.get("paths"))
        if "paths" not in push:
            # Omitted `paths` means every path, the mirror of the `branches`
            # case above. Reading it as "no paths" made the broadest deploy
            # workflow contribute nothing -- coverage inverting with reach.
            paths = ["*", "**"]
        globs.extend((str(p), path.name) for p in paths)
    # Workflows present but none readable means the derivation is broken, not
    # that nothing deploys.
    # Every workflow must parse, not merely one of them. `parsed > 0` was the
    # first attempt and does not achieve the intent: with one file broken among
    # many, its paths vanish while the read still reports healthy, which is the
    # under-detection the derivation exists to prevent.
    return tuple(dict.fromkeys(globs)), parsed == len(names)


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


@functools.lru_cache(maxsize=1)
def _required_checks() -> tuple[frozenset[str], bool]:
    """Display names of the `blocks_merge: true` gates, and whether they read.

    This catches a required gate that never registered at all. It deliberately
    does NOT block on `skipping`: infra-ci.yml:69-82 states that skipping a
    required job via `if:` is a designed passing state -- a PR whose whole diff
    is Markdown needs none of those gates, and PRs #709 and #673 merged in
    exactly that shape. An earlier version of this function blocked on it, which
    made every docs-only PR unmergeable forever, including the one carrying this
    repository's own merge-authority rules.

    The case that version meant to catch -- `detect-changes` failing and taking
    its dependents down as `skipped` -- is already caught, because that job is
    itself red and `not_green` scans every check. The skip branch bought nothing
    and cost the designed path.

    The inventory stores a job key; `gh pr checks` reports the workflow's
    display name, so the name is read from the workflow rather than guessed.
    An unreadable inventory returns unhealthy, and the caller blocks on it:
    losing this list loses the only check that notices a gate never ran.
    """
    try:
        doc = yaml.safe_load((ROOT / "docs/ssot/ci-gate-inventory.yaml").read_text())
        gates = [g for g in (doc.get("gates") or []) if g.get("blocks_merge")]
    except (OSError, yaml.YAMLError, AttributeError):
        return frozenset(), False
    if not gates:
        return frozenset(), False
    names: set[str] = set()
    for gate in gates:
        job, workflow = gate.get("job"), gate.get("workflow")
        display = job
        try:
            spec = yaml.safe_load((ROOT / str(workflow)).read_text())
            display = ((spec.get("jobs") or {}).get(job) or {}).get("name") or job
        except (OSError, yaml.YAMLError, AttributeError, TypeError):
            pass
        if display:
            names.add(str(display))
    return frozenset(names), True


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
    # GitHub's own merge computation. "UNKNOWN" means GitHub is still computing
    # a test merge and the caller should re-poll. ABSENT means `gh` was asked
    # for the field and did not return it -- a gh version change, a permission
    # downgrade, an API shape change -- which must block rather than pass: the
    # field's whole job is to block, so losing it silently loses the check.
    # "" is reserved for a HeadFacts built by hand, where the field was never
    # requested and there is nothing to have lost.
    mergeable: str = ""
    merge_state: str = ""
    # Files the base branch has changed since this head diverged from it. Their
    # intersection with `files` is what makes a green check stale.
    base_changed_files: tuple[str, ...] = ()
    # What GitHub says the PR changes, against what `files` actually returned.
    # `gh pr view --json files` caps at 100; both the protected-file and the
    # deploy-triggering checks iterate `files`, so a larger PR silently drops
    # the owner gate. Measured on finance_report#2042: changedFiles=163,
    # files=100, and everything from docs/ libs/ scripts/ tools/ uv.lock
    # onward was past the cut.
    changed_files: int = 0
    # GitHub's own review verdict. The gate judged unresolved threads only, so a
    # reviewer clicking Request changes without leaving a resolvable thread was
    # invisible -- and with required_approving_review_count: 0 the ruleset does
    # not withhold the merge either, so mergeStateStatus stays CLEAN.
    review_decision: str = ""
    # Fields this call asked `gh` for and did not get back. Guarding one field
    # at a time left changedFiles with the old `or 0` treatment, and
    # `if facts.changed_files` then switched off the very guard that exists to
    # stop a truncated file list from dropping the owner gates -- a blind audit
    # showed the gate issuing `gh pr merge` on a 163-file PR because of it.
    absent_fields: tuple[str, ...] = ()
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


GH_TIMEOUT_S = 60


def _gh(argv: Sequence[str]) -> str:
    # This runs unattended, so stdin is closed rather than inherited: a gh that
    # decides to prompt (auth re-login, a confirmation) would otherwise block on
    # a terminal that is not there, and the gate would hang instead of failing.
    # The timeout is the same argument for the network.
    try:
        result = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gh {' '.join(argv)}: no response in {GH_TIMEOUT_S}s"
        ) from exc
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
            gh(
                [
                    "api",
                    f"repos/{repo}/compare/{head_sha}...{base}",
                    "--jq",
                    "{files:[.files[]?.filename]}",
                ]
            )
        )
    except (RuntimeError, json.JSONDecodeError):
        return ()
    return tuple(str(f) for f in payload.get("files") or [])


def _field(view: dict, name: str) -> str:
    """A requested field's value, or ABSENT when it is missing OR empty.

    Keying on the key alone was one layer too shallow: `gh` returning
    `{"mergeable": null}` kept the key present, coalesced to "", and re-opened
    the fail-open. Both shapes mean the same thing -- the answer this call asked
    for did not arrive -- so both must block.
    """
    value = view.get(name)
    return str(value) if value else ABSENT


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
                "number,state,isDraft,baseRefName,headRefOid,files,changedFiles,commits,id,reviews,"
                "reviewDecision,"
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
    # The commits connection is capped at 100 and returned oldest-first, so on a
    # long branch the newest commit -- the one the quiet period is measured from
    # -- is exactly the one missing. And an empty list yields last_push = 0.0,
    # which reads as "pushed in 1970" and settles instantly. Both are caught by
    # asking whether the head itself came back.
    head_sha = str(view.get("headRefOid") or "")
    head_seen = any(str(c.get("oid") or "") == head_sha for c in commits)
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
        changed_files=int(view.get("changedFiles") or 0),
        review_decision=str(view.get("reviewDecision") or ""),
        absent_fields=tuple(
            name
            for name in (
                "files",
                "changedFiles",
                "commits",
                "mergeable",
                "mergeStateStatus",
            )
            if name not in view
        ),
        last_push_at=last_push if head_seen else 0.0,
        checks=tuple(
            (str(c["name"]), str(c.get("bucket") or c.get("state") or ""))
            for c in checks
        ),
        unresolved_threads=sum(1 for n in nodes if not n.get("isResolved")),
        review_threads_total=int(review_threads.get("totalCount") or len(nodes)),
        node_id=str(view.get("id") or ""),
        # ABSENT, not "", when gh omits a field this call explicitly requested.
        mergeable=_field(view, "mergeable"),
        merge_state=_field(view, "mergeStateStatus"),
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
    # No translation is needed for `**`: unlike a shell glob, fnmatch's `*`
    # matches "/" as well, so "a/**" and "a/*" both already match "a/b/c.py".
    # An earlier version of this function called a _normalise() helper that
    # claimed to convert them and was in fact a no-op ("a/**"[:-1] + "*" is
    # "a/**"), which is worse than doing nothing: it read as though the case
    # were handled.
    for glob in DEPLOY_TRIGGERING_GLOBS:
        if fnmatch.fnmatch(path, glob):
            return "on merge"
    for glob, workflow in _declared_deploy_globs()[0]:
        if fnmatch.fnmatch(path, glob) and workflow not in NON_PROD_DEPLOY_WORKFLOWS:
            return workflow
    return ""


def _as_list(value: object) -> list[str]:
    """A YAML field that accepts a string or a list, read as a list either way."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


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
    if facts.state == "OPEN" and not facts.last_push_at:
        reasons.append(
            "no timestamp for the head commit, so the settling window cannot be "
            "measured: gh returned no commits, or the head was past the 100 it "
            "returns oldest-first"
        )
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
    absent = sorted(
        set(facts.absent_fields)
        | {
            name
            for name, value in (
                ("mergeable", facts.mergeable),
                ("mergeStateStatus", facts.merge_state),
            )
            if value == ABSENT
        }
    )
    if is_open and absent:
        reasons.append(
            f"gh did not return {', '.join(absent)} although asked for it: the "
            "conflict check is unavailable, so this cannot be judged ready"
        )
    # ABSENT is already named once above; letting it fall through would repeat
    # the same fact as three reasons and bury the one that explains it.
    if is_open and facts.mergeable == ABSENT:
        pass
    elif is_open and facts.mergeable == "CONFLICTING":
        reasons.append(
            "GitHub reports mergeable=CONFLICTING: rebase or merge main first"
        )
    elif is_open and facts.mergeable == "UNKNOWN":
        reasons.append(
            "GitHub is still computing mergeable (UNKNOWN): re-run in a moment "
            "rather than treating it as clean"
        )
    elif is_open and facts.mergeable and facts.mergeable != "MERGEABLE":
        reasons.append(f"GitHub reports mergeable={facts.mergeable}, not MERGEABLE")
    if (
        is_open
        and facts.merge_state
        and facts.merge_state != ABSENT
        and facts.merge_state not in MERGE_STATES_OK
    ):
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
    if facts.changed_files and len(facts.files) < facts.changed_files:
        reasons.append(
            f"gh returned {len(facts.files)} of {facts.changed_files} changed files "
            "(the API caps at 100): the protected-file and deploy checks read that "
            "list, so neither can be trusted here"
        )
        owner = True
    governing = sorted(f for f in facts.files if f in SELF_GOVERNING_FILES)
    if governing:
        reasons.append(
            f"changes what decides merges ({', '.join(governing)}): the working-tree "
            f"copy is what judged this PR, so owner approval of head "
            f"{facts.head_sha[:7]} is required"
        )
        owner = True
    protected = sorted(f for f in facts.files if f in PROTECTED_FILES)
    if protected:
        reasons.append(
            f"touches protected file(s) {', '.join(protected)}: owner approval of "
            f"head {facts.head_sha[:7]} required"
        )
        owner = True
    if not _declared_deploy_globs()[1]:
        reasons.append(
            "cannot read .github/workflows to determine which paths deploy on "
            "merge: fix the read rather than merging on an unknown"
        )
        # exit 1 means "not yet"; a poller retries it forever. Not knowing
        # whether a merge deploys is precisely the case that must escalate.
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
    required, inventory_read = _required_checks()
    if not inventory_read:
        reasons.append(
            "cannot read docs/ssot/ci-gate-inventory.yaml: the required-check "
            "list is unavailable, so a gate that never ran cannot be noticed"
        )
    reported = {name for name, _ in facts.checks}
    missing = sorted(required - reported)
    if missing:
        reasons.append(f"required check(s) never reported: {', '.join(missing)}")
    # `skipping` is the designed state for a docs-only PR, and blocking on it
    # outright made those permanently unmergeable. But the gate still has to
    # tell 适用 from 意外 skipped, and the workflow's own answer cannot be
    # trusted for it: infra-ci computes has_non_doc from `git diff` against the
    # base, so a rewritten base yields a wrong file list, emits
    # has_non_doc=false, and reports Detect Non-Doc Changes GREEN while every
    # required gate skips. GitHub's own file list for the PR is independent of
    # that computation, so it is what decides here.
    if any(not f.endswith(".md") for f in facts.files):
        skipped = sorted(
            name
            for name, verdict in facts.checks
            if name in required and verdict in ("skipping", "SKIPPED")
        )
        if skipped:
            reasons.append(
                f"required check(s) skipped although this PR changes non-Markdown "
                f"files: {', '.join(skipped)}"
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
        shown = ", ".join(stale[:4]) + (
            f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
        )
        reasons.append(
            f"checks are green against a base that has since changed {len(stale)} of "
            f"this PR's files ({shown}): update the branch so they re-run"
        )
    if facts.review_decision == "CHANGES_REQUESTED":
        reasons.append(
            "a reviewer has requested changes: resolve it with them rather than "
            "merging over it"
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
            if audit_proc.returncode != 0:
                verdict.reasons.append(
                    f"omca audit failed to run (rc={audit_proc.returncode}): {audit_proc.stderr.strip()[:100]}"
                )
                verdict.ready = False
            else:
                report = json.loads(audit_proc.stdout)
                passed, blocking, _ = evaluate_audit_report(
                    report, expect_sha=facts.head_sha
                )
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
