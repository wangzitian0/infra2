"""Backward-compatibility shim — the implementation lives in `libs.observability.signal_entries`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.signal_entries import (
    CADENCE,
    render_internal_signal_entries,
)

__all__ = [
    "CADENCE",
    "render_internal_signal_entries",
]
