"""Backward-compatibility shim — the implementation lives in `libs.observability.ledger`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.ledger import (
    WORST_SIGNALS_SHOWN,
    apply_unavailability,
    build_environment_line,
    build_report_message,
    outage_intervals,
    summarize_ledger,
)

__all__ = [
    "WORST_SIGNALS_SHOWN",
    "apply_unavailability",
    "build_environment_line",
    "build_report_message",
    "outage_intervals",
    "summarize_ledger",
]
