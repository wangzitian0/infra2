#!/usr/bin/env python3
"""Verify backup freshness manifests and alert through the bridge."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from libs.backup_verification import (
    build_backup_alert_payload,
    load_backup_inventory,
    verify_backup_manifest,
)
from libs.infra_probes import post_alert_bridge_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--now", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    entries = load_backup_inventory()
    report = verify_backup_manifest(
        entries,
        manifest,
        now=args.now or int(manifest.get("verified_at") or time.time()),
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))

    if report["status"] == "pass":
        return 0

    if os.getenv("BACKUP_VERIFY_DRY_RUN", "0") == "1":
        print(json.dumps(build_backup_alert_payload(report), indent=2, sort_keys=True))
        return 1

    bridge_url = os.getenv("ALERT_BRIDGE_URL")
    if bridge_url:
        post_alert_bridge_payload(bridge_url, build_backup_alert_payload(report))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
