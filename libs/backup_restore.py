"""Backup restore rehearsal helpers.

These helpers intentionally separate the durable backup proof from the
anonymized snapshot pipeline. A rehearsal restores an encrypted real-data
disaster-recovery artifact into an explicitly throwaway target and validates
basic database invariants.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from libs.backup_verification import (
    BackupEntry,
    _parse_timestamp,
    verify_backup_manifest,
)


class BackupRestoreError(RuntimeError):
    """Raised when a restore rehearsal cannot run safely."""


@dataclass(frozen=True)
class RestoreRehearsalPlan:
    service_id: str
    source_uri: str
    archive_path: str
    target_container: str
    pg_user: str
    database: str
    invariant_sql: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def latest_artifact_for_service(
    manifest: dict[str, Any], service_id: str
) -> dict[str, Any]:
    """Return the newest manifest artifact for one service."""
    candidates = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and item.get("service_id") == service_id
    ]
    if not candidates:
        raise BackupRestoreError(f"manifest has no artifact for {service_id}")
    return max(candidates, key=lambda item: _parse_timestamp(item.get("created_at")))


def assert_manifest_is_rehearsable(
    entry: BackupEntry,
    manifest: dict[str, Any],
    *,
    now: int,
) -> dict[str, Any]:
    """Verify freshness/off-host constraints before any restore attempt."""
    report = verify_backup_manifest([entry], manifest, now=now)
    if report["status"] != "pass":
        failures = [
            check["summary"] for check in report["checks"] if check["status"] != "pass"
        ]
        raise BackupRestoreError(
            "; ".join(failures) or "backup manifest verification failed"
        )
    return latest_artifact_for_service(manifest, entry.service_id)


def assert_rehearsal_target(
    target_container: str, *, allow_non_rehearsal_target: bool = False
) -> None:
    """Refuse to restore real backup data into an ambiguous live-looking target."""
    if allow_non_rehearsal_target:
        return
    lowered = target_container.lower()
    if (
        "rehearsal" not in lowered
        and "restore" not in lowered
        and "throwaway" not in lowered
    ):
        raise BackupRestoreError(
            "target container must look disposable (contains rehearsal, restore, or throwaway)"
        )


def materialize_artifact(
    artifact: dict[str, Any],
    download_dir: Path,
    *,
    runner=subprocess.run,
) -> Path:
    """Resolve a local artifact path, downloading remote artifacts with rclone."""
    remote_uri = str(artifact.get("remote_uri") or "")
    if remote_uri.startswith("local:"):
        return Path(remote_uri.removeprefix("local:"))
    if ":" not in remote_uri:
        raise BackupRestoreError(f"unsupported backup artifact URI: {remote_uri}")

    download_dir.mkdir(parents=True, exist_ok=True)
    destination = planned_artifact_path(artifact, download_dir)
    result = runner(
        ["rclone", "copyto", remote_uri, str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BackupRestoreError(
            result.stderr.strip() or f"rclone download failed: {remote_uri}"
        )
    return destination


def planned_artifact_path(artifact: dict[str, Any], download_dir: Path) -> Path:
    """Return where an artifact would be read from or downloaded to."""
    remote_uri = str(artifact.get("remote_uri") or "")
    if remote_uri.startswith("local:"):
        return Path(remote_uri.removeprefix("local:"))
    if ":" not in remote_uri:
        raise BackupRestoreError(f"unsupported backup artifact URI: {remote_uri}")
    return download_dir / remote_uri.rsplit("/", 1)[-1]


def build_postgres_rehearsal_plan(
    *,
    entry: BackupEntry,
    artifact: dict[str, Any],
    archive_path: Path,
    target_container: str,
    pg_user: str = "postgres",
    database: str = "postgres",
    invariant_sql: tuple[str, ...] = (
        "SELECT 1",
        "SELECT count(*) >= 1 FROM pg_database",
    ),
) -> RestoreRehearsalPlan:
    if "pg" not in entry.method and "pg" not in str(artifact.get("method") or ""):
        raise BackupRestoreError(
            f"restore rehearsal currently supports postgres backups, got {entry.method}"
        )
    assert_rehearsal_target(target_container)
    return RestoreRehearsalPlan(
        service_id=entry.service_id,
        source_uri=str(artifact.get("remote_uri") or ""),
        archive_path=str(archive_path),
        target_container=target_container,
        pg_user=pg_user,
        database=database,
        invariant_sql=invariant_sql,
    )


def run_postgres_restore_rehearsal(
    plan: RestoreRehearsalPlan,
    *,
    popen=subprocess.Popen,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Restore a gzipped pg dump into the target and run invariant checks."""
    assert_rehearsal_target(plan.target_container)
    archive_path = Path(plan.archive_path)
    if not archive_path.exists():
        raise BackupRestoreError(f"backup archive is missing: {archive_path}")

    restore_cmd = [
        "docker",
        "exec",
        "-i",
        plan.target_container,
        "psql",
        "-U",
        plan.pg_user,
        "-v",
        "ON_ERROR_STOP=1",
        plan.database,
    ]
    with gzip.open(archive_path, "rb") as dump:
        proc = popen(restore_cmd, stdin=subprocess.PIPE)
        assert proc.stdin is not None
        with proc.stdin:
            shutil.copyfileobj(dump, proc.stdin)
        rc = proc.wait()
    if rc != 0:
        raise BackupRestoreError(f"postgres restore command failed with exit code {rc}")

    for sql in plan.invariant_sql:
        result = runner(
            [
                "docker",
                "exec",
                plan.target_container,
                "psql",
                "-U",
                plan.pg_user,
                "-d",
                plan.database,
                "-Atqc",
                sql,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise BackupRestoreError(
                result.stderr.strip() or f"restore invariant failed: {sql}"
            )

    return {
        "status": "pass",
        "service_id": plan.service_id,
        "target_container": plan.target_container,
        "invariants_checked": len(plan.invariant_sql),
    }
