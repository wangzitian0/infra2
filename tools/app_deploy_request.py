#!/usr/bin/env python3
"""CLI entrypoint for app_deploy_request."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.app_deploy_request_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
