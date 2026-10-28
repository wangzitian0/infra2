"""Step-based pipeline evaluator for the PR merge gate."""

from __future__ import annotations

import math
import re

from common.gate.inventory import (
    _declared_deploy_globs,
    _deploy_triggering,
    _required_check_workflows,
    _required_checks,
)

# Keep module import so the closure walk in self_governance.py sees production_contract.
import common.gate.production_contract as production_contract  # noqa: F401
from common.gate.self_governance import (
    SCHEDULED_AUTOMATION_FILES,
    _mutation_evidence_listed,  # noqa: F401
    _owner_instruction_quoted,
    is_self_governing,
)
from common.gate.types import (
    ABSENT,
    ACT,
    AUTOMATED_REVIEWERS,
    BLOCKING_SEVERITY_TOTAL,
    GREEN_BUCKETS,
    GREEN_STATES,
    MAX_REVIEW_THREADS,
    MERGE_STATES_OK,
    OWNER,
    QUIET_MINUTES,
    REVIEW,
    RULE_TEXT_FILES,
    SETTLE_SECONDS,
    UNEVALUABLE,
    WAIT,
    WORKFLOW_PREFIX,
    HeadFacts,
    Reasons,
    Verdict,
    _is_local_root_repo,
    _normalized_path,
)

# Check states GitHub reports while a check has not finished yet.
PENDING_STATES = frozenset(
    {"pending", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
)


def _check_pr_state_and_conflicts(facts: HeadFacts, reasons: Reasons) -> bool:
    """Validate PR state, base branch, mergeability, and conflict status."""
    owner = False
    if facts.state == "OPEN" and not facts.last_push_at:
        reasons.append(
            "no timestamp for the head commit, so the settling window cannot be "
            "measured: gh returned no commits, or the head was past the 100 it "
            "returns oldest-first",
            UNEVALUABLE,
        )
    if facts.state != "OPEN":
        reasons.append(f"pull request is {facts.state or 'unknown'}, not OPEN", ACT)
    if facts.draft:
        reasons.append("pull request is a draft", ACT)
    if facts.base != "main":
        reasons.append(f"base branch is {facts.base!r}, not main", REVIEW)

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
            "conflict check is unavailable, so this cannot be judged ready",
            UNEVALUABLE,
        )

    if is_open and facts.mergeable == ABSENT:
        pass
    elif is_open and facts.mergeable == "CONFLICTING":
        reasons.append(
            "GitHub reports mergeable=CONFLICTING: rebase or merge main first", ACT
        )
    elif is_open and facts.mergeable == "UNKNOWN":
        reasons.append(
            "GitHub is still computing mergeable (UNKNOWN): re-run in a moment "
            "rather than treating it as clean"
        )
    elif is_open and facts.mergeable and facts.mergeable != "MERGEABLE":
        reasons.append(
            f"GitHub reports mergeable={facts.mergeable}, not MERGEABLE", ACT
        )

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
        elif facts.merge_state == "BLOCKED":
            # Usually required checks still running; red checks are reported below.
            reasons.append(f"mergeStateStatus is {facts.merge_state}, not CLEAN")
        else:
            reasons.append(f"mergeStateStatus is {facts.merge_state}, not CLEAN", ACT)

    if facts.changed_files and len(facts.files) < facts.changed_files:
        reasons.append(
            f"gh returned {len(facts.files)} of {facts.changed_files} changed files "
            "(the API caps at 100): the protected-file and deploy checks read that "
            "list, so neither can be trusted here",
            UNEVALUABLE,
        )

    return owner


