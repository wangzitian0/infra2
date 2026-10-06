"""Frozen shim: the implementation lives in ``libs.observability.watchers.resident`` (#955).

Import from ``libs.observability.watchers.resident`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.observability.watchers.resident import (
    ResidentWatcher,
    build_watchers,
)

__all__ = [
    "ResidentWatcher",
    "build_watchers",
]
