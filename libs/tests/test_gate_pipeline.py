"""Unit tests for the modular Step Pipeline in libs/gate/evaluator.py."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from libs.gate.client import collect
from libs.gate.evaluator import (
    _check_checks_green_and_stale,
    _check_pr_state_and_conflicts,
    _check_reviews,
    _check_settling_window,
    evaluate,
)
from libs.gate.inventory import _required_checks
from libs.gate.review import request_copilot_review
from libs.gate.types import (
    ACT,
    COPILOT_BOT_ID,
    Reasons,
    HeadFacts,
)


def _base_facts(**kwargs) -> HeadFacts:
    defaults = dict(
        number=100,
        state="OPEN",
        draft=False,
        base="main",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        files=("libs/gate/evaluator.py",),
        last_push_at=1000.0,
        checks=(("CI", "pass"),),
        unresolved_threads=0,
        unresolved_weight=0.0,
        review_threads_total=0,
        repo="wangzitian0/infra2",
        node_id="PR_100",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        base_changed_files=(),
        proven_tighter=(),
        rule_drift=(),
        changed_files=1,
        review_decision="APPROVED",
        absent_fields=(),
        reviews=(
            (
                "copilot-pull-request-reviewer",
                "0123456789abcdef0123456789abcdef01234567",
                1050.0,
            ),
        ),
        body="## Summary\nClean change",
    )
    defaults.update(kwargs)
    return HeadFacts(**defaults)


def test_step_pr_state_and_conflicts_valid():
    reasons = Reasons()
    facts = _base_facts()
    owner = _check_pr_state_and_conflicts(facts, reasons)
    assert not owner
    assert not reasons


def test_step_pr_state_and_conflicts_not_open():
    reasons = Reasons()
    facts = _base_facts(state="CLOSED")
    owner = _check_pr_state_and_conflicts(facts, reasons)
    assert not owner
    assert any("not OPEN" in r for r in reasons)
    assert ACT in reasons.kinds  # a closed PR needs someone to act (#740)


def test_step_pr_state_and_conflicts_wrong_base():
    reasons = Reasons()
    facts = _base_facts(base="feature")
    owner = _check_pr_state_and_conflicts(facts, reasons)
    assert owner
    assert any("not main" in r for r in reasons)


def test_step_pr_state_and_conflicts_conflicting():
    reasons = Reasons()
    facts = _base_facts(mergeable="CONFLICTING")
    owner = _check_pr_state_and_conflicts(facts, reasons)
    assert not owner
    assert any("CONFLICTING" in r for r in reasons)


def test_step_checks_green_and_stale():
    reasons = Reasons()
    facts = _base_facts(checks=(("CI", "fail"),))
    _check_checks_green_and_stale(facts, is_open=True, reasons=reasons)
    assert any("check(s) not green" in r for r in reasons)


def test_step_reviews():
    reasons = Reasons()
    facts = _base_facts(unresolved_weight=1.5, unresolved_threads=2)
    _check_reviews(facts, reasons)
    assert any("unresolved review thread(s)" in r for r in reasons)


def test_step_settling_window_clock_pending():
    reasons = Reasons()
    facts = _base_facts(last_push_at=1000.0)
    # now = 1000 + 5 min (quiet_minutes = 12, so remaining = 7 min = 420s)
    remaining = _check_settling_window(
        facts,
        now=1300.0,
        quiet_minutes=12,
        policy="clock",
        settle_minutes=3,
        reasons=reasons,
    )
    assert remaining == 420
    assert any("quiet period has 420s to run" in r for r in reasons)


def test_step_settling_window_clock_passed():
    reasons = Reasons()
    facts = _base_facts(last_push_at=1000.0)
    # now = 1000 + 15 min (> 12 min)
    remaining = _check_settling_window(
        facts,
        now=1900.0,
        quiet_minutes=12,
        policy="clock",
        settle_minutes=3,
        reasons=reasons,
    )
    assert remaining == 0
    assert not reasons


def test_evaluate_step_pipeline_end_to_end():
    facts = _base_facts(last_push_at=1000.0)
    verdict = evaluate(facts, now=2000.0, quiet_minutes=12, policy="clock")
    # In wangzitian0/infra2, libs/gate/evaluator.py is in self_governing closure,
    # so without owner proof it requires owner action.
    assert not verdict.ready
    assert verdict.owner_required
    assert any("changes what decides merges" in r for r in verdict.reasons)


# --- #1075: an app or bot account's PR gets no automated review -------------

NOW = 1_800_000_000.0
HEAD = "0123456789abcdef0123456789abcdef01234567"
BOT = ("Bot", "infra-flash")
USER = ("User", "wangzitian0")
COPILOT = "copilot-pull-request-reviewer"


def _green_facts(**kwargs) -> HeadFacts:
    """An infra2 PR that passes every condition except, maybe, the settle time:
    each required check green, no protected or deploy path, no review yet."""
    required = sorted(_required_checks()[0])
    assert required, "the inventory read failed: 'all green' would prove nothing"
    defaults = dict(
        head_sha=HEAD,
        files=("libs/probe_specs.py",),
        checks=tuple((name, "pass") for name in required),
        reviews=(),
        author=BOT,
        last_push_at=NOW - 4 * 60,
    )
    defaults.update(kwargs)
    return _base_facts(**defaults)


@pytest.mark.parametrize(
    "author",
    (BOT, ("", "app/infra-flash"), ("", "dependabot[bot]")),
    ids=("graphql-bot", "gh-app-login", "rest-bot-login"),
)
def test_a_bot_opened_pr_with_no_review_settles_three_minutes_after_the_push(author):
    verdict = evaluate(_green_facts(author=author), now=NOW, policy="either")
    assert verdict.ready and verdict.exit_code == 0 and verdict.reasons == []


def test_a_bot_opened_pr_waits_for_the_rest_of_the_three_minutes():
    verdict = evaluate(
        _green_facts(last_push_at=NOW - 2 * 60), now=NOW, policy="either"
    )
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 60
    assert "infra-flash is an app or bot account" in verdict.reasons[0]
    assert "quiet period has 60s to run" in verdict.reasons[0]


@pytest.mark.parametrize("author", (USER, ("", "")), ids=("user", "unread"))
def test_a_user_opened_pr_with_no_review_keeps_the_twelve_minute_clock(author):
    # An author the gate could not read counts as a user: the longer wait.
    verdict = evaluate(_green_facts(author=author), now=NOW, policy="either")
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 8 * 60
    assert "--request-review asks Copilot" in verdict.reasons[0]


def test_a_bot_opened_pr_with_a_review_of_the_head_keeps_the_event_rule():
    # Pushed 4 min ago: the 3-minute bot clock would let this go now. The review
    # of the head is 60 s old, so the event rule still holds it for 120 s.
    reviewed = _green_facts(reviews=((COPILOT, HEAD, NOW - 60),))
    settling = evaluate(reviewed, now=NOW, policy="either")
    assert settling.exit_code == 1 and settling.quiet_remaining_seconds == 120
    assert "head reviewed 60s ago" in settling.reasons[0]
    assert evaluate(reviewed, now=NOW + 120, policy="either").ready
    # A review of an older head is no review of this head: the bot clock applies.
    stale = _green_facts(reviews=((COPILOT, "0" * 40, NOW - 60),))
    assert evaluate(stale, now=NOW, policy="either").ready


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        (lambda f: replace(f, checks=f.checks + (("Extra", "fail"),)), "not green"),
        (lambda f: replace(f, unresolved_threads=1, unresolved_weight=1.0), "weigh 1"),
    ),
    ids=("red-check", "open-thread"),
)
def test_a_settled_bot_opened_pr_still_needs_every_other_condition(change, expected):
    verdict = evaluate(change(_green_facts()), now=NOW, policy="either")
    assert not verdict.ready and verdict.exit_code == 3
    assert any(expected in r for r in verdict.reasons), verdict.reasons
    # Settled: the settle time is not what holds it.
    assert not any("quiet period" in r for r in verdict.reasons), verdict.reasons


def test_only_the_either_policy_shortens_the_clock_for_a_bot():
    facts = _green_facts()
    clock = evaluate(facts, now=NOW, policy="clock")
    assert clock.exit_code == 1 and clock.quiet_remaining_seconds == 8 * 60
    assert evaluate(facts, now=NOW, policy="event").exit_code == 1
    # The bot clock is never longer than the user clock.
    short = _green_facts(last_push_at=NOW - 90)
    assert evaluate(short, now=NOW, policy="either", quiet_minutes=1).ready


_COPILOT_REQUEST = {"__typename": "Bot", "id": COPILOT_BOT_ID, "login": COPILOT}


class _ReviewGh:
    """gh for one PR: records calls; `requested` is what a re-read of
    reviewRequests returns after the mutation."""

    def __init__(self, *, requested=(), author=BOT, reviews=(), reread_fails=False):
        self.calls: list[list[str]] = []
        self.requested = list(requested)
        self.author = author
        self.reviews = list(reviews)
        self.reread_fails = reread_fails

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
                    "headRefOid": HEAD,
                    "reviews": self.reviews,
                    "files": [{"path": "src/app.py"}],
                    "changedFiles": 1,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "body": "",
                    "commits": [{"oid": HEAD, "committedDate": "2026-09-15T06:55:22Z"}],
                }
            )
        if argv[:2] == ["pr", "checks"]:
            return json.dumps([{"name": "CI", "state": "SUCCESS", "bucket": "pass"}])
        if argv[:1] == ["api"] and "/compare/" in argv[1]:
            return json.dumps({"files": []})
        if argv[:2] == ["api", "graphql"] and "requestReviews" in argv[3]:
            return json.dumps(
                {"data": {"requestReviews": {"pullRequest": {"number": 704}}}}
            )
        if argv[:2] == ["api", "graphql"] and "reviewRequests" in argv[3]:
            if self.reread_fails:
                raise RuntimeError("gh api graphql: timed out")
            nodes = [{"requestedReviewer": r} for r in self.requested]
            return json.dumps({"data": {"node": {"reviewRequests": {"nodes": nodes}}}})
        if argv[:2] == ["api", "graphql"] and "reviewThreads" in argv[3]:
            author = (
                {"__typename": self.author[0], "login": self.author[1]}
                if self.author
                else None
            )
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "author": author,
                                "reviewThreads": {"totalCount": 0, "nodes": []},
                            }
                        }
                    }
                }
            )
        raise AssertionError(argv)

    def index(self, word: str) -> int:
        return next(i for i, c in enumerate(self.calls) if word in c[-1])


def test_the_review_request_reports_true_only_when_copilot_is_pending():
    empty = _ReviewGh()
    assert request_copilot_review(_base_facts(), gh=empty) is False
    # The re-read comes after the mutation, so it sees what the mutation did.
    assert empty.index("requestReviews") < empty.index("reviewRequests")
    pending = _ReviewGh(requested=[_COPILOT_REQUEST])
    assert request_copilot_review(_base_facts(), gh=pending) is True
    human = _ReviewGh(requested=[{"__typename": "User"}])
    assert request_copilot_review(_base_facts(), gh=human) is False
    # A re-read that fails does not claim a registered request.
    broken = _ReviewGh(requested=[_COPILOT_REQUEST], reread_fails=True)
    assert request_copilot_review(_base_facts(), gh=broken) is False


def _main_output(gh, capsys, *, at_minutes: float) -> tuple[int, str]:
    from tools import pr_merge_gate

    pushed = 1789455322.0  # 2026-09-15T06:55:22Z, the head commit's date above
    rc = pr_merge_gate.main(
        ["704", "--repo", "wangzitian0/truealpha", "--policy", "either"]
        + ["--request-review"],
        gh=gh,
        now=lambda: pushed + at_minutes * 60,
    )
    # Rich wraps at 80 columns when stdout is not a terminal.
    return rc, " ".join(capsys.readouterr().out.split())


def test_the_gate_warns_when_the_request_did_not_register(capsys):
    rc, out = _main_output(_ReviewGh(author=BOT), capsys, at_minutes=4)
    assert "the Copilot review request was not registered" in out
    assert "no automated review will come" in out
    assert "Copilot review requested" not in out
    # End to end through collect: the author came from GraphQL, so 4 min is enough.
    assert rc == 0, out


def test_the_gate_says_requested_only_when_the_request_registered(capsys):
    gh = _ReviewGh(author=USER, requested=[_COPILOT_REQUEST])
    rc, out = _main_output(gh, capsys, at_minutes=4)
    assert f"Copilot review requested on {HEAD[:7]}" in out
    assert "not registered" not in out
    assert rc == 1, out  # a user's PR still waits for the 12-minute clock


def test_collect_reads_the_author_and_an_unknown_author_stays_empty():
    facts = collect(704, repo="wangzitian0/truealpha", gh=_ReviewGh(author=BOT))
    assert facts.author == ("Bot", "infra-flash")
    deleted = collect(704, repo="wangzitian0/truealpha", gh=_ReviewGh(author=None))
    assert deleted.author == ("", "")
