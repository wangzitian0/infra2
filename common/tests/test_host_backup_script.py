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

from common.backup.verification import load_backup_inventory

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
    # Off-host uploads go to their own log, so the call order of the archive steps
    # stays readable in SHIM_LOG.
    # A fake remote under FAKE_REMOTE_ROOT: `fake-remote:infra2/a/b` is `$ROOT/infra2/a/b`.
    # FAKE_RCLONE_COPY_FAIL=1 fails every upload; FAKE_RCLONE_SIZE_FAIL=1 fails `size`;
    # FAKE_RCLONE_SIZE_EXTRA_BYTES=N / FAKE_RCLONE_SIZE_EXTRA_FILES=N add N (also
    # negative) bytes or files to the size answer: a larger or a partial remote run.
    "rclone": """#!/usr/bin/env bash
echo "rclone $*" >> "$RCLONE_LOG"
path_of() { printf '%s/%s' "$FAKE_REMOTE_ROOT" "${1#*:}"; }
case "$1" in
  copyto)
    if [ -n "${FAKE_RCLONE_COPY_FAIL:-}" ]; then echo "upload failed" >&2; exit 1; fi
    mkdir -p "$(dirname "$(path_of "$3")")"
    cp "$2" "$(path_of "$3")"
    ;;
  size)
    if [ -n "${FAKE_RCLONE_SIZE_FAIL:-}" ]; then echo "size failed" >&2; exit 1; fi
    dir="$(path_of "${@: -1}")"
    count=0
    bytes=0
    if [ -d "$dir" ]; then
      while IFS= read -r file; do
        count=$((count + 1))
        bytes=$((bytes + $(wc -c < "$file")))
      done < <(find "$dir" -type f)
    fi
    bytes=$((bytes + ${FAKE_RCLONE_SIZE_EXTRA_BYTES:-0}))
    count=$((count + ${FAKE_RCLONE_SIZE_EXTRA_FILES:-0}))
    printf '{"count":%s,"bytes":%s,"sizeless":0}\\n' "$count" "$bytes"
    ;;
esac
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
    rclone_log = tmp_path / "rclone.log"
    rclone_log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SHIM_LOG": str(log),
        "BACKUP_OUTPUT_DIR": str(out),
        "BACKUP_DATA_ROOT": str(data),
        # The scheduled runs always upload off-host (docs/ssot/ops.recovery.md).
        "BACKUP_REMOTE": "fake-remote:infra2",
        "RCLONE_LOG": str(rclone_log),
        "FAKE_REMOTE_ROOT": str(tmp_path / "remote"),
    }
    tool_versions = Path.home() / ".tool-versions"
    if tool_versions.exists():
        (tmp_path / ".tool-versions").write_text(
            tool_versions.read_text(encoding="utf-8"), encoding="utf-8"
        )
    asdf_dir = Path.home() / ".asdf"
    if asdf_dir.exists():
        env["ASDF_DATA_DIR"] = os.environ.get("ASDF_DATA_DIR", str(asdf_dir))
        env["ASDF_DIR"] = os.environ.get("ASDF_DIR", str(asdf_dir))
    return {
        "env": env,
        "out": out,
        "log": log,
        "rclone_log": rclone_log,
        "remote": tmp_path / "remote",
    }


def _run(
    host, **extra: str | None
) -> tuple[subprocess.CompletedProcess[str], dict, list[str]]:
    """Run the script; an ``extra`` value of None removes that variable."""
    bash = shutil.which("bash")
    assert bash, "bash is required"
    env = {**host["env"], **extra}
    proc = subprocess.run(
        [bash, str(SCRIPT)],
        env={key: value for key, value in env.items() if value is not None},
        capture_output=True,
        text=True,
        timeout=60,
    )
    manifests = sorted(host["out"].glob("*/manifest.json"))
    if not manifests and proc.returncode == 0:
        # A verified off-host run removes its local directory (#1624); the latest
        # manifest in the output directory is the record that stays.
        manifests = sorted(
            host["out"].glob("*-manifest.json"), key=lambda p: p.stat().st_mtime
        )
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
    staging_inventory = load_backup_inventory(environment="staging")
    staging_services = {entry.service_id for entry in staging_inventory}
    assert len(staging_services) == 14
    assert {
        "platform/clickhouse",
        "platform/openpanel",
        "platform/signoz",
    }.isdisjoint(staging_services)
    for entry in staging_inventory:
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
    assert _ids(manifest) == staging_services


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
    # a failed run keeps its own directory and never touches another run
    assert run_dir.is_dir()
    assert all(run_dir.exists() for run_dir in old)


def _remote_files(host) -> list[Path]:
    return sorted(
        p
        for p in (host["remote"] / "infra2/weekly/production").rglob("*")
        if p.is_file()
    )


def test_a_verified_upload_removes_the_local_run_and_keeps_the_latest_manifest(
    host,
) -> None:
    """#1624: Google is the store; the local run is a temporary copy."""
    proc, manifest, _ = _run(host)
    assert proc.returncode == 0, proc.stderr
    assert not list(host["out"].glob("production-*/")), "the local run must be gone"
    assert (host["out"] / "production-manifest.json").is_file()
    # every artifact and the run manifest are in the remote run
    assert len(_remote_files(host)) == len(manifest["artifacts"]) + 1
    report = proc.stdout.strip().splitlines()[-1]
    assert report.startswith("fake-remote:infra2/weekly/production/"), report
    assert report.endswith("/manifest.json"), report
    assert "removed local" in proc.stderr


