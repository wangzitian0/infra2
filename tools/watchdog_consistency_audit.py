#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.observability.watchdog_audit`` (#1270)."""

from __future__ import annotations

from common.observability.watchdog_audit import main

if __name__ == "__main__":
    raise SystemExit(main())
