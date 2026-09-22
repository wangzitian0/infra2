"""Infra2 Backup Rehearsal SSOT."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libs.backup_restore import (
    BackupRestoreError,
    RestoreRehearsalPlan,
    assert_rehearsal_target,
    build_postgres_rehearsal_plan,
    materialize_artifact,
    run_postgres_restore_rehearsal,
)
from libs.backup_verification import BackupEntry


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
    "assert_rehearsal_target",
    "create_rehearsal_plan",
    "execute_rehearsal",
    "materialize_artifact",
]
