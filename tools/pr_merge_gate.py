#!/usr/bin/env python3
"""The session-scoped merge authority of AGENTS.md, as a check instead of a habit.

An agent merging under session authority must see, for one head, that every blocking
check is green, every review thread is resolved, the head has been quiet for twelve
minutes since its last push, and that the change touches no protected file. A protected
file needs the owner's approval of that exact head. A deploy on merge needs it too, but
only while the production lock is not verified (#1138).
On 2026-09-15 three of five merges landed 21–95 s before the quiet period had elapsed,
each time because the timing was judged by eye between other work. This tool judges it.

    python -m tools.pr_merge_gate 704            # verdict, exit 0 when mergeable by rule
    python -m tools.pr_merge_gate 704 --merge    # squash-merge only when the verdict is ready

Exit codes (#740), so a caller never reads the prose to decide:

    0  ready (or merged with --merge)
    1  wait: time alone fixes it (check pending, no check yet, head still settling)
    2  needs the owner (unproven self-governing change, a workflow that defines a
       required check, base is not main, or a deploy-triggering path while the
       production lock is not verified)
    3  action required: waiting will not fix it (red check, open thread, conflict,
       draft, PR not open, base changed under green checks)
    4  could not evaluate: not a verdict on the PR. This checkout's merge rules are
       not main's (#873; pull main), or gh failed or returned data the gate cannot
       read. Fix the cause and re-run.

Two policies for the settling condition:

- ``clock`` (default, AGENTS.md as written): twelve minutes since the last push.
- ``event``: an automated review has been submitted on the *current* head and sixty
  seconds have passed since that review — the thing the clock was waiting for, measured.
  Copilot does not review a PR that an app or bot account opened (#1075). For such a PR
  the review signal is a PR comment with the heading
  ``### Independent verification of head <sha7>`` for the current head and at least one
  named test (``test_...`` or ``path::name``); its time is when its text was written.
- ``either``: whichever of the two is satisfied first — the review lets a head go early,
  the clock stays the upper bound when no automated review ever arrives.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import yaml

from libs.gate import (
    ABSENT,
    AUTOMATED_REVIEWERS,
    BLOCKING_SEVERITY_TOTAL,
    COPILOT_BOT_ID,
    DEFAULT_REPO,
    DEPLOY_MARKERS,
    DEPLOY_TRIGGERING_GLOBS,
    DIRECTION_PROOFS,
    GH_TIMEOUT_S,
    GREEN_BUCKETS,
    GREEN_STATES,
    HeadFacts,
    MAX_REVIEW_THREADS,
    MAX_THREAD_COMMENTS,
    MERGE_STATES_OK,
    NO_CHECKS_REPORTED,
    NON_PROD_DEPLOY_WORKFLOWS,
    QUIET_MINUTES,
    ROOT,
    RULE_TEXT_FILES,
    Runner,
    SETTLE_SECONDS,
    SEVERITY_WEIGHTS,
    UNLABELLED_SEVERITY_WEIGHT,
    Verdict,
    WORKFLOW_DIR,
    WORKFLOW_PREFIX,
    _all_workflow_files,
    _as_list,
    _base_changed_files,
    _blob_sha,
    _blocking_coordinates,
    _declared_deploy_globs,
    _deploy_triggering,
    _direction_proof_for,
    _epoch,
    _field,
    _file_at,
    _gh,
    _inventory_only_gained_authority,
    _is_local_root_repo,
    _owner_instruction_quoted,
    _production_lock_failures,
    _proven_tighter,
    _read_checks,
    _repo_deps,
    _repo_slug,
    _required_checks,
    _workflow_only_gained_authority,
    _working_tree_rule_drift,
    collect,
    evaluate,
    LOCK_STATES,
    is_self_governing,
    lock_line,
    lock_status,
    render,
    request_copilot_review,
    self_governing_files,
    thread_weight,
)

__all__ = [
    "ABSENT",
    "AUTOMATED_REVIEWERS",
    "BLOCKING_SEVERITY_TOTAL",
    "COPILOT_BOT_ID",
    "DEFAULT_REPO",
    "DEPLOY_MARKERS",
    "DEPLOY_TRIGGERING_GLOBS",
    "DIRECTION_PROOFS",
    "GH_TIMEOUT_S",
    "GREEN_BUCKETS",
    "GREEN_STATES",
    "HeadFacts",
    "MAX_REVIEW_THREADS",
    "MAX_THREAD_COMMENTS",
    "MERGE_STATES_OK",
    "NON_PROD_DEPLOY_WORKFLOWS",
    "NO_CHECKS_REPORTED",
    "QUIET_MINUTES",
    "ROOT",
    "RULE_TEXT_FILES",
    "Runner",
    "SETTLE_SECONDS",
    "SEVERITY_WEIGHTS",
    "UNLABELLED_SEVERITY_WEIGHT",
    "Verdict",
    "WORKFLOW_DIR",
    "WORKFLOW_PREFIX",
    "_all_workflow_files",
    "_as_list",
    "_base_changed_files",
    "_blob_sha",
    "_blocking_coordinates",
    "_declared_deploy_globs",
    "_deploy_triggering",
    "_direction_proof_for",
    "_epoch",
    "_field",
    "_file_at",
    "_gh",
    "_inventory_only_gained_authority",
    "_is_local_root_repo",
    "_owner_instruction_quoted",
    "_production_lock_failures",
    "_proven_tighter",
    "_read_checks",
    "_repo_deps",
    "_repo_slug",
    "_required_checks",
    "_workflow_only_gained_authority",
    "_working_tree_rule_drift",
    "collect",
    "evaluate",
    "is_self_governing",
    "LOCK_STATES",
    "lock_line",
    "lock_status",
    "main",
    "render",
    "request_copilot_review",
    "self_governing_files",
    "subprocess",
    "thread_weight",
    "yaml",
]


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
        help=f"settling rule: {QUIET_MINUTES} min after the push (clock), "
        f"{SETTLE_SECONDS} s after an automated review of the head or, for a "
        "bot-opened PR, its independent verification comment (event), or "
        "whichever comes first (either)",
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
    from libs.gate.evaluator import (
        LOCK_NOT_REACHED,
        LOCK_SKIPPED,
        lock_line,
        lock_state,
    )
    from libs.gate.types import EXIT_UNEVALUABLE

    def unevaluable(
        why: str, *, state: str = "not_read", detail: str = LOCK_NOT_REACHED
    ) -> int:
        if args.json:
            print(
                json.dumps(
                    {
                        "number": args.number,
                        "ready": False,
                        "outcome": "unevaluable",
                        "exit_code": EXIT_UNEVALUABLE,
                        "reasons": [why],
                        # No verdict, so no verified lock (#1138).
                        "lock_verified": False,
                        "lock_failures": None,
                        "lock_state": state,
                    }
                )
            )
        else:
            warning(
                f"#{args.number}: could not evaluate (not a verdict): {why}\n"
                f"  {lock_line(state, detail)}"
            )
        return EXIT_UNEVALUABLE

    # Preflight (#873): judge only with main's rules, and know it before reading any
    # PR state. A stale checkout is a local fact with a local fix (pull main); it is
    # not a reason to ask the owner.
    if _is_local_root_repo(args.repo):
        drift = _working_tree_rule_drift(args.repo, "main", gh=gh)
        if drift:
            return unevaluable(
                "this checkout's merge rules are not main's "
                f"({', '.join(drift)}): pull main and re-run"
            )

    try:
        facts = collect(args.number, repo=args.repo, gh=gh)
    except Exception as exc:  # noqa: BLE001 - any read failure is "could not evaluate"
        if getattr(exc, "lock_read", False):
            state, detail = "stopped_before_verdict", ""
        elif getattr(exc, "lock_skipped", False):
            state, detail = "not_read", LOCK_SKIPPED
        else:
            state, detail = "not_read", LOCK_NOT_REACHED
        return unevaluable(
            f"reading the PR failed: {type(exc).__name__}: {exc}",
            state=state,
            detail=detail,
        )
    if args.request_review and not any(
        r[0] in AUTOMATED_REVIEWERS and r[2] > 0 for r in facts.reviews_on_head()
    ):
        if request_copilot_review(facts, gh=gh):
            warning(
                f"#{facts.number}: Copilot review requested on {facts.head_sha[:7]}"
            )
        else:
            warning(
                f"#{facts.number}: the Copilot review request was not registered; "
                "no automated review will come"
            )
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
                stdin=subprocess.DEVNULL,
                timeout=30.0,
            )
            if audit_proc.returncode != 0:
                verdict.reasons.append(
                    f"omca audit failed to run (rc={audit_proc.returncode}): {audit_proc.stderr.strip()[:100]}"
                )
                verdict.ready = False
                verdict.unevaluable = True
            else:
                report = json.loads(audit_proc.stdout)
                passed, blocking, _ = evaluate_audit_report(
                    report, expect_sha=facts.head_sha
                )
                if not passed:
                    for b in blocking:
                        verdict.reasons.append(f"omca audit blocked: {b}")
                    verdict.ready = False
                    verdict.action_required = True
        except Exception as exc:
            verdict.reasons.append(f"omca audit execution error: {exc}")
            verdict.ready = False
            verdict.unevaluable = True
    if args.json:
        print(
            json.dumps(
                {
                    "number": facts.number,
                    "head": facts.head_sha,
                    "ready": verdict.ready,
                    "owner_required": verdict.owner_required,
                    "outcome": verdict.outcome,
                    "exit_code": verdict.exit_code,
                    "reasons": verdict.reasons,
                    "quiet_remaining_seconds": verdict.quiet_remaining_seconds,
                    "policy": args.policy,
                    # Informational (#1138): the lock never changes the exit code alone.
                    "lock_state": lock_state(facts),
                    "lock_verified": facts.lock_failures == (),
                    "lock_failures": (
                        None
                        if facts.lock_failures is None
                        else list(facts.lock_failures)
                    ),
                }
            )
        )
    else:
        text = render(facts, verdict)
        if verdict.ready:
            success(text)
        elif verdict.owner_required or verdict.unevaluable:
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
