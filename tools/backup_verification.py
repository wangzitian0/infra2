#!/usr/bin/env python3
# alert-delivery-exempt: manual SOP tool (Infra-011 SOP-004/005), not scheduled; alert path fires only under operator invocation; review=2027-04-01
"""CLI entrypoint for backup_verification."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.backup.backup_verification_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
