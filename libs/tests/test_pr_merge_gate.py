"""tools/pr_merge_gate: the session-scoped merge authority as a check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import pr_merge_gate as gate

ROOT = Path(__file__).resolve().parents[2]

NOW = 1_800_000_000.0


def _lru_cached_in_gate():
    """Every ``lru_cache``d function in the module, found rather than listed.

    The hand-written version named two and the module grew a third
    (``self_governing_files``, #787). Nothing noticed until an audit ran two
    tests in the order a developer would (`pytest <file>` alone): a test that
    monkeypatches ``gate.ROOT`` poisons that cache permanently, the protected
    set degrades to its fallback, and a later pull request that changes
    ``ci-gate-inventory.yaml`` -- a genuinely self-governing file -- comes back
    ``ready=True, owner_required=False``. The gate's own core invariant, gone,
    with every test still green in the default collection order.

    So the list is computed. A fourth cache cannot be missed.
    """
    return [
        value
        for value in vars(gate).values()
        if callable(value) and hasattr(value, "cache_clear")
    ]


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Every filesystem read in the gate is ``lru_cache``d and takes no
    arguments, so a test that monkeypatches ``yaml``, ``ROOT`` or
    ``WORKFLOW_DIR`` poisons them for the rest of the session.

    Per-test ``finally`` blocks were the first attempt and did not hold: a
    mutation audit found 2 of 120 shuffled orders failing with a 16-test blast
    radius because one test cleared one cache and not the other, and the fix
    for that immediately reintroduced it in a newly added test. Clearing all of
    them around every test removes the discipline requirement instead of
    restating it.
    """
    for cached in _lru_cached_in_gate():
        cached.cache_clear()
    yield
    for cached in _lru_cached_in_gate():
        cached.cache_clear()


def test_every_cached_read_is_cleared_between_tests():
    """The fixture above must find them, not have them listed. A third cache
    was added and the two-name list did not notice; this asserts the discovery
    works and that there is more than one to find."""
    found = {c.__name__ for c in _lru_cached_in_gate()}
    assert {
        "_declared_deploy_globs",
        "_required_checks",
        "self_governing_files",
    } <= found, found


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
    # unresolved_weight, not the thread count: AGENTS.md weighs findings and
    # blocks at 1.0. Two unlabelled threads are 0.5 each, which is what this
    # case was always describing -- enough to block.
    verdict = gate.evaluate(
        _facts(
            checks=(("Tests", "pending"),), unresolved_threads=2, unresolved_weight=1.0
        ),
        now=NOW,
    )
    assert verdict.exit_code == 1 and not verdict.owner_required
    assert "check(s) not green: Tests" in verdict.reasons
    assert any("weigh 1" in r for r in verdict.reasons)


def test_one_unlabelled_finding_no_longer_blocks_on_its_own():
    """The behaviour change this implementation introduces, stated outright.

    AGENTS.md has always said high=1.0 / middle=0.5 / low=0.25, unlabelled as
    middle, blocking at 1.0. The gate counted threads instead, so a single
    wording nit from a reviewer that labels nothing -- no automated reviewer
    does -- stalled a merge exactly as hard as a correctness defect. This is
    the rule the document already carried; the code has caught up to it.
    """
    verdict = gate.evaluate(
        _green(unresolved_threads=1, unresolved_weight=0.5), now=NOW
    )
    assert not any("weigh" in r for r in verdict.reasons), verdict.reasons


def test_one_high_finding_still_blocks_alone():
    """The other half: weighting must not make a real finding cheaper."""
    verdict = gate.evaluate(
        _green(unresolved_threads=1, unresolved_weight=1.0), now=NOW
    )
    assert any("weigh 1" in r for r in verdict.reasons)


@pytest.mark.parametrize(
    ("bodies", "expected"),
    (
        (["no label here"], gate.UNLABELLED_SEVERITY_WEIGHT),
        (["severity: low"], 0.25),
        (["Severity: HIGH"], 1.0),
        (["**severity: middle**"], 0.5),
        # The highest label in the thread wins: a reply downgrading its own nit
        # must not lower a `high` raised above it.
        (["severity: low", "on reflection severity: high"], 1.0),
        # Prose is not a label. Inferring from wording would make the verdict
        # depend on phrasing.
        (["this is a high severity problem"], gate.UNLABELLED_SEVERITY_WEIGHT),
    ),
)
def test_thread_weight_reads_only_explicit_labels(bodies, expected):
    assert gate.thread_weight(bodies) == expected


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
        self,
        *,
        unresolved=0,
        states=("pass",),
        pushed_iso="2026-09-15T06:55:22Z",
        body="",
    ):
        self.calls: list[list[str]] = []
        self.unresolved = unresolved
        self.states = states
        self.pushed_iso = pushed_iso
        self.body = body

    def __call__(self, argv):
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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
                    "body": self.body,
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


