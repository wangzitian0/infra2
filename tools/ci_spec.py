"""Single source of truth for cross-repo CI testing hierarchy and compute budgets.

Defines the Left-to-Right Testing Hierarchy (L0 Gate -> L1 Integrate -> L2 Heavy -> L3 Release):
- L0 PR Gate: Total wall clock <= 90s, individual test <= 30s, shards between 2 and 8.
- L2 Heavy / Nightly: Benchmarks, full reconciliation, and soak testing.
"""
from __future__ import annotations

SPEC_VERSION = "1.0.0"

# L0 Gate Budgets
GATE_WALL_CLOCK_BUDGET_S = 90     # Hard ceiling for PR gate wall-clock runtime
SOFT_WARN_S = 75                  # Soft warning line for PR gate
MAX_SINGLE_TEST_S = 30            # Single tests exceeding this budget are forbidden in L0
TARGET_PER_SHARD_S = 45           # Optimal target duration per shard
SHARD_MIN = 2                     # Minimum shards when sharding is activated
SHARD_MAX = 8                     # Hard upper limit on shards to prevent VM cold-start fragmentation

# Forbidden test patterns in PR / merge-queue gates (must migrate to L2 nightly)
BANNED_IN_GATE_PATTERNS = (
    "benchmark",
    "make bench",
    "reconciliation",
    "full_recon",
    "full_reconcile",
    "soak_test",
)

# Markers forbidden in L0 gate pytest runs
BANNED_GATE_MARKERS = (
    "benchmark",
    "slow",
    "reconciliation",
    "full_recon",
)
