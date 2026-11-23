#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.backup.restore_rehearsal_cli`` (infra2#1823 Step 4.3).

The host cron runs ``PYTHONPATH=. python3 tools/run_restore_rehearsal.py`` (ops.recovery.md).
"""

from __future__ import annotations

from common.backup.restore_rehearsal_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
