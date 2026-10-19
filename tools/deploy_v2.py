#!/usr/bin/env python3
"""CLI front door for unified deploy_v2 (delegating to ``common.deploy.v2``, #1270)."""

from __future__ import annotations

import sys

from common.deploy import v2 as _v2
from common.deploy.v2 import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(_v2.main())
else:
    sys.modules[__name__] = _v2
