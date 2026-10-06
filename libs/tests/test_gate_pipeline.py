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
    SETTLE_SECONDS,
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
        settle_seconds=60,
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
        settle_seconds=60,
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


# --- #1075: settle 60 s after a review; a bot-opened PR needs a verification --

NOW = 1_800_000_000.0
HEAD = "0123456789abcdef0123456789abcdef01234567"
OLD_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
BOT = ("Bot", "infra-flash")
USER = ("User", "wangzitian0")
COPILOT = "copilot-pull-request-reviewer"


def _verification(sha: str = HEAD, tests: str = "- `test_gate_pipeline.py::test_x`"):
    return f"### Independent verification of head {sha[:7]}\n\n{tests}\n"


def _green_facts(**kwargs) -> HeadFacts:
    """An infra2 PR that passes every condition except, maybe, the settle time:
    each required check green, no protected or deploy path, no review."""
    required = sorted(_required_checks()[0])
    assert required, "the inventory read failed: 'all green' would prove nothing"
    defaults = dict(
        head_sha=HEAD,
        files=("libs/probe_specs.py",),
        checks=tuple((name, "pass") for name in required),
        reviews=(),
        author=USER,
        last_push_at=NOW - 4 * 60,
    )
    defaults.update(kwargs)
    return _base_facts(**defaults)


def test_the_settle_after_a_review_is_sixty_seconds():
    assert SETTLE_SECONDS == 60
    reviewed = _green_facts(reviews=((COPILOT, HEAD, NOW - 59),))
    waiting = evaluate(reviewed, now=NOW, policy="either")
    assert waiting.exit_code == 1 and waiting.quiet_remaining_seconds == 1
    assert "head reviewed 59s ago; settling for 1s more" in waiting.reasons[0]
    assert evaluate(reviewed, now=NOW + 2, policy="either").ready  # 61 s


def test_a_user_pr_with_no_review_keeps_the_twelve_minute_clock():
    verdict = evaluate(_green_facts(), now=NOW, policy="either")
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 8 * 60
    assert "--request-review asks Copilot" in verdict.reasons[0]


def test_a_bot_pr_settles_sixty_seconds_after_its_verification_comment():
    verified = _green_facts(author=BOT, comments=((_verification(), NOW - 59),))
    waiting = evaluate(verified, now=NOW, policy="either")
    assert waiting.exit_code == 1 and waiting.quiet_remaining_seconds == 1
    assert "head verified in a PR comment 59s ago" in waiting.reasons[0]
    assert evaluate(verified, now=NOW + 2, policy="either").ready  # 61 s


@pytest.mark.parametrize(
    "author",
    (BOT, ("", "app/infra-flash"), ("", "dependabot[bot]")),
    ids=("graphql-bot", "gh-app-login", "rest-bot-login"),
)
def test_every_bot_author_form_takes_the_verification_path(author):
    verified = _green_facts(author=author, comments=((_verification(), NOW - 61),))
    assert evaluate(verified, now=NOW, policy="either").ready


@pytest.mark.parametrize(
    "body",
    (
        _verification(sha=OLD_HEAD),  # names an older head
        _verification(tests="- I read the diff; it looks fine."),  # names no test
        f"Independent verification of head {HEAD[:7]}\n- `test_x`",  # not a heading
        f"### Independent verification of head {HEAD[:6]}\n- `test_x`",  # 6 chars
    ),
    ids=("old-head", "no-test", "no-heading", "short-sha"),
)
def test_a_comment_that_breaks_a_rule_is_no_verification(body):
    facts = _green_facts(author=BOT, comments=((body, NOW - 120),))
    verdict = evaluate(facts, now=NOW, policy="either")
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 8 * 60
    assert "no independent verification of head 0123456" in verdict.reasons[0]


def test_the_test_name_can_be_a_path_and_node_id():
    body = _verification(tests="- libs/tests/test_a.py::test_b passed")
    facts = _green_facts(author=BOT, comments=((body, NOW - 61),))
    assert evaluate(facts, now=NOW, policy="either").ready
    node = _verification(tests="- Suite::case passed")
    facts = _green_facts(author=BOT, comments=((node, NOW - 61),))
    assert evaluate(facts, now=NOW, policy="either").ready


