"""Backward-compatibility shim — the implementation lives in `libs.deploy.iac_runner_client`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.deploy.iac_runner_client import (
    STATUS_NOT_FOUND_GRACE_SECONDS,
    STATUS_POLL_BACKOFF,
    STATUS_POLL_INITIAL_SECONDS,
    STATUS_POLL_MAX_SECONDS,
    poll_platform_deploy_status,
    status_poll_attempts,
    status_poll_delays,
    trigger_platform_deploy,
)

__all__ = [
    "STATUS_NOT_FOUND_GRACE_SECONDS",
    "STATUS_POLL_BACKOFF",
    "STATUS_POLL_INITIAL_SECONDS",
    "STATUS_POLL_MAX_SECONDS",
    "poll_platform_deploy_status",
    "status_poll_attempts",
    "status_poll_delays",
    "trigger_platform_deploy",
]
