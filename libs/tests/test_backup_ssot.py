"""SSOT invariants for backup architecture and host projection."""

from __future__ import annotations

from pathlib import Path

from libs.backup.emitter import (
    emit_backup_targets,
    emit_runner_lines,
)
from libs.backup.verification import load_backup_inventory

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_host_backup_script_has_zero_hardcoded_services() -> None:
    """The host backup runner must not contain hardcoded service identifiers."""
    script_path = REPO_ROOT / "tools/host_backup.sh"
    content = script_path.read_text(encoding="utf-8")

    assert "SERVICES=$(cat <<EOF" not in content
    assert "platform/postgres|pg|" not in content
    assert "platform/s3|path|" not in content
    assert "libs.backup.emitter" in content


def test_emitter_covers_entire_backup_inventory() -> None:
    """The emitter must cover every declared BackupFacet entry."""
    inventory = load_backup_inventory()
    inventory_ids = {entry.service_id for entry in inventory}

    targets = emit_backup_targets(inventory)
    target_ids = {target.service_id for target in targets}

    assert target_ids == inventory_ids
    assert len(targets) == len(inventory)


def test_emitter_preserves_safety_invariant_618_order() -> None:
    """Logical database dumps run first; busy path archives run last."""
    targets = emit_backup_targets()
    kinds = [target.kind for target in targets]

    # All pg dumps appear before any redis dump
    pg_indices = [i for i, k in enumerate(kinds) if k == "pg"]
    redis_indices = [i for i, k in enumerate(kinds) if k == "redis"]
    path_indices = [i for i, k in enumerate(kinds) if k == "path"]

    assert max(pg_indices) < min(redis_indices)
    assert max(redis_indices) < min(path_indices)

    # platform/s3 must be the last target
    assert targets[-1].service_id == "platform/s3"


def test_dokploy_adapter_handles_staging_suffix() -> None:
    """Bootstrap services share state; platform/app services receive env suffix."""
    targets = {
        t.service_id: t
        for t in emit_backup_targets(environment="staging", data_root="/data")
    }

    # Bootstrap services must not have -staging suffix
    assert targets["bootstrap/1password"].primary_target == "/data/bootstrap/1password"
    assert targets["bootstrap/vault"].primary_target == "/data/bootstrap/vault"
    assert (
        targets["bootstrap/iac_runner"].primary_target == "/data/bootstrap/iac-runner"
    )

    # Database containers must have -staging suffix
    assert targets["platform/postgres"].primary_target == "platform-postgres-staging"
    assert (
        targets["finance_report/postgres"].primary_target
        == "finance_report-postgres-staging"
    )
    assert targets["truealpha/postgres"].primary_target == "truealpha-postgres-staging"

    # Redis containers and paths must have -staging suffix
    assert targets["platform/redis"].primary_target == "platform-redis-staging"
    assert targets["platform/redis"].secondary_target == "/data/platform/redis-staging"

    # Path services must have -staging suffix
    assert targets["platform/s3"].primary_target == "/data/platform/minio-staging"
    assert (
        targets["platform/alerting"].primary_target == "/data/platform/alerting-staging"
    )


def test_runner_lines_format() -> None:
    """Generated lines must conform to the runner pipe-delimited format."""
    lines = emit_runner_lines()
    assert len(lines) == 17
    for line in lines:
        parts = line.split("|")
        assert len(parts) in (3, 4)
        service_id, kind, primary = parts[0], parts[1], parts[2]
        assert "/" in service_id
        assert kind in ("pg", "redis", "path")
        assert primary
