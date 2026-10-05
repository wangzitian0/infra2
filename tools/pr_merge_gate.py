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
    SETTLE_MINUTES,
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
    _proven_tighter,
    _read_checks,
    _repo_deps,
    _repo_slug,
    _required_checks,
    _workflow_only_gained_authority,
    _working_tree_rule_drift,
    collect,
    evaluate,
    is_self_governing,
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
    "SETTLE_MINUTES",
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
                stdin=subprocess.DEVNULL,
                timeout=30.0,
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
