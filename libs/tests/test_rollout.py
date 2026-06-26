"""Unit tests for libs/deploy/rollout.py — the unified deployment poller (D5 draft).

Each test pins one of the three existing pollers' contracts onto the unified
``wait_for_deployment`` flags, proving the single loop can reproduce all of them.
"""

import pytest

from libs.deploy.rollout import RolloutError, wait_for_deployment


class _Deployments:
    """Returns the next queued deployment-list per call; the last entry repeats."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.calls = 0

    def __call__(self):
        i = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return self._frames[i]


def _no_sleep(_):  # deterministic, no real waiting
    return None


def _clock(ticks):
    """A monotonic() stub that yields successive values then holds the last."""
    seq = list(ticks)

    def now():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return now


# --- deployer contract: running is success, no-raise on timeout ---------------


def test_running_is_success_when_not_requiring_terminal():
    deployments = _Deployments([[{"id": "d2", "status": "running"}]])
    result = wait_for_deployment(
        deployments,
        before_ids={"d1"},
        timeout_seconds=30,
        interval_seconds=1,
        require_terminal=False,
        _sleep=_no_sleep,
    )
    assert result.status == "running"
    assert result.ok is True
    assert result.new_ids == ("d2",)


# --- promote contract: poll PAST running to terminal-good ---------------------


def test_require_terminal_polls_past_running_to_done():
    deployments = _Deployments(
        [
            [{"id": "d2", "status": "running"}],
            [{"id": "d2", "status": "done"}],
        ]
    )
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=30,
        interval_seconds=1,
        require_terminal=True,
        _sleep=_no_sleep,
    )
    assert result.status == "done"
    assert result.ok is True
    assert deployments.calls >= 2  # it kept polling past `running`


# --- error handling: raise (deployer/promote) vs classify (canary) ------------


def test_error_status_raises_when_raise_on_error():
    deployments = _Deployments([[{"id": "d2", "status": "error"}]])
    with pytest.raises(RolloutError):
        wait_for_deployment(
            deployments,
            before_ids=set(),
            timeout_seconds=30,
            interval_seconds=1,
            raise_on_error=True,
            _sleep=_no_sleep,
        )


def test_error_status_classified_when_not_raising():
    deployments = _Deployments([[{"id": "d2", "status": "error"}]])
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=30,
        interval_seconds=1,
        raise_on_error=False,
        _sleep=_no_sleep,
    )
    assert result.status == "error"
    assert result.ok is False


# --- timeout: raise (promote) vs classify (deployer/canary) -------------------


def test_timeout_raises_when_raise_on_timeout():
    deployments = _Deployments([[]])  # no new record ever appears
    with pytest.raises(TimeoutError):
        wait_for_deployment(
            deployments,
            before_ids=set(),
            timeout_seconds=0,
            interval_seconds=1,
            raise_on_timeout=True,
            _sleep=_no_sleep,
            _now=_clock([0.0, 1.0]),
        )


def test_timeout_classified_when_not_raising():
    deployments = _Deployments([[]])
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=0,
        interval_seconds=1,
        raise_on_timeout=False,
        _sleep=_no_sleep,
        _now=_clock([0.0, 1.0]),
    )
    assert result.status == "timeout"
    assert result.ok is False


# --- only NEW records count ---------------------------------------------------


def test_pre_existing_record_is_ignored():
    # d1 is already done before the deploy, but it's in before_ids -> not success
    deployments = _Deployments([[{"id": "d1", "status": "done"}]])
    result = wait_for_deployment(
        deployments,
        before_ids={"d1"},
        timeout_seconds=0,
        interval_seconds=1,
        raise_on_timeout=False,
        _sleep=_no_sleep,
        _now=_clock([0.0, 1.0]),
    )
    assert result.status == "timeout"  # no NEW record, so it times out