def test_the_newest_matching_comment_decides():
    comments = ((_verification(), NOW - 600), (_verification(), NOW - 30))
    verdict = evaluate(
        _green_facts(author=BOT, comments=comments), now=NOW, policy="either"
    )
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 30


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        (lambda f: replace(f, checks=f.checks + (("Extra", "fail"),)), "not green"),
        (lambda f: replace(f, unresolved_threads=1, unresolved_weight=1.0), "weigh 1"),
    ),
    ids=("red-check", "open-thread"),
)
def test_a_verified_bot_pr_still_needs_every_other_condition(change, expected):
    verified = _green_facts(author=BOT, comments=((_verification(), NOW - 61),))
    verdict = evaluate(change(verified), now=NOW, policy="either")
    assert not verdict.ready and verdict.exit_code == 3
    assert any(expected in r for r in verdict.reasons), verdict.reasons
    # Settled: the settle time is not what holds it.
    assert not any("settling" in r or "quiet" in r for r in verdict.reasons)


def test_a_bot_pr_with_no_evidence_waits_the_twelve_minute_clock():
    bare = _green_facts(author=BOT)
    verdict = evaluate(bare, now=NOW, policy="either")
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 8 * 60
    assert "infra-flash is an app or bot account" in verdict.reasons[0]
    assert "### Independent verification of head 0123456" in verdict.reasons[0]
    assert evaluate(bare, now=NOW + 8 * 60, policy="either").ready  # 12 min


def test_a_bot_pr_with_a_copilot_review_needs_no_comment():
    reviewed = _green_facts(author=BOT, reviews=((COPILOT, HEAD, NOW - 59),))
    waiting = evaluate(reviewed, now=NOW, policy="either")
    assert waiting.exit_code == 1 and "head reviewed 59s ago" in waiting.reasons[0]
    assert evaluate(reviewed, now=NOW + 2, policy="either").ready


def test_a_comment_does_not_replace_the_review_of_a_user_pr():
    facts = _green_facts(author=USER, comments=((_verification(), NOW - 120),))
    verdict = evaluate(facts, now=NOW, policy="either")
    assert verdict.exit_code == 1 and verdict.quiet_remaining_seconds == 8 * 60


def test_the_clock_policy_ignores_the_comment():
    facts = _green_facts(author=BOT, comments=((_verification(), NOW - 120),))
    assert evaluate(facts, now=NOW, policy="clock").quiet_remaining_seconds == 480
    assert evaluate(facts, now=NOW, policy="event").ready


_COPILOT_REQUEST = {"__typename": "Bot", "id": COPILOT_BOT_ID, "login": COPILOT}
_UNSET = object()


class _Gh:
    """gh for one PR: records calls. `requested` is what a re-read of
    reviewRequests returns after the mutation; `comments` is the raw
    `comments` block of the facts query (`_UNSET` leaves the key out)."""

    def __init__(
        self,
        *,
        requested=(),
        author=BOT,
        comments=_UNSET,
        reread_fails=False,
    ):
        self.calls: list[list[str]] = []
        self.requested = list(requested)
        self.author = author
        self.comments = comments
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
                    "reviews": [],
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
            pull = {
                "author": (
                    {"__typename": self.author[0], "login": self.author[1]}
                    if self.author
                    else None
                ),
                "reviewThreads": {"totalCount": 0, "nodes": []},
            }
            if self.comments is not _UNSET:
                pull["comments"] = self.comments
            return json.dumps({"data": {"repository": {"pullRequest": pull}}})
        raise AssertionError(argv)

    def index(self, word: str) -> int:
        return next(i for i, c in enumerate(self.calls) if word in c[-1])


PUSHED = 1789455322.0  # 2026-09-15T06:55:22Z, the head commit's date in _Gh


