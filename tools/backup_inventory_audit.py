#!/usr/bin/env python3
"""Audit backup inventory coverage for registered service data paths."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.backup.verification import validate_backup_inventory  # noqa: E402


def main() -> int:
    ids = validate_backup_inventory()
    print(f"✅ Backup inventory covers {len(ids)} services")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
