"""Unit tests for tools/run_restore_rehearsal.py.

Asserts docker command structure, network isolation, invariant thresholds,
teardown safety, and CLI entry point without requiring Docker or a live host.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.run_restore_rehearsal import main, run_rehearsal


@pytest.fixture
def mock_manifest_file(tmp_path: Path) -> Path:
    now_ts = int(time.time())
    manifest_data = {
        "schema_version": 1,
        "generated_at": now_ts,
        "verified_at": now_ts,
        "artifacts": [
            {
                "service_id": "finance_report/postgres",
                "created_at": now_ts,
                "size_bytes": 1024,
                "sha256": "0123456789abcdef" * 4,
                "remote_uri": "gdrive-backup:infra2/weekly/20260921T033000Z/finance_report/postgres/dump.sql.gz",
                "method": "pg_dumpall_gz",
            }
        ],
    }
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")
    return manifest_file


def test_run_rehearsal_docker_args_and_invariants(
    tmp_path: Path, mock_manifest_file: Path
) -> None:
    dl_dir = tmp_path / "infra2-backup-restore-rehearsal"
    fake_archive = dl_dir / "dump.sql.gz"
    dl_dir.mkdir(parents=True, exist_ok=True)
    fake_archive.write_bytes(b"dummy sql")

    captured_cmds: list[list[str]] = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        mock_res = MagicMock()
        if "psql" in cmd:
            mock_res.stdout = "42\n"
        else:
            mock_res.stdout = "ready\n"
        mock_res.returncode = 0
        return mock_res

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("tools.run_restore_rehearsal.wait_for_postgres"),
        patch(
            "tools.run_restore_rehearsal.materialize_artifact",
            return_value=fake_archive,
        ),
        patch(
            "tools.run_restore_rehearsal.run_postgres_restore_rehearsal"
        ) as mock_restore,
    ):
        report = run_rehearsal(
            manifest_path=str(mock_manifest_file),
            service_id="finance_report/postgres",
            database="finance_report",
            download_dir=str(dl_dir),
            keep_container=False,
        )

        assert report["status"] == "PASS"
        assert report["service_id"] == "finance_report/postgres"
        assert report["teardown"] is True

        # Assert docker run command has network isolation and trust auth
        docker_run_cmds = [
            cmd
            for cmd in captured_cmds
            if len(cmd) > 2 and cmd[0] == "docker" and cmd[1] == "run"
        ]
        assert len(docker_run_cmds) == 1
        run_cmd = docker_run_cmds[0]
        assert "--network=none" in run_cmd
        assert "POSTGRES_HOST_AUTH_METHOD=trust" in " ".join(run_cmd)
        assert "--memory=1g" in run_cmd
        assert "--cpus=1" in run_cmd

        # Assert plan invariants contain enforceable thresholds
        assert mock_restore.call_count == 1
        plan = mock_restore.call_args[0][0]
        assert len(plan.invariant_sql) == 5
        invariants = " ".join(plan.invariant_sql)
        # Thresholds are tuned per service, so assert the property that matters
        # rather than the numbers: every floor must actually be enforceable and
        # above 1, since a `< 1` floor only proves the database is not empty.
        floors = [int(m) for m in re.findall(r"< (\d+) THEN RAISE EXCEPTION", invariants)]
        assert len(floors) >= 2
        assert all(floor > 1 for floor in floors), f"tautological floor in {floors}"
        assert "unexpected alembic_version" in invariants

        # Assert container teardown
        rm_cmds = [
            cmd
            for cmd in captured_cmds
            if len(cmd) > 2 and cmd[0] == "docker" and cmd[1] == "rm"
        ]
        assert any("-f" in cmd for cmd in rm_cmds)


def test_teardown_safety_guard_rejects_arbitrary_path(
    tmp_path: Path, mock_manifest_file: Path
) -> None:
    unsafe_dir = Path("/etc/cron.d")  # should never be wiped
    fake_archive = tmp_path / "dump.sql.gz"
    fake_archive.write_bytes(b"dummy")

    with (
        patch("subprocess.run", return_value=MagicMock(stdout="0\n", returncode=0)),
        patch("tools.run_restore_rehearsal.wait_for_postgres"),
        patch(
            "tools.run_restore_rehearsal.materialize_artifact",
            return_value=fake_archive,
        ),
        patch("tools.run_restore_rehearsal.run_postgres_restore_rehearsal"),
        patch("shutil.rmtree") as mock_rmtree,
    ):
        run_rehearsal(
            manifest_path=str(mock_manifest_file),
            service_id="finance_report/postgres",
            database="finance_report",
            download_dir=str(unsafe_dir),
            keep_container=False,
        )
        # rmtree must NOT be called for unsafe_dir
        mock_rmtree.assert_not_called()


def test_main_cli_success(
    mock_manifest_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_report = {
        "status": "PASS",
        "service_id": "finance_report/postgres",
        "database": "finance_report",
        "duration_seconds": 1.2,
    }
    with (
        patch("tools.run_restore_rehearsal.run_rehearsal", return_value=fake_report),
        patch(
            "sys.argv",
            ["run_restore_rehearsal.py", "--manifest", str(mock_manifest_file)],
        ),
    ):
        exit_code = main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "RESTORE_PROOF: PASS" in captured.out
