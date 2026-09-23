"""Backward-compatibility shim — the implementation lives in
`libs.observability.breakdown`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.breakdown import (
    BREAKDOWN_PATTERNS,
    Breakdown,
    BreakdownVerdict,
    analyze_container_logs,
    broken_state,
    build_breakdown_alert_payload,
    classify_reason,
    container_identity,
    container_name,
    find_breakdown_containers,
    find_breakdown_reason,
)

__all__ = [
    "BREAKDOWN_PATTERNS",
    "Breakdown",
    "BreakdownVerdict",
    "analyze_container_logs",
    "broken_state",
    "build_breakdown_alert_payload",
    "classify_reason",
    "container_identity",
    "container_name",
    "find_breakdown_containers",
    "find_breakdown_reason",
]
