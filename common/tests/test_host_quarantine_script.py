"""Unit tests for bootstrap/01.dokploy_install/host_guard/host_quarantine.sh (#1373)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bootstrap/01.dokploy_install/host_guard/host_quarantine.sh"

BASE_EPOCH = 1761523200  # 2025-10-27T00:00:00Z
TTL_SECONDS = 15 * 86400


def _run_quarantine(
    tmp_path: Path,
    *args: str,
    batch_id: str = "20261027T060000Z",
    now_epoch: int = BASE_EPOCH,
    crontab_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "INFRA2_QUARANTINE_ROOT_PREFIX": str(tmp_path),
        "INFRA2_QUARANTINE_DIR": str(tmp_path / "data/backups/quarantine"),
        "INFRA2_QUARANTINE_TTL_DAYS": "15",
        "INFRA2_QUARANTINE_BATCH_ID": batch_id,
        "INFRA2_QUARANTINE_NOW_EPOCH": str(now_epoch),
    }
    if crontab_file is not None:
        env["INFRA2_CRONTAB_FILE"] = str(crontab_file)
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _seed_host_litter(tmp_path: Path) -> dict[str, bytes]:
    """Create representative litter items across all categories and return file payloads."""
    files: dict[str, bytes] = {
        "root/ + .Destination)": b"escaped-destination-parens\n",
        "root/ .Destination}}{{end}}": b"escaped-destination-braces\n",
        "root/None/stale.txt": b"unexpanded-deployer-path\n",
        "root/backups/truealpha-prod-backup.sh": b"#!/bin/sh\npg_dump truealpha\n",
        "root/openclaw-discord-data-final-20260915T104201Z.tgz": b"openclaw-tgz-bytes",
        "zookeeper/myid": b"1\n",
        "zookeeper/version-2/log.1": b"zk-tx-log-10b",
        "data/finance/wealthfolio.db": b"sqlite-wealthfolio-bytes",
        "tmp/pgtest.hehE.sh": b"#!/bin/sh\necho debug\n",
        "usr/local/sbin/tianclaw-vps-disk-maintenance.sh": b"#!/bin/sh\nrm -rf /tmp/old\n",
    }
    for rel, payload in files.items():
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(payload)

    f3cnn_dir = tmp_path / "tmp/F3CNN"
    f3cnn_dir.mkdir(parents=True, exist_ok=True)
    f3cnn_dir.chmod(0o777)
    lock_file = f3cnn_dir / "opend.lock"
    lock_file.write_bytes(b"live-opend-lock")
    lock_file.chmod(0o666)
    return files


def test_invalid_flag_exits_2(tmp_path: Path) -> None:
    res = _run_quarantine(tmp_path, "--invalid-flag")
    assert res.returncode == 2
    assert "usage:" in res.stderr


def test_check_detects_litter_cron_and_f3cnn_without_mutation(tmp_path: Path) -> None:
    _seed_host_litter(tmp_path)
    cron_file = tmp_path / "root.crontab"
    cron_Text = (
        "0 3 * * * /usr/local/sbin/infra2-host-backup.sh\n"
        "30 2 * * * /root/backups/truealpha-prod-backup.sh\n"
        "15 4 * * * /usr/local/sbin/tianclaw-vps-disk-maintenance.sh\n"
        "# 0 1 * * * /root/backups/truealpha-prod-backup.sh --already-commented\n"
    )
    cron_file.write_text(cron_Text, encoding="utf-8")

    res = _run_quarantine(tmp_path, "--check", crontab_file=cron_file)
    assert res.returncode == 0, res.stderr

    out = res.stdout
    assert "FOUND 27 /root/ + .Destination) (oct19_ssh_inspect_escape)" in out
    assert "FOUND 27 /root/ .Destination}}{{end}} (oct19_ssh_inspect_escape)" in out
    assert "FOUND 25 /root/None (unexpanded_deployer_data_path)" in out
    assert "FOUND 15 /zookeeper (unexpanded_data_path_mount)" in out
    assert "FOUND 24 /data/finance (retired_wealthfolio_data_dir)" in out
    assert (
        "PERMISSION_DRIFT /tmp/F3CNN (files wider than 0600; live OpenD lock dir, do not move)"
        in out
    )
    assert "STALE_CRON 30 2 * * * /root/backups/truealpha-prod-backup.sh" in out
    assert (
        "STALE_CRON 15 4 * * * /usr/local/sbin/tianclaw-vps-disk-maintenance.sh" in out
    )
    assert "infra2-host-backup.sh" not in out
    assert "--already-commented" not in out

    # Verify zero mutation during --check
    assert (tmp_path / "root/ + .Destination)").exists()
    assert (tmp_path / "root/ .Destination}}{{end}}").exists()
    assert (tmp_path / "zookeeper/myid").exists()
    assert not (tmp_path / "data/backups/quarantine").exists()
    assert cron_file.read_text(encoding="utf-8") == cron_Text
    assert ((tmp_path / "tmp/F3CNN/opend.lock").stat().st_mode & 0o777) == 0o666


def test_stage_moves_litter_records_manifest_updates_cron_and_tightens_f3cnn(
    tmp_path: Path,
) -> None:
    seeded = _seed_host_litter(tmp_path)
    cron_file = tmp_path / "root.crontab"
    cron_file.write_text(
        "0 3 * * * /usr/local/sbin/infra2-host-backup.sh\n"
        "30 2 * * * /root/backups/truealpha-prod-backup.sh\n"
        "0 5 * * 0 /usr/local/bin/run_restore_rehearsal.py\n"
        "15 4 * * * /usr/local/sbin/tianclaw-vps-disk-maintenance.sh\n",
        encoding="utf-8",
    )

    batch_id = "20261027T060000Z"
    res = _run_quarantine(
        tmp_path,
        "--stage",
        batch_id=batch_id,
        now_epoch=BASE_EPOCH,
        crontab_file=cron_file,
    )
    assert res.returncode == 0, res.stderr

    quarantine_dir = tmp_path / "data/backups/quarantine"
    batch_dir = quarantine_dir / batch_id

    # Original paths are moved into quarantine/<batch-id>/files/<rel_path>
    for rel, expected_bytes in seeded.items():
        assert not (tmp_path / rel).exists()
        moved_file = batch_dir / "files" / rel
        assert moved_file.exists()
        assert moved_file.read_bytes() == expected_bytes

    # Manifest has valid JSONL entries with 15-day retention epoch and sha256
    manifest_path = quarantine_dir / "manifest.jsonl"
    assert manifest_path.exists()
    records = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 9
    by_orig = {r["original_path"]: r for r in records}
    assert "/root/ + .Destination)" in by_orig
    assert "/root/ .Destination}}{{end}}" in by_orig
    assert "/zookeeper" in by_orig
    assert "/data/finance" in by_orig

    escape_rec = by_orig["/root/ + .Destination)"]
    assert escape_rec["batch_id"] == batch_id
    assert escape_rec["quarantined_at_epoch"] == BASE_EPOCH
    assert escape_rec["purge_after_epoch"] == BASE_EPOCH + TTL_SECONDS
    assert escape_rec["status"] == "quarantined"
    assert (
        escape_rec["sha256"]
        == hashlib.sha256(b"escaped-destination-parens\n").hexdigest()
    )
    assert by_orig["/zookeeper"]["sha256"] == "directory"

    # Crontab backup and selective commenting
    assert (batch_dir / "crontab.before").exists()
    updated_cron = cron_file.read_text(encoding="utf-8").splitlines()
    assert "0 3 * * * /usr/local/sbin/infra2-host-backup.sh" in updated_cron
    assert "0 5 * * 0 /usr/local/bin/run_restore_rehearsal.py" in updated_cron
    commented_truealpha = [
        line for line in updated_cron if "truealpha-prod-backup.sh" in line
    ]
    assert len(commented_truealpha) == 1
    assert commented_truealpha[0].startswith("# [quarantined ")
    assert f"batch={batch_id}" in commented_truealpha[0]

    # /tmp/F3CNN remains in place with tightened permissions (dir 0700, file 0600)
    f3cnn_dir = tmp_path / "tmp/F3CNN"
    lock_file = f3cnn_dir / "opend.lock"
    assert lock_file.exists()
    assert lock_file.read_bytes() == b"live-opend-lock"
    assert (f3cnn_dir.stat().st_mode & 0o777) == 0o700
    assert (lock_file.stat().st_mode & 0o777) == 0o600

    # Subsequent --check is completely clean
    check_after = _run_quarantine(tmp_path, "--check", crontab_file=cron_file)
    assert check_after.returncode == 0
    assert check_after.stdout.strip() == ""

    # --list prints manifest records
    list_res = _run_quarantine(tmp_path, "--list")
    assert list_res.returncode == 0
    assert len(list_res.stdout.strip().splitlines()) == 9

    # Idempotent re-run of --stage leaves no empty second batch directory
    second_batch = "20261027T070000Z"
    rerun_res = _run_quarantine(
        tmp_path,
        "--stage",
        batch_id=second_batch,
        now_epoch=BASE_EPOCH + 3600,
        crontab_file=cron_file,
    )
    assert rerun_res.returncode == 0
    assert not (quarantine_dir / second_batch).exists()


def test_purge_expired_enforces_15_day_retention_window(tmp_path: Path) -> None:
    _seed_host_litter(tmp_path)
    batch_id = "20261027T060000Z"
    stage_res = _run_quarantine(
        tmp_path, "--stage", batch_id=batch_id, now_epoch=BASE_EPOCH
    )
    assert stage_res.returncode == 0

    quarantine_dir = tmp_path / "data/backups/quarantine"
    batch_dir = quarantine_dir / batch_id
    manifest_path = quarantine_dir / "manifest.jsonl"

    # 1 second before 15 days: batch MUST NOT be deleted
    early_res = _run_quarantine(
        tmp_path,
        "--purge-expired",
        batch_id=batch_id,
        now_epoch=BASE_EPOCH + TTL_SECONDS - 1,
    )
    assert early_res.returncode == 0
    assert batch_dir.exists()
    early_records = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(r["status"] == "quarantined" for r in early_records)

    # At exactly 15 days: batch directory is purged and manifest status becomes "purged"
    expire_epoch = BASE_EPOCH + TTL_SECONDS
    purge_res = _run_quarantine(
        tmp_path,
        "--purge-expired",
        batch_id=batch_id,
        now_epoch=expire_epoch,
    )
    assert purge_res.returncode == 0
    assert not batch_dir.exists()
    purged_records = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(r["status"] == "purged" for r in purged_records)
    assert all(r["purged_at_epoch"] == expire_epoch for r in purged_records)


def test_second_batch_data_litter_quarantined(tmp_path: Path) -> None:
    """Verify second batch data litter entries are detected and staged."""
    files: dict[str, bytes] = {
        "data/truealpha/ingest/pyvenv.cfg": b"home = /usr/bin\n",
        "data/platform/activepieces/cache.json": b'{"cache": true}\n',
        "data/platform/openpanel-staging/db.sqlite": b"sqlite-openpanel-data\n",
        "data/bootstrap/1password_dbreset_backup_20260609T132132Z/1password.sqlite": b"sqlite-1password-data\n",
    }
    for rel, payload in files.items():
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(payload)

    # Assert --check reports all 4
    check_res = _run_quarantine(tmp_path, "--check")
    assert check_res.returncode == 0, check_res.stderr
    out = check_res.stdout
    assert "/data/truealpha/ingest (legacy_host_ingest_venv)" in out
    assert "/data/platform/activepieces (retired_activepieces_data_dir)" in out
    assert (
        "/data/platform/openpanel-staging (prod_only_service_staging_leftover)" in out
    )
    assert (
        "/data/bootstrap/1password_dbreset_backup_20260609T132132Z (legacy_1password_upgrade_backup)"
        in out
    )

    # Assert --stage moves all 4 into the quarantine batch directory
    batch_id = "20261027T080000Z"
    stage_res = _run_quarantine(tmp_path, "--stage", batch_id=batch_id)
    assert stage_res.returncode == 0, stage_res.stderr

    quarantine_dir = tmp_path / "data/backups/quarantine"
    batch_dir = quarantine_dir / batch_id
    for rel, payload in files.items():
        assert not (tmp_path / rel).exists()
        moved = batch_dir / "files" / rel
        assert moved.exists()
        assert moved.read_bytes() == payload
