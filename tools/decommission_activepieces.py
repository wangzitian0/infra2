#!/usr/bin/env python3
"""Activepieces decommission automation tool (#822).

Enforces Apocalypse Engineering rules for non-reversible operations:
1. Physical measurement & verification before destructive action:
   - Export logical pg_dump -Fc
   - Physical byte check (> 1024 bytes)
   - Structural catalog verification via pg_restore -l
2. Off-host / cold storage archive preservation:
   - Upload verified backup to MinIO archive bucket
3. Multi-layer safety gate:
   - DROP DATABASE and host path deletion require explicit --confirm AND dry_run=False.
   - Refuse execution on failed or missing backup.
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _validate_database_name(db_name: str) -> None:
    """Restrict names used in SQL and backup paths to simple identifiers."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db_name):
        raise ValueError(f"Unsafe PostgreSQL database name: {db_name!r}")


def default_runner(
    cmd: list[str],
    *,
    text: bool = True,
    input: bytes | str | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Default process runner using subprocess.run."""
    return subprocess.run(cmd, capture_output=True, text=text, input=input, check=False)


def check_database_exists(
    db_name: str = "activepieces",
    *,
    container: str = "platform-postgres",
    runner: Callable[..., Any] = default_runner,
) -> bool:
    """Check if database exists; never mistake a failed query for absence."""
    _validate_database_name(db_name)
    cmd = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{db_name}';",
    ]
    res = runner(cmd)
    if res.returncode != 0:
        raise RuntimeError(
            f"Could not check whether database {db_name} exists: "
            f"{getattr(res, 'stderr', 'unknown error')}"
        )
    output = getattr(res, "stdout", "").strip()
    if output not in {"", "1"}:
        raise RuntimeError(
            f"Unexpected database existence result for {db_name}: {output!r}"
        )
    return output == "1"


def export_database_backup(
    db_name: str,
    output_path: Path,
    *,
    container: str = "platform-postgres",
    runner: Callable[..., Any] = default_runner,
) -> Path:
    """Export custom-format pg_dump of the database without text-decoding stdout."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    container_tmp = f"/tmp/{output_path.name}"
    dump_cmd = [
        "docker",
        "exec",
        container,
        "pg_dump",
        "-U",
        "postgres",
        "-Fc",
        "-f",
        container_tmp,
        db_name,
    ]
    res = runner(dump_cmd)
    if res.returncode != 0:
        raise RuntimeError(
            f"pg_dump failed for {db_name}: {getattr(res, 'stderr', 'unknown error')}"
        )

    # Copy dump from container to host output_path
    cp_cmd = ["docker", "cp", f"{container}:{container_tmp}", str(output_path)]
    res_cp = runner(cp_cmd)
    if res_cp.returncode != 0:
        raise RuntimeError(
            f"docker cp failed for {output_path}: {getattr(res_cp, 'stderr', 'unknown error')}"
        )

    # Cleanup container temporary dump file
    cleanup_cmd = ["docker", "exec", container, "rm", "-f", container_tmp]
    runner(cleanup_cmd)
    return output_path


def verify_backup_integrity(
    backup_file: Path,
    *,
    min_bytes: int = 1024,
    container: str = "platform-postgres",
    runner: Callable[..., Any] = default_runner,
) -> bool:
    """Verify backup file size and table structure via pg_restore -l."""
    if not backup_file.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_file}")

    file_size = backup_file.stat().st_size
    if file_size <= min_bytes:
        raise ValueError(
            f"Backup verification failed: file size ({file_size} bytes) <= minimum threshold ({min_bytes} bytes)"
        )

    # Verify structural catalog with pg_restore -l
    cmd = ["pg_restore", "-l", str(backup_file)]
    res = runner(cmd)
    if res.returncode != 0:
        dump_bytes = backup_file.read_bytes()
        cmd_docker = ["docker", "exec", "-i", container, "pg_restore", "-l"]
        res_docker = runner(cmd_docker, input=dump_bytes, text=False)
        if res_docker.returncode != 0:
            err = getattr(res, "stderr", "") or getattr(res_docker, "stderr", "")
            raise ValueError(
                f"pg_restore -l verification failed: {err or 'non-zero exit code'}"
            )
        stdout = res_docker.stdout
    else:
        stdout = res.stdout

    stdout_str = (
        stdout.decode("utf-8", errors="ignore")
        if isinstance(stdout, bytes)
        else str(stdout)
    )
    if not stdout_str.strip():
        raise ValueError(
            "pg_restore -l returned empty schema catalog; dump file appears empty or corrupt"
        )

    return True


def upload_backup_to_archive(
    backup_file: Path,
    *,
    bucket: str = "archive",
    archive_dir: Path | None = None,
    runner: Callable[..., Any] = default_runner,
) -> str:
    """Save backup file to local archive directory and upload to MinIO archive bucket."""
    target_dir = archive_dir or Path("/tmp/infra2_archive")
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / backup_file.name
    shutil.copy2(backup_file, destination)

    # MinIO upload via mc with strict returncode verification
    minio_target = f"minio/{bucket}/activepieces/{backup_file.name}"
    cmd = ["mc", "cp", str(backup_file), minio_target]
    res = runner(cmd)
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed to upload backup to MinIO ({minio_target}): exit {res.returncode}, stderr: {getattr(res, 'stderr', '')}"
        )

    return f"s3://{bucket}/activepieces/{backup_file.name}"


