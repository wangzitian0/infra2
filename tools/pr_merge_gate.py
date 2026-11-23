#!/usr/bin/env python3
"""CLI entry point of the merge gate: ``python -m tools.pr_merge_gate <n>``.

The logic is in ``common/gate/pr_merge_gate.py`` (docs/ssot/core.harness.md §5.3).
This file is the seed of the gate's self-governing closure
(``common.gate.self_governance.self_governing_files``); the closure follows its
import into the common module.
"""

from __future__ import annotations

import sys

# AGENTS.md merge gate rule 5 names `tools/pr_merge_gate.self_governing_files()`.
from common.gate.pr_merge_gate import main, self_governing_files  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