def test_the_script_removes_only_its_own_run(host) -> None:
    older_production = host["out"] / "production-20260101T000000Z"
    older_production.mkdir()
    (older_production / "old.tar.gz").write_text("old", encoding="utf-8")
    staging_dir = host["out"] / "staging-20260101T000000Z"
    staging_dir.mkdir()
    proc, _, _ = _run(host)
    assert proc.returncode == 0, proc.stderr
    assert (older_production / "old.tar.gz").is_file(), "another run is never touched"
    assert staging_dir.is_dir(), "another environment is never touched"
    assert [p.name for p in host["out"].glob("production-2*/")] == [
        older_production.name
    ]


@pytest.mark.parametrize(
    ("knob", "value"),
    [
        ("FAKE_RCLONE_SIZE_EXTRA_BYTES", "1"),  # more bytes than local
        ("FAKE_RCLONE_SIZE_EXTRA_BYTES", "-1"),  # a partial upload: fewer bytes
        ("FAKE_RCLONE_SIZE_EXTRA_FILES", "1"),  # an extra remote file
        ("FAKE_RCLONE_SIZE_EXTRA_FILES", "-1"),  # a missing remote file, same bytes
    ],
)
def test_a_remote_run_that_differs_keeps_the_local_run(host, knob, value) -> None:
    proc, _, _ = _run(host, **{knob: value})
    assert proc.returncode == 1
    assert "does not hold the same files" in proc.stderr
    (run_dir,) = host["out"].glob("production-2*/")
    assert (run_dir / "manifest.json").is_file()
    assert Path(proc.stdout.strip().splitlines()[-1]) == run_dir / "manifest.json"
    assert (host["out"] / "production-manifest.json").is_file()


def test_a_remote_size_that_cannot_be_read_keeps_the_local_run(host) -> None:
    proc, _, _ = _run(host, FAKE_RCLONE_SIZE_FAIL="1")
    assert proc.returncode == 1
    assert "local run kept" in proc.stderr
    assert len(list(host["out"].glob("production-2*/"))) == 1


def test_a_failed_upload_keeps_the_local_run(host) -> None:
    proc, _, _ = _run(host, FAKE_RCLONE_COPY_FAIL="1")
    assert proc.returncode != 0
    (run_dir,) = host["out"].glob("production-2*/")
    assert run_dir.is_dir()
    assert "removed local" not in proc.stderr


def test_the_script_has_no_local_retention_count() -> None:
    assert "BACKUP_KEEP" not in SCRIPT.read_text(encoding="utf-8")


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
    assert "common.backup.emitter" in body


def test_emitter_services_match_declared_backup_inventory() -> None:
    from common.backup.emitter import emit_backup_targets

    targets = emit_backup_targets()
    target_ids = {t.service_id for t in targets}
    declared_ids = {entry.service_id for entry in load_backup_inventory()}
    assert target_ids == declared_ids