def _check_root_repo_rules_and_drift(facts: HeadFacts, reasons: Reasons) -> bool:
    """Validate rule drift, self-governing closure, and deploy-triggering paths."""
    owner = False
    if not _is_local_root_repo(facts.repo):
        return owner

    if facts.rule_drift:
        reasons.append(
            f"the working tree's copy of the merge rules is not "
            f"{facts.base}'s ({', '.join(facts.rule_drift)}): every verdict here "
            f"would be issued under rules that are not merged — check out {facts.base} "
            f"cleanly and re-run, or land those changes first",
            UNEVALUABLE,
        )

    # The production lock (#1138) replaces the owner for a workflow file, unless the
    # file defines a required check. A required-check workflow stays in the review set
    # when the lock holds, and goes to the owner when the lock does not hold.
    lock_holds = facts.lock_failures == ()
    required_workflows = _required_check_workflows()

    def _requires(path: str) -> bool:
        return required_workflows is not None and _normalized_path(path) in (
            required_workflows
        )

    scheduled_automation = {_normalized_path(p) for p in SCHEDULED_AUTOMATION_FILES}

    def _is_scheduled_automation(path: str) -> bool:
        return _normalized_path(path) in scheduled_automation

    uncovered = sorted(
        f
        for f in facts.files
        if _normalized_path(f).startswith(WORKFLOW_PREFIX)
        and (required_workflows is None or _requires(f))
    )
    lock_covered = {
        f
        for f in facts.files
        if lock_holds and f.startswith(WORKFLOW_PREFIX) and f not in uncovered
    }
    quoted_instruction = _owner_instruction_quoted(facts.body)
    tree_approved = bool(
        facts.head_tree_sha and facts.head_tree_sha in set(facts.approved_tree_shas)
    )
    review_recorded = (
        any(r[0] in AUTOMATED_REVIEWERS and r[2] > 0 for r in facts.reviews_on_head())
        or _verified_at(facts) is not None
    )
    checklist_clears = lock_holds and review_recorded
    governing = sorted(
        f for f in facts.files if is_self_governing(f) and f not in lock_covered
    )
    # A held file never clears through the direction proof, in any lock state.
    # Rule-text files (AGENTS.md / docs/ssot/ops.merge-gate.md) require a quoted
    # owner instruction, an independent verification comment, or an approved tree SHA.
    # Scheduled automation files always require the owner.
    unproven = [
        f
        for f in governing
        if (f not in facts.proven_tighter or f in uncovered)
        and not (
            f in RULE_TEXT_FILES
            and (quoted_instruction or (lock_holds and _verified_at(facts) is not None))
        )
        and not (
            not _is_scheduled_automation(f)
            and f not in RULE_TEXT_FILES
            and (f not in uncovered or required_workflows is not None)
            and checklist_clears
        )
        and not tree_approved
    ]
    # The reason for a held file comes first. The "without a proof" line names only
    # the files that have no proof (#1138 round 4).
    held = [f for f in unproven if f in uncovered]
    if held:
        held_kind = REVIEW if lock_holds else OWNER
        if required_workflows is None:
            reasons.append(
                "cannot read a blocking gate with a workflow from "
                "docs/ssot/ci-gate-inventory.yaml: no workflow file clears through "
                f"the lock or the direction proof ({', '.join(held)})",
                held_kind,
            )
        else:
            for f in held:
                if _requires(f):
                    if lock_holds:
                        reasons.append(
                            f"{f} defines a required check: an independent review of "
                            f"head {facts.head_sha[:7]} is required; the workflow "
                            "proof does not read job bodies, so it cannot clear this file",
                            REVIEW,
                        )
                    else:
                        reasons.append(
                            f"{f} defines a required check: the owner approves head "
                            f"{facts.head_sha[:7]}; the workflow proof does not read job "
                            "bodies, so it cannot clear this file",
                            OWNER,
                        )
        if not lock_holds:
            owner = True
    proofless = [f for f in unproven if f not in facts.proven_tighter]
    owner_proofless = [
        f
        for f in proofless
        if _is_scheduled_automation(f)
        or (not lock_holds and _normalized_path(f).startswith(WORKFLOW_PREFIX))
    ]
    review_proofless = [f for f in proofless if f not in owner_proofless]
    if owner_proofless:
        reasons.append(
            f"changes what decides merges ({', '.join(owner_proofless)}) without a mechanical "
            f"proof that the change can only make this gate say no more often: the "
            f"working-tree copy is what judged this PR, so owner approval of head "
            f"{facts.head_sha[:7]} is required",
            OWNER,
        )
        owner = True
    if review_proofless:
        reasons.append(
            f"changes what decides merges ({', '.join(review_proofless)}) without a mechanical "
            f"proof that the change can only make this gate say no more often: the "
            f"working-tree copy is what judged this PR, so an independent review of head "
            f"{facts.head_sha[:7]} is required",
            REVIEW,
        )
        if any(f in RULE_TEXT_FILES for f in review_proofless):
            reasons.append(
                "rule-text files (AGENTS.md / docs/ssot/ops.merge-gate.md) can clear "
                "this instead by citing the owner instruction that authorised the "
                "edit in the PR body, under a heading matching 'owner instruction' / "
                "'owner 指示' followed by a quoted line (`> ...` or 「...」)",
                REVIEW,
            )

    if not _declared_deploy_globs()[1]:
        reasons.append(
            "cannot read .github/workflows to determine which paths deploy on "
            "merge: fix the read rather than merging on an unknown",
            UNEVALUABLE,
        )

    fired = {f: _deploy_triggering(f) for f in facts.files}
    deploying = sorted(f for f, w in fired.items() if w)
    # With the lock, a production job that the merge starts waits for the reviewer of
    # the production environment. Reasons cannot carry a note that does not block.
    if deploying and not lock_holds and not tree_approved:
        workflows = sorted({fired[f] for f in deploying if fired[f] != "on merge"})
        via = f" via {', '.join(workflows)}" if workflows else ""
        reasons.append(
            f"merging would trigger a deploy{via} ({', '.join(deploying)}): owner "
            f"approval of head {facts.head_sha[:7]} required",
            OWNER,
        )
        owner = True

    # This line explains an owner verdict that is already set; it does not add one.
    held_workflows = [
        f for f in unproven if _normalized_path(f).startswith(WORKFLOW_PREFIX)
    ]
    if facts.lock_failures and (deploying or held_workflows):
        reasons.append(
            f"production lock not verified: {'; '.join(facts.lock_failures)}",
            OWNER,
        )

    required, inventory_read = _required_checks()
    if not inventory_read:
        reasons.append(
            "cannot read docs/ssot/ci-gate-inventory.yaml: the required-check "
            "list is unavailable, so a gate that never ran cannot be noticed",
            UNEVALUABLE,
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
                f"files: {', '.join(skipped)}",
                ACT,
            )

    return owner


