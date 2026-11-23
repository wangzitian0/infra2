#!/usr/bin/env python3
"""CLI and invoke wrapper delegating to ``common.core.service_offboard`` (infra2#1823 Step 4.3).

Usage:
    python -m tools.service_offboard <service> [--project apps] [--env production] [--purge-data]
    invoke service.offboard <service> [--project apps] [--env production] [--purge-data]
"""

from __future__ import annotations

from common.core.service_offboard import main, offboard_task

__all__ = ["main", "offboard_task"]

if __name__ == "__main__":
    raise SystemExit(main())
