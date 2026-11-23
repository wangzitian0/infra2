#!/usr/bin/env python3
"""CLI entry point of the host hygiene schedule: ``python -m tools.host_hygiene_schedule``.

The logic is in ``common/deploy/host_hygiene_schedule.py`` (docs/ssot/core.harness.md
§5.3). This file is scheduled automation (AGENTS.md merge gate rule 6).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.host_hygiene_schedule import main  # noqa: E402

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
