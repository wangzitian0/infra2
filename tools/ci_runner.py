#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.gate.ci_runner`` (infra2#1823 Step 4.3)."""

from __future__ import annotations

import sys
from pathlib import Path

# `python tools/ci_runner.py` puts tools/ on sys.path, not the repository root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.gate.ci_runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