def test_collect_reads_the_pr_body_for_the_owner_instruction_citation():
    facts = gate.collect(704, gh=_Gh(body="## Owner instruction\n\n> 引用测试"))
    assert facts.body == "## Owner instruction\n\n> 引用测试"


def test_collect_defaults_a_missing_body_to_empty_not_none():
    # gh returns null, not an absent key, for an empty PR description. `HeadFacts.body`
    # is read by a regex, so it must be a str even when GitHub gave nothing back.
    facts = gate.collect(704, gh=_Gh(body=None))
    assert facts.body == ""
    assert facts.review_threads_total == 1
    assert facts.node_id == "PR_node"
    assert facts.reviews_on_head()[0][0] == "copilot-pull-request-reviewer"


def test_request_review_asks_copilot_only_when_the_head_is_unreviewed(capsys):
    class _Unreviewed(_Gh):
        def __call__(self, argv):
            if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
                # A clean checkout: every rule file matches the base branch.
                return _tree_payload()
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
            if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
                # A clean checkout: every rule file matches the base branch.
                return _tree_payload()
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
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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


# -- #814: rule-text files get a second way to clear "what decides merges" --------
#
# AGENTS.md / ops.merge-gate.md have no direction-proof reading (prose, not a closed
# schema), so they always went to the owner even for a change that only tightens the
# gate. 2026-09-22 owner instruction ("授权给你 merge 权限啊。为什么卡我这？") added a
# second, non-directional way out for exactly these two files: cite the instruction
# in the PR body. `_owner_instruction_quoted` is the mechanical check for that
# citation; it must not read anything else in self_governing_files() as excused.


@pytest.mark.parametrize(
    "body",
    [
        "## Owner instruction\n\n> 授权给你 merge 权限啊。为什么卡我这？",
        "## owner 指示\n\n> 授权给你 merge 权限啊。为什么卡我这？",
        "Owner instruction:\n> quote right under it, no blank line",
        "## Owner Instruction\n\n「授权给你 merge 权限啊」",
        "## Owner instruction\n\n\n> quote after two blank lines",
        "intro text\n\n## Owner instruction\n> quote\n\nmore text after the quote",
        "## Owner instruction\n\n> 授权给你 merge 权限啊",
        # A 「...」 quote merely needs to appear on the line, not open it -- the
        # SSOT wording is "含「...」原话", not "line starts with 「".
        "## Owner instruction\n\n"
        "2026-09-22: 「加速收敛啊，包括 sub-agent」;「总裁要约定心跳机制的哇」",
    ],
)
def test_owner_instruction_quoted_recognises_a_header_and_its_quote(body):
    assert gate._owner_instruction_quoted(body)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no header at all, just prose that mentions the owner instruction",
        "## Owner instruction\n\nthis only claims one exists, no quote follows",
        "## Owner instruction",  # header with nothing after it
        "## Owner instruction\n\n\n",  # header, then only blank lines to EOF
        "## Something else\n\n> a quote, but under the wrong header",
        # A bare `>` (or `「」`) is a citation of nothing -- it must not count as
        # quoting the instruction just because it looks like markdown quote syntax.
        "## Owner instruction\n\n>",
        "## Owner instruction\n\n>   ",
        "## Owner instruction\n\n「」",
        # A stray `」` is not "content" either -- `\S` matches a closing bracket
        # just as readily as real text, so a naive fix for the empty-quote bug
        # above can still be fooled by an empty quote followed by loose `」`s.
        "## Owner instruction\n\n「」」",
        "## Owner instruction\n\n「」 」",
    ],
)
def test_owner_instruction_quoted_rejects_a_header_without_a_quote_beneath_it(body):
    assert not gate._owner_instruction_quoted(body)


