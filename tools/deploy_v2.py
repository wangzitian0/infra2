#!/usr/bin/env python3
"""CLI front door for unified deploy_v2 (delegating to ``common.deploy.v2``, #1270)."""

from __future__ import annotations

from common.deploy.v2 import main

if __name__ == "__main__":
    raise SystemExit(main())