def _check_checks_green_and_stale(
    facts: HeadFacts, *, is_open: bool, reasons: Reasons
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
        finished_red = any(
            verdict not in PENDING_STATES
            for name, verdict in facts.checks
            if name in not_green
        )
        reasons.append(
            f"check(s) not green: {', '.join(not_green)}",
            ACT if finished_red else WAIT,
        )

    stale = sorted(set(facts.files) & set(facts.base_changed_files))
    if stale and not not_green and is_open:
        shown = ", ".join(stale[:4]) + (
            f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
        )
        reasons.append(
            f"checks are green against a base that has since changed {len(stale)} of "
            f"this PR's files ({shown}): update the branch so they re-run",
            ACT,
        )


def _check_reviews(facts: HeadFacts, reasons: Reasons) -> None:
    """Validate review decision and unresolved thread weights."""
    if facts.review_decision == "CHANGES_REQUESTED":
        reasons.append(
            "a reviewer has requested changes: resolve it with them rather than "
            "merging over it",
            ACT,
        )
    if facts.unresolved_weight >= BLOCKING_SEVERITY_TOTAL:
        reasons.append(
            f"{facts.unresolved_threads} unresolved review thread(s) weigh "
            f"{facts.unresolved_weight:g} (AGENTS.md blocks at "
            f"{BLOCKING_SEVERITY_TOTAL:g}; unlabelled counts as middle)",
            ACT,
        )
    if facts.review_threads_total > MAX_REVIEW_THREADS:
        reasons.append(
            f"{facts.review_threads_total} review threads, only the first "
            f"{MAX_REVIEW_THREADS} were read: resolve or evaluate by hand",
            ACT,
        )


def _opened_by_bot(facts: HeadFacts) -> bool:
    """True when an app or bot account opened the PR. Copilot does not review
    such a PR, and a review request from the app token does not register (#1075)."""
    typename, login = facts.author
    return typename == "Bot" or login.endswith("[bot]") or login.startswith("app/")


VERIFICATION_HEADING = "### Independent verification of head"
_VERIFICATION_HEADING_RE = re.compile(
    r"^" + re.escape(VERIFICATION_HEADING) + r"[ \t]+`?([0-9a-f]{7,40})`?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_TEST_NAME_RE = re.compile(r"\btest_\w+|[\w./-]+::\w+")


def _verified_at(facts: HeadFacts) -> float | None:
    """When the newest independent verification comment of the current head got
    its text, or None. The heading must name the current head (7 or more leading
    hex characters of its SHA) and a later line must name a test (#1075)."""
    head = facts.head_sha.lower()
    times = [
        at
        for body, at in facts.comments
        if len(head) >= 7
        and any(
            head.startswith(match.group(1).lower())
            and _TEST_NAME_RE.search(body, match.end())
            for match in _VERIFICATION_HEADING_RE.finditer(body)
        )
    ]
    return max(times, default=None)


def _check_settling_window(
    facts: HeadFacts,
    *,
    now: float,
    quiet_minutes: int,
    policy: str,
    settle_seconds: int,
    reasons: Reasons,
) -> int:
    """Evaluate quiet period or event settling policy; return remaining seconds."""
    if policy not in ("clock", "event", "either"):
        raise ValueError(f"unknown policy {policy!r}")
    if not facts.last_push_at:
        return 0

    clock_remaining = math.ceil(facts.last_push_at + quiet_minutes * 60 - now)
    clock_reason = (
        f"head pushed {int(now - facts.last_push_at)}s ago; quiet period has "
        f"{clock_remaining}s to run"
    )

    automated = [
        r for r in facts.reviews_on_head() if r[0] in AUTOMATED_REVIEWERS and r[2] > 0
    ]
    # A bot-opened PR gets no Copilot review. An independent verification
    # comment of the head is its review signal; nothing else shortens its wait.
    bot_unreviewed = not automated and _opened_by_bot(facts)
    verified_at = _verified_at(facts) if bot_unreviewed else None
    if automated:
        reviewed_at = max(r[2] for r in automated)
        event_remaining = math.ceil(reviewed_at + settle_seconds - now)
        event_reason = (
            f"head reviewed {int(now - reviewed_at)}s ago; settling for "
            f"{event_remaining}s more"
        )
    elif verified_at is not None:
        event_remaining = math.ceil(verified_at + settle_seconds - now)
        event_reason = (
            f"head verified in a PR comment {int(now - verified_at)}s ago; "
            f"settling for {event_remaining}s more"
        )
    elif bot_unreviewed:
        event_remaining = None
        event_reason = (
            f"no automated review and no independent verification of head "
            f"{facts.head_sha[:7]} yet: {facts.author[1]} is an app or bot account, "
            f"so a verifier comments '{VERIFICATION_HEADING} {facts.head_sha[:7]}' "
            "and names the tests"
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
    settle_seconds: int = SETTLE_SECONDS,
) -> Verdict:
    """The rule, in one place. Every failed condition is a reason; owner-only
    conditions are named as such so a caller never waits on something time cannot fix."""
    reasons = Reasons()

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
        settle_seconds=settle_seconds,
        reasons=reasons,
    )

    return Verdict(
        ready=not reasons,
        owner_required=owner1 or owner2 or (OWNER in reasons.kinds),
        reasons=list(reasons),
        quiet_remaining_seconds=max(0, remaining),
        action_required=ACT in reasons.kinds,
        unevaluable=UNEVALUABLE in reasons.kinds,
        review_required=REVIEW in reasons.kinds,
    )


LOCK_VERIFIED = (
    "production lock verified (reviewer, admin bypass off, policies main and v*, "
    "contract holds)"
)


LOCK_NOT_READ = "production lock NOT verified: not read"
LOCK_STOPPED = "production lock NOT verified: the run stopped before a verdict"
# Why a lock was not read: skipped by design, or the run stopped before the read.
LOCK_SKIPPED = "the lock is read only for an open PR of this repository"
LOCK_NOT_REACHED = "the run stopped before the lock read"
LOCK_STATES = ("verified", "not_verified", "not_read", "stopped_before_verdict")


def lock_state(facts: HeadFacts) -> str:
    """The lock state of a verdict (#1138). `stopped_before_verdict` has no facts, so
    only the exit-4 path of the gate reports it."""
    if facts.lock_failures is None:
        return "not_read"
    return "not_verified" if facts.lock_failures else "verified"


def lock_line(state: str, detail: str = "") -> str:
    """The one lock line of every output. It ends with the state that the JSON verdict
    carries in `lock_state`. It informs; it never decides."""
    text = {
        "verified": LOCK_VERIFIED,
        "not_verified": f"production lock NOT verified: {detail}",
        "not_read": f"{LOCK_NOT_READ} ({detail})",
        "stopped_before_verdict": LOCK_STOPPED,
    }[state]
    # Not "[...]": the console reads square brackets as markup and drops them.
    return f"{text} | lock_state={state}"


def lock_status(facts: HeadFacts) -> str:
    """The lock line for a verdict with facts."""
    state = lock_state(facts)
    detail = (
        "; ".join(facts.lock_failures or ())
        if state == "not_verified"
        else LOCK_SKIPPED
    )
    return lock_line(state, detail)


def evaluate_scope(
    files: tuple[str, ...],
    *,
    lock_failures: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Classify changed paths before or after PR creation (#1104)."""
    lock_holds = lock_failures == ()
    required_workflows = _required_check_workflows()

    def _requires(path: str) -> bool:
        return required_workflows is not None and _normalized_path(path) in (
            required_workflows
        )

    uncovered = sorted(
        f
        for f in files
        if _normalized_path(f).startswith(WORKFLOW_PREFIX)
        and (required_workflows is None or _requires(f))
    )
    lock_covered = {
        f
        for f in files
        if lock_holds
        and _normalized_path(f).startswith(WORKFLOW_PREFIX)
        and f not in uncovered
    }
    governing = sorted(
        f for f in files if is_self_governing(f) and f not in lock_covered
    )
    fired = {f: _deploy_triggering(f) for f in files}
    deploying = sorted(f for f, w in fired.items() if w)

    blocking_files = sorted(
        set(uncovered) | set(governing) | (set() if lock_holds else set(deploying))
    )
    if uncovered:
        scope_class = "owner_held"
    elif governing:
        scope_class = "gate_closure"
    elif deploying and not lock_holds:
        scope_class = "deploy_unverified"
    else:
        scope_class = "autonomous"

    state = (
        "not_read"
        if lock_failures is None
        else ("not_verified" if lock_failures else "verified")
    )
    return {
        "lock_state": state,
        "scope_class": scope_class,
        "blocking_files": blocking_files,
        "deploy_triggering_files": deploying,
    }


def render(facts: HeadFacts, verdict: Verdict) -> str:
    """Render a human-readable verdict description."""
    head = f"#{facts.number} @ {facts.head_sha[:7]}"
    if verdict.ready:
        return (
            f"{head}: mergeable under session authority — checks green "
            f"({len(facts.checks)}), threads resolved, settled, "
            f"no path that needs the owner; {lock_status(facts)}"
        )
    kind = {
        "unevaluable": "could not evaluate (not a verdict on the PR)",
        "owner": "needs the owner",
        "review_required": "review required",
        "action": "action required (waiting will not fix it)",
        "wait": "not yet",
    }[verdict.outcome]
    lines = [f"{head}: {kind}", *(f"  - {r}" for r in verdict.reasons)]
    return "\n".join([*lines, f"  {lock_status(facts)}"])
