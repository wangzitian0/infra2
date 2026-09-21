#!/usr/bin/env python3
"""Automated end-to-end sandboxed backup restore rehearsal for Infra2.

Spins up an ephemeral, isolated throwaway container (zero host port binds,
zero mount to production /data, memory-capped), restores the latest verified
off-host backup artifact from Google Drive, validates core database and
domain-specific invariants, and guarantees clean teardown.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from libs.backup_restore import (
    assert_manifest_is_rehearsable,
    assert_rehearsal_target,
    build_postgres_rehearsal_plan,
    materialize_artifact,
    run_postgres_restore_rehearsal,
)
from libs.backup_verification import load_backup_inventory


def wait_for_postgres(container: str, user: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", user],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(
        f"Sandbox container {container} failed to become ready within {timeout}s"
    )


def run_rehearsal(
    *,
    manifest_path: str,
    service_id: str = "finance_report/postgres",
    database: str = "finance_report",
    download_dir: str = "/tmp/infra2-backup-restore-rehearsal",
    image: str = "postgres:16-alpine",
    keep_container: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"^[a-zA-Z0-9_]+$", database):
        raise ValueError(f"Invalid database name: {database!r}")

    safe_name = service_id.replace("/", "-")
    container = f"{safe_name}-restore-rehearsal-throwaway"
    bootstrap_user = "postgres"
    start_time = time.time()

    entries = {entry.service_id: entry for entry in load_backup_inventory()}
    if service_id not in entries:
        raise ValueError(f"Unknown backup service: {service_id}")

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    artifact = assert_manifest_is_rehearsable(
        entries[service_id],
        manifest,
        now=int(time.time()),
    )
    assert_rehearsal_target(container)

    # 1. Start isolated sandbox container (hard-disable networking, enforce local trust auth)
    print(f"[*] Starting sandbox container: {container} (image: {image})...")
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    run_cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "--network=none",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e",
        f"POSTGRES_USER={bootstrap_user}",
        "--memory=1g",
        "--cpus=1",
        image,
    ]
    subprocess.run(run_cmd, check=True, capture_output=True)

    try:
        # 2. Wait for readiness
        wait_for_postgres(container, bootstrap_user)
        print("[+] Sandbox database is ready for ingestion.")

        # 3. Materialize artifact (local or rclone download from Google Drive)
        print(f"[*] Materializing artifact from {artifact.get('remote_uri')}...")
        dl_dir = Path(download_dir)
        archive_path = materialize_artifact(artifact, dl_dir)
        print(
            f"[+] Materialized archive: {archive_path} ({archive_path.stat().st_size} bytes)"
        )

        # 4. Define invariants (connectivity, DB existence, table threshold, domain specific rows)
        if service_id == "finance_report/postgres":
            invariants = (
                "SELECT 1",
                f"SELECT count(*) >= 1 FROM pg_database WHERE datname = '{database}'",
                "DO $$ BEGIN IF (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public') < 50 THEN RAISE EXCEPTION 'table count below threshold (<50)'; END IF; END $$;",
                "DO $$ BEGIN IF (SELECT count(*) FROM accounts) < 5 THEN RAISE EXCEPTION 'accounts count below threshold (<5)'; END IF; END $$;",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM alembic_version WHERE version_num IN ('0063_enum_case_compat', '0062_bank_custody')) THEN RAISE EXCEPTION 'unexpected alembic_version'; END IF; END $$;",
            )
        elif service_id == "truealpha/postgres":
            invariants = (
                "SELECT 1",
                f"SELECT count(*) >= 1 FROM pg_database WHERE datname = '{database}'",
                "DO $$ BEGIN IF (SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('raw', 'staging', 'mart', 'app')) < 50 THEN RAISE EXCEPTION 'table count below threshold (<50)'; END IF; END $$;",
                "DO $$ BEGIN IF (SELECT count(*) FROM mart.topt_capture_status WHERE complete = true) < 1 THEN RAISE EXCEPTION 'topt_capture_status count below threshold (<1)'; END IF; END $$;",
                "DO $$ BEGIN IF (SELECT count(*) FROM mart.topt_gppe_results WHERE payload->>'availability' = 'available') < 100 THEN RAISE EXCEPTION 'topt_gppe_results available below threshold (<100)'; END IF; END $$;",
            )
        else:
            invariants = (
                "SELECT 1",
                f"SELECT count(*) >= 1 FROM pg_database WHERE datname = '{database}'",
                "DO $$ BEGIN IF (SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')) < 1 THEN RAISE EXCEPTION 'no tables restored'; END IF; END $$;",
                "SELECT current_database()",
                "SELECT 1",
            )
        plan = build_postgres_rehearsal_plan(
            entry=entries[service_id],
            artifact=artifact,
            archive_path=archive_path,
            target_container=container,
            pg_user=bootstrap_user,
            database=database,
            invariant_sql=invariants,
        )

        # 5. Execute restore & invariants
        print(
            f"[*] Restoring into {container} and running {len(invariants)} invariants..."
        )
        run_postgres_restore_rehearsal(plan)
        print("[+] Rehearsal ingestion and base invariants PASSED.")

        # 6. Collect detailed business row counts for proof
        def query_val(sql: str) -> str:
            res = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "psql",
                    "-U",
                    bootstrap_user,
                    "-d",
                    database,
                    "-Atqc",
                    sql,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return res.stdout.strip()

        domain_stats: dict[str, Any] = {}
        if service_id == "finance_report/postgres":
            tables_count = int(
                query_val(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
            domain_stats = {
                "accounts_count": int(query_val("SELECT count(*) FROM accounts")),
                "alembic_version": query_val("SELECT version_num FROM alembic_version"),
            }
        elif service_id == "truealpha/postgres":
            tables_count = int(
                query_val(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('raw', 'staging', 'mart', 'app')"
                )
            )
            domain_stats = {
                "topt_capture_complete_count": int(
                    query_val(
                        "SELECT count(*) FROM mart.topt_capture_status WHERE complete = true"
                    )
                ),
                "topt_gppe_available_count": int(
                    query_val(
                        "SELECT count(*) FROM mart.topt_gppe_results WHERE payload->>'availability' = 'available'"
                    )
                ),
            }
        else:
            tables_count = int(
                query_val(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                )
            )
        db_size_bytes = int(query_val("SELECT pg_database_size(current_database())"))

        elapsed = round(time.time() - start_time, 2)
        evidence = {
            "status": "PASS",
            "service_id": service_id,
            "database": database,
            "artifact_uri": artifact.get("remote_uri"),
            "artifact_sha256": artifact.get("sha256"),
            "archive_size_bytes": archive_path.stat().st_size,
            "restored_db_size_bytes": db_size_bytes,
            "verified_tables_count": tables_count,
            "verified_accounts_count": domain_stats.get("accounts_count", 0),
            "verified_alembic_version": domain_stats.get("alembic_version", "n/a"),
            "domain_stats": domain_stats,
            "duration_seconds": elapsed,
            "sandbox_container": container,
            "teardown": not keep_container,
        }
        return evidence

    finally:
        if not keep_container:
            print(f"[*] Teardown: removing sandbox container {container}...")
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            p_dl = Path(download_dir).resolve()
            # Safety guard: only rmtree if path contains rehearsal marker or is inside OS tempdir
            tmp_root = Path(tempfile.gettempdir()).resolve()
            if p_dl.exists() and (
                "infra2-backup-restore-rehearsal" in p_dl.name
                or "rehearsal" in p_dl.name
                or p_dl.is_relative_to(tmp_root)
            ):
                shutil.rmtree(p_dl, ignore_errors=True)
            print("[+] Teardown completed. Zero remnants.")
        else:
            print(f"[!] Sandbox container {container} preserved for inspection.")


def default_database_for_service(service_id: str) -> str:
    if service_id == "truealpha/postgres":
        return "truealpha"
    elif service_id == "finance_report/postgres":
        return "finance_report"
    return "postgres"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="/data/backups/infra2/manifest.json")
    parser.add_argument("--service-id", default="finance_report/postgres")
    parser.add_argument("--database", default=None)
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args()

    # If manifest default doesn't exist, search for latest TS manifest
    manifest_path = args.manifest
    if not Path(manifest_path).exists():
        candidates = sorted(Path("/data/backups/infra2").glob("*/manifest.json"))
        if candidates:
            manifest_path = str(candidates[-1])

    services = (
        ["finance_report/postgres", "truealpha/postgres"]
        if args.service_id == "all"
        else [args.service_id]
    )

    all_passed = True
    for s_id in services:
        db = args.database or default_database_for_service(s_id)
        report = run_rehearsal(
            manifest_path=manifest_path,
            service_id=s_id,
            database=db,
            keep_container=args.keep_container,
        )
        print("\n" + "=" * 60)
        print(f"RESTORE REHEARSAL PROOF REPORT ({s_id}):")
        print("=" * 60)
        print(json.dumps(report, indent=2))
        print(f"RESTORE_PROOF: {report['status']}")
        if report["status"] != "PASS":
            all_passed = False

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
