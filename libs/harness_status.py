"""Backward-compatibility shim — the implementation lives in `libs.core.harness.status`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.core.harness.status import (
    RepositoryStatus,
    Runner,
    WorkspaceStatus,
    repository_status,
    workspace_status,
)

__all__ = [
    "RepositoryStatus",
    "Runner",
    "WorkspaceStatus",
    "repository_status",
    "workspace_status",
]
