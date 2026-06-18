#!/usr/bin/env python3
"""Restore a verified backup artifact into a throwaway database target."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from libs.backup_restore import (
    assert_manifest_is_rehearsable,
    assert_rehearsal_target,
    build_postgres_rehearsal_plan,
    materialize_artifact,
    planned_artifact_path,
    run_postgres_restore_rehearsal,
)
from libs.backup_verification import load_backup_inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--service-id", default="finance_report/postgres")
    parser.add_argument("--target-container", required=True)
    parser.add_argument(
        "--download-dir", default="/tmp/infra2-backup-restore-rehearsal"
    )
    parser.add_argument("--pg-user", default="postgres")
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--invariant-sql", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    entries = {entry.service_id: entry for entry in load_backup_inventory()}
    if args.service_id not in entries:
        raise SystemExit(f"Unknown backup service: {args.service_id}")

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    artifact = assert_manifest_is_rehearsable(
        entries[args.service_id],
        manifest,
        now=int(time.time()),
    )
    assert_rehearsal_target(args.target_container)
    download_dir = Path(args.download_dir)
    archive = (
        planned_artifact_path(artifact, download_dir)
        if args.dry_run
        else materialize_artifact(artifact, download_dir)
    )
    invariants = tuple(args.invariant_sql) or (
        "SELECT 1",
        "SELECT count(*) >= 1 FROM pg_database",
    )
    plan = build_postgres_rehearsal_plan(
        entry=entries[args.service_id],
        artifact=artifact,
        archive_path=archive,
        target_container=args.target_container,
        pg_user=args.pg_user,
        database=args.database,
        invariant_sql=invariants,
    )

    if args.dry_run:
        print(
            json.dumps(
                {"status": "planned", "plan": plan.to_dict()}, indent=2, sort_keys=True
            )
        )
        return 0

    print(json.dumps(run_postgres_restore_rehearsal(plan), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
