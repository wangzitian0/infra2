#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.core.scaffold`` (infra2#1823 Step 4.3)."""

from __future__ import annotations

from common.core.scaffold import main

if __name__ == "__main__":
    raise SystemExit(main())
