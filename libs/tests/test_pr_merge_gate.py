"""tools/pr_merge_gate: the session-scoped merge authority as a check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import pr_merge_gate as gate

ROOT = Path(__file__).resolve().parents[2]

NOW = 1_800_000_000.0


def _facts(**overrides) -> gate.HeadFacts:
    base = dict(
        number=704,
        state="OPEN",
        draft=False,
        base="main",
        head_sha="abcdef0123456789",
        files=("libs/probe_specs.py",),
        last_push_at=NOW - 13 * 60,
        checks=(("Lint", "pass"), ("Tests", "pass"), ("Docs", "skipping")),
        unresolved_threads=0,
    )
    base.update(overrides)
    return gate.HeadFacts(**base)


def test_a_settled_green_head_is_mergeable():
    verdict = gate.evaluate(_facts(), now=NOW)
    assert verdict.ready and verdict.exit_code == 0 and verdict.reasons == []


def test_the_quiet_period_is_counted_from_the_last_push():
    verdict = gate.evaluate(_facts(last_push_at=NOW - 11 * 60), now=NOW)
    assert not verdict.ready and verdict.exit_code == 1
    assert verdict.quiet_remaining_seconds == 60
    assert "quiet period has 60s to run" in verdict.reasons[0]


def test_pending_checks_and_open_threads_are_not_yet_not_owner():
    verdict = gate.evaluate(
        _facts(checks=(("Tests", "pending"),), unresolved_threads=2), now=NOW
    )
    assert verdict.exit_code == 1 and not verdict.owner_required
    assert "check(s) not green: Tests" in verdict.reasons
    assert "2 review thread(s) unresolved" in verdict.reasons


def test_a_protected_file_or_a_deploy_triggering_path_needs_the_owner():
    protected = gate.evaluate(_facts(files=("AGENTS.md", "libs/x.py")), now=NOW)
    assert protected.exit_code == 2 and "AGENTS.md" in protected.reasons[0]
    runner = gate.evaluate(
        _facts(files=("bootstrap/06.iac_runner/compose.yaml",)), now=NOW
    )
    assert runner.exit_code == 2 and "trigger a deploy" in runner.reasons[0]
    workflow = gate.evaluate(_facts(files=(".github/workflows/deploy.yml",)), now=NOW)
    assert workflow.owner_required
    # deploy-cloudflare-watchdog.yml runs `wrangler deploy` on a push under this path
    worker = gate.evaluate(
        _facts(files=("cloudflare/infra-watchdog/worker.js",)), now=NOW
    )
    assert worker.exit_code == 2 and "trigger a deploy" in worker.reasons[0]


def test_the_deploy_triggering_globs_cover_every_push_triggered_deploy_workflow():
    import yaml

    workflows = ROOT / ".github/workflows"
    for name in ("deploy.yml", "deploy-cloudflare-watchdog.yml"):
        workflow = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))
        on = workflow.get("on", workflow.get(True))
        for pattern in on["push"]["paths"]:
            sample = pattern.replace("**", "sub/file").replace("*", "file")
            assert gate._deploy_triggering(sample), (name, pattern)


def test_a_fractional_second_inside_the_window_is_inside_the_window():
    verdict = gate.evaluate(_facts(last_push_at=NOW - 12 * 60 + 0.4), now=NOW)
    assert not verdict.ready and verdict.quiet_remaining_seconds == 1


def test_more_threads_than_were_read_is_not_a_clean_verdict():
    verdict = gate.evaluate(_facts(review_threads_total=150), now=NOW)
    assert not verdict.ready and "only the first 100 were read" in verdict.reasons[0]


def test_state_is_the_fallback_when_gh_reports_no_bucket():
    assert gate.evaluate(_facts(checks=(("Tests", "SUCCESS"),)), now=NOW).ready
    assert not gate.evaluate(_facts(checks=(("Tests", "FAILURE"),)), now=NOW).ready


def test_event_policy_waits_for_a_review_of_the_head_then_settles_briefly():
    head = "abcdef0123456789"
    unreviewed = gate.evaluate(
        _facts(head_sha=head, last_push_at=NOW - 60), now=NOW, policy="event"
    )
    assert (
        not unreviewed.ready
        and "no automated review on head abcdef0" in unreviewed.reasons[0]
    )

    stale = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", "0000000", NOW - 600),),
    )
    assert not gate.evaluate(
        stale, now=NOW, policy="event"
    ).ready  # reviewed an older head

    fresh = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", head, NOW - 100),),
    )
    settling = gate.evaluate(fresh, now=NOW, policy="event")
    assert not settling.ready and settling.quiet_remaining_seconds == 80
    assert gate.evaluate(fresh, now=NOW + 80, policy="event").ready
    # a human review alone does not count as the automated pass the rule waits for
    human = _facts(head_sha=head, reviews=(("wangzitian0", head, NOW - 600),))
    assert not gate.evaluate(human, now=NOW, policy="event").ready
    with pytest.raises(ValueError):
        gate.evaluate(_facts(), now=NOW, policy="vibes")


def test_either_takes_the_review_early_and_keeps_the_clock_as_the_bound():
    head = "abcdef0123456789"
    reviewed = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", head, NOW - 200),),
    )
    assert gate.evaluate(reviewed, now=NOW, policy="either").ready  # 200 s after review
    unreviewed = _facts(head_sha=head, last_push_at=NOW - 11 * 60)
    pending = gate.evaluate(unreviewed, now=NOW, policy="either")
    assert not pending.ready and pending.quiet_remaining_seconds == 60
    assert (
        "no automated review" in pending.reasons[0]
        and "quiet period" in pending.reasons[0]
    )
    assert gate.evaluate(
        unreviewed, now=NOW + 60, policy="either"
    ).ready  # the clock bound


def test_a_review_without_a_submission_time_is_not_a_submitted_review():
    head = "abcdef0123456789"
    unsubmitted = _facts(
        head_sha=head,
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", head, 0.0),),
    )
    verdict = gate.evaluate(unsubmitted, now=NOW, policy="event")
    assert not verdict.ready and "no automated review on head" in verdict.reasons[0]
    assert not gate.evaluate(unsubmitted, now=NOW, policy="either").ready


def test_the_clock_policy_ignores_reviews():
    facts = _facts(
        last_push_at=NOW - 60,
        reviews=(("copilot-pull-request-reviewer", "abcdef0123456789", NOW - 30),),
    )
    assert not gate.evaluate(facts, now=NOW).ready


def test_no_checks_yet_is_not_green():
    verdict = gate.evaluate(_facts(checks=()), now=NOW)
    assert not verdict.ready and "no checks reported yet" in verdict.reasons


class _Gh:
    """Canned gh answers; records a merge if asked."""

    def __init__(
        self, *, unresolved=0, states=("pass",), pushed_iso="2026-09-15T06:55:22Z"
    ):
        self.calls: list[list[str]] = []
        self.unresolved = unresolved
        self.states = states
        self.pushed_iso = pushed_iso

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 704,
                    "id": "PR_node",
                    "state": "OPEN",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "feedfacefeedface",
                    "reviews": [
                        {
                            "author": {"login": "copilot-pull-request-reviewer"},
                            "commit": {"oid": "feedfacefeedface"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-09-15T06:58:00Z",
                        }
                    ],
                    "files": [{"path": "libs/probe_specs.py"}],
                    "mergeable": getattr(self, "mergeable", "MERGEABLE"),
                    "mergeStateStatus": getattr(self, "merge_state", "CLEAN"),
                    "commits": [
                        {"committedDate": "2026-09-15T06:40:00Z"},
                        {"committedDate": self.pushed_iso},
                    ],
                }
            )
        if argv[:2] == ["pr", "checks"]:
            # gh's real shape (read live 2026-09-15): `state` is SUCCESS/SKIPPED/…,
            # `bucket` is pass/fail/pending/skipping; there is no `conclusion` field,
            # and `pr view --json files,commits` returns flat arrays.
            return json.dumps(
                [
                    {"name": f"c{i}", "state": "COMPLETED", "bucket": s}
                    for i, s in enumerate(self.states)
                ]
            )
        if argv[:2] == ["api", "graphql"] and "requestReviews" in argv[3]:
            self.review_requests = getattr(self, "review_requests", 0) + 1
            return json.dumps(
                {"data": {"requestReviews": {"pullRequest": {"number": 704}}}}
            )
        if argv[:2] == ["api", "graphql"]:
            nodes = [{"isResolved": False}] * self.unresolved + [{"isResolved": True}]
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "totalCount": len(nodes),
                                    "nodes": nodes,
                                }
                            }
                        }
                    }
                }
            )
        if argv[:1] == ["api"] and "/compare/" in argv[1]:
            # Files the base has changed since the head diverged. Defaults to
            # none so existing cases keep their meaning; a case that wants the
            # stale-green path sets `base_changed`.
            return json.dumps({"files": list(getattr(self, "base_changed", ()))})
        if argv[:2] == ["pr", "merge"]:
            return ""
        raise AssertionError(argv)


def test_collect_reads_the_newest_commit_as_the_last_push():
    facts = gate.collect(704, gh=_Gh())
    assert facts.head_sha == "feedfacefeedface"
    assert facts.last_push_at == gate._epoch("2026-09-15T06:55:22Z")
    assert facts.checks == (("c0", "pass"),) and facts.unresolved_threads == 0
    assert facts.review_threads_total == 1
    assert facts.node_id == "PR_node"
    assert facts.reviews_on_head()[0][0] == "copilot-pull-request-reviewer"


def test_request_review_asks_copilot_only_when_the_head_is_unreviewed(capsys):
    class _Unreviewed(_Gh):
        def __call__(self, argv):
            out = super().__call__(argv)
            if list(argv)[:2] == ["pr", "view"]:
                doc = json.loads(out)
                doc["reviews"] = []
                return json.dumps(doc)
            return out

    gh = _Unreviewed()
    assert (
        gate.main(
            ["704", "--policy", "event", "--request-review"], gh=gh, now=lambda: NOW
        )
        == 1
    )
    assert gh.review_requests == 1
    assert "no automated review on head" in capsys.readouterr().out
    reviewed = _Gh()
    gate.main(
        ["704", "--policy", "event", "--request-review"], gh=reviewed, now=lambda: NOW
    )
    assert getattr(reviewed, "review_requests", 0) == 0


def test_main_merges_only_a_ready_head_and_pins_the_head_commit(capsys):
    gh = _Gh()
    ready_at = gate._epoch("2026-09-15T06:55:22Z") + 12 * 60
    assert gate.main(["704", "--merge"], gh=gh, now=lambda: ready_at - 1) == 1
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)
    assert "quiet period has 1s to run" in capsys.readouterr().out

    assert gate.main(["704", "--merge"], gh=gh, now=lambda: ready_at) == 0
    merge = next(c for c in gh.calls if c[:2] == ["pr", "merge"])
    assert merge[merge.index("--match-head-commit") + 1] == "feedfacefeedface"
    assert "merged #704 at feedfac" in capsys.readouterr().out


def test_main_json_reports_the_owner_gate(capsys):
    class _Protected(_Gh):
        def __call__(self, argv):
            out = super().__call__(argv)
            if list(argv)[:2] == ["pr", "view"]:
                doc = json.loads(out)
                doc["files"].append({"path": "CLAUDE.md"})
                return json.dumps(doc)
            return out

    assert gate.main(["704", "--json"], gh=_Protected(), now=lambda: NOW) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["owner_required"] and doc["ready"] is False


def test_a_failing_gh_call_is_an_error_not_a_verdict():
    def boom(argv):
        raise RuntimeError("gh pr view: not found")

    with pytest.raises(RuntimeError, match="not found"):
        gate.collect(999, gh=boom)


class _NoChecksYet(_Gh):
    """gh before the first check registers: `pr checks` exits 1 with no JSON."""

    def __init__(self, error="no checks reported on the 'harness/x' branch", **kw):
        super().__init__(**kw)
        self.error = error

    def __call__(self, argv):
        if list(argv)[:2] == ["pr", "checks"]:
            self.calls.append(list(argv))
            raise RuntimeError(f"gh {' '.join(argv)}: {self.error}")
        return super().__call__(argv)


def test_no_checks_reported_by_gh_is_zero_checks_not_a_crash(capsys):
    facts = gate.collect(704, gh=_NoChecksYet())
    assert facts.checks == ()
    ready_at = gate._epoch("2026-09-15T06:55:22Z") + 12 * 60
    # Before the fix this raised (a traceback, also exit 1) and the reason never printed.
    assert gate.main(["704"], gh=_NoChecksYet(), now=lambda: ready_at) == 1
    assert "no checks reported yet" in capsys.readouterr().out


def test_any_other_checks_failure_still_raises():
    with pytest.raises(RuntimeError, match="HTTP 502"):
        gate.collect(704, gh=_NoChecksYet(error="HTTP 502: Bad Gateway"))


def test_pr_merge_gate_audit_flag_detects_blockers(monkeypatch, capsys):
    from unittest.mock import MagicMock
    ready_at = gate._epoch("2026-09-15T06:55:22Z") + 12 * 60
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = json.dumps({
        "verdict": "BLOCKED",
        "commit": "feedfacefeedface",
        "findings": [{"severity": "CRITICAL", "topic": "Memory Leak", "details": "leak"}],
    })
    monkeypatch.setattr(gate.subprocess, "run", mock_run)
    exit_code = gate.main(["704", "--audit"], gh=_Gh(), now=lambda: ready_at)
    assert exit_code == 1
    assert "omca audit blocked" in capsys.readouterr().out


# --- GitHub's own merge computation ------------------------------------------
#
# These exist because a real CONFLICTING pull request (infra2#758) reached
# `evaluate` and came back reported only as "2 review thread(s) unresolved".
# The gate had never asked GitHub whether the branch merges, while its green
# verdict printed the word "mergeable" regardless. AGENTS.md's 合流真源唯一
# names three things -- right base, `mergeable`, no conflict -- and only the
# first was checked.


def test_a_conflicting_head_is_blocked_and_says_so():
    verdict = gate.evaluate(_facts(mergeable="CONFLICTING"), now=NOW)
    assert not verdict.ready
    assert any("CONFLICTING" in r for r in verdict.reasons)
    # It must not be reported as an owner decision: a rebase fixes it.
    assert not verdict.owner_required


def test_unknown_mergeable_asks_for_a_re_poll_rather_than_assuming_clean():
    verdict = gate.evaluate(_facts(mergeable="UNKNOWN"), now=NOW)
    assert not verdict.ready
    reason = next(r for r in verdict.reasons if "UNKNOWN" in r)
    assert "re-run" in reason


def test_a_dirty_merge_state_blocks_even_when_mergeable_is_unset():
    verdict = gate.evaluate(_facts(merge_state="DIRTY"), now=NOW)
    assert not verdict.ready
    assert any("mergeStateStatus is DIRTY" in r for r in verdict.reasons)


def test_a_hand_built_fact_without_merge_fields_is_not_invented_as_a_blocker():
    # "" is reserved for a HeadFacts constructed by hand, where the field was
    # never requested and so nothing was lost. collect() never produces it.
    assert gate.evaluate(_facts(mergeable="", merge_state=""), now=NOW).ready


def test_a_field_gh_was_asked_for_and_did_not_return_blocks():
    # This test replaces one that asserted the opposite and was wrong. The
    # earlier reasoning -- "absence of evidence is not a conflict" -- does not
    # hold for a field collect() always requests: losing it silently loses the
    # only check that catches a conflicting branch. Probed before fixing:
    # collect() with mergeable omitted from gh's output returned ready=True
    # with an empty reasons list.
    for field in ("mergeable", "merge_state"):
        verdict = gate.evaluate(_facts(**{field: gate.ABSENT}), now=NOW)
        assert not verdict.ready, field
        assert any("gh did not return" in r for r in verdict.reasons), field


def test_collect_marks_an_omitted_field_absent_rather_than_empty():
    def fake(argv):
        if argv[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 1,
                    "state": "OPEN",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "f" * 40,
                    "files": [],
                    "commits": [],
                    "reviews": [],
                    "id": "X",
                }  # mergeable / mergeStateStatus deliberately omitted
            )
        if argv[:2] == ["pr", "checks"]:
            return "[]"
        if argv[:2] == ["api", "graphql"]:
            return json.dumps(
                {"data": {"repository": {"pullRequest": {"reviewThreads": {"totalCount": 0, "nodes": []}}}}}
            )
        if argv[:1] == ["api"]:
            return json.dumps({"files": []})
        raise AssertionError(argv)

    facts = gate.collect(1, gh=fake)
    assert facts.mergeable == gate.ABSENT
    assert facts.merge_state == gate.ABSENT


# --- stale green --------------------------------------------------------------
#
# A green check proves the tree it ran on. finance_report's AGENTS.md states the
# hazard for the adjacent field: mergeStateStatus "can flip from CLEAN to
# DIRTY/BEHIND the instant a sibling PR merges to main". The checks do not flip;
# they simply never re-run, and stay green about a tree that no longer exists.


def test_green_against_a_base_that_rewrote_this_prs_files_is_not_ready():
    verdict = gate.evaluate(
        _facts(base_changed_files=("libs/probe_specs.py", "docs/unrelated.md")),
        now=NOW,
    )
    assert not verdict.ready
    reason = next(r for r in verdict.reasons if "green against a base" in r)
    assert "libs/probe_specs.py" in reason
    assert "docs/unrelated.md" not in reason  # only the overlap is the problem
    assert "1 of" in reason


def test_a_base_that_changed_other_files_leaves_the_head_ready():
    assert gate.evaluate(
        _facts(base_changed_files=("docs/unrelated.md",)), now=NOW
    ).ready


def test_stale_green_is_not_reported_on_top_of_a_failing_check():
    # Naming both would send the caller to update a branch whose checks are red
    # anyway; the failure is the thing to fix first.
    verdict = gate.evaluate(
        _facts(
            checks=(("Lint", "fail"),),
            base_changed_files=("libs/probe_specs.py",),
        ),
        now=NOW,
    )
    assert not verdict.ready
    assert not any("green against a base" in r for r in verdict.reasons)


def test_an_unreadable_comparison_raises_no_concern(monkeypatch):
    # Best-effort by construction: the compare call is an extra round trip that
    # must never be able to manufacture a blocker it cannot substantiate.
    def boom(argv):
        raise RuntimeError("gh: API rate limit exceeded")

    assert gate._base_changed_files("o/r", "deadbeef", "main", gh=boom) == ()
    assert gate._base_changed_files("o/r", "", "main") == ()


def test_a_merged_head_is_not_re_litigated_on_questions_that_have_no_answer():
    # GitHub stops computing a test merge once a PR closes, so every merged head
    # reports mergeable=UNKNOWN. Reporting that turned a one-line "not OPEN"
    # verdict into four lines about a question that no longer has an answer --
    # observed on infra2#763 minutes after it merged.
    verdict = gate.evaluate(
        _facts(
            state="MERGED",
            mergeable="UNKNOWN",
            merge_state="UNKNOWN",
            base_changed_files=("libs/probe_specs.py",),
        ),
        now=NOW,
    )
    assert not verdict.ready
    assert verdict.reasons == ["pull request is MERGED, not OPEN"]


def test_an_unknown_merge_state_is_transient_not_a_conflict():
    # Same cause as mergeable=UNKNOWN: GitHub is still computing the test merge.
    # "not CLEAN" read like a settled conflict and sent the caller to rebase.
    verdict = gate.evaluate(_facts(merge_state="UNKNOWN"), now=NOW)
    assert not verdict.ready
    reason = next(r for r in verdict.reasons if "mergeStateStatus" in r)
    assert "re-run in a moment" in reason
    assert "not CLEAN" not in reason


def test_a_closed_pr_costs_no_comparison_round_trip():
    calls: list[list[str]] = []

    def fake(argv):
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 704,
                    "state": "MERGED",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "feedfacefeedface",
                    "files": [],
                    "commits": [],
                    "reviews": [],
                    "id": "PR_x",
                    "mergeable": "UNKNOWN",
                    "mergeStateStatus": "UNKNOWN",
                }
            )
        if argv[:2] == ["pr", "checks"]:
            return "[]"
        if argv[:2] == ["api", "graphql"]:
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {"totalCount": 0, "nodes": []}
                            }
                        }
                    }
                }
            )
        raise AssertionError(argv)

    facts = gate.collect(704, gh=fake)
    assert facts.base_changed_files == ()
    assert not any("/compare/" in a for call in calls for a in call)


# --- deploy-triggering paths, derived ----------------------------------------


def test_ops_checks_push_paths_are_treated_as_deploy_triggering():
    # The hand-written list named deploy.yml and the watchdog but missed
    # ops-checks.yml, whose push paths each start a live deploy_v2 canary on
    # merge. infra2#758 edits that workflow, and the gate would have said
    # session authority sufficed.
    assert gate._deploy_triggering(".github/workflows/ops-checks.yml")
    assert gate._deploy_triggering("tools/deploy_v2.py")
    assert gate._deploy_triggering("libs/deploy_contract.py")


def test_ordinary_paths_are_still_not_deploy_triggering():
    # Derivation must not swallow the repository whole: over-blocking would
    # send every PR to the owner and make the signal worthless.
    assert not gate._deploy_triggering("tools/pi_chain_smoke.py")
    assert not gate._deploy_triggering("tools/README.md")
    assert not gate._deploy_triggering("libs/tests/test_pr_merge_gate.py")


def test_the_written_globs_survive_derivation():
    # These have no workflow path filter to be derived from -- a runner rebuild
    # and a bootstrap self-update are triggered by the merge itself.
    assert gate._deploy_triggering("bootstrap/06.iac_runner/main.tf")
    assert gate._deploy_triggering("scripts/deploy_iac_runner_bootstrap.sh")
    assert gate._deploy_triggering("cloudflare/infra-watchdog/src/index.ts")


def test_a_deploy_triggering_path_routes_to_the_owner():
    verdict = gate.evaluate(
        _facts(files=("tools/deploy_v2.py",)), now=NOW
    )
    assert not verdict.ready
    assert verdict.owner_required
    assert any("trigger a deploy" in r for r in verdict.reasons)


def test_an_unreadable_workflow_directory_leaves_the_written_globs_standing(monkeypatch):
    # A malformed or missing workflow must not take the gate offline, and must
    # not quietly stop the written list from blocking.
    gate._declared_deploy_globs.cache_clear()
    monkeypatch.setattr(gate, "WORKFLOW_DIR", gate.ROOT / "does-not-exist")
    try:
        assert gate._declared_deploy_globs() == ((), True)
        assert gate._deploy_triggering("bootstrap/06.iac_runner/main.tf")
        assert not gate._deploy_triggering("tools/deploy_v2.py")
    finally:
        gate._declared_deploy_globs.cache_clear()


def test_the_deploy_reason_names_the_workflow_that_fires():
    # "tools/deploy_v2.py triggers a deploy" makes an owner go and find out
    # which one. Naming it makes the line a judgement they can act on.
    verdict = gate.evaluate(_facts(files=("tools/deploy_v2.py",)), now=NOW)
    reason = next(r for r in verdict.reasons if "trigger a deploy" in r)
    assert "ops-checks.yml" in reason
    assert "tools/deploy_v2.py" in reason


def test_a_merge_triggered_path_needs_no_workflow_to_name():
    # bootstrap/06.iac_runner has no workflow path filter -- the merge itself
    # rebuilds the runner -- so there is nothing to cite and the reason says so
    # by omission rather than by inventing a source.
    verdict = gate.evaluate(_facts(files=("bootstrap/06.iac_runner/main.tf",)), now=NOW)
    reason = next(r for r in verdict.reasons if "trigger a deploy" in r)
    assert " via " not in reason


def test_the_observability_apply_is_deploy_triggering():
    # apply-observability.yml pushes to main and runs
    # `invoke fr-observability.shared.apply-alerts` against live SigNoz.
    # AGENTS.md names "observability apply" as a high-risk merge side effect,
    # and the first marker list missed it -- the same omission the derivation
    # was written to stop.
    gate._declared_deploy_globs.cache_clear()
    assert (
        gate._deploy_triggering("finance_report/finance_report/observability/x.yaml")
        == "apply-observability.yml"
    )
    assert gate._deploy_triggering("libs/alerting.py") == "apply-observability.yml"


def test_a_trigger_field_may_be_a_string_or_omitted():
    # Actions accepts a bare string wherever it accepts a list, and an omitted
    # `branches` means every branch -- main included. Reading only the list
    # form under-detected, which is the one direction that matters.
    assert gate._as_list("main") == ["main"]
    assert gate._as_list(["main", "release"]) == ["main", "release"]
    assert gate._as_list(None) == []
    assert gate._as_list(7) == []


def test_double_star_globs_match_without_translation():
    # fnmatch's "*" matches "/" too, so "a/**" already matches "a/b/c.py".
    # A helper that claimed to translate them was a no-op -- worse than
    # nothing, because it read as though the case were handled.
    assert gate._deploy_triggering("bootstrap/06.iac_runner/deep/nested/main.tf")
    assert gate._deploy_triggering("cloudflare/infra-watchdog/a/b/index.ts")


def test_an_unreadable_workflow_set_blocks_instead_of_quietly_under_detecting(monkeypatch):
    # Found by probing rather than by reading. With every workflow unparseable,
    # the first version returned zero derived globs and libs/alerting.py -- a
    # production observability apply -- came back not deploy-triggering, with a
    # verdict that said nothing about why. Failure here removes a blocker, so
    # swallowing it is the opposite of safe; the docstring had borrowed that
    # reasoning from _base_changed_files, whose failure can only add one.
    class Boom:
        class YAMLError(Exception):
            pass

        @staticmethod
        def safe_load(_):
            raise Boom.YAMLError("unparseable")

    gate._declared_deploy_globs.cache_clear()
    monkeypatch.setattr(gate, "yaml", Boom)
    try:
        _, healthy = gate._declared_deploy_globs()
        assert not healthy
        verdict = gate.evaluate(_facts(), now=NOW)
        assert not verdict.ready
        assert any("cannot read .github/workflows" in r for r in verdict.reasons)
    finally:
        gate._declared_deploy_globs.cache_clear()


def test_an_empty_workflow_directory_is_healthy_not_broken(monkeypatch):
    # A repository with no workflows is a real state, and the unit tests point
    # WORKFLOW_DIR at an empty path on purpose. Treating that as a malfunction
    # would block every PR in such a repo.
    gate._declared_deploy_globs.cache_clear()
    monkeypatch.setattr(gate, "WORKFLOW_DIR", gate.ROOT / "no-such-dir")
    try:
        assert gate._declared_deploy_globs() == ((), True)
        assert gate.evaluate(_facts(), now=NOW).ready
    finally:
        gate._declared_deploy_globs.cache_clear()