def test_collect_reads_the_author_and_the_comments():
    block = {
        "nodes": [
            {"body": "a", "createdAt": "2026-09-15T07:00:00Z", "lastEditedAt": None},
            {
                "body": "b",
                "createdAt": "2026-09-15T07:00:00Z",
                "lastEditedAt": "2026-09-15T07:05:00Z",
            },
        ]
    }
    facts = collect(704, repo="wangzitian0/truealpha", gh=_Gh(comments=block))
    assert facts.author == ("Bot", "infra-flash")
    # An edit dates the text: an old comment edited to name a new head is new.
    assert facts.comments == (("a", PUSHED + 278), ("b", PUSHED + 578))


@pytest.mark.parametrize(
    "block",
    (
        _UNSET,
        None,
        "unreadable",
        {"nodes": None},
        {"nodes": ["x", {"body": None, "createdAt": "2026-09-15T07:00:00Z"}]},
        {"nodes": [{"body": "b", "createdAt": "not a time"}]},
        {"nodes": [{"body": "b"}]},
    ),
    ids=(
        "missing",
        "null",
        "string",
        "null-nodes",
        "bad-node",
        "bad-time",
        "no-time",
    ),
)
def test_a_missing_or_unreadable_comment_list_is_no_comment(block):
    facts = collect(704, repo="wangzitian0/truealpha", gh=_Gh(comments=block))
    assert facts.comments == ()


def test_collect_reads_a_deleted_author_as_empty():
    facts = collect(704, repo="wangzitian0/truealpha", gh=_Gh(author=None))
    assert facts.author == ("", "")


def test_the_review_request_reports_true_only_when_copilot_is_pending():
    empty = _Gh()
    assert request_copilot_review(_base_facts(), gh=empty) is False
    # The re-read comes after the mutation, so it sees what the mutation did.
    assert empty.index("requestReviews") < empty.index("reviewRequests")
    pending = _Gh(requested=[_COPILOT_REQUEST])
    assert request_copilot_review(_base_facts(), gh=pending) is True
    human = _Gh(requested=[{"__typename": "User"}])
    assert request_copilot_review(_base_facts(), gh=human) is False
    # A re-read that fails does not claim a registered request.
    broken = _Gh(requested=[_COPILOT_REQUEST], reread_fails=True)
    assert request_copilot_review(_base_facts(), gh=broken) is False


def _main(gh, capsys, *, at_seconds: float, extra=()) -> tuple[int, str]:
    from tools import pr_merge_gate

    rc = pr_merge_gate.main(
        ["704", "--repo", "wangzitian0/truealpha", "--policy", "either", *extra],
        gh=gh,
        now=lambda: PUSHED + at_seconds,
    )
    # Rich wraps at 80 columns when stdout is not a terminal.
    return rc, " ".join(capsys.readouterr().out.split())


def test_the_gate_warns_when_the_request_did_not_register(capsys):
    rc, out = _main(_Gh(), capsys, at_seconds=240, extra=["--request-review"])
    assert "the Copilot review request was not registered" in out
    assert "no automated review will come" in out
    assert "Copilot review requested" not in out
    assert rc == 1, out  # a bot PR with no evidence waits the 12-minute clock


def test_the_gate_says_requested_only_when_the_request_registered(capsys):
    gh = _Gh(author=USER, requested=[_COPILOT_REQUEST])
    rc, out = _main(gh, capsys, at_seconds=240, extra=["--request-review"])
    assert f"Copilot review requested on {HEAD[:7]}" in out
    assert "not registered" not in out
    assert rc == 1, out


def test_the_gate_reads_the_verification_comment_end_to_end(capsys):
    block = {
        "nodes": [
            {
                "body": _verification(),
                "createdAt": "2026-09-15T06:57:00Z",  # PUSHED + 98 s
                "lastEditedAt": None,
            }
        ]
    }
    rc, out = _main(_Gh(comments=block), capsys, at_seconds=98 + 59)
    assert rc == 1 and "settling for 1s more" in out, out
    rc, out = _main(_Gh(comments=block), capsys, at_seconds=98 + 61)
    assert rc == 0, out


def test_the_help_text_states_the_settle_in_seconds(capsys):
    from tools import pr_merge_gate

    with pytest.raises(SystemExit):
        pr_merge_gate.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "12 min after the push (clock), 60 s after an automated review" in text
    assert "3 min" not in text
