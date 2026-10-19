"""Single source of truth for cross-repo CI testing hierarchy and compute budgets (#788, #840, #1270).

Canonical implementation lives in ``infra2_sdk.ci``.
"""

from __future__ import annotations

from infra2_sdk.ci import (
    BANNED_GATE_MARKERS,
    BANNED_IN_GATE_PATTERNS,
    GATE_EXEMPT_RE,
    GATE_WALL_CLOCK_BUDGET_S,
    MAX_SINGLE_TEST_S,
    SHARD_MAX,
    SHARD_MIN,
    SOFT_WARN_S,
    SPEC_VERSION,
    TARGET_PER_SHARD_S,
    defanged_steps,
    load_workflow,
    read_workflow,
    workflow_read_error,
)

__all__ = [
    "BANNED_GATE_MARKERS",
    "BANNED_IN_GATE_PATTERNS",
    "GATE_EXEMPT_RE",
    "GATE_WALL_CLOCK_BUDGET_S",
    "MAX_SINGLE_TEST_S",
    "SHARD_MAX",
    "SHARD_MIN",
    "SOFT_WARN_S",
    "SPEC_VERSION",
    "TARGET_PER_SHARD_S",
    "defanged_steps",
    "load_workflow",
    "read_workflow",
    "workflow_read_error",
]
