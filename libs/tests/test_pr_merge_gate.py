"""tools/pr_merge_gate: the session-scoped merge authority as a check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import pr_merge_gate as gate

ROOT = Path(__file__).resolve().parents[2]

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Both filesystem reads in the gate are ``lru_cache``d and take no
    arguments, so a test that monkeypatches ``yaml``, ``ROOT`` or
    ``WORKFLOW_DIR`` poisons them for the rest of the session.

    Per-test ``finally`` blocks were the first attempt and did not hold: a
    mutation audit found 2 of 120 shuffled orders failing with a 16-test blast
    radius because one test cleared one cache and not the other, and the fix
    for that immediately reintroduced it in a newly added test. Clearing both
    around every test removes the discipline requirement instead of restating
    it.
    """
    gate._declared_deploy_globs.cache_clear()
    gate._required_checks.cache_clear()
    yield
    gate._declared_deploy_globs.cache_clear()
    gate._required_checks.cache_clear()


def _facts(**overrides) -> gate.HeadFacts:
    base = dict(
        number=704,
        state="OPEN",
        draft=False,
        base="main",
        head_sha="abcdef0123456789",
        files=("libs/probe_specs.py",),
        last_push_at=NOW - 13 * 60,
        checks=tuple((name, "pass") for name in sorted(gate._required_checks()[0]))
        + (("Docs", "skipping"),),
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
    required = sorted(gate._required_checks()[0])
    # Required checks reported by `state` rather than `bucket`; the point of the
    # test is the fallback, so they must still be present.
    assert gate.evaluate(
        _facts(checks=tuple((name, "SUCCESS") for name in required)), now=NOW
    ).ready
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
                    "changedFiles": 1,
                    "mergeable": getattr(self, "mergeable", "MERGEABLE"),
                    "mergeStateStatus": getattr(self, "merge_state", "CLEAN"),
                    "commits": [
                        {"oid": "0" * 40, "committedDate": "2026-09-15T06:40:00Z"},
                        {"oid": "feedfacefeedface", "committedDate": self.pushed_iso},
                    ],
                }
            )
        if argv[:2] == ["pr", "checks"]:
            # gh's real shape (read live 2026-09-15): `state` is SUCCESS/SKIPPED/…,
            # `bucket` is pass/fail/pending/skipping; there is no `conclusion` field,
            # and `pr view --json files,commits` returns flat arrays.
            # The required gates are emitted green as background, so a case can
            # vary `states` to exercise one condition without tripping the
            # separate "a required check never reported" rule.
            return json.dumps(
                [
                    {"name": name, "state": "COMPLETED", "bucket": "pass"}
                    for name in sorted(gate._required_checks()[0])
                ]
                + [
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
    # A known epoch, not _epoch() on both sides -- that tautology survived
    # mutating _epoch to return a constant 42.0.
    assert facts.last_push_at == 1789455322.0
    assert ("c0", "pass") in facts.checks and facts.unresolved_threads == 0
    # Pinned literally, not compared against _required_checks(): the fixture
    # builds its checks FROM that call, so asserting equality made whatever it
    # returned definitionally correct. Mutating it to {"Totally Made Up Gate"}
    # killed no tests.
    assert sorted(gate._required_checks()[0]) == [
        "Lint Python Code",
        "Test 1Password Healthcheck",
        "Test Deployer Hash Logic",
        "Validate Compose Files",
        "Validate Deployer Classes",
        "Validate Vault Agent Config",
        "Validate Workspace Harness",
    ]
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
                doc["files"].append({"path": "AGENTS.md"})
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
    mock_run.return_value.stdout = json.dumps(
        {
            "verdict": "BLOCKED",
            "commit": "feedfacefeedface",
            "findings": [
                {"severity": "CRITICAL", "topic": "Memory Leak", "details": "leak"}
            ],
        }
    )
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


def test_a_prod_reaching_deploy_needs_the_owner_and_a_canary_does_not():
    # Owner approval is scoped by environment (owner, 2026-09-21): staging, the
    # reserved pr-0 canary slot and the report-branch-main preview are the
    # agent's to merge; prod is not. apply-observability writes to live SigNoz,
    # so it stays owner-required; ops-checks' canary targets the reserved
    # ephemeral slot, so it does not.
    assert gate._deploy_triggering("libs/alerting.py") == "apply-observability.yml"
    # deploy.yml is in the written list, which reports "on merge" -- it has no
    # workflow to cite because the merge itself is the trigger.
    assert gate._deploy_triggering(".github/workflows/deploy.yml") == "on merge"
    assert not gate._deploy_triggering("tools/deploy_v2.py")
    assert not gate._deploy_triggering(".github/workflows/ops-checks.yml")


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
    # libs/alerting.py, not tools/deploy_v2.py: the latter only starts the
    # reserved-slot canary, which is the agent's to merge since the owner
    # scoped approval by environment.
    verdict = gate.evaluate(_facts(files=("libs/alerting.py",)), now=NOW)
    assert not verdict.ready
    assert verdict.owner_required
    assert any("trigger a deploy" in r for r in verdict.reasons)


def test_a_missing_workflow_directory_is_a_broken_read_not_an_empty_repo(monkeypatch):
    # Replaces a test that asserted ((), True) here AND that tools/deploy_v2.py
    # was not deploy-triggering -- it ratified the silent loss of every derived
    # deploy path. Path.glob swallows scandir errors, so a missing directory is
    # indistinguishable from an empty one by its result and the OSError branch
    # never fires; existence has to be asked directly.
    gate._declared_deploy_globs.cache_clear()
    monkeypatch.setattr(gate, "WORKFLOW_DIR", gate.ROOT / "does-not-exist")
    try:
        assert gate._declared_deploy_globs() == ((), False)
        assert gate._deploy_triggering("bootstrap/06.iac_runner/main.tf")
        verdict = gate.evaluate(_facts(), now=NOW)
        assert not verdict.ready
        assert any("cannot read .github/workflows" in r for r in verdict.reasons)
    finally:
        gate._declared_deploy_globs.cache_clear()
        gate._required_checks.cache_clear()


def test_the_deploy_reason_names_the_workflow_that_fires():
    # "tools/deploy_v2.py triggers a deploy" makes an owner go and find out
    # which one. Naming it makes the line a judgement they can act on.
    verdict = gate.evaluate(_facts(files=("libs/alerting.py",)), now=NOW)
    reason = next(r for r in verdict.reasons if "trigger a deploy" in r)
    assert "apply-observability.yml" in reason
    assert "libs/alerting.py" in reason


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


def test_an_unreadable_workflow_set_blocks_instead_of_quietly_under_detecting(
    monkeypatch,
):
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

    # _required_checks is cached and also parses YAML, so the fake propagates
    # into it and (frozenset(), False) sticks for the rest of the session --
    # 2 of 120 shuffled orders failed, taking 16 tests with them, because this
    # cleared only the other cache.
    gate._declared_deploy_globs.cache_clear()
    gate._required_checks.cache_clear()
    monkeypatch.setattr(gate, "yaml", Boom)
    try:
        _, healthy = gate._declared_deploy_globs()
        assert not healthy
        verdict = gate.evaluate(_facts(), now=NOW)
        assert not verdict.ready
        assert any("cannot read .github/workflows" in r for r in verdict.reasons)
    finally:
        gate._declared_deploy_globs.cache_clear()
        gate._required_checks.cache_clear()


def test_an_existing_but_empty_workflow_directory_is_healthy(tmp_path, monkeypatch):
    # A repository that genuinely has no workflows is a real state; blocking
    # every PR there would be its own bug. The distinction from the test above
    # is whether the directory exists -- the one thing Path.glob's result
    # cannot tell you.
    gate._declared_deploy_globs.cache_clear()
    monkeypatch.setattr(gate, "WORKFLOW_DIR", tmp_path)
    try:
        assert gate._declared_deploy_globs() == ((), True)
        assert gate.evaluate(_facts(), now=NOW).ready
    finally:
        gate._declared_deploy_globs.cache_clear()


# --- findings from the /audit scouts -----------------------------------------


def _green(**overrides) -> gate.HeadFacts:
    """A head that is ready but for whatever the caller overrides."""
    required = sorted(gate._required_checks()[0])
    base = dict(
        checks=tuple((name, "pass") for name in required),
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        changed_files=1,
    )
    base.update(overrides)
    return _facts(**base)


def test_a_docs_only_pr_whose_required_checks_skipped_is_still_mergeable():
    # This replaces a test asserting the opposite, which was wrong and broke a
    # designed path. infra-ci.yml:69-82 states that skipping a required job via
    # `if:` is a passing state: a PR whose whole diff is Markdown needs none of
    # those gates, and #709 and #673 merged in exactly that shape. Blocking it
    # made every docs-only PR unmergeable forever -- including the one carrying
    # this repository's own merge-authority rules.
    #
    # The case the old assertion meant to catch, `detect-changes` failing and
    # taking its dependents down as skipped, is covered by the next test: that
    # job is itself red, and `not_green` scans every check.
    required = sorted(gate._required_checks()[0])
    verdict = gate.evaluate(
        _green(
            files=("docs/ssot/ops.pipeline.md",),
            checks=tuple((name, "skipping") for name in required),
        ),
        now=NOW,
    )
    assert verdict.ready


def test_detect_changes_failing_still_blocks_even_though_its_dependents_skipped():
    required = sorted(gate._required_checks()[0])
    verdict = gate.evaluate(
        _green(
            checks=tuple((name, "skipping") for name in required)
            + (("Detect Non-Doc Changes", "fail"),)
        ),
        now=NOW,
    )
    assert not verdict.ready
    assert any("Detect Non-Doc Changes" in r for r in verdict.reasons)


def test_a_required_check_that_never_reported_is_not_absence_of_a_problem():
    verdict = gate.evaluate(_green(checks=(("Some Other Job", "pass"),)), now=NOW)
    assert not verdict.ready
    assert any("never reported" in r for r in verdict.reasons)


def test_a_truncated_file_list_cannot_be_trusted_for_the_owner_gates():
    # gh pr view --json files caps at 100. Both the protected-file and the
    # deploy-triggering checks read that list, so a larger PR would silently
    # drop the owner gate. Real shape: finance_report#2042, 163 changed, 100
    # returned, with every deploy-triggering path past the cut.
    verdict = gate.evaluate(
        _green(files=tuple(f"f{i}.py" for i in range(100)), changed_files=163), now=NOW
    )
    assert not verdict.ready
    assert verdict.owner_required
    assert any("100 of 163" in r for r in verdict.reasons)


def test_changing_what_decides_merges_needs_the_owner():
    # The gate decides from the working tree, which for an agent merging its
    # own PR is that PR's branch -- the change judges itself. #765 moved the
    # merge-authority rules into SSOT, out of the protected file, while a
    # .md-only PR also skips every required check.
    for path in (
        "tools/pr_merge_gate.py",
        "libs/tests/test_pr_merge_gate.py",
        "docs/ssot/ops.merge-gate.md",
        "docs/ssot/ci-gate-inventory.yaml",
        "AGENTS.md",
    ):
        verdict = gate.evaluate(_green(files=(path,)), now=NOW)
        assert not verdict.ready, path
        assert verdict.owner_required, path
        assert any("what decides merges" in r for r in verdict.reasons), path


def test_an_ordinary_green_head_still_merges():
    # The guards above must not make every PR owner-required; that would make
    # the signal worthless and push everything back onto the owner.
    verdict = gate.evaluate(_green(files=("libs/x.py",)), now=NOW)
    assert verdict.ready
    assert not verdict.owner_required


def test_a_head_missing_from_the_commit_page_cannot_be_judged_settled():
    # The commits connection is capped at 100 and returned oldest-first, so on a
    # long branch the newest commit -- the very one the quiet period measures
    # from -- is the one missing. And an empty list yields last_push_at = 0.0,
    # which reads as "pushed in 1970" and settles instantly. Measured before
    # this guard: last_push_at=0.0 gave ready=True with an empty reasons list.
    verdict = gate.evaluate(_facts(last_push_at=0.0), now=NOW)
    assert not verdict.ready
    assert any("settling window cannot be measured" in r for r in verdict.reasons)


def test_collect_refuses_a_commit_page_that_does_not_contain_the_head():
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
                    "changedFiles": 0,
                    # An old commit, and no sign of the head: exactly the shape
                    # a >100-commit branch returns.
                    "commits": [
                        {"oid": "0" * 40, "committedDate": "2020-01-01T00:00:00Z"}
                    ],
                    "reviews": [],
                    "id": "X",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
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
        if argv[:1] == ["api"]:
            return json.dumps({"files": []})
        raise AssertionError(argv)

    assert gate.collect(1, gh=fake).last_push_at == 0.0


def test_gh_is_called_without_a_terminal_and_with_a_deadline(monkeypatch):
    # This runs unattended. A gh that decides to prompt would block on a
    # terminal that is not there, and the gate would hang rather than fail.
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return R()

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    gate._gh(["pr", "view", "1"])
    assert seen["stdin"] is gate.subprocess.DEVNULL
    # A literal, not gate.GH_TIMEOUT_S: comparing the observed value against
    # the constant it came from passes for any value, including 0.
    assert seen["timeout"] == 60


def test_a_required_check_skipped_on_a_code_pr_is_the_unexpected_kind():
    # Blocking every skip made docs-only PRs unmergeable; allowing every skip
    # lost the 意外 skipped clause entirely. The workflow's own answer cannot
    # decide it: infra-ci computes has_non_doc from `git diff` against the base,
    # so a rewritten base yields a wrong file list, emits has_non_doc=false, and
    # reports Detect Non-Doc Changes GREEN while every required gate skips.
    # GitHub's file list for the PR is independent of that computation.
    required = sorted(gate._required_checks()[0])
    verdict = gate.evaluate(
        _green(
            files=("libs/deploy_contract.py",),
            checks=tuple((name, "skipping") for name in required)
            + (("Detect Non-Doc Changes", "pass"),),
        ),
        now=NOW,
    )
    assert not verdict.ready
    assert any("changes non-Markdown files" in r for r in verdict.reasons)


def test_a_requested_change_blocks_even_when_it_left_no_thread():
    # The gate counted unresolved threads only, so a reviewer clicking Request
    # changes without an inline thread was invisible -- and with
    # required_approving_review_count: 0 the ruleset does not withhold the merge
    # either, so mergeStateStatus stays CLEAN and nothing else notices.
    verdict = gate.evaluate(
        _green(review_decision="CHANGES_REQUESTED", unresolved_threads=0), now=NOW
    )
    assert not verdict.ready
    assert any("requested changes" in r for r in verdict.reasons)


def test_a_field_gh_omitted_blocks_even_when_its_own_guard_would_switch_off():
    # changedFiles was guarded one field at a time and kept the old `or 0`
    # treatment, so `if facts.changed_files` switched off the very check that
    # stops a truncated file list from dropping the owner gates. A blind audit
    # showed the gate issuing `gh pr merge` on a 163-file PR because of it.
    # The guard is now over the set of requested-but-missing fields, so the
    # next field added cannot repeat it.
    verdict = gate.evaluate(_green(absent_fields=("changedFiles",)), now=NOW)
    assert not verdict.ready
    assert any("gh did not return changedFiles" in r for r in verdict.reasons)


def test_collect_records_every_requested_field_gh_did_not_return():
    def fake(argv):
        if argv[:2] == ["pr", "view"]:
            # No files, changedFiles, commits, mergeable or mergeStateStatus.
            return json.dumps(
                {
                    "number": 1,
                    "state": "OPEN",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "f" * 40,
                    "reviews": [],
                    "id": "X",
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
        if argv[:1] == ["api"]:
            return json.dumps({"files": []})
        raise AssertionError(argv)

    facts = gate.collect(1, gh=fake)
    assert set(facts.absent_fields) == {
        "files",
        "changedFiles",
        "commits",
        "mergeable",
        "mergeStateStatus",
    }


def test_a_tags_only_workflow_cannot_be_started_by_a_merge():
    # `tags` without `branches` means tag pushes only. Reading an absent
    # `branches` as "every branch" is right in general and wrong here; it was
    # masked until the omitted-`paths` case stopped defaulting to nothing, at
    # which point reconcile-iac-inputs.yml made every file deploy-triggering.
    gate._declared_deploy_globs.cache_clear()
    try:
        workflows = {w for _, w in gate._declared_deploy_globs()[0]}
        assert "reconcile-iac-inputs.yml" not in workflows
        assert not gate._deploy_triggering("libs/probe_specs.py")
    finally:
        gate._declared_deploy_globs.cache_clear()


def test_an_unreadable_workflow_read_escalates_rather_than_asking_for_patience(
    monkeypatch,
):
    # exit 1 means "not yet" and a poller retries it forever. Not knowing
    # whether a merge deploys is exactly the case that must escalate.
    class Boom:
        class YAMLError(Exception):
            pass

        @staticmethod
        def safe_load(_):
            raise Boom.YAMLError("unparseable")

    gate._declared_deploy_globs.cache_clear()
    monkeypatch.setattr(gate, "yaml", Boom)
    try:
        verdict = gate.evaluate(_green(), now=NOW)
        assert not verdict.ready
        assert verdict.owner_required
        assert verdict.exit_code == 2
    finally:
        gate._declared_deploy_globs.cache_clear()


def test_a_draft_is_not_mergeable():
    # Zero coverage before this: `draft=` appeared once in the whole file, as
    # the fixture's own default. Mutating `if facts.draft:` to `if False:`
    # killed no tests, and "PR 非 Draft" is the first condition AGENTS.md names.
    verdict = gate.evaluate(_green(draft=True), now=NOW)
    assert not verdict.ready
    assert any("draft" in r for r in verdict.reasons)


def test_a_pr_targeting_something_other_than_main_needs_the_owner():
    # Same: `base=` appeared once, as base="main". This branch sets owner too,
    # so both the block and the escalation were uncovered.
    verdict = gate.evaluate(_green(base="release/1.0"), now=NOW)
    assert not verdict.ready
    assert verdict.owner_required
    assert any("not main" in r for r in verdict.reasons)


def test_the_written_globs_are_load_bearing_on_their_own():
    # The list named in the source and the list derived from the workflows
    # overlap completely, so a test asserting a written path matched proved
    # nothing -- dropping any written glob killed no tests. Isolate them.
    gate._declared_deploy_globs.cache_clear()
    try:
        derived = {glob for glob, _ in gate._declared_deploy_globs()[0]}
        for written in gate.DEPLOY_TRIGGERING_GLOBS:
            if written in derived:
                continue  # covered either way; not what this test is for
            assert gate._deploy_triggering(
                written.replace("**", "x").replace("*", "x")
            ), written
    finally:
        gate._declared_deploy_globs.cache_clear()


def test_an_unreadable_inventory_blocks_through_the_real_path(monkeypatch):
    # The "cannot read ci-gate-inventory.yaml" blocker was only ever reached by
    # forcing _required_checks to return False. Through the real code path --
    # the file simply not being there -- nothing exercised it.
    gate._required_checks.cache_clear()
    monkeypatch.setattr(gate, "ROOT", gate.ROOT / "does-not-exist")
    try:
        assert gate._required_checks() == (frozenset(), False)
        verdict = gate.evaluate(_green(), now=NOW)
        assert not verdict.ready
        assert any("ci-gate-inventory" in r for r in verdict.reasons)
    finally:
        gate._required_checks.cache_clear()


def test_a_null_valued_field_is_absent_too_not_merely_a_missing_key():
    # The class guard asked "was the key there?" while _field(), two lines
    # above it in the same commit, asked "did an answer arrive?". A blind audit
    # drove {"files": null, "changedFiles": null} end to end: empty file list,
    # truncation guard switched off by its own `if facts.changed_files`, every
    # owner gate iterating nothing -- exit 0, and the tool printed
    # "merged #704". None of these five is ever legitimately empty on a real PR.
    def fake(argv):
        if argv[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 1,
                    "state": "OPEN",
                    "isDraft": False,
                    "baseRefName": "main",
                    "headRefOid": "f" * 40,
                    "files": None,
                    "changedFiles": None,
                    "commits": None,
                    "reviews": [],
                    "id": "X",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            )
        if argv[:2] == ["pr", "checks"]:
            return json.dumps(
                [
                    {"name": n, "state": "COMPLETED", "bucket": "pass"}
                    for n in sorted(gate._required_checks()[0])
                ]
            )
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
        if argv[:1] == ["api"]:
            return json.dumps({"files": []})
        raise AssertionError(argv)

    facts = gate.collect(1, gh=fake)
    assert set(facts.absent_fields) == {"files", "changedFiles", "commits"}
    verdict = gate.evaluate(facts, now=NOW)
    assert not verdict.ready
    assert any("gh did not return" in r for r in verdict.reasons)


def test_a_directory_that_exists_but_cannot_be_read_is_not_healthy(tmp_path):
    # is_dir() is True for a directory with no read permission, and Path.glob
    # swallows the PermissionError exactly as it swallows FileNotFoundError --
    # so the fix for the missing-directory case walked straight back into the
    # trap its own docstring describes: healthy=True, zero derived globs, and a
    # ready verdict with an empty reasons list while libs/alerting.py (a live
    # SigNoz apply) came back not deploy-triggering.
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "x.yml").write_text(
        "on:\n  push:\n    branches: [main]\n    paths: ['libs/**']\njobs: {}\n"
        "# terraform apply\n",
        encoding="utf-8",
    )
    import os as _os

    _os.chmod(workflows, 0o000)
    try:
        gate._declared_deploy_globs.cache_clear()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gate, "WORKFLOW_DIR", workflows)
            globs, healthy = gate._declared_deploy_globs()
            assert globs == ()
            assert not healthy
    finally:
        _os.chmod(workflows, 0o755)
        gate._declared_deploy_globs.cache_clear()