def test_owner_instruction_quoted_tries_a_later_header_after_an_empty_one():
    body = (
        "## Owner instruction\n\nfirst attempt claims one, quotes nothing\n\n"
        "## Owner instruction\n\n> second attempt actually quotes the words"
    )
    assert gate._owner_instruction_quoted(body)


def test_rule_text_files_clear_the_owner_gate_when_the_body_cites_the_instruction():
    verdict = gate.evaluate(
        _green(
            files=("AGENTS.md", "docs/ssot/ops.merge-gate.md"),
            body="## Owner instruction\n\n> 授权给你 merge 权限啊。为什么卡我这？",
        ),
        now=NOW,
    )
    assert verdict.ready and verdict.exit_code == 0, verdict.reasons


def test_rule_text_files_still_need_the_owner_without_a_citation():
    """The control for the test above: an empty body is the pre-#814 status quo,
    and the reason must point the caller at the way out."""
    verdict = gate.evaluate(
        _green(files=("AGENTS.md", "docs/ssot/ops.merge-gate.md")), now=NOW
    )
    assert verdict.owner_required and verdict.exit_code == 2
    assert any("what decides merges" in r for r in verdict.reasons)
    assert any("citing the owner instruction" in r for r in verdict.reasons), (
        verdict.reasons
    )


def test_a_header_with_no_quote_beneath_it_still_needs_the_owner():
    verdict = gate.evaluate(
        _green(
            files=("AGENTS.md",),
            body="## Owner instruction\n\nthis merely asserts one exists",
        ),
        now=NOW,
    )
    assert verdict.owner_required and verdict.exit_code == 2


def test_a_citation_does_not_excuse_the_gates_own_code():
    """A quote in the body proves nothing about pr_merge_gate.py itself: the
    defendant-rewrites-the-law hazard self_governing_files() exists for is
    unaffected by what the PR body says about itself. Mirrors
    test_a_proven_file_does_not_excuse_an_unproven_one for the direction-proof
    carve-out."""
    verdict = gate.evaluate(
        _green(
            files=("AGENTS.md", "tools/pr_merge_gate.py"),
            body="## Owner instruction\n\n> 授权给你 merge 权限啊。为什么卡我这？",
        ),
        now=NOW,
    )
    assert verdict.owner_required and verdict.exit_code == 2
    assert "tools/pr_merge_gate.py" in verdict.reasons[0]
    assert "AGENTS.md" not in verdict.reasons[0], (
        "the cited rule-text file must not be named as a blocker"
    )


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
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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
        if argv[:1] == ["api"] and "/git/trees/" in argv[1]:
            # A clean checkout: every rule file matches the base branch.
            return _tree_payload()
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


# -- Direction of a change to the rules that judge merges ----------------------------
#
# Owner instruction 2026-09-22: "方向可机械证明为收紧则放行。放松需要给我审核."


def _inv(*gates: tuple[str, str, str, bool]) -> str:
    body = "".join(
        f"  - id: {i}\n"
        f"    stage: github_ci.merge_authority\n"
        f"    task_category: t\n"
        f"    workflow: {w}\n"
        f"    job: {j}\n"
        f"    blocks_merge: {str(b).lower()}\n"
        for i, w, j, b in gates
    )
    return f"version: 1\nrepo_prefix: infra_ci.\ngates:\n{body}"


BASE_INV = _inv(
    ("infra_ci.lint", ".github/workflows/infra-ci.yml", "lint-python", True),
    ("infra_ci.compose", ".github/workflows/infra-ci.yml", "validate-compose", True),
)


