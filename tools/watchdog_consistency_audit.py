#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.observability.watchdog_audit`` (#1270)."""

from __future__ import annotations

import sys

from common.observability import watchdog_audit as _watchdog_audit
from common.observability.watchdog_audit import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(_watchdog_audit.main())
else:
    sys.modules[__name__] = _watchdog_audit
