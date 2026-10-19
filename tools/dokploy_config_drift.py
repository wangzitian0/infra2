#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.deploy.config_drift`` (#1270)."""

from __future__ import annotations

from common.deploy.config_drift import main

if __name__ == "__main__":
    raise SystemExit(main())