def test_an_unchanged_blocking_set_is_proven_tighter():
    """The backfill shape: rows added, authority untouched. Equal is non-loosening."""
    assert gate._inventory_only_gained_authority(BASE_INV, BASE_INV)


def test_registering_a_non_blocking_gate_is_proven_tighter():
    head = BASE_INV + (
        "  - id: infra_ci.smoke\n"
        "    stage: ops.scheduled_cleanup\n"
        "    task_category: t\n"
        "    workflow: .github/workflows/ops-checks.yml\n"
        "    job: pi-chain-smoke\n"
        "    blocks_merge: false\n"
    )
    assert gate._inventory_only_gained_authority(BASE_INV, head)


def test_promoting_a_gate_to_blocking_is_proven_tighter():
    head = _inv(
        ("infra_ci.lint", ".github/workflows/infra-ci.yml", "lint-python", True),
        (
            "infra_ci.compose",
            ".github/workflows/infra-ci.yml",
            "validate-compose",
            True,
        ),
        (
            "infra_ci.vault",
            ".github/workflows/infra-ci.yml",
            "validate-vault-agent",
            True,
        ),
    )
    assert gate._inventory_only_gained_authority(BASE_INV, head)


def test_removing_a_blocking_gate_is_not_proven():
    head = _inv(
        ("infra_ci.lint", ".github/workflows/infra-ci.yml", "lint-python", True)
    )
    assert not gate._inventory_only_gained_authority(BASE_INV, head)


def test_demoting_a_blocking_gate_is_not_proven():
    head = _inv(
        ("infra_ci.lint", ".github/workflows/infra-ci.yml", "lint-python", True),
        (
            "infra_ci.compose",
            ".github/workflows/infra-ci.yml",
            "validate-compose",
            False,
        ),
    )
    assert not gate._inventory_only_gained_authority(BASE_INV, head)


def test_repointing_a_blocking_gate_at_another_job_is_not_proven():
    """The id set is unchanged and the count is unchanged -- but the gate now
    watches a different job, which is how a blocking check becomes a passing one
    without anything looking removed."""
    head = _inv(
        ("infra_ci.lint", ".github/workflows/infra-ci.yml", "lint-python", True),
        ("infra_ci.compose", ".github/workflows/infra-ci.yml", "detect-changes", True),
    )
    assert not gate._inventory_only_gained_authority(BASE_INV, head)


def test_an_empty_base_blocking_set_proves_nothing():
    """Otherwise every head is a superset of nothing -- and the change that would
    benefit most from that is the one that empties the set."""
    assert not gate._inventory_only_gained_authority(_inv(), BASE_INV)
    assert not gate._inventory_only_gained_authority("gates: []\n", BASE_INV)


@pytest.mark.parametrize("bad", ["", "gates: [\n", "gates: notalist\n", "- a\n- b\n"])
def test_an_unreadable_version_proves_nothing(bad):
    assert not gate._inventory_only_gained_authority(bad, BASE_INV)
    assert not gate._inventory_only_gained_authority(BASE_INV, bad)


def test_only_files_with_a_stated_proof_can_be_proven():
    """Python in the closure has no mechanical reading of "stricter", so it must never
    be proven -- not even when the two versions are identical."""
    calls: list[list[str]] = []

    def fake_gh(argv):
        calls.append(list(argv))
        return "print('hello')\n"

    proven = gate._proven_tighter(
        "o/r",
        "main",
        "deadbeef",
        ("tools/pr_merge_gate.py", "AGENTS.md", "libs/console.py"),
        gh=fake_gh,
    )
    assert proven == ()
    assert calls == [], "a file with no stated proof must not even be fetched"


def test_an_unreachable_version_is_not_proven():
    """Fail closed: if either side cannot be read, nothing is proven."""

    def fake_gh(argv):
        raise RuntimeError("404")

    assert (
        gate._proven_tighter(
            "o/r", "main", "deadbeef", ("docs/ssot/ci-gate-inventory.yaml",), gh=fake_gh
        )
        == ()
    )


