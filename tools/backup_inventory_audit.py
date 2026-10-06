#!/usr/bin/env python3
"""Audit backup inventory coverage for registered service data paths."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.backup.verification import load_backup_inventory  # noqa: E402


def main() -> int:
    entries = load_backup_inventory()
    assert entries, "Backup inventory must not be empty"
    ids = [e.service_id for e in entries]
    assert len(ids) == len(set(ids)), f"Duplicate backup service_id: {ids}"
    for e in entries:
        assert e.data_path.startswith("/data/"), (
            f"{e.service_id} data_path must live under /data/ (got: {e.data_path})"
        )
        assert e.method, f"{e.service_id} missing backup method"
    print(f"✅ Backup inventory covers {len(ids)} services")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
