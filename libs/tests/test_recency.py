"""Tests for the #531 recency-window primitive: `libs/recency.py`.

Pure function, no Docker/mocking needed -- every case is expressed directly
in count/timestamp/now terms.
"""

from __future__ import annotations

from libs.recency import is_recently_flapping

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
