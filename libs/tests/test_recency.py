"""Tests for the #531 recency-window primitive: `libs/recency.py`.

Pure function, no Docker/mocking needed -- every case is expressed directly
in count/timestamp/now terms.
"""

from __future__ import annotations

from libs.recency import (
    ConsecutiveObservationState,
    evaluate_consecutive_hysteresis,
    is_recently_flapping,
)

NOW = 1_000_000.0  # fixed epoch for deterministic ages
THRESHOLD = 3
WINDOW = 3600  # 1h, matching classify_container's default


def test_recently_flapping_and_should_flag():
    """High count AND the most recent occurrence was moments ago -> flag."""
    assert is_recently_flapping(
        event_count=9,
        last_event_at=NOW - 30,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_historically_bad_but_now_stable_should_not_flag():
    """The #531 platform/prefect scenario: 1781 lifetime restarts, but the last
    one was 12+ days ago -- long-resolved history, not a live problem."""
    twelve_days_ago = NOW - (12 * 24 * 3600)
    assert not is_recently_flapping(
        event_count=1781,
        last_event_at=twelve_days_ago,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_never_had_an_event_should_not_flag():
    """Zero count never flags, regardless of what last_event_at holds."""
    assert not is_recently_flapping(
        event_count=0,
        last_event_at=0.0,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )
    # Even a nonsensical "recent" last_event_at can't make a zero count flag.
    assert not is_recently_flapping(
        event_count=0,
        last_event_at=NOW,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_count_exactly_at_threshold_does_not_flag():
    """Matches the pre-#531 `restart_count > max_restart_count` convention:
    the count must strictly EXCEED the threshold, even if very recent."""
    assert not is_recently_flapping(
        event_count=THRESHOLD,
        last_event_at=NOW,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_count_one_over_threshold_and_recent_flags():
    assert is_recently_flapping(
        event_count=THRESHOLD + 1,
        last_event_at=NOW,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_window_boundary_is_inclusive():
    """age == recency_window_seconds still counts as recent."""
    assert is_recently_flapping(
        event_count=THRESHOLD + 1,
        last_event_at=NOW - WINDOW,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_one_second_past_window_boundary_does_not_flag():
    assert not is_recently_flapping(
        event_count=THRESHOLD + 1,
        last_event_at=NOW - WINDOW - 1,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


def test_future_last_event_at_is_clamped_to_zero_age_not_negative():
    """Clock skew / a not-yet-elapsed timestamp: age clamps to 0 (maximally
    recent) rather than going negative -- a deliberate fail-safe so a clock
    anomaly can't silently suppress a real signal."""
    assert is_recently_flapping(
        event_count=THRESHOLD + 1,
        last_event_at=NOW + 500,
        now=NOW,
        count_threshold=THRESHOLD,
        recency_window_seconds=WINDOW,
    )


# --- evaluate_consecutive_hysteresis / ConsecutiveObservationState (#475) ---
# Pure state-machine tests, independent of tools/container_breakdown_watch.py's
# Docker plumbing -- every case expressed directly as a sequence of poll
# observations against a fresh ConsecutiveObservationState.

FAILURE_THRESHOLD = 3
RECOVERY_THRESHOLD = 5
RENOTIFY = 1800


def _poll(state, is_bad_now, now, **overrides):
    kwargs = dict(
        state=state,
        is_bad_now=is_bad_now,
        now=now,
        failure_threshold=FAILURE_THRESHOLD,
        recovery_threshold=RECOVERY_THRESHOLD,
        renotify_seconds=RENOTIFY,
    )
    kwargs.update(overrides)
    return evaluate_consecutive_hysteresis(**kwargs)


def test_single_bad_poll_does_not_fire_below_failure_threshold():
    state = ConsecutiveObservationState()
    assert _poll(state, True, NOW) == "none"
    assert _poll(state, True, NOW + 60) == "none"
    assert state.bad_streak == 2
    assert not state.active


def test_nth_consecutive_bad_poll_fires():
    state = ConsecutiveObservationState()
    for i in range(FAILURE_THRESHOLD - 1):
        assert _poll(state, True, NOW + i * 60) == "none"
    action = _poll(state, True, NOW + (FAILURE_THRESHOLD - 1) * 60)
    assert action == "fire"
    assert state.active
    assert state.last_alert_at == NOW + (FAILURE_THRESHOLD - 1) * 60


def test_good_poll_resets_bad_streak_before_threshold():
    """A single healthy poll before the incident became active must fully reset
    the streak -- the blip never happened, no partial credit."""
    state = ConsecutiveObservationState()
    _poll(state, True, NOW)
    _poll(state, True, NOW + 60)
    assert state.bad_streak == 2
    assert _poll(state, False, NOW + 120) == "none"
    assert state.bad_streak == 0
    assert not state.active


def test_active_incident_renotify_gated_not_reset_by_bad_streak():
    state = ConsecutiveObservationState()
    for i in range(FAILURE_THRESHOLD):
        action = _poll(state, True, NOW + i * 60)
    assert action == "fire"
    fire_time = state.last_alert_at
    # further bad polls within the renotify window: no new fire
    assert _poll(state, True, fire_time + 60) == "none"
    assert _poll(state, True, fire_time + 120) == "none"
    assert state.last_alert_at == fire_time
    # a bad poll AFTER the renotify window elapses re-notifies (same incident)
    action = _poll(state, True, fire_time + RENOTIFY)
    assert action == "fire"
    assert state.last_alert_at == fire_time + RENOTIFY
    assert state.active  # still the same incident throughout


def test_resolve_requires_consecutive_healthy_polls_at_recovery_threshold():
    state = ConsecutiveObservationState()
    for i in range(FAILURE_THRESHOLD):
        _poll(state, True, NOW + i * 60)
    assert state.active
    t = NOW + FAILURE_THRESHOLD * 60
    for i in range(RECOVERY_THRESHOLD - 1):
        assert _poll(state, False, t + i * 60) == "none"
        assert state.active  # still active -- not enough consecutive healthy polls yet
    action = _poll(state, False, t + (RECOVERY_THRESHOLD - 1) * 60)
    assert action == "resolve"
    assert not state.active
    assert state.good_streak == 0
    assert state.last_alert_at == 0.0


def test_relapse_before_recovery_threshold_is_the_same_incident():
    """The core #475 property: a bad poll seen mid-recovery (before recovery_threshold
    consecutive healthy polls) must NOT be treated as a new incident and must NOT reset
    the renotify clock -- this is exactly what produced the 333 firing+resolved storm."""
    state = ConsecutiveObservationState()
    for i in range(FAILURE_THRESHOLD):
        _poll(state, True, NOW + i * 60)
    fire_time = state.last_alert_at
    t = fire_time + 60
    _poll(state, False, t)  # 1 healthy poll (below recovery_threshold=5)
    _poll(state, False, t + 60)  # 2 healthy polls
    assert state.good_streak == 2
    assert state.active
    # relapse: broken again before reaching recovery_threshold
    action = _poll(state, True, t + 120)
    assert action == "none"  # NOT a new fire -- still well inside renotify window
    assert state.active  # still the same incident
    assert state.good_streak == 0  # recovery streak reset by the relapse
    assert state.last_alert_at == fire_time  # renotify clock untouched by the relapse


def test_context_field_is_opaque_passthrough():
    """The caller's payload (e.g. the latest Breakdown) survives untouched -- this
    primitive never inspects it."""
    state = ConsecutiveObservationState()
    state.context = {"container": "vault-agent"}
    _poll(state, True, NOW)
    assert state.context == {"container": "vault-agent"}
