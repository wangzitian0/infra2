#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.core.env_inventory`` (#1270)."""

from __future__ import annotations

from common.core.env_inventory import main

if __name__ == "__main__":
    raise SystemExit(main())