def test_emitter_path_sources_match_backup_facets() -> None:
    from common.backup.emitter import emit_backup_targets

    targets = {t.service_id: t for t in emit_backup_targets(data_root="/custom/data")}
    for entry in load_backup_inventory():
        if entry.service_id not in PATH_SERVICES:
            continue
        target = targets[entry.service_id]
        expected_path = entry.data_path.replace("/data", "/custom/data", 1)
        assert target.primary_target == expected_path


# --- #1030: a run without an off-host remote must not replace the off-host record ---


def test_an_off_host_run_uploads_every_artifact_and_records_remote_uris(host) -> None:
    proc, manifest, _ = _run(host)
    assert proc.returncode == 0, proc.stderr
    uris = [artifact["remote_uri"] for artifact in manifest["artifacts"]]
    assert uris and all(
        uri.startswith("fake-remote:infra2/weekly/production/") for uri in uris
    ), uris
    uploads = [
        line
        for line in host["rclone_log"].read_text().splitlines()
        if line.startswith("rclone copyto") and not line.endswith("manifest.json")
    ]
    assert len(uploads) == len(uris), uploads


def test_an_unset_remote_is_refused_before_any_archive(host) -> None:
    """#1030: an unset BACKUP_REMOTE used to mean 'local only', and a manual run
    replaced the off-host manifest that the watchdog reads."""
    latest = host["out"] / "production-manifest.json"
    latest.write_text('{"keep": "me"}', encoding="utf-8")

    proc, _, calls = _run(host, BACKUP_REMOTE=None)

    assert proc.returncode == 2, proc.stderr
    assert "BACKUP_LOCAL_ONLY" in proc.stderr
    assert calls == [], "no archive work may start"
    assert not list(host["out"].glob("production-*/")), "no run directory"
    assert latest.read_text(encoding="utf-8") == '{"keep": "me"}'


def test_local_only_and_a_remote_together_are_refused(host) -> None:
    proc, _, calls = _run(host, BACKUP_LOCAL_ONLY="1")
    assert proc.returncode == 2, proc.stderr
    assert calls == []


def test_local_only_zero_with_a_remote_is_an_off_host_run(host) -> None:
    """Only the value 1 enables local-only; 0 means off (Copilot on #1032)."""
    proc, manifest, _ = _run(host, BACKUP_LOCAL_ONLY="0")
    assert proc.returncode == 0, proc.stderr
    assert all(
        a["remote_uri"].startswith("fake-remote:") for a in manifest["artifacts"]
    )


@pytest.mark.parametrize("value", ["yes", "true", "2"])
def test_an_unknown_local_only_value_is_refused(host, value) -> None:
    proc, _, calls = _run(host, BACKUP_REMOTE=None, BACKUP_LOCAL_ONLY=value)
    assert proc.returncode == 2, proc.stderr
    assert calls == []


def test_a_local_only_run_never_replaces_the_canonical_manifest(host) -> None:
    latest = host["out"] / "production-manifest.json"
    latest.write_text('{"off-host": "record"}', encoding="utf-8")

    proc, manifest, _ = _run(host, BACKUP_REMOTE=None, BACKUP_LOCAL_ONLY="1")

    assert proc.returncode == 0, proc.stderr
    assert latest.read_text(encoding="utf-8") == '{"off-host": "record"}'
    assert _ids(manifest) == ALL_SERVICES
    assert all(a["remote_uri"].startswith("local:") for a in manifest["artifacts"])
    assert host["rclone_log"].read_text() == ""


def test_a_local_only_run_deletes_no_earlier_run(host) -> None:
    for i in range(1, 4):
        (host["out"] / f"production-2026010{i}T000000Z").mkdir()

    proc, _, _ = _run(host, BACKUP_REMOTE=None, BACKUP_LOCAL_ONLY="1")

    assert proc.returncode == 0, proc.stderr
    assert len(list(host["out"].glob("production-*/"))) == 4, "own run stays: only copy"


