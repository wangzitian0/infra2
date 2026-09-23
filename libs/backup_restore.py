"""Backward-compatibility shim — the implementation lives in `libs.backup.rehearsal`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.backup.rehearsal import (
    BackupRestoreError,
    RehearsalSpecification,
    RestoreRehearsalPlan,
    assert_manifest_is_rehearsable,
    assert_rehearsal_target,
    build_postgres_rehearsal_plan,
    create_rehearsal_plan,
    execute_rehearsal,
    latest_artifact_for_service,
    materialize_artifact,
    planned_artifact_path,
    run_postgres_restore_rehearsal,
)

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
