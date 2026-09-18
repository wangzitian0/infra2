"""tools/host_backup.sh backs up every registered service even when one fails (#618, truealpha#650).

The script runs for real against a temp data root; `docker`, `tar`, `stat` and
`sha256sum` are PATH shims that log each call and fail on request.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from libs.backup_verification import load_backup_inventory

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools/host_backup.sh"

PG_SERVICES = {"platform/postgres", "finance_report/postgres", "truealpha/postgres"}
REDIS_SERVICES = {"platform/redis", "finance_report/redis"}
PATH_SERVICES = {
    "bootstrap/vault",
    "platform/clickhouse",
    "platform/authentik",
    "platform/minio",
}
ALL_SERVICES = PG_SERVICES | REDIS_SERVICES | PATH_SERVICES

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
        "bootstrap/vault",
        "platform/clickhouse",
        "platform/authentik",
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


def test_tar_exit_1_is_a_warning_and_the_run_continues(host) -> None:
    proc, manifest, _ = _run(host, FAKE_TAR_EXIT="platform/redis=1,minio=1")
    assert proc.returncode == 0, proc.stderr
    assert _ids(manifest) == ALL_SERVICES
    assert "WARN platform/redis: tar exit 1" in proc.stderr
    assert "WARN platform/minio: tar exit 1" in proc.stderr


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
    run_dir = Path(proc.stdout.strip().splitlines()[-1]).parent
    # a failed dump or tar leaves no half-written archive behind
    assert not list(run_dir.glob("finance_report_postgres_*"))
    assert not list(run_dir.glob("platform_clickhouse_*"))
    # retention is skipped on a failed run: older good runs survive
    assert all(run_dir.exists() for run_dir in old)


def test_retention_keeps_the_newest_runs_on_success(host) -> None:
    for i in range(1, 6):
        (host["out"] / f"2026010{i}T000000Z").mkdir()
        os.utime(host["out"] / f"2026010{i}T000000Z", (i, i))
    proc, _, _ = _run(host, BACKUP_KEEP="2")
    assert proc.returncode == 0, proc.stderr
    kept = sorted(p.name for p in host["out"].iterdir())
    assert len(kept) == 2
    assert "20260105T000000Z" in kept


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
    assert "platform/minio" not in _ids(manifest)
    assert "FAILED platform/minio" in proc.stderr
    assert "missing data directory for platform/minio" in proc.stderr


def test_script_services_are_declared_backup_inventory() -> None:
    body = SCRIPT.read_text()
    block = body[body.index("SERVICES=$(cat <<EOF") : body.index("\nEOF\n")]
    ids = set(re.findall(r"^([a-z_]+/[a-z_]+)\|(?:pg|redis|path)\|", block, re.M))
    assert ids == ALL_SERVICES
    declared = {entry.service_id for entry in load_backup_inventory()}
    assert ids <= declared, ids - declared
