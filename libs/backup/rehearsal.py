"""Backup restore rehearsal helpers.

SSOT for ``libs.backup``'s rehearsal planning and sandboxed execution;
``libs.backup_restore`` is a backward-compatibility shim over this module.

These helpers intentionally separate the durable backup proof from the
anonymized snapshot pipeline. A rehearsal restores an encrypted real-data
disaster-recovery artifact into an explicitly throwaway target and validates
basic database invariants.
"""

from __future__ import annotations

import concurrent.futures
import gzip
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from libs.backup.verification import (
    BackupEntry,
    _parse_timestamp,
    verify_backup_manifest,
)


# pg_dumpall emits the source cluster's own superuser role, but the rehearsal
# sandbox already bootstraps as `postgres`, so re-creating it aborts the restore
# under ON_ERROR_STOP=1. Only these exact statement prefixes are dropped.
_REDUNDANT_ROLE_STMTS = (b"CREATE ROLE postgres;", b"CREATE ROLE postgres ")

# `COPY <table> (...) FROM stdin;` opens a raw-data block terminated by `\.`.
# Every line in between is table data, not SQL.
_COPY_FROM_STDIN = re.compile(rb"^COPY\s.*\sFROM\s+stdin;", re.IGNORECASE)


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

    restore_target_db = plan.database
    try:
        with gzip.open(archive_path, "rt", errors="ignore") as f:
            header_sample = f.read(2048)
            if (
                "pg_dumpall" in header_sample
                or "CREATE ROLE" in header_sample
                or "CREATE DATABASE" in header_sample
            ):
                restore_target_db = "postgres"
    except Exception:
        pass

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
        restore_target_db,
    ]
    filtered_roles = 0
    stopped_reading = False
    with gzip.open(archive_path, "rb") as dump:
        proc = popen(restore_cmd, stdin=subprocess.PIPE)
        if proc.stdin is None:  # pragma: no cover - stdin=PIPE always gives a pipe
            proc.kill()
            proc.wait()
            raise BackupRestoreError("restore subprocess exposed no stdin pipe")
        try:
            with proc.stdin:
                in_copy_data = False
                for line in dump:
                    # Inside a COPY block every line is raw table data, so the
                    # role filter must not inspect it: a row whose first column
                    # starts with the prefix would be dropped and the restore
                    # would silently lose data while still reporting a pass.
                    if in_copy_data:
                        if line.rstrip(b"\r\n") == b"\\.":
                            in_copy_data = False
                        proc.stdin.write(line)
                        continue
                    if line.startswith(_REDUNDANT_ROLE_STMTS):
                        filtered_roles += 1
                        continue
                    if _COPY_FROM_STDIN.match(line):
                        in_copy_data = True
                    proc.stdin.write(line)
        except BrokenPipeError:
            # psql runs with ON_ERROR_STOP=1, so it can exit while we are still
            # streaming. Its own exit code and stderr are the real diagnosis,
            # so fall through and reap it instead of surfacing the pipe error.
            stopped_reading = True
        finally:
            # Reap on every path. Any other exception from the write loop (a
            # corrupt gzip member, an OSError) would otherwise propagate past
            # wait() and leave psql running with nothing to read.
            rc = proc.wait()
    if rc != 0:
        raise BackupRestoreError(f"postgres restore command failed with exit code {rc}")
    if stopped_reading:
        # psql stopped reading before the dump was fully streamed but still
        # exited 0. The restore is incomplete, so this must not pass as success.
        raise BackupRestoreError(
            "postgres restore command stopped reading before the dump was "
            "fully streamed, yet exited 0: the restore is incomplete"
        )

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
        "filtered_role_statements": filtered_roles,
    }


@dataclass(frozen=True)
class RehearsalSpecification:
    """Consolidated specification for a disaster-recovery rehearsal (B-01)."""

    entry: BackupEntry
    artifact: dict[str, Any]
    archive_path: Path
    target_container: str
    pg_user: str
    database: str
    invariant_sql: tuple[str, ...]
    allow_non_rehearsal_target: bool = False


def create_rehearsal_plan(
    spec: RehearsalSpecification | None = None,
    **kwargs: Any,
) -> RestoreRehearsalPlan:
    """B-01: Build a rehearsal plan from a specification or kwargs."""
    if spec is not None:
        return build_postgres_rehearsal_plan(
            entry=spec.entry,
            artifact=spec.artifact,
            archive_path=spec.archive_path,
            target_container=spec.target_container,
            pg_user=spec.pg_user,
            database=spec.database,
            invariant_sql=spec.invariant_sql,
        )
    return build_postgres_rehearsal_plan(**kwargs)


def execute_rehearsal(
    plan: RestoreRehearsalPlan,
    *,
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """B-02: Execute restore rehearsal with timeout guard."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_postgres_restore_rehearsal, plan, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise BackupRestoreError(
                f"postgres restore rehearsal timed out after {timeout_seconds}s"
            ) from exc


__all__ = [
    "BackupRestoreError",
    "RehearsalSpecification",
    "RestoreRehearsalPlan",
    "assert_manifest_is_rehearsable",
    "assert_rehearsal_target",
    "build_postgres_rehearsal_plan",
    "create_rehearsal_plan",
    "execute_rehearsal",
    "latest_artifact_for_service",
    "materialize_artifact",
    "planned_artifact_path",
    "run_postgres_restore_rehearsal",
]
