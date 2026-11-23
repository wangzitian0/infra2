#!/usr/bin/env python3
"""CLI entry point for the disabled SigNoz alert-rule canary.

The logic is in `common.signoz_alert_rule_probe` (core.harness.md §5.3, #1823).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.signoz_alert_rule_probe import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
