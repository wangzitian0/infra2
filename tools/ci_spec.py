"""Backward-compatibility shim for CI hierarchy spec and workflow parser.

Canonical implementation moved to libs.core.ci_spec (#840).
"""

from __future__ import annotations

from libs.core.ci_spec import (
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
