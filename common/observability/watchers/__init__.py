"""Infra2 Observability Watchers SSOT."""

from __future__ import annotations

from common.observability.watchers.breakdown_watch import (
    BreakdownWatch,
    sweep as sweep_breakdowns,
)

# O-02 Canonical alias
ContainerBreakdownWatcher = BreakdownWatch

__all__ = [
    "BreakdownWatch",
    "ContainerBreakdownWatcher",
    "sweep_breakdowns",
]
