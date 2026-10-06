"""Frozen shim: the implementation lives in ``libs.deploy.compose_lock`` (#955).

Import from ``libs.deploy.compose_lock`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.deploy.compose_lock import (
    compose_write_lock,
)

__all__ = [
    "compose_write_lock",
]
