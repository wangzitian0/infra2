"""Tests for backup inventory and freshness verification."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from libs.backup_verification import (
    BackupEntry,
    BackupManifestError,
    build_backup_alert_payload,
    discover_deployer_data_paths,
    inventory_data_paths,
    load_backup_inventory,
    _parse_timestamp,
    verify_backup_manifest,
)
from libs.backup_restore import (
    BackupRestoreError,
    assert_manifest_is_rehearsable,
    assert_rehearsal_target,
    build_postgres_rehearsal_plan,
    latest_artifact_for_service,
    materialize_artifact,
)


ROOT = Path(__file__).resolve().parents[2]


def _load_backup_runner():
    path = ROOT / "tools/backup_runner.py"
    spec = importlib.util.spec_from_file_location("backup_runner_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_backup_verification_tool():
    path = ROOT / "tools/backup_verification.py"
    spec = importlib.util.spec_from_file_location("backup_verification_tool", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_inventory_covers_deployer_data_paths() -> None:
    """#158: every deployer-owned DATA_PATH has backup inventory coverage."""
    discovered = discover_deployer_data_paths()
    inventory = inventory_data_paths(load_backup_inventory())

    missing = {
        service_id: data_path
        for service_id, data_path in discovered.items()
        if service_id not in inventory
    }
    assert missing == {}


def test_backup_manifest_requires_fresh_off_host_artifacts() -> None:
    """#158/#162: stale, empty, or local artifacts fail loudly."""
    entry = load_backup_inventory()[0]
    now = 1_800_000_000
    manifest = {
        "artifacts": [
            {
                "service_id": entry.service_id,
                "created_at": now - 3600,
                "size_bytes": 1024,
                "sha256": "a" * 64,
                "remote_uri": f"{entry.remote}:infra2/{entry.service_id}.tar.zst",
            }
        ]
    }

    report = verify_backup_manifest([entry], manifest, now=now)

    assert report["status"] == "pass"
    assert report["checks"][0]["summary"] == "backup artifact is fresh and verifiable"

    stale = {
        "artifacts": [
            {
                "service_id": entry.service_id,
                "created_at": now - (entry.rpo_hours + 1) * 3600,
                "size_bytes": 1024,
                "sha256": "a" * 64,
                "remote_uri": f"{entry.remote}:infra2/{entry.service_id}.tar.zst",
            }
        ]
    }
    failed = verify_backup_manifest([entry], stale, now=now)

    assert failed["status"] == "fail"
    assert failed["checks"][0]["summary"] == "backup artifact is stale"


def test_backup_manifest_invalid_timestamp_becomes_failed_check() -> None:
    """#158: malformed manifests fail loudly without crashing verification."""
    entry = load_backup_inventory()[0]
    report = verify_backup_manifest(
        [entry],
        {
            "artifacts": [
                {
                    "service_id": entry.service_id,
                    "created_at": "not-a-date",
                    "size_bytes": 1024,
                    "sha256": "a" * 64,
                    "remote_uri": f"{entry.remote}:infra2/{entry.service_id}.tar.gz",
                }
            ]
        },
        now=1_800_000_000,
    )

    assert report["status"] == "fail"
    assert report["checks"][0]["summary"] == "backup artifact timestamp is invalid"
    assert "invalid created_at timestamp" in report["checks"][0]["evidence"]["error"]


def test_parse_timestamp_has_clear_error_for_empty_values() -> None:
    with pytest.raises(BackupManifestError, match="created_at must be"):
        _parse_timestamp("")


def test_backup_failures_build_alert_payload() -> None:
    entry = load_backup_inventory()[0]
    report = verify_backup_manifest([entry], {"artifacts": []}, now=1_800_000_000)

    payload = build_backup_alert_payload(report)

    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "InfraBackupVerificationFailed"
    assert payload["alerts"][0]["labels"]["service"] == entry.service_id


def test_backup_runner_creates_archive_and_checksum(tmp_path) -> None:
    """#158: backup runner can produce manifest-ready artifacts."""
    backup_runner = _load_backup_runner()
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("important", encoding="utf-8")
    entry = BackupEntry(
        service_id="test/service",
        data_path=str(source),
        method="filesystem_archive",
        restore_command="restore",
        remote="r2",
        retention_days=30,
        rpo_hours=24,
    )

    archive = backup_runner._archive_entry(entry, tmp_path / "out", 1_800_000_000)
    digest = backup_runner._sha256(archive)

    assert archive.exists()
    assert archive.stat().st_size > 0
    assert len(digest) == 64