def test_a_proven_inventory_change_does_not_need_the_owner():
    verdict = gate.evaluate(
        _facts(
            files=("docs/ssot/ci-gate-inventory.yaml", "tools/ci_gate_audit.py"),
            proven_tighter=("docs/ssot/ci-gate-inventory.yaml",),
        ),
        now=NOW,
    )
    assert verdict.ready and verdict.exit_code == 0, verdict.reasons


def test_the_same_change_unproven_still_needs_the_owner():
    """The control for the test above: it is the proof doing the work, not the path."""
    verdict = gate.evaluate(
        _facts(files=("docs/ssot/ci-gate-inventory.yaml", "tools/ci_gate_audit.py")),
        now=NOW,
    )
    assert verdict.owner_required and verdict.exit_code == 2
    assert "ci-gate-inventory.yaml" in verdict.reasons[0]


def test_a_proven_file_does_not_excuse_an_unproven_one():
    """Tightening the inventory must not buy a free edit to the gate's own code."""
    verdict = gate.evaluate(
        _facts(
            files=("docs/ssot/ci-gate-inventory.yaml", "tools/pr_merge_gate.py"),
            proven_tighter=("docs/ssot/ci-gate-inventory.yaml",),
        ),
        now=NOW,
    )
    assert verdict.owner_required and verdict.exit_code == 2
    assert "tools/pr_merge_gate.py" in verdict.reasons[0]
    assert "ci-gate-inventory.yaml" not in verdict.reasons[0], (
        "the proven file must not be named as a blocker"
    )


def test_hand_built_facts_prove_nothing_by_default():
    """The field defaults to empty, so every code path that does not run the proof
    falls back to the owner rather than past them."""
    assert (
        gate.HeadFacts(
            number=1,
            state="OPEN",
            draft=False,
            base="main",
            head_sha="a" * 40,
            files=("docs/ssot/ci-gate-inventory.yaml",),
            last_push_at=NOW,
            checks=(),
            unresolved_threads=0,
        ).proven_tighter
        == ()
    )


# -- Are the rules doing the judging the merged ones? --------------------------------
#
# Measured 2026-09-22: a background poller merged #791 while the working tree sat on
# the branch of #792 -- the unreviewed PR that relaxes exactly the rule #791 needed
# relaxed. Nothing was lost (#791's content was non-loosening), but the verdict came
# from a law that had not been enacted.


def _tree_payload(overrides: dict[str, str] | None = None, *, truncated=False) -> str:
    tree = []
    for path in sorted(gate.self_governing_files()):
        sha = gate._blob_sha((gate.ROOT / path).read_bytes())
        tree.append({"path": path, "sha": (overrides or {}).get(path, sha)})
    return json.dumps({"truncated": truncated, "tree": tree})


