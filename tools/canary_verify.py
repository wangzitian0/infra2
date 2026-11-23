#!/usr/bin/env python3
"""CLI entry point for Canary Todo touch-reality verification.

The logic is in `common.observability.canary_verify` (core.harness.md §5.3, #1823).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.canary_verify import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