def test_backup_verification_cli_uses_current_time_not_manifest_verified_at(
    tmp_path, monkeypatch
) -> None:
    """#162: later verification must not reuse stale manifest verified_at."""
    tool = _load_backup_verification_tool()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"verified_at": 123, "artifacts": []}',
        encoding="utf-8",
    )
    captured = {}

    monkeypatch.setattr(tool, "load_backup_inventory", lambda: [])
    monkeypatch.setattr(tool.time, "time", lambda: 456)

    def fake_verify(entries, manifest, *, now):
        captured["now"] = now
        return {"status": "pass", "checks": []}

    monkeypatch.setattr(tool, "verify_backup_manifest", fake_verify)
    monkeypatch.setattr(
        "sys.argv", ["backup_verification.py", "--manifest", str(manifest_path)]
    )

    assert tool.main() == 0
    assert captured["now"] == 456


def test_backup_restore_rehearsal_requires_verified_off_host_manifest(tmp_path) -> None:
    """Infra-011.17 / #945: restore rehearsal consumes only fresh off-host artifacts."""
    entry = BackupEntry(
        service_id="finance_report/postgres",
        data_path="/data/finance_report/postgres",
        method="pg_dump_plus_data_archive",
        restore_command="restore latest finance_report pg_dump",
        remote="r2",
        retention_days=30,
        rpo_hours=24,
    )
    now = 1_800_000_000
    manifest = {
        "artifacts": [
            {
                "service_id": entry.service_id,
                "created_at": now - 60,
                "size_bytes": 2048,
                "sha256": "a" * 64,
                "remote_uri": "r2:infra2/finance_report/postgres/dump.sql.gz",
                "method": "pg_dumpall_gz",
            }
        ]
    }

    artifact = assert_manifest_is_rehearsable(entry, manifest, now=now)

    assert artifact["remote_uri"].startswith("r2:")

    local_manifest = {
        "artifacts": [
            {
                **manifest["artifacts"][0],
                "remote_uri": f"local:{tmp_path / 'dump.sql.gz'}",
            }
        ]
    }
    with pytest.raises(BackupRestoreError, match="not off-host"):
        assert_manifest_is_rehearsable(entry, local_manifest, now=now)


def test_backup_restore_rehearsal_downloads_remote_artifact(tmp_path) -> None:
    """Infra-011.17 / #945: remote artifacts are materialized with rclone copyto."""
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        calls.append(cmd)
        return Result()

    archive = materialize_artifact(
        {"remote_uri": "r2:infra2/finance_report/postgres/dump.sql.gz"},
        tmp_path,
        runner=fake_run,
    )

    assert archive == tmp_path / "dump.sql.gz"
    assert calls == [
        [
            "rclone",
            "copyto",
            "r2:infra2/finance_report/postgres/dump.sql.gz",
            str(tmp_path / "dump.sql.gz"),
        ]
    ]


def test_backup_restore_rehearsal_refuses_live_looking_targets(tmp_path) -> None:
    """Infra-011.17 / #945: real backup restores require a throwaway target."""
    with pytest.raises(BackupRestoreError, match="target container"):
        assert_rehearsal_target("finance_report-postgres")

    assert_rehearsal_target("finance_report-postgres-restore-rehearsal")

    entry = BackupEntry(
        service_id="finance_report/postgres",
        data_path="/data/finance_report/postgres",
        method="pg_dump_plus_data_archive",
        restore_command="restore",
        remote="r2",
        retention_days=30,
        rpo_hours=24,
    )
    archive = tmp_path / "dump.sql.gz"
    archive.write_bytes(b"fake")

    plan = build_postgres_rehearsal_plan(
        entry=entry,
        artifact={
            "remote_uri": "r2:infra2/finance_report/postgres/dump.sql.gz",
            "method": "pg_dumpall_gz",
        },
        archive_path=archive,
        target_container="finance_report-postgres-restore-rehearsal",
    )

    assert plan.service_id == "finance_report/postgres"
    assert plan.target_container == "finance_report-postgres-restore-rehearsal"
    assert "SELECT count(*) >= 1 FROM pg_database" in plan.invariant_sql


def test_backup_restore_rehearsal_selects_latest_artifact() -> None:
    """Infra-011.17 / #945: rehearsal uses the latest artifact for a service."""
    artifact = latest_artifact_for_service(
        {
            "artifacts": [
                {
                    "service_id": "finance_report/postgres",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "service_id": "finance_report/postgres",
                    "created_at": "2026-01-02T00:00:00Z",
                },
                {
                    "service_id": "platform/postgres",
                    "created_at": "2026-01-03T00:00:00Z",
                },
            ]
        },
        "finance_report/postgres",
    )

    assert artifact["created_at"] == "2026-01-02T00:00:00Z"
