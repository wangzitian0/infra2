"""Frozen shim: the implementation lives in ``libs.observability.watchers.deploy_queue_guard`` (#955).

Import from ``libs.observability.watchers.deploy_queue_guard`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.observability.watchers.deploy_queue_guard import (
    DEFAULT_CEILING,
    DEFAULT_GRACE,
    DEFAULT_INTERVAL,
    DEFAULT_RENOTIFY,
    DeployQueueGuard,
    UNREGISTERED_BY_DESIGN,
    _env_int,
    _list_composes,
    _load_env_file,
    _post_alert,
    _remediate,
    logger,
    run_once,
)

__all__ = [
    "DEFAULT_CEILING",
    "DEFAULT_GRACE",
    "DEFAULT_INTERVAL",
    "DEFAULT_RENOTIFY",
    "DeployQueueGuard",
    "UNREGISTERED_BY_DESIGN",
    "_env_int",
    "_list_composes",
    "_load_env_file",
    "_post_alert",
    "_remediate",
    "logger",
    "run_once",
]
