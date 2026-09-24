"""Unit tests for tools/run_restore_rehearsal.py.

Asserts docker command structure, network isolation, invariant thresholds,
teardown safety, and CLI entry point without requiring Docker or a live host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.run_restore_rehearsal import main, run_rehearsal, wait_for_postgres


def test_wait_for_postgres_ignores_temporary_init_server() -> None:
    """The image's init server can pass pg_isready before the final server starts."""
    log_reads = 0
    readiness_checks = 0

    def fake_run(cmd, *args, **kwargs):
        nonlocal log_reads, readiness_checks
        result = MagicMock(returncode=0)
        if cmd[:2] == ["docker", "logs"]:
            log_reads += 1
            result.stderr = ""
            result.stdout = (
                "PostgreSQL init process complete; ready for start up."
                if log_reads >= 2
                else "database system is ready to accept connections"
            )
        elif cmd[:2] == ["docker", "exec"]:
            assert log_reads >= 2, "checked readiness against the temporary server"
            readiness_checks += 1
            result.returncode = 0 if readiness_checks >= 2 else 2
        return result

    with (
        patch("tools.run_restore_rehearsal.subprocess.run", side_effect=fake_run),
        patch("tools.run_restore_rehearsal.time.sleep"),
    ):
        wait_for_postgres("infra022-restore-rehearsal", "postgres", timeout=5)

    assert log_reads >= 2
    assert readiness_checks == 2


@pytest.fixture
def mock_manifest_file(tmp_path: Path) -> Path:
    now_ts = int(time.time())
    manifest_data = {
        "schema_version": 1,
        "environment": "production",
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


def test_rehearsal_rejects_staging_manifest_before_starting_container(
    mock_manifest_file: Path,
) -> None:
    manifest = json.loads(mock_manifest_file.read_text(encoding="utf-8"))
    manifest["environment"] = "staging"
    mock_manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    with patch("subprocess.run") as run_command:
        with pytest.raises(ValueError, match="expected 'production'"):
            run_rehearsal(manifest_path=str(mock_manifest_file))
    run_command.assert_not_called()


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
        floors = [
            int(m) for m in re.findall(r"< (\d+) THEN RAISE EXCEPTION", invariants)
        ]
        assert len(floors) >= 2
        assert all(floor > 1 for floor in floors), f"tautological floor in {floors}"
        assert "unexpected alembic_version" in invariants
        assert "0065_user_soft_delete" in invariants

        # Assert container teardown
        rm_cmds = [
            cmd
            for cmd in captured_cmds
            if len(cmd) > 2 and cmd[0] == "docker" and cmd[1] == "rm"
        ]
        assert len(rm_cmds) == 1
        assert "-f" in rm_cmds[0] and "-v" in rm_cmds[0]


def test_failed_sandbox_start_removes_container_and_volume(
    mock_manifest_file: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError):
            run_rehearsal(manifest_path=str(mock_manifest_file))

    assert len([c for c in commands if c[:2] == ["docker", "run"]]) == 1
    cleanup = [c for c in commands if c[:2] == ["docker", "rm"]]
    assert len(cleanup) == 1
    assert "-f" in cleanup[0] and "-v" in cleanup[0]


def test_failed_cleanup_is_reported_even_when_start_fails(
    mock_manifest_file: Path,
) -> None:
    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:2] == ["docker", "rm"]:
            return MagicMock(returncode=1, stderr="removal failed")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="removal failed"):
            run_rehearsal(manifest_path=str(mock_manifest_file))


def test_start_failure_before_container_exists_keeps_original_error(
    mock_manifest_file: Path,
) -> None:
    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.CalledProcessError(125, cmd)
        if cmd[:2] == ["docker", "rm"]:
            return MagicMock(returncode=1, stderr="No such container")
        if cmd[:3] == ["docker", "container", "inspect"]:
            return MagicMock(returncode=1)
        raise AssertionError(cmd)

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            run_rehearsal(manifest_path=str(mock_manifest_file))

    assert exc.value.returncode == 125


