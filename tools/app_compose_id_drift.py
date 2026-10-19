#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.deploy.compose_id_drift`` (#1270)."""

from __future__ import annotations

import sys

from common.deploy import compose_id_drift as _compose_id_drift
from common.deploy.compose_id_drift import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(_compose_id_drift.main())
else:
    sys.modules[__name__] = _compose_id_drift
