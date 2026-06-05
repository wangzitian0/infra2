"""Tests for backup inventory and freshness verification."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from libs.backup_verification import (
    BackupEntry,
    build_backup_alert_payload,
    discover_deployer_data_paths,
    inventory_data_paths,
    load_backup_inventory,
    verify_backup_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


def _load_backup_runner():
    path = ROOT / "tools/backup_runner.py"
    spec = importlib.util.spec_from_file_location("backup_runner_under_test", path)
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