def test_blob_sha_is_the_one_git_computes():
    """The comparison is worthless if the two sides hash differently."""
    import subprocess

    for path in ("AGENTS.md", "tools/pr_merge_gate.py"):
        want = subprocess.run(
            ["git", "hash-object", path],
            cwd=gate.ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert gate._blob_sha((gate.ROOT / path).read_bytes()) == want


def test_a_matching_tree_is_no_drift():
    """The case that must stay quiet, or the gate blocks every merge forever."""
    assert (
        gate._working_tree_rule_drift("o/r", "main", gh=lambda a: _tree_payload()) == ()
    )


def test_a_differing_rule_file_is_drift():
    payload = _tree_payload({"AGENTS.md": "0" * 40})
    assert gate._working_tree_rule_drift("o/r", "main", gh=lambda a: payload) == (
        "AGENTS.md",
    )


def test_a_rule_file_missing_from_the_base_tree_is_drift():
    """A path the base does not have compares equal to nothing, so it must be named."""
    full = json.loads(_tree_payload())
    full["tree"] = [e for e in full["tree"] if e["path"] != "tools/pr_merge_gate.py"]
    payload = json.dumps(full)
    assert "tools/pr_merge_gate.py" in gate._working_tree_rule_drift(
        "o/r", "main", gh=lambda a: payload
    )


@pytest.mark.parametrize(
    "gh",
    [
        pytest.param(lambda a: _tree_payload(truncated=True), id="truncated"),
        pytest.param(lambda a: json.dumps({"tree": []}), id="empty"),
        pytest.param(lambda a: "not json", id="unparseable"),
        pytest.param(
            lambda a: (_ for _ in ()).throw(RuntimeError("404")), id="unreachable"
        ),
    ],
)
def test_a_base_tree_that_cannot_be_read_counts_as_drift(gh):
    """'I could not tell whether I am judging by the merged rules' must stop a
    merge, not wave it through."""
    drift = gate._working_tree_rule_drift("o/r", "main", gh=gh)
    assert drift and "could not be read" in drift[0]


def test_no_repo_or_base_counts_as_drift():
    assert gate._working_tree_rule_drift("", "main", gh=lambda a: _tree_payload())
    assert gate._working_tree_rule_drift("o/r", "", gh=lambda a: _tree_payload())


def test_rule_drift_stops_a_merge_that_is_otherwise_ready():
    """It is not about what the PR changes: an unrelated PR is judged by the same
    working-tree rules, so it is stopped by the same fact."""
    verdict = gate.evaluate(_facts(rule_drift=("tools/pr_merge_gate.py",)), now=NOW)
    assert verdict.owner_required and verdict.exit_code == 2
    assert "not merged" in verdict.reasons[0]
    assert "tools/pr_merge_gate.py" in verdict.reasons[0]


def test_rule_drift_is_not_excused_by_a_direction_proof():
    """The proof says the PR's own change is safe; drift says the judge is not the
    merged one. Different facts, and the second one is not answered by the first."""
    verdict = gate.evaluate(
        _facts(
            files=("docs/ssot/ci-gate-inventory.yaml",),
            proven_tighter=("docs/ssot/ci-gate-inventory.yaml",),
            rule_drift=("tools/pr_merge_gate.py",),
        ),
        now=NOW,
    )
    assert verdict.owner_required and verdict.exit_code == 2


# -- workflow 文件也决定这个门禁怎么判（审计 2026-09-22 HIGH）----------------------

WORKFLOW_BASE = """on:
  push:
    paths: ["libs/alerting.py", ".github/workflows/apply-observability.yml"]
jobs:
  apply:
    name: Apply observability
"""


def test_workflow_files_are_self_governing():
    """闭包走不到它们 —— `WORKFLOW_DIR = ROOT / ".github" / "workflows"` 是路径拼接，
    `_repo_deps` 的字面量正则看不见 —— 但门禁每次判定都从工作树重读它们。

    实测到的绕过：一个 PR 同时改 `libs/alerting.py` 和 `apply-observability.yml`，
    把前者从后者的 `on.push.paths` 删掉，于是「合流会触发 observability apply」
    这条升级不再成立，判定 ready=True / owner_required=False / exit=0 —— 而那正是
    AGENTS.md 明文必须回 owner 的一类。
    """
    closure = gate.self_governing_files()
    workflows = {f for f in closure if f.startswith(".github/workflows/")}
    assert workflows, f"没有任何 workflow 在闭包里：{sorted(closure)}"
    assert ".github/workflows/apply-observability.yml" in workflows


def test_dropping_a_deploy_path_is_not_proven_tighter():
    """那个绕过本身。删掉一条 path = 少一类改动要回 owner = 放松。"""
    head = WORKFLOW_BASE.replace('"libs/alerting.py", ', "")
    assert not gate._workflow_only_gained_authority(WORKFLOW_BASE, head)


def test_adding_a_deploy_path_is_proven_tighter():
    """反向：多一条 path = 多一类改动要回 owner = 收紧，不该惊动 owner。"""
    head = WORKFLOW_BASE.replace("paths: [", 'paths: ["libs/new.py", ')
    assert gate._workflow_only_gained_authority(WORKFLOW_BASE, head)
    assert gate._workflow_only_gained_authority(WORKFLOW_BASE, WORKFLOW_BASE)


def test_renaming_a_job_is_not_proven_tighter():
    """必需检查的显示名由 job 的 `name:` 解析而来。改名会让一条必需检查
    「从没报告过」，而那条路径拦不住。"""
    head = WORKFLOW_BASE.replace("name: Apply observability", "name: Renamed")
    assert not gate._workflow_only_gained_authority(WORKFLOW_BASE, head)


@pytest.mark.parametrize("bad", ["on: [", "", "- a\n- b\n", "just text\n"])
def test_an_unreadable_workflow_proves_nothing(bad):
    assert not gate._workflow_only_gained_authority(WORKFLOW_BASE, bad)
    assert not gate._workflow_only_gained_authority(bad, WORKFLOW_BASE)


def test_only_workflows_get_the_workflow_proof():
    """闭包里的 Python 和规则散文仍然一律回 owner。"""
    assert gate._direction_proof_for(".github/workflows/deploy.yml") is not None
    assert gate._direction_proof_for("docs/ssot/ci-gate-inventory.yaml") is not None
    for path in ("tools/pr_merge_gate.py", "AGENTS.md", "libs/console.py"):
        assert gate._direction_proof_for(path) is None, path


# -- workflow 方向证明的三个误判形态（#809 review）---------------------------------

WF_NO_PATHS = "on:\n  push:\n    branches: [main]\njobs:\n  a:\n    name: A\n"
WF_WITH_PATHS = 'on:\n  push:\n    paths: ["x"]\njobs:\n  a:\n    name: A\n'


def test_a_missing_paths_key_means_all_paths_not_none():
    """Actions 把「没有 paths」当成「所有路径」。当成空集的话，给一个本来无 paths
    的 workflow 加上过滤 —— 一次**收窄**、一次放松 —— 会被证明成收紧。

    这正是 `_inventory_only_gained_authority` 里防过的空集陷阱换了个形状：那边是
    「base 为空则任何 head 都是超集」，这边是「base 缺 key 被读成空」。
    """
    assert not gate._workflow_only_gained_authority(WF_NO_PATHS, WF_WITH_PATHS)
    # 反向：去掉过滤 = 触发面变大 = 更多改动要回 owner = 收紧
    assert gate._workflow_only_gained_authority(WF_WITH_PATHS, WF_NO_PATHS)


def test_a_scalar_paths_value_is_not_a_set_of_characters():
    """`paths: "x"` 用 `frozenset(str(v) for v in raw)` 会拆成字符集合，
    于是比较的是字母而不是路径。"""
    scalar = 'on:\n  push:\n    paths: "x"\njobs:\n  a:\n    name: A\n'
    assert not gate._workflow_only_gained_authority(scalar, WF_WITH_PATHS)
    assert not gate._workflow_only_gained_authority(WF_WITH_PATHS, scalar)


def test_a_job_without_a_name_reports_under_its_job_id():
    """`_required_checks()` 取 `name or job_id`，这里必须同一个取法。只收有 `name:`
    的 job 会让「给无名 job 加 name」看起来是超集，实际是把那条必需检查改了名，
    旧名字从此不再报告 —— 而「必需检查从没报告过」这条路径是拦不住的。"""
    unnamed = 'on:\n  push:\n    paths: ["x"]\njobs:\n  a: {}\n'
    assert not gate._workflow_only_gained_authority(unnamed, WF_WITH_PATHS)
    # 同一份不动必须仍然成立，否则这条断言是靠「什么都证明不了」通过的
    assert gate._workflow_only_gained_authority(unnamed, unnamed)


@pytest.mark.parametrize(
    "bad", ["on:\n  push:\n    paths: ['x']\n", "jobs: notamapping\n", "on: [\n"]
)
def test_a_workflow_without_a_jobs_mapping_proves_nothing(bad):
    assert not gate._workflow_only_gained_authority(WF_WITH_PATHS, bad)
    assert not gate._workflow_only_gained_authority(bad, WF_WITH_PATHS)
