"""Invoke wrapper delegating to ``common.core.local_init`` (infra2#1823 Step 4.3).

Usage:
    invoke local.init       # Check and install all dependencies
    invoke local.check      # Check dependencies only
"""

from __future__ import annotations

from common.core.local_init import bootstrap, check, init, phase, version

__all__ = ["bootstrap", "check", "init", "phase", "version"]
