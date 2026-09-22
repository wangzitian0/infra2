"""Unit tests for tools/decommission_activepieces.py (#822).

Enforces Apocalypse Engineering rules:
- Never execute destructive operations without verified cold backup.
- Never drop database when backup is missing, empty (<= 1024 bytes), or invalid schema.
- Never drop database without explicit confirmation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
import pytest

from tools.decommission_activepieces import (
    check_database_exists,
    cleanup_data_path,
    decommission_activepieces,
    drop_database,
    export_database_backup,
    verify_backup_integrity,
)


class MockRunner:
    """Mock process runner recording executed commands."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        cmd_str = " ".join(cmd)

        for pattern, res in self.responses.items():
            if pattern in cmd_str:
                if isinstance(res, Exception):
                    raise res
                return res

        # Default success response
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="1\n", stderr="")

    def has_command_containing(self, substring: str) -> bool:
        return any(substring in " ".join(cmd) for cmd in self.commands)


def test_check_database_exists() -> None:
    runner_exists = MockRunner(
        {"SELECT 1 FROM pg_database": subprocess.CompletedProcess([], 0, "1\n", "")}
    )
    assert check_database_exists("activepieces", runner=runner_exists) is True

    runner_absent = MockRunner(
        {"SELECT 1 FROM pg_database": subprocess.CompletedProcess([], 0, "0\n", "")}
    )
    assert check_database_exists("activepieces", runner=runner_absent) is False


def test_verify_backup_integrity_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.dump"
    with pytest.raises(FileNotFoundError):
        verify_backup_integrity(missing)


def test_verify_backup_integrity_rejects_empty_or_small_file(tmp_path: Path) -> None:
    small_file = tmp_path / "small.dump"
    small_file.write_bytes(b"A" * 512)  # 512 bytes <= 1024 bytes threshold

    with pytest.raises(ValueError, match="minimum threshold"):
        verify_backup_integrity(small_file, min_bytes=1024)


def test_verify_backup_integrity_rejects_invalid_pg_restore_catalog(
    tmp_path: Path,
) -> None:
    fake_dump = tmp_path / "fake.dump"
    fake_dump.write_bytes(b"A" * 2048)  # Size > 1024 bytes

    # Mock pg_restore failing
    runner_fail = MockRunner(
        {"pg_restore -l": subprocess.CompletedProcess([], 1, "", "corrupt header")}
    )
    with pytest.raises(ValueError, match="pg_restore -l verification failed"):
        verify_backup_integrity(fake_dump, runner=runner_fail)

    # Mock pg_restore returning empty catalog
    runner_empty = MockRunner(
        {"pg_restore -l": subprocess.CompletedProcess([], 0, "   \n", "")}
    )
    with pytest.raises(ValueError, match="empty schema catalog"):
        verify_backup_integrity(fake_dump, runner=runner_empty)


def test_verify_backup_integrity_accepts_valid_dump(tmp_path: Path) -> None:
    valid_dump = tmp_path / "valid.dump"
    valid_dump.write_bytes(b"POSTGRES_DUMP_DATA" * 100)  # > 1024 bytes

    runner_ok = MockRunner(
        {
            "pg_restore -l": subprocess.CompletedProcess(
                [], 0, "1; 1259 TABLE public users\n", ""
            )
        }
    )
    assert verify_backup_integrity(valid_dump, runner=runner_ok) is True


def test_drop_database_refuses_without_confirm() -> None:
    runner = MockRunner()
    with pytest.raises(PermissionError, match="requires explicit --confirm"):
        drop_database("activepieces", confirm=False, dry_run=False, runner=runner)

    assert not runner.has_command_containing("DROP DATABASE")


def test_drop_database_skips_in_dry_run() -> None:
    runner = MockRunner()
    result = drop_database("activepieces", confirm=True, dry_run=True, runner=runner)
    assert result is False
    assert not runner.has_command_containing("DROP DATABASE")