def test_failed_restore_respects_keep_container(
    tmp_path: Path, mock_manifest_file: Path
) -> None:
    archive = tmp_path / "dump.sql.gz"
    archive.write_bytes(b"dump")
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        return MagicMock(returncode=0)

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("tools.run_restore_rehearsal.wait_for_postgres"),
        patch("tools.run_restore_rehearsal.materialize_artifact", return_value=archive),
        patch(
            "tools.run_restore_rehearsal.run_postgres_restore_rehearsal",
            side_effect=RuntimeError("restore failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="restore failed"):
            run_rehearsal(
                manifest_path=str(mock_manifest_file), keep_container=True
            )

    assert not any(cmd[:2] == ["docker", "rm"] for cmd in commands)


def test_sequential_runs_use_separate_download_directories(
    tmp_path: Path, mock_manifest_file: Path
) -> None:
    archive = tmp_path / "dump.sql.gz"
    archive.write_bytes(b"dump")
    download_root = tmp_path / "infra2-backup-restore-rehearsal"

    def fake_run(cmd, *args, **kwargs):
        return MagicMock(returncode=0, stdout="42\n")

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("tools.run_restore_rehearsal.wait_for_postgres"),
        patch(
            "tools.run_restore_rehearsal.materialize_artifact",
            return_value=archive,
        ) as materialize,
        patch("tools.run_restore_rehearsal.run_postgres_restore_rehearsal"),
    ):
        for _ in range(2):
            run_rehearsal(
                manifest_path=str(mock_manifest_file),
                download_dir=str(download_root),
            )

    directories = [call.args[1] for call in materialize.call_args_list]
    assert len(directories) == 2
    assert directories[0] != directories[1]
    assert all(path.parent == download_root for path in directories)


def test_teardown_safety_guard_rejects_arbitrary_path(
    tmp_path: Path, mock_manifest_file: Path
) -> None:
    unsafe_dir = Path("/etc/cron.d")  # should never be wiped
    with (
        patch("subprocess.run") as run_command,
        patch("shutil.rmtree") as mock_rmtree,
    ):
        with pytest.raises(ValueError, match="unsafe download_dir"):
            run_rehearsal(
                manifest_path=str(mock_manifest_file),
                service_id="finance_report/postgres",
                database="finance_report",
                download_dir=str(unsafe_dir),
                keep_container=False,
            )
        run_command.assert_not_called()
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


def test_cron_invocation_starts_in_a_fresh_interpreter() -> None:
    """The VPS cron runs this file with ``PYTHONPATH=.`` from the repo root (#892).

    The repo's top-level ``platform/`` package then shadows the stdlib module, and
    on Linux ``uuid`` calls ``platform.system()`` at import time. pytest has already
    loaded the real ``platform`` in this process, so only a fresh interpreter shows
    the crash; it is Linux-only because CPython skips that import on darwin/win32.
    The cron sets no PYTHONSAFEPATH, so the CI job's value is dropped here.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    result = subprocess.run(
        [
            sys.executable,
            "tools/run_restore_rehearsal.py",
            "--service-id",
            "all",
            "--help",
        ],
        cwd=repo_root,
        env={**env, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "--service-id" in result.stdout


def test_run_restore_rehearsal_rejects_unsafe_database_name() -> None:
    from tools.run_restore_rehearsal import run_rehearsal

    with pytest.raises(ValueError, match="Invalid database name"):
        run_rehearsal(
            manifest_path="/dummy/manifest.json",
            service_id="finance_report/postgres",
            database="finance_report'; DROP TABLE accounts; --",
        )

    with pytest.raises(ValueError, match="Invalid database name: ''"):
        run_rehearsal(
            manifest_path="/dummy/manifest.json",
            service_id="finance_report/postgres",
            database="",
        )


def test_rehearsal_all_attempts_every_service_when_one_fails(
    monkeypatch, capsys
) -> None:
    """One failing service never stops the others (#618)."""
    import tools.run_restore_rehearsal as rrr

    attempted: list[str] = []

    def fake_run_rehearsal(
        *, manifest_path, environment, service_id, database, keep_container
    ):
        assert environment == "production"
        assert manifest_path == "/data/backups/infra2/production-manifest.json"
        attempted.append(service_id)
        if service_id == "finance_report/postgres":
            raise RuntimeError("sandbox container failed to become ready")
        return {"status": "PASS", "service_id": service_id}

    monkeypatch.setattr(rrr, "run_rehearsal", fake_run_rehearsal)
    monkeypatch.setattr("sys.argv", ["run_restore_rehearsal", "--service-id", "all"])

    rc = rrr.main()

    assert attempted == ["finance_report/postgres", "truealpha/postgres"]
    assert rc == 1
    out = capsys.readouterr().out
    assert "finance_report/postgres" in out
    assert "RESTORE_PROOF: FAIL" in out
    assert "RESTORE_PROOF: PASS" in out


def test_rehearsal_all_survives_a_malformed_report(monkeypatch, capsys) -> None:
    """A report missing 'status' must not abort the services that follow it."""
    import tools.run_restore_rehearsal as rrr

    attempted: list[str] = []

    def fake_run_rehearsal(
        *, manifest_path, environment, service_id, database, keep_container
    ):
        assert environment == "production"
        assert manifest_path == "/data/backups/infra2/production-manifest.json"
        attempted.append(service_id)
        if service_id == "finance_report/postgres":
            return {"service_id": service_id}  # no "status" key
        return {"status": "PASS", "service_id": service_id}

    monkeypatch.setattr(rrr, "run_rehearsal", fake_run_rehearsal)
    monkeypatch.setattr("sys.argv", ["run_restore_rehearsal", "--service-id", "all"])

    rc = rrr.main()

    assert attempted == ["finance_report/postgres", "truealpha/postgres"]
    assert rc == 1
    assert "KeyError" in capsys.readouterr().out
