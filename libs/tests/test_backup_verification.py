"""Tests for backup inventory and freshness verification."""

from __future__ import annotations

import hashlib
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
    planned_artifact_path,
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


def _load_backup_restore_rehearsal_tool():
    path = ROOT / "tools/backup_restore_rehearsal.py"
    spec = importlib.util.spec_from_file_location("backup_restore_rehearsal_tool", path)
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
    assert payload["alerts"][0]["labels"]["service_id"] == entry.service_id
    assert payload["alerts"][0]["labels"]["failure_domain"] == "backup"


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
    assert (
        planned_artifact_path(
            {"remote_uri": "r2:infra2/finance_report/postgres/dump.sql.gz"},
            tmp_path,
        )
        == tmp_path / "dump.sql.gz"
    )


def test_materialize_artifact_verifies_real_checksum_and_cleans_corrupted(tmp_path) -> None:
    content = b"valid sql dump data"
    correct_hash = hashlib.sha256(content).hexdigest()
    corrupt_hash = "f" * 64

    class Result:
        returncode = 0
        stderr = ""

    def mock_download_good(cmd, **_kwargs):
        dest = Path(cmd[3])
        dest.write_bytes(content)
        return Result()

    # Case 1: valid sha256 matches
    archive = materialize_artifact(
        {
            "remote_uri": "r2:infra2/finance_report/postgres/dump.sql.gz",
            "sha256": correct_hash,
        },
        tmp_path,
        runner=mock_download_good,
    )
    assert archive.exists()
    assert archive.read_bytes() == content

    # Case 2: checksum mismatch raises BackupRestoreError and unlinks corrupted file
    def mock_download_corrupt(cmd, **_kwargs):
        dest = Path(cmd[3])
        dest.write_bytes(content)
        return Result()

    target_file = tmp_path / "corrupt.sql.gz"
    with pytest.raises(BackupRestoreError, match="checksum mismatch"):
        materialize_artifact(
            {
                "remote_uri": "r2:infra2/finance_report/postgres/corrupt.sql.gz",
                "sha256": corrupt_hash,
            },
            tmp_path,
            runner=mock_download_corrupt,
        )
    assert not target_file.exists(), "Corrupted download file must be cleaned up"

    # Case 3: local: uri also verifies sha256
    local_file = tmp_path / "local.sql.gz"
    local_file.write_bytes(content)
    with pytest.raises(BackupRestoreError, match="checksum mismatch"):
        materialize_artifact(
            {
                "remote_uri": f"local:{local_file}",
                "sha256": corrupt_hash,
            },
            tmp_path,
        )


def test_execute_rehearsal_handles_timeout_cleanly(monkeypatch) -> None:
    import time
    from libs.backup.rehearsal import RestoreRehearsalPlan, execute_rehearsal

    plan = RestoreRehearsalPlan(
        service_id="test/postgres",
        source_uri="test:dump.sql.gz",
        archive_path=Path("/tmp/fake.sql.gz"),
        target_container="test-postgres-restore-rehearsal",
        pg_user="postgres",
        database="testdb",
        invariant_sql=("SELECT 1",),
    )

    def slow_restore(*args, **kwargs):
        time.sleep(0.5)
        return {}

    monkeypatch.setattr(
        "libs.backup.rehearsal.run_postgres_restore_rehearsal", slow_restore
    )
    with pytest.raises(BackupRestoreError, match="timed out"):
        execute_rehearsal(plan, timeout_seconds=0.05)


def test_detect_dump_target_db(tmp_path) -> None:
    import gzip
    from libs.backup.rehearsal import _detect_dump_target_db

    # Case 1: dump with CREATE ROLE / pg_dumpall triggers 'postgres' db
    dump_cluster = tmp_path / "cluster.sql.gz"
    with gzip.open(dump_cluster, "wb") as f:
        f.write(b"-- pg_dumpall output\nCREATE ROLE test;\n")
    assert _detect_dump_target_db(dump_cluster, "appdb") == "postgres"

    # Case 2: standard single-db dump keeps default_db
    dump_single = tmp_path / "single.sql.gz"
    with gzip.open(dump_single, "wb") as f:
        f.write(b"CREATE TABLE users (id int);\n")
    assert _detect_dump_target_db(dump_single, "appdb") == "appdb"

    # Case 3: non-gzip file falls back gracefully without crash
    plain_file = tmp_path / "plain.sql"
    plain_file.write_text("CREATE TABLE plain (id int);")
    assert _detect_dump_target_db(plain_file, "appdb") == "appdb"


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
                    "created_at": 1_800_000_000,
                },
                {
                    "service_id": "platform/postgres",
                    "created_at": "9999-01-03T00:00:00Z",
                },
            ]
        },
        "finance_report/postgres",
    )

    assert artifact["created_at"] == 1_800_000_000


