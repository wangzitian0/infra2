#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.core.env_inventory`` (#1270)."""

from __future__ import annotations

import sys

from common.core import env_inventory as _env_inventory
from common.core.env_inventory import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(_env_inventory.main())
else:
    sys.modules[__name__] = _env_inventory
