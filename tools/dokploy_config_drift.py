#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.deploy.config_drift`` (#1270)."""

from __future__ import annotations

import sys

from common.deploy import config_drift as _config_drift
from common.deploy.config_drift import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(_config_drift.main())
else:
    sys.modules[__name__] = _config_drift
