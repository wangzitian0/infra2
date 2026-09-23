"""Infra2 Observability Container Breakdown SSOT."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from libs.container_breakdown import (
    BREAKDOWN_PATTERNS,
    Breakdown,
    broken_state,
    build_breakdown_alert_payload,
    classify_reason,
    container_identity,
    container_name,
    find_breakdown_containers,
)


@dataclass(frozen=True)
class BreakdownVerdict:
    """O-01: Container log breakdown diagnostic verdict."""

    cause: str
    detail: str
    raw_logs: str = ""


def analyze_container_logs(
    container: Any, log_chunk: str
) -> BreakdownVerdict:
    """O-01: Analyze container logs to determine failure root cause."""
    cause, detail = classify_reason(log_chunk)
    return BreakdownVerdict(cause=cause, detail=detail, raw_logs=log_chunk)


# Compatibility alias
find_breakdown_reason = classify_reason


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