def test_a_run_through_a_symlink_finds_the_emitter(host, tmp_path) -> None:
    """The host installs the script as a symlink in /usr/local/sbin. The service list
    comes from common.backup.emitter in the checkout the symlink points into, so the
    script must resolve the link before it derives the repo root (#1030; the VPS ran
    on an uncommitted hot-patch of exactly this)."""
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    link = sbin / "infra2-host-backup.sh"
    link.symlink_to(SCRIPT)
    bash = shutil.which("bash")
    assert bash, "bash is required"
    proc = subprocess.run(
        [bash, str(link)],
        env=host["env"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(
        (host["out"] / "production-manifest.json").read_text(encoding="utf-8")
    )
    assert _ids(manifest) == ALL_SERVICES


def test_an_empty_service_list_fails_instead_of_writing_an_empty_manifest(
    host, tmp_path
) -> None:
    """An emitter that prints nothing must not produce a green run with 0 artifacts."""
    fake_bin = tmp_path / "fake-python"
    fake_bin.mkdir()
    shim = fake_bin / "python3"
    shim.write_text("#!/usr/bin/env bash\nexit 0\n")
    shim.chmod(0o755)
    latest = host["out"] / "production-manifest.json"
    latest.write_text('{"off-host": "record"}', encoding="utf-8")

    proc, _, calls = _run(host, PATH=f"{fake_bin}{os.pathsep}{host['env']['PATH']}")

    assert proc.returncode != 0, proc.stderr
    assert "no services" in proc.stderr
    assert calls == []
    assert not list(host["out"].glob("production-*/")), "no empty run directory"
    assert latest.read_text(encoding="utf-8") == '{"off-host": "record"}'


def test_backup_syncs_repo_before_emitting_services(host, tmp_path) -> None:
    """When BACKUP_SYNC_REPO=1 (or REPO_ROOT=/opt/infra2), git pull --ff-only runs before archiving."""
    bin_dir = Path(host["env"]["PATH"].split(os.pathsep)[0])
    git_shim = bin_dir / "git"
    git_shim.write_text('#!/usr/bin/env bash\necho "git $*" >> "$SHIM_LOG"\nexit 0\n')
    git_shim.chmod(0o755)

    proc, manifest, calls = _run(host, BACKUP_SYNC_REPO="1")
    assert proc.returncode == 0, proc.stderr
    assert _ids(manifest) == ALL_SERVICES
    assert (
        calls[0].startswith("git -C ") and "pull --ff-only -q origin main" in calls[0]
    )
    first_archive_idx = min(
        i for i, c in enumerate(calls) if c.startswith(("docker ", "tar "))
    )
    assert first_archive_idx > 0


def test_backup_archives_availability_ledger_when_present(host, tmp_path) -> None:
    """Archive observability/availability_ledger when BACKUP_LEDGER_ROOT has availability-ledger.json."""
    ledger_root = tmp_path / "infra2-availability-ledger"
    ledger_root.mkdir()
    (ledger_root / "availability-ledger.json").write_text(
        '{"schema": 2}', encoding="utf-8"
    )

    proc, manifest, _ = _run(host, BACKUP_LEDGER_ROOT=str(ledger_root))
    assert proc.returncode == 0, proc.stderr
    assert _ids(manifest) == ALL_SERVICES | {"observability/availability_ledger"}
    ledger_artifacts = [
        a
        for a in manifest["artifacts"]
        if a["service_id"] == "observability/availability_ledger"
    ]
    assert len(ledger_artifacts) == 1
    assert ledger_artifacts[0]["method"] == "filesystem_archive"


def test_backup_warns_when_ledger_dir_exists_without_marker_file(
    host, tmp_path
) -> None:
    """Emit a warning to stderr when BACKUP_LEDGER_ROOT exists without availability-ledger.json."""
    ledger_root = tmp_path / "infra2-availability-ledger-empty"
    ledger_root.mkdir()

    proc, manifest, _ = _run(host, BACKUP_LEDGER_ROOT=str(ledger_root))
    assert proc.returncode == 0, proc.stderr
    assert "observability/availability_ledger" not in _ids(manifest)
    assert (
        f"WARN observability/availability_ledger: directory {ledger_root} exists without availability-ledger.json; skipping archive"
        in proc.stderr
    )
