"""tools/host_backup.sh backs up every registered service even when one fails (#618, truealpha#650).

The script runs for real against a temp data root; `docker`, `tar`, `stat` and
`sha256sum` are PATH shims that log each call and fail on request.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from libs.backup_verification import load_backup_inventory

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools/host_backup.sh"

INVENTORY = load_backup_inventory()
ALL_SERVICES = {entry.service_id for entry in INVENTORY}
PG_SERVICES = {
    entry.service_id for entry in INVENTORY if entry.method.startswith("pg_dump")
}
REDIS_SERVICES = {
    entry.service_id for entry in INVENTORY if entry.method.startswith("redis")
}
PATH_SERVICES = ALL_SERVICES - PG_SERVICES - REDIS_SERVICES

SHIMS = {
    # docker exec <container> pg_dumpall ... | docker exec <container> sh -c '...redis-cli SAVE'
    "docker": r"""#!/usr/bin/env bash
echo "docker $*" >> "$SHIM_LOG"
container="$2"
if [ "$container" = "${FAKE_PG_FAIL:-}" ]; then echo "no such container" >&2; exit 1; fi
case "$*" in
  *pg_dumpall*) echo "-- dump of $container" ;;
  *"redis-cli SAVE"*) echo "${FAKE_REDIS_REPLY:-OK}" ;;
esac
""",
    # tar --warning=... -czf <archive> -C <dir> <member>; FAKE_TAR_EXIT="minio=1,clickhouse=2"
    "tar": r"""#!/usr/bin/env bash
echo "tar $*" >> "$SHIM_LOG"
archive=""; dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    -czf) archive="$2"; shift ;;
    -C) dir="$2"; shift ;;
  esac
  shift
done
echo "archive of $dir" > "$archive"
IFS=',' read -r -a rules <<< "${FAKE_TAR_EXIT:-}"
for rule in "${rules[@]}"; do
  case "$dir" in *"${rule%%=*}"*) exit "${rule#*=}" ;; esac
done
exit 0
""",
    "stat": """#!/usr/bin/env bash\nwc -c < "${@: -1}" | tr -d ' '\n""",
    "sha256sum": """#!/usr/bin/env bash\necho "0000000000000000000000000000000000000000000000000000000000000000  $1"\n""",
}


@pytest.fixture
def host(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in SHIMS.items():
        shim = bin_dir / name
        shim.write_text(body)
        shim.chmod(0o755)
    data = tmp_path / "data"
    for sub in ("platform/redis", "finance_report/redis"):
        (data / sub).mkdir(parents=True)
        (data / sub / "dump.rdb").write_text("rdb")
        (data / sub / "appendonly.aof").write_text("aof")
    for sub in (
        "bootstrap/1password",
        "bootstrap/iac-runner",
        "bootstrap/vault",
        "platform/alerting",
        "platform/free",
        "platform/openpanel",
        "platform/portal",
        "platform/signoz",
        "platform/clickhouse",
        "platform/authentik",
        "truealpha/dagster",
        "platform/minio",
    ):
        (data / sub).mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SHIM_LOG": str(log),
        "BACKUP_OUTPUT_DIR": str(out),
        "BACKUP_DATA_ROOT": str(data),
    }
    return {"env": env, "out": out, "log": log}


def _run(
    host, **extra: str
) -> tuple[subprocess.CompletedProcess[str], dict, list[str]]:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    proc = subprocess.run(
        [bash, str(SCRIPT)],
        env={**host["env"], **extra},
        capture_output=True,
        text=True,
        timeout=60,
    )
    manifests = sorted(host["out"].glob("*/manifest.json"))
    manifest = json.loads(manifests[-1].read_text()) if manifests else {}
    return proc, manifest, host["log"].read_text().splitlines()


def _ids(manifest: dict) -> set[str]:
    return {artifact["service_id"] for artifact in manifest.get("artifacts", [])}


def test_every_service_is_archived_and_dumps_run_before_path_archives(host) -> None:
    proc, manifest, calls = _run(host)
    assert proc.returncode == 0, proc.stderr
    assert manifest["environment"] == "production"
    assert (host["out"] / "production-manifest.json").exists()
    assert stat.S_IMODE(host["out"].stat().st_mode) == 0o700
    latest = host["out"] / "production-manifest.json"
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    assert _ids(manifest) == ALL_SERVICES
    last_dump = max(i for i, call in enumerate(calls) if "pg_dumpall" in call)
    first_path_tar = min(
        i
        for i, call in enumerate(calls)
        if call.startswith("tar ") and call.endswith(" .")
    )
    assert last_dump < first_path_tar, calls
    # minio is the busiest live tree: archived last so nothing waits behind it
    assert "platform/minio" in calls[-1], calls


def test_existing_latest_manifest_permissions_are_tightened(host) -> None:
    latest = host["out"] / "production-manifest.json"
    latest.write_text("old manifest", encoding="utf-8")
    latest.chmod(0o644)

    proc, manifest, _ = _run(host)

    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    assert json.loads(latest.read_text(encoding="utf-8")) == manifest


def test_staging_run_covers_all_declared_services(host) -> None:
    data_root = Path(host["env"]["BACKUP_DATA_ROOT"])
    for entry in load_backup_inventory():
        if entry.service_id.startswith("bootstrap/"):
            continue
        source = data_root / entry.data_path.removeprefix("/data/")
        staging_source = Path(f"{source}-staging")
        staging_source.mkdir(parents=True, exist_ok=True)
        if entry.service_id in REDIS_SERVICES:
            (staging_source / "dump.rdb").write_text("staging rdb")

    proc, manifest, _ = _run(host, ENV_SUFFIX="-staging")
    assert proc.returncode == 0, proc.stderr
    assert manifest["environment"] == "staging"
    assert (host["out"] / "staging-manifest.json").exists()
    assert _ids(manifest) == ALL_SERVICES


