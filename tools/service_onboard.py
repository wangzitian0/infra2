#!/usr/bin/env python3
"""CLI and invoke wrapper delegating to ``common.core.service_onboard`` (infra2#1823 Step 4.3).

Usage:
    python -m tools.service_onboard <service> [--project apps] [--env production] [--db postgres]
    invoke service.onboard <service> [--project apps] [--env production] [--db postgres]
"""

from __future__ import annotations

from common.core.service_onboard import main, onboard_task

__all__ = ["main", "onboard_task"]

if __name__ == "__main__":
    raise SystemExit(main())