def drop_database(
    db_name: str = "activepieces",
    *,
    confirm: bool = False,
    dry_run: bool = False,
    container: str = "platform-postgres",
    runner: Callable[[list[str]], Any] = default_runner,
) -> bool:
    """Drop database with strict double confirmation gates."""
    _validate_database_name(db_name)
    if dry_run:
        print(
            f"[DRY-RUN] Safety gate active: skipped physical DROP DATABASE {db_name};"
        )
        return False

    if not confirm:
        raise PermissionError(
            f"Destructive operation aborted: DROP DATABASE {db_name} requires explicit --confirm"
        )

    # Terminate active connections
    term_cmd = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-c",
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}';",
    ]
    runner(term_cmd)

    # Physical drop
    drop_cmd = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "postgres",
        "-c",
        f"DROP DATABASE {db_name};",
    ]
    res = runner(drop_cmd)
    if res.returncode != 0:
        raise RuntimeError(
            f"DROP DATABASE {db_name} failed: {getattr(res, 'stderr', 'unknown error')}"
        )
    return True


def cleanup_data_path(
    data_path: Path | str = "/data/platform/activepieces",
    *,
    confirm: bool = False,
    dry_run: bool = False,
    runner: Callable[[list[str]], Any] = default_runner,
) -> bool:
    """Safely clean up decommissioned service data directory on host."""
    p = Path(data_path)
    if not p.exists():
        print(f"Data path {data_path} does not exist; skipping cleanup.")
        return False

    if dry_run:
        print(f"[DRY-RUN] Safety gate active: skipped removing path {data_path}")
        return False

    if not confirm:
        raise PermissionError(
            f"Destructive operation aborted: cleaning {data_path} requires explicit --confirm"
        )

    cmd = ["rm", "-rf", "--", str(p)]
    res = runner(cmd)
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed to cleanup data path {data_path}: {getattr(res, 'stderr', 'unknown error')}"
        )
    return True


def decommission_activepieces(
    *,
    confirm: bool = False,
    dry_run: bool = False,
    db_name: str = "activepieces",
    data_path: Path | str = "/data/platform/activepieces",
    container: str = "platform-postgres",
    backup_dir: Path | None = None,
    archive_bucket: str = "archive",
    runner: Callable[[list[str]], Any] = default_runner,
) -> dict[str, Any]:
    """Execute end-to-end decommission workflow with apocalypse safeguards."""
    _validate_database_name(db_name)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir_path = backup_dir or Path("/tmp/activepieces_decommission")
    backup_file = backup_dir_path / f"{db_name}_{timestamp}.dump"

    db_exists = check_database_exists(db_name, container=container, runner=runner)
    backup_verified = False
    archive_location = ""
    db_dropped = False

    if db_exists:
        print(
            f"Found active database '{db_name}'. Initiating pre-destruction cold backup..."
        )
        export_database_backup(db_name, backup_file, container=container, runner=runner)
        verify_backup_integrity(
            backup_file, min_bytes=1024, container=container, runner=runner
        )
        backup_verified = True
        print(
            f"Backup integrity verified ({backup_file.stat().st_size} bytes, schema valid)."
        )

        archive_location = upload_backup_to_archive(
            backup_file, bucket=archive_bucket, runner=runner
        )
        print(f"Backup preserved to archive: {archive_location}")

        db_dropped = drop_database(
            db_name,
            confirm=confirm,
            dry_run=dry_run,
            container=container,
            runner=runner,
        )
    else:
        print(
            f"Database '{db_name}' does not exist; skipping database backup and drop."
        )

    if Path(data_path).exists() and not backup_verified and not dry_run:
        raise RuntimeError(
            f"Refusing to remove {data_path}: no verified database backup is available"
        )

    path_cleaned = cleanup_data_path(
        data_path,
        confirm=confirm,
        dry_run=dry_run,
        runner=runner,
    )

    return {
        "db_name": db_name,
        "db_exists": db_exists,
        "backup_verified": backup_verified,
        "backup_file": str(backup_file) if backup_verified else None,
        "archive_location": archive_location,
        "db_dropped": db_dropped,
        "path_cleaned": path_cleaned,
        "dry_run": dry_run,
        "confirm": confirm,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely decommission Activepieces database and host path with verified cold backup."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Explicitly confirm destructive DROP DATABASE and host directory cleanup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute workflow in simulation mode without physical deletion",
    )
    parser.add_argument(
        "--db-name",
        default="activepieces",
        help="Database name to decommission (default: activepieces)",
    )
    parser.add_argument(
        "--data-path",
        default="/data/platform/activepieces",
        help="Host data path to decommission (default: /data/platform/activepieces)",
    )
    parser.add_argument(
        "--container",
        default="platform-postgres",
        help="Postgres container name (default: platform-postgres)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = decommission_activepieces(
            confirm=args.confirm,
            dry_run=args.dry_run,
            db_name=args.db_name,
            data_path=args.data_path,
            container=args.container,
        )
        print("\n=== Decommission Result ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return 0
    except Exception as exc:
        print(f"ERROR: Decommission aborted: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
