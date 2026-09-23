"""Backward-compatibility shim for deploy failure snapshot.

Canonical implementation moved to libs.deploy.failure_snapshot (#840).
"""

from __future__ import annotations

from libs.deploy.failure_snapshot import (
    _SNAPSHOT_FIELDS,
    _deployment_timestamp,
    _latest_deployment,
    _md_cell,
    build_snapshot,
    classify,
    emit_failure_snapshot,
    render_markdown,
)

__all__ = [
    "_SNAPSHOT_FIELDS",
    "_deployment_timestamp",
    "_latest_deployment",
    "_md_cell",
    "build_snapshot",
    "classify",
    "emit_failure_snapshot",
    "render_markdown",
]
