"""Time-windowed health-signal primitive: is a bad signal recent, or historical?

Canonical implementation lives in ``infra2_sdk.observability`` (#1270).
Includes a stdlib fallback for bare-interpreter CI jobs (``ops-checks.yml``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from infra2_sdk.observability import (
        ConsecutiveObservationState,
        evaluate_consecutive_hysteresis,
        is_recently_flapping,
    )
except ImportError:  # pragma: no cover - bare interpreter without infra2-sdk installed

    def is_recently_flapping(  # type: ignore[misc]
        *,
        event_count: int,
        last_event_at: float,
        now: float,
        count_threshold: int,
        recency_window_seconds: int,
    ) -> bool:
        if event_count <= count_threshold:
            return False
        age_seconds = max(0.0, now - last_event_at)
        return age_seconds <= recency_window_seconds

    @dataclass
    class ConsecutiveObservationState:  # type: ignore[no-redef]
        active: bool = False
        bad_streak: int = 0
        good_streak: int = 0
        last_alert_at: float = 0.0
        context: object = field(default=None)

    def evaluate_consecutive_hysteresis(  # type: ignore[misc]
        *,
        state: ConsecutiveObservationState,
        is_bad_now: bool,
        now: float,
        failure_threshold: int,
        recovery_threshold: int,
        renotify_seconds: int,
    ) -> str:
        if is_bad_now:
            state.good_streak = 0
            state.bad_streak += 1
            if not state.active:
                if state.bad_streak < max(1, failure_threshold):
                    return "none"
                state.active = True
                state.last_alert_at = now
                return "fire"
            if renotify_seconds > 0 and now - state.last_alert_at >= renotify_seconds:
                state.last_alert_at = now
                return "fire"
            return "none"

        state.bad_streak = 0
        if not state.active:
            return "none"
        state.good_streak += 1
        if state.good_streak < max(1, recovery_threshold):
            return "none"
        state.active = False
        state.good_streak = 0
        state.last_alert_at = 0.0
        return "resolve"


__all__ = [
    "ConsecutiveObservationState",
    "evaluate_consecutive_hysteresis",
    "is_recently_flapping",
]
