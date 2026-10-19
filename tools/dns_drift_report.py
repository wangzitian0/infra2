#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.deploy.dns_drift`` (#1270)."""

from __future__ import annotations

import sys

from common.deploy import dns_drift as _dns_drift
from common.deploy.dns_drift import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(_dns_drift.main())
else:
    sys.modules[__name__] = _dns_drift