def test_backup_restore_rehearsal_dry_run_has_no_download_side_effect(
    tmp_path, monkeypatch, capsys
) -> None:
    """Infra-011.17 / #945: dry-run validates target and plans without rclone."""
    tool = _load_backup_restore_rehearsal_tool()
    entry = BackupEntry(
        service_id="finance_report/postgres",
        data_path="/data/finance_report/postgres",
        method="pg_dump_plus_data_archive",
        restore_command="restore",
        remote="r2",
        retention_days=30,
        rpo_hours=24,
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"artifacts":[]}', encoding="utf-8")
    artifact = {
        "remote_uri": "r2:infra2/finance_report/postgres/dump.sql.gz",
        "method": "pg_dumpall_gz",
    }

    monkeypatch.setattr(tool, "load_backup_inventory", lambda: [entry])
    monkeypatch.setattr(
        tool, "assert_manifest_is_rehearsable", lambda *_args, **_kwargs: artifact
    )

    def fail_download(*_args, **_kwargs):
        raise AssertionError("dry-run must not download artifacts")

    monkeypatch.setattr(tool, "materialize_artifact", fail_download)

    rc = tool.main(
        [
            "--manifest",
            str(manifest_path),
            "--target-container",
            "finance-report-postgres-restore-rehearsal",
            "--download-dir",
            str(tmp_path / "downloads"),
            "--dry-run",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert '"status": "planned"' in out
    assert str(tmp_path / "downloads" / "dump.sql.gz") in out


def test_run_postgres_restore_rehearsal_filters_create_role_postgres(tmp_path) -> None:
    """Postgres 16 dumpall includes CREATE ROLE postgres which fails on existing superuser."""
    import gzip
    from libs.backup_restore import RestoreRehearsalPlan, run_postgres_restore_rehearsal

    dump_file = tmp_path / "dump.sql.gz"
    with gzip.open(dump_file, "wb") as f:
        f.write(
            b"CREATE ROLE postgres;\nCREATE ROLE postgres WITH SUPERUSER;\nCREATE ROLE app_user;\nCREATE DATABASE testdb;\n"
        )

    plan = RestoreRehearsalPlan(
        service_id="test/postgres",
        source_uri="test:dump.sql.gz",
        archive_path=dump_file,
        target_container="test-postgres-restore-rehearsal",
        pg_user="postgres",
        database="testdb",
        invariant_sql=("SELECT 1",),
    )

    written_lines = []

    class MockStdin:
        def write(self, data: bytes):
            written_lines.append(data)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class MockProc:
        def __init__(self):
            self.stdin = MockStdin()

        def wait(self):
            return 0

    class MockResult:
        returncode = 0
        stdout = "1\n"
        stderr = ""

    def mock_popen(cmd, stdin=None):
        return MockProc()

    def mock_runner(cmd, **kwargs):
        return MockResult()

    res = run_postgres_restore_rehearsal(plan, popen=mock_popen, runner=mock_runner)
    assert res["status"] == "pass"
    assert b"CREATE ROLE postgres;\n" not in written_lines
    assert b"CREATE ROLE postgres WITH SUPERUSER;\n" not in written_lines
    assert b"CREATE ROLE app_user;\n" in written_lines
    assert b"CREATE DATABASE testdb;\n" in written_lines


def _fake_restore(
    payload: bytes,
    tmp_path,
    *,
    stdin_cls=None,
    wait_rc: int = 0,
    invariant_stdout: str = "1\n",
):
    """Stream `payload` through the real restore path, returning what reached psql."""
    import gzip

    from libs.backup_restore import RestoreRehearsalPlan, run_postgres_restore_rehearsal

    dump_file = tmp_path / "dump.sql.gz"
    with gzip.open(dump_file, "wb") as handle:
        handle.write(payload)

    written: list[bytes] = []
    waited: list[bool] = []

    class RecordingStdin:
        def write(self, data: bytes) -> None:
            written.append(data)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Proc:
        def __init__(self) -> None:
            self.stdin = (stdin_cls or RecordingStdin)()

        def wait(self) -> int:
            waited.append(True)
            return wait_rc

    class Result:
        returncode = 0
        stdout = invariant_stdout
        stderr = ""

    plan = RestoreRehearsalPlan(
        service_id="test/postgres",
        source_uri="test:dump.sql.gz",
        archive_path=dump_file,
        target_container="test-postgres-restore-rehearsal",
        pg_user="postgres",
        database="testdb",
        invariant_sql=("SELECT 1",),
    )
    result = run_postgres_restore_rehearsal(
        plan,
        popen=lambda cmd, stdin=None: Proc(),
        runner=lambda cmd, **kwargs: Result(),
    )
    return result, written, waited


@pytest.mark.parametrize("output", ["f\n", "0\n", "\n", "t\nf\n", "present\n"])
def test_restore_rejects_false_or_ambiguous_select_invariant(tmp_path, output) -> None:
    from libs.backup_restore import BackupRestoreError

    with pytest.raises(BackupRestoreError, match="one true scalar"):
        _fake_restore(b"SELECT 1;\n", tmp_path, invariant_stdout=output)


def test_restore_filter_never_drops_copy_block_data(tmp_path) -> None:
    """A COPY data row starting with the role prefix is table data, not a statement."""
    row_semicolon = b"CREATE ROLE postgres; -- a note a user saved\n"
    row_space = b"CREATE ROLE postgres was executed by admin\n"
    payload = (
        b"CREATE ROLE postgres;\n"
        b"CREATE ROLE app_user;\n"
        b"COPY app.notes (body) FROM stdin;\n"
        + row_semicolon
        + row_space
        + b"harmless note\n"
        b"\\.\n"
        b"CREATE DATABASE testdb;\n"
    )
    result, written, _ = _fake_restore(payload, tmp_path)

    # Raw COPY data survives verbatim: dropping it would silently lose rows
    # while the restore still reported a pass.
    assert row_semicolon in written
    assert row_space in written
    # The genuine role statement outside the COPY block is still filtered.
    assert b"CREATE ROLE postgres;\n" not in written
    assert b"CREATE ROLE app_user;\n" in written
    assert result["filtered_role_statements"] == 1


def test_restore_filter_resumes_after_copy_terminator(tmp_path) -> None:
    """The role filter re-arms once the COPY block closes with a backslash-dot."""
    payload = (
        b"COPY app.notes (body) FROM stdin;\n"
        b"CREATE ROLE postgres; inside the block\n"
        b"\\.\n"
        b"CREATE ROLE postgres;\n"
        b"CREATE DATABASE testdb;\n"
    )
    result, written, _ = _fake_restore(payload, tmp_path)

    assert b"CREATE ROLE postgres; inside the block\n" in written
    assert b"CREATE ROLE postgres;\n" not in written
    assert result["filtered_role_statements"] == 1


def test_restore_reaps_psql_when_it_exits_mid_stream(tmp_path) -> None:
    """psql runs with ON_ERROR_STOP=1, so it can close the pipe while we write."""
    from libs.backup_restore import BackupRestoreError

    class EarlyExitStdin:
        def __init__(self) -> None:
            self.count = 0

        def write(self, data: bytes) -> None:
            self.count += 1
            if self.count > 2:
                raise BrokenPipeError(32, "Broken pipe")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    payload = b"".join(b"INSERT INTO t VALUES (%d);\n" % i for i in range(50))

    # The subprocess exit code is the real diagnosis, so it must be reaped and
    # surfaced instead of letting the raw BrokenPipeError escape.
    with pytest.raises(BackupRestoreError, match="exit code 3"):
        _fake_restore(payload, tmp_path, stdin_cls=EarlyExitStdin, wait_rc=3)


def test_restore_rejects_a_psql_that_stopped_reading_but_exited_zero(tmp_path) -> None:
    """An EPIPE with exit 0 means psql quit early: the restore is incomplete."""
    from libs.backup_restore import BackupRestoreError

    class EarlyExitStdin:
        def __init__(self) -> None:
            self.count = 0

        def write(self, data: bytes) -> None:
            self.count += 1
            if self.count > 2:
                raise BrokenPipeError(32, "Broken pipe")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    payload = b"".join(b"INSERT INTO t VALUES (%d);\n" % i for i in range(50))

    # rc == 0 must NOT launder a truncated stream into a pass.
    with pytest.raises(BackupRestoreError, match="stopped reading"):
        _fake_restore(payload, tmp_path, stdin_cls=EarlyExitStdin, wait_rc=0)


def test_restore_reaps_psql_when_the_write_loop_raises_something_else(tmp_path) -> None:
    """Any error while streaming must still reap the child, not leave it running."""
    import gzip

    from libs.backup_restore import RestoreRehearsalPlan, run_postgres_restore_rehearsal

    dump_file = tmp_path / "dump.sql.gz"
    with gzip.open(dump_file, "wb") as handle:
        handle.write(b"SELECT 1;\nSELECT 2;\n")

    waited: list[bool] = []

    class ExplodingStdin:
        def write(self, data: bytes) -> None:
            raise OSError(5, "Input/output error")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Proc:
        def __init__(self) -> None:
            self.stdin = ExplodingStdin()

        def wait(self) -> int:
            waited.append(True)
            return 0

    plan = RestoreRehearsalPlan(
        service_id="test/postgres",
        source_uri="test:dump.sql.gz",
        archive_path=dump_file,
        target_container="test-postgres-restore-rehearsal",
        pg_user="postgres",
        database="testdb",
        invariant_sql=("SELECT 1",),
    )

    with pytest.raises(OSError):
        run_postgres_restore_rehearsal(
            plan,
            popen=lambda cmd, stdin=None: Proc(),
            runner=lambda cmd, **kwargs: None,
        )
    assert waited == [True], "psql was left unreaped when the write loop failed"
