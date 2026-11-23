#!/usr/bin/env python3
"""CLI entry point for the resident infra probe runner.

The logic is in `common.observability.infra_probe_runner` (core.harness.md §5.3, #1823).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.infra_probe_runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
