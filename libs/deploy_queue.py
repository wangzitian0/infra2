"""Frozen shim: the implementation lives in ``libs.deploy.queue`` (#955).

Import from ``libs.deploy.queue`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.deploy.queue import (
    ComposeDeployments,
    QUEUE_IMPACT,
    RUNNING_STATUS,
    StuckDeploy,
    build_deploy_guard_alert_payload,
    deployment_start_epoch,
    find_stuck_deploys,
    is_running,
    parse_epoch_seconds,
)

__all__ = [
    "ComposeDeployments",
    "QUEUE_IMPACT",
    "RUNNING_STATUS",
    "StuckDeploy",
    "build_deploy_guard_alert_payload",
    "deployment_start_epoch",
    "find_stuck_deploys",
    "is_running",
    "parse_epoch_seconds",
]
