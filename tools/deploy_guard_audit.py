#!/usr/bin/env python3
"""CLI entrypoint for deploy_guard_audit."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.deploy_guard_audit import (  # noqa: E402
    cli,
    confirmed_finding_pairs,
    main,
    observed_all_clean,
    unevaluated_prefixes,
)

__all__ = [
    "cli",
    "confirmed_finding_pairs",
    "main",
    "observed_all_clean",
    "unevaluated_prefixes",
]

if __name__ == "__main__":
    raise SystemExit(cli())