def test_decommission_aborts_without_dropping_db_when_unconfirmed(
    tmp_path: Path,
) -> None:
    """Invariant: Unconfirmed decommission must NEVER drop database."""
    runner = MockRunner(
        {
            "SELECT 1 FROM pg_database": subprocess.CompletedProcess([], 0, "1\n", ""),
            "pg_dump": subprocess.CompletedProcess([], 0, b"X" * 2048, ""),
            "pg_restore -l": subprocess.CompletedProcess([], 0, "TABLE test\n", ""),
        }
    )

    with pytest.raises(PermissionError):
        decommission_activepieces(
            confirm=False,
            dry_run=False,
            backup_dir=tmp_path,
            runner=runner,
        )

    # Ensure no DROP DATABASE command was ever executed
    assert not runner.has_command_containing("DROP DATABASE")


def test_decommission_aborts_without_dropping_db_when_backup_is_empty(
    tmp_path: Path,
) -> None:
    """Invariant: Corrupt/empty backup must immediately halt before any destructive action."""
    runner = MockRunner(
        {
            "SELECT 1 FROM pg_database": subprocess.CompletedProcess([], 0, "1\n", ""),
            # pg_dump produces tiny output <= 1024 bytes
            "pg_dump": subprocess.CompletedProcess([], 0, b"TINY", ""),
        }
    )

    with pytest.raises(ValueError, match="minimum threshold"):
        decommission_activepieces(
            confirm=True,
            dry_run=False,
            backup_dir=tmp_path,
            runner=runner,
        )

    assert not runner.has_command_containing("DROP DATABASE")


def test_decommission_aborts_without_dropping_db_when_backup_fails(
    tmp_path: Path,
) -> None:
    """Invariant: Failed backup command halts execution."""
    runner = MockRunner(
        {
            "SELECT 1 FROM pg_database": subprocess.CompletedProcess([], 0, "1\n", ""),
            "pg_dump": subprocess.CompletedProcess([], 1, "", "out of disk space"),
        }
    )

    with pytest.raises(RuntimeError, match="pg_dump failed"):
        decommission_activepieces(
            confirm=True,
            dry_run=False,
            backup_dir=tmp_path,
            runner=runner,
        )

    assert not runner.has_command_containing("DROP DATABASE")


def test_decommission_successful_flow_with_confirmation(tmp_path: Path) -> None:
    """Full happy path with confirmed execution."""
    host_data = tmp_path / "host_activepieces"
    host_data.mkdir()

    runner = MockRunner(
        {
            "SELECT 1 FROM pg_database": subprocess.CompletedProcess([], 0, "1\n", ""),
            "pg_dump": subprocess.CompletedProcess([], 0, b"DUMP_CONTENT" * 150, ""),
            "pg_restore -l": subprocess.CompletedProcess(
                [], 0, "1; TABLE public flows\n", ""
            ),
            "DROP DATABASE": subprocess.CompletedProcess([], 0, "DROP DATABASE\n", ""),
            "rm -rf": subprocess.CompletedProcess([], 0, "", ""),
        }
    )

    res = decommission_activepieces(
        confirm=True,
        dry_run=False,
        backup_dir=tmp_path,
        data_path=host_data,
        runner=runner,
    )

    assert res["db_exists"] is True
    assert res["backup_verified"] is True
    assert res["db_dropped"] is True
    assert res["path_cleaned"] is True
    assert runner.has_command_containing("DROP DATABASE activepieces;")
    assert runner.has_command_containing("rm -rf")


def test_export_database_backup(tmp_path: Path) -> None:
    runner = MockRunner(
        {"pg_dump": subprocess.CompletedProcess([], 0, b"DUMP_DATA", "")}
    )
    out = tmp_path / "dump.dump"
    result_path = export_database_backup("activepieces", out, runner=runner)
    assert result_path.exists()
    assert result_path.read_bytes() == b"DUMP_DATA"


def test_cleanup_data_path_refuses_without_confirm(tmp_path: Path) -> None:
    data_dir = tmp_path / "data_activepieces"
    data_dir.mkdir()
    runner = MockRunner()
    with pytest.raises(PermissionError):
        cleanup_data_path(data_dir, confirm=False, runner=runner)
