#!/usr/bin/env python3
"""Thin CLI shim for the stage record reader (common/deploy/stage_record_reader.py)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.stage_record_reader import main, verified_sdk_versions  # noqa: E402

__all__ = ["main", "verified_sdk_versions"]

if __name__ == "__main__":
    raise SystemExit(main())
