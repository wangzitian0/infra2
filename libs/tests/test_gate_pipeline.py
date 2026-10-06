"""Unit tests for the modular Step Pipeline in libs/gate/evaluator.py."""

from __future__ import annotations

from libs.gate.evaluator import (
    _check_checks_green_and_stale,
    _check_pr_state_and_conflicts,
    _check_reviews,
    _check_settling_window,
    evaluate,
)
from libs.gate.types import (
    ACT,
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
    assert not owner  # retarget the PR; not a production deployment (#1040)
    assert any("not main" in r for r in reasons)
    assert ACT in reasons.kinds


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
    # In wangzitian0/infra2, libs/gate/evaluator.py decides whether a merge reaches
    # production. Without a tightening proof it needs the owner (the reservation
    # guards itself, owner principle 2026-10-06).
    assert not verdict.ready
    assert verdict.owner_required
    assert any("detects production deployments" in r for r in verdict.reasons)
