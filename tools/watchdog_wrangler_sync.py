#!/usr/bin/env python3
"""Sync or check cloudflare/infra-watchdog/wrangler.toml against SSOT signals."""

from __future__ import annotations

from common.observability.watchdog_wrangler_sync import main

if __name__ == "__main__":
    raise SystemExit(main())