def test_unknown_environment_suffix_fails_before_archiving(host) -> None:
    proc, manifest, calls = _run(host, ENV_SUFFIX="-unknown")
    assert proc.returncode == 2
    assert manifest == {}
    assert calls == []


def test_tar_exit_1_is_a_warning_and_the_run_continues(host) -> None:
    proc, manifest, _ = _run(host, FAKE_TAR_EXIT="platform/redis=1,minio=1")
    assert proc.returncode == 0, proc.stderr
    assert _ids(manifest) == ALL_SERVICES
    assert "WARN platform/redis: tar exit 1" in proc.stderr
    assert "WARN platform/s3: tar exit 1" in proc.stderr


def test_a_failing_service_does_not_stop_the_rest(host) -> None:
    old = [host["out"] / f"2026010{i}T000000Z" for i in range(1, 9)]
    for run_dir in old:
        run_dir.mkdir()
    proc, manifest, _ = _run(
        host,
        FAKE_PG_FAIL="finance_report-postgres",
        FAKE_TAR_EXIT="clickhouse=2",
        BACKUP_KEEP="1",
    )
    assert proc.returncode == 1
    assert _ids(manifest) == ALL_SERVICES - {
        "finance_report/postgres",
        "platform/clickhouse",
    }
    assert "FAILED finance_report/postgres" in proc.stderr
    assert "FAILED platform/clickhouse" in proc.stderr
    assert "2 service(s) FAILED" in proc.stderr
    latest = json.loads(
        (host["out"] / "production-manifest.json").read_text(encoding="utf-8")
    )
    assert _ids(latest) == _ids(manifest), "latest must expose the failed run"
    run_dir = Path(proc.stdout.strip().splitlines()[-1]).parent
    # a failed dump or tar leaves no half-written archive behind
    assert not list(run_dir.glob("finance_report_postgres_*"))
    assert not list(run_dir.glob("platform_clickhouse_*"))
    # retention is skipped on a failed run: older good runs survive
    assert all(run_dir.exists() for run_dir in old)


def test_retention_keeps_the_newest_runs_on_success(host) -> None:
    for i in range(1, 6):
        (host["out"] / f"production-2026010{i}T000000Z").mkdir()
        os.utime(host["out"] / f"production-2026010{i}T000000Z", (i, i))
    staging_dir = host["out"] / "staging-20260101T000000Z"
    staging_dir.mkdir()
    proc, _, _ = _run(host, BACKUP_KEEP="2")
    assert proc.returncode == 0, proc.stderr
    kept = sorted(p.name for p in host["out"].glob("production-*/"))
    assert len(kept) == 2
    assert "production-20260105T000000Z" in kept
    assert staging_dir.exists(), "production retention must not prune staging"


def test_redis_save_authenticates_and_only_dump_rdb_is_archived(host) -> None:
    proc, manifest, calls = _run(
        host, FAKE_REDIS_REPLY="NOAUTH Authentication required."
    )
    assert proc.returncode == 0, proc.stderr
    assert REDIS_SERVICES <= _ids(manifest)
    saves = [call for call in calls if "redis-cli SAVE" in call]
    assert len(saves) == 2
    assert all(
        'REDISCLI_AUTH="$PASSWORD"' in call and ". /secrets/.env" in call
        for call in saves
    )
    redis_tars = [
        call for call in calls if call.startswith("tar ") and "/redis" in call
    ]
    assert len(redis_tars) == 2
    assert all(call.endswith(" dump.rdb") for call in redis_tars), redis_tars
    # an unauthenticated reply is not a snapshot: say so
    assert "WARN platform/redis: SAVE not confirmed" in proc.stderr


def test_redis_without_a_snapshot_fails_that_service(host) -> None:
    (Path(host["env"]["BACKUP_DATA_ROOT"]) / "finance_report/redis/dump.rdb").unlink()
    proc, manifest, _ = _run(host)
    assert proc.returncode == 1
    assert "finance_report/redis" not in _ids(manifest)
    assert "FAILED finance_report/redis" in proc.stderr


def test_missing_path_directory_fails_that_service(host) -> None:
    shutil.rmtree(Path(host["env"]["BACKUP_DATA_ROOT"]) / "platform/minio")
    proc, manifest, _ = _run(host)
    assert proc.returncode == 1
    assert "platform/s3" not in _ids(manifest)
    assert "FAILED platform/s3" in proc.stderr
    assert "missing data directory for platform/s3" in proc.stderr


def test_script_has_no_hardcoded_services() -> None:
    body = SCRIPT.read_text()
    assert "SERVICES=$(cat <<EOF" not in body
    assert "platform/postgres|pg|" not in body
    assert "platform/s3|path|" not in body
    assert "libs.backup.emitter" in body


def test_emitter_services_match_declared_backup_inventory() -> None:
    from libs.backup.emitter import emit_backup_targets

    targets = emit_backup_targets()
    target_ids = {t.service_id for t in targets}
    declared_ids = {entry.service_id for entry in load_backup_inventory()}
    assert target_ids == declared_ids


def test_emitter_path_sources_match_backup_facets() -> None:
    from libs.backup.emitter import emit_backup_targets

    targets = {t.service_id: t for t in emit_backup_targets(data_root="/custom/data")}
    for entry in load_backup_inventory():
        if entry.service_id not in PATH_SERVICES:
            continue
        target = targets[entry.service_id]
        expected_path = entry.data_path.replace("/data", "/custom/data", 1)
        assert target.primary_target == expected_path
