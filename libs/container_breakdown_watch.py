"""Backward-compatibility shim — the implementation lives in
`libs.observability.watchers.breakdown_watch`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.watchers.breakdown_watch import (
    DEFAULT_CHRONIC_DIGEST_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_INTERVAL,
    DEFAULT_LOG_TAIL,
    DEFAULT_RECOVERY_THRESHOLD,
    DEFAULT_RENOTIFY,
    DEFAULT_STAY_RESOLVED_SECONDS,
    BreakdownWatch,
    logger,
    run_once,
    sweep,
)

__all__ = [
    "BreakdownWatch",
    "DEFAULT_CHRONIC_DIGEST_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_INTERVAL",
    "DEFAULT_LOG_TAIL",
    "DEFAULT_RECOVERY_THRESHOLD",
    "DEFAULT_RENOTIFY",
    "DEFAULT_STAY_RESOLVED_SECONDS",
    "logger",
    "run_once",
    "sweep",
]
