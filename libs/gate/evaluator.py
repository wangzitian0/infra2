"""Step-based pipeline evaluator for the PR merge gate."""

from __future__ import annotations

import math

from libs.gate.inventory import (
    _declared_deploy_globs,
    _deploy_triggering,
    _required_checks,
)
from libs.gate.self_governance import (
    _owner_instruction_quoted,
    is_self_governing,
)
from libs.gate.types import (
    ABSENT,
    AUTOMATED_REVIEWERS,
    BLOCKING_SEVERITY_TOTAL,
    GREEN_BUCKETS,
    GREEN_STATES,
    MAX_REVIEW_THREADS,
    MERGE_STATES_OK,
    QUIET_MINUTES,
    RULE_TEXT_FILES,
    SETTLE_MINUTES,
    HeadFacts,
    Verdict,
    _is_local_root_repo,
)


def _check_pr_state_and_conflicts(facts: HeadFacts, reasons: list[str]) -> bool:
    """Validate PR state, base branch, mergeability, and conflict status."""
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

    return owner


def _check_root_repo_rules_and_drift(facts: HeadFacts, reasons: list[str]) -> bool:
    """Validate rule drift, self-governing closure, and deploy-triggering paths."""
    owner = False
    if not _is_local_root_repo(facts.repo):
        return owner

    if facts.rule_drift:
        reasons.append(
            f"the working tree's copy of the merge rules is not "
            f"{facts.base}'s ({', '.join(facts.rule_drift)}): every verdict here "
            f"would be issued under rules that are not merged — check out {facts.base} "
            f"cleanly and re-run, or land those changes first"
        )
        owner = True

    quoted_instruction = _owner_instruction_quoted(facts.body)
    governing = sorted(f for f in facts.files if is_self_governing(f))
    unproven = [
        f
        for f in governing
        if f not in facts.proven_tighter
        and not (f in RULE_TEXT_FILES and quoted_instruction)
    ]
    if unproven:
        reasons.append(
            f"changes what decides merges ({', '.join(unproven)}) without a mechanical "
            f"proof that the change can only make this gate say no more often: the "
            f"working-tree copy is what judged this PR, so owner approval of head "
            f"{facts.head_sha[:7]} is required"
        )
        if any(f in RULE_TEXT_FILES for f in unproven):
            reasons.append(
                "rule-text files (AGENTS.md / docs/ssot/ops.merge-gate.md) can clear "
                "this instead by citing the owner instruction that authorised the "
                "edit in the PR body, under a heading matching 'owner instruction' / "
                "'owner 指示' followed by a quoted line (`> ...` or 「...」)"
            )
        owner = True

    if not _declared_deploy_globs()[1]:
        reasons.append(
            "cannot read .github/workflows to determine which paths deploy on "
            "merge: fix the read rather than merging on an unknown"
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

    return owner


def _check_checks_green_and_stale(
    facts: HeadFacts, *, is_open: bool, reasons: list[str]
) -> None:
    """Validate check outcomes and detect stale green runs against updated base."""
    not_green = sorted(
        name
        for name, verdict in facts.checks
        if verdict not in GREEN_BUCKETS and verdict not in GREEN_STATES
    )
    if not facts.checks:
        reasons.append("no checks reported yet")
    if not_green:
        reasons.append(f"check(s) not green: {', '.join(not_green)}")

    stale = sorted(set(facts.files) & set(facts.base_changed_files))
    if stale and not not_green and is_open:
        shown = ", ".join(stale[:4]) + (
            f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
        )
        reasons.append(
            f"checks are green against a base that has since changed {len(stale)} of "
            f"this PR's files ({shown}): update the branch so they re-run"
        )


def _check_reviews(facts: HeadFacts, reasons: list[str]) -> None:
    """Validate review decision and unresolved thread weights."""
    if facts.review_decision == "CHANGES_REQUESTED":
        reasons.append(
            "a reviewer has requested changes: resolve it with them rather than "
            "merging over it"
        )
    if facts.unresolved_weight >= BLOCKING_SEVERITY_TOTAL:
        reasons.append(
            f"{facts.unresolved_threads} unresolved review thread(s) weigh "
            f"{facts.unresolved_weight:g} (AGENTS.md blocks at "
            f"{BLOCKING_SEVERITY_TOTAL:g}; unlabelled counts as middle)"
        )
    if facts.review_threads_total > MAX_REVIEW_THREADS:
        reasons.append(
            f"{facts.review_threads_total} review threads, only the first "
            f"{MAX_REVIEW_THREADS} were read: resolve or evaluate by hand"
        )


def _check_settling_window(
    facts: HeadFacts,
    *,
    now: float,
    quiet_minutes: int,
    policy: str,
    settle_minutes: int,
    reasons: list[str],
) -> int:
    """Evaluate quiet period or event settling policy; return remaining seconds."""
    if policy not in ("clock", "event", "either"):
        raise ValueError(f"unknown policy {policy!r}")

    clock_remaining = math.ceil(facts.last_push_at + quiet_minutes * 60 - now)
    clock_reason = (
        f"head pushed {int(now - facts.last_push_at)}s ago; quiet period has "
        f"{clock_remaining}s to run"
    )

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
        event_remaining = None
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

    return remaining


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

    # Step 1: PR state, base branch, conflicts, and truncation
    owner1 = _check_pr_state_and_conflicts(facts, reasons)

    # Step 2: Root repo rules, drift, self-governing closure, and deploy triggers
    owner2 = _check_root_repo_rules_and_drift(facts, reasons)

    # Step 3: CI checks greenness and freshness
    _check_checks_green_and_stale(
        facts, is_open=(facts.state == "OPEN"), reasons=reasons
    )

    # Step 4: Review threads and decision
    _check_reviews(facts, reasons)

    # Step 5: Settling window
    remaining = _check_settling_window(
        facts,
        now=now,
        quiet_minutes=quiet_minutes,
        policy=policy,
        settle_minutes=settle_minutes,
        reasons=reasons,
    )

    return Verdict(
        ready=not reasons,
        owner_required=owner1 or owner2,
        reasons=reasons,
        quiet_remaining_seconds=max(0, remaining),
    )


def render(facts: HeadFacts, verdict: Verdict) -> str:
    """Render a human-readable verdict description."""
    head = f"#{facts.number} @ {facts.head_sha[:7]}"
    if verdict.ready:
        return (
            f"{head}: mergeable under session authority — checks green "
            f"({len(facts.checks)}), threads resolved, settled, "
            "no protected or deploy-triggering paths"
        )
    kind = "needs the owner" if verdict.owner_required else "not yet"
    return f"{head}: {kind}\n" + "\n".join(f"  - {r}" for r in verdict.reasons)
