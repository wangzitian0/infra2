#!/usr/bin/env python3
"""CLI entry point for the observability write-then-query probes.

The logic is in `common.observability.observability_roundtrip_probe` (core.harness.md §5.3, #1823).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.observability_roundtrip_probe import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
