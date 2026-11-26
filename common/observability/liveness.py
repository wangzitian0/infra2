"""Scheduler liveness bounds and flapping hysteresis (absorbed from infra2-sdk v4.9.1)."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.request import urlopen

from common.core._sdk_wire import parse_iso_utc
from common.core.http_transport import _github_headers, _github_json_fetcher

_GITHUB_URL_HEADER = "github"
_API_TIMEOUT_SECONDS = 20.0


def is_recently_flapping(
    *,
    event_count: int,
    last_event_at: float,
    now: float,
    count_threshold: int,
    recency_window_seconds: int,
) -> bool:
    return (
        event_count > count_threshold
        and max(0.0, now - last_event_at) <= recency_window_seconds
    )


@dataclass
class ConsecutiveObservationState:
    active: bool = False
    bad_streak: int = 0
    good_streak: int = 0
    last_alert_at: float = 0.0
    context: object = field(default=None)


def evaluate_consecutive_hysteresis(
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
            state.active, state.last_alert_at = True, now
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
    state.active, state.good_streak, state.last_alert_at = False, 0, 0.0
    return "resolve"


MISSED_TICKS_TOLERATED = 2
DELAY_ALLOWANCE = timedelta(hours=1)
_STARTED = ("in_progress", "completed")
_STARTUP_FAILURE = "startup_failure"
_API_TIMEOUT_SECONDS = 20.0


class PeerReadError(RuntimeError):
    pass


def github_getter(
    token: str = "", *, timeout: float = _API_TIMEOUT_SECONDS, opener=urlopen
) -> Callable[[str], object]:
    return _github_json_fetcher(
        headers=_github_headers(
            token=token, user_agent="infra2-scheduler-peer-liveness/1.0"
        ),
        timeout=timeout,
        error_cls=PeerReadError,
        opener=opener,
    )


def bound_for(gap: timedelta) -> timedelta:
    return MISSED_TICKS_TOLERATED * gap + DELAY_ALLOWANCE


def parse_bound_cap_hours(raw: str | None) -> timedelta | None:
    text = (raw or "").strip()
    if not text:
        return None
    hours = float(text)
    if not math.isfinite(hours) or hours < 0:
        raise ValueError(f"bound cap {text!r} must be a finite number of hours >= 0")
    return timedelta(hours=hours)


def _timestamp(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise PeerReadError(f"{what} has no timestamp ({value!r})")
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        msg = "without a zone" if "time zone" in str(exc) else "an unreadable"
        if msg == "without a zone":
            raise PeerReadError(
                f"{what} has a timestamp without a zone ({value!r})"
            ) from exc
        raise PeerReadError(f"{what} has an unreadable timestamp {value!r}") from exc


def newest_tick(runs: Iterable[dict]) -> datetime | None:
    ticks = [
        _timestamp(r.get("created_at"), f"run {r.get('id')}")
        for r in runs
        if r.get("event") == "schedule"
        and r.get("status") in _STARTED
        and r.get("conclusion") != _STARTUP_FAILURE
    ]
    return max(ticks) if ticks else None


__all__ = [
    "ConsecutiveObservationState",
    "DELAY_ALLOWANCE",
    "MISSED_TICKS_TOLERATED",
    "PeerReadError",
    "bound_for",
    "evaluate_consecutive_hysteresis",
    "github_getter",
    "is_recently_flapping",
    "newest_tick",
    "parse_bound_cap_hours",
]
