"""Peer liveness for truealpha's ``scheduler-liveness`` workflow (truealpha#876).

Canonical implementation lives in ``infra2_sdk.observability`` (#1270).
"""

from __future__ import annotations

from collections.abc import Callable

from infra2_sdk.ci import ScheduleError, crons_in, largest_gap
from infra2_sdk.observability import (
    DELAY_ALLOWANCE,
    MISSED_TICKS_TOLERATED,
    PeerReadError,
    PeerVerdict,
    bound_for,
    evaluate_peer_liveness,
    github_getter,
    newest_tick,
    parse_bound_cap_hours,
)

PEER_REPOSITORY = "wangzitian0/truealpha"
PEER_WORKFLOW = "scheduler-liveness.yml"
BOUND_CAP_ENV = "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS"
SCHEDULED_PAGE = 5
UNFILTERED_PAGE = 50
API_TIMEOUT_SECONDS = 20.0

OK = "OK"
STALE = "STALE"
DISABLED = "DISABLED"
NEVER = "NEVER"
INVALID = "INVALID"
UNVERIFIABLE = "UNVERIFIABLE"

Get = Callable[[str], object]

__all__ = [
    "API_TIMEOUT_SECONDS",
    "BOUND_CAP_ENV",
    "DELAY_ALLOWANCE",
    "DISABLED",
    "Get",
    "INVALID",
    "MISSED_TICKS_TOLERATED",
    "NEVER",
    "OK",
    "PEER_REPOSITORY",
    "PEER_WORKFLOW",
    "PeerReadError",
    "PeerVerdict",
    "SCHEDULED_PAGE",
    "STALE",
    "ScheduleError",
    "UNFILTERED_PAGE",
    "UNVERIFIABLE",
    "bound_for",
    "crons_in",
    "evaluate_peer_liveness",
    "github_getter",
    "largest_gap",
    "newest_tick",
    "parse_bound_cap_hours",
]
