#!/usr/bin/env python3
"""CLI entrypoint for the out-of-band watchdog (common/observability/out_of_band_watchdog.py).

Workflow steps also import `deliver_out_of_band_alert` from this path; it stays here.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.out_of_band_watchdog import (  # noqa: E402,F401
    deliver_out_of_band_alert,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
