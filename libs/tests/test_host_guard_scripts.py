"""Exercise the host guards with fake commands and a disposable filesystem."""

from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PING_URL = "https://hc-ping.com/test-check-uuid"
WARNING_PING_URL = "https://hc-ping.com/test-warning-uuid"


@pytest.fixture
def guard_host(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = tmp_path / "commands.log"
    commands.touch()
    for name, body in {
        "df": (
            '#!/bin/sh\npercent="$FAKE_DISK_PERCENT"\n'
            'if [ -n "${FAKE_DISK_PERCENT_FILE:-}" ]; then percent="$(cat "$FAKE_DISK_PERCENT_FILE")"; fi\n'
            'printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n'
            '/dev/test 100 10 90 %s%% /data\\n" "$percent"\n'
        ),
        "docker": (
            '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$GUARD_COMMANDS"\n'
            'if [ -n "${FAKE_DISK_PERCENT_AFTER_PRUNE:-}" ]; then '
            'printf "%s" "$FAKE_DISK_PERCENT_AFTER_PRUNE" > "$FAKE_DISK_PERCENT_FILE"; fi\n'
            'test "${FAKE_PRUNE_OK:-1}" = 1\n'
        ),
        "dockerd": '#!/bin/sh\ncp "$3" "$GUARD_CAPTURE_CONFIG"\n',
        "curl": '#!/bin/sh\nprintf "curl %s\\n" "$*" >> "$GUARD_COMMANDS"\n',
        "stat": (
            '#!/bin/sh\nfor arg do file="$arg"; done\nwc -c < "$file" | tr -d " "\n'
        ),
        "timeout": (
            '#!/bin/sh\nprintf "timeout %s\\n" "$*" >> "$GUARD_COMMANDS"\n'
            'test "$FAKE_DOCKER_OK" = 1\n'
        ),
    }.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GUARD_COMMANDS": str(commands),
        "GUARD_CAPTURE_CONFIG": str(tmp_path / "candidate.json"),
        "DISK_GUARDIAN_DATA_PATH": str(tmp_path),
        "DISK_GUARDIAN_LOG_ROOT": str(tmp_path / "logs"),
        "DISK_GUARDIAN_PING_URL": PING_URL,
        "DISK_GUARDIAN_WARNING_PING_URL": WARNING_PING_URL,
        "HOST_HEARTBEAT_PING_URL": PING_URL,
        "FAKE_DISK_PERCENT": "65",
        "FAKE_DOCKER_OK": "1",
    }


def _run(name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "tools" / name)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def _commands(env: dict[str, str]) -> str:
    return Path(env["GUARD_COMMANDS"]).read_text(encoding="utf-8")


def test_disk_guardian_below_warning_does_not_prune(guard_host) -> None:
    result = _run("disk_guardian.sh", guard_host)
    assert result.returncode == 0, result.stderr
    commands = _commands(guard_host)
    assert "docker " not in commands
    assert PING_URL in commands
    assert WARNING_PING_URL in commands
    assert "/fail" not in commands


def test_disk_guardian_warning_prunes_only_safe_cache(guard_host) -> None:
    guard_host["FAKE_DISK_PERCENT"] = "80"
    result = _run("disk_guardian.sh", guard_host)
    assert result.returncode == 0, result.stderr
    commands = _commands(guard_host)
    assert "docker image prune -f --filter dangling=true" in commands
    assert "docker builder prune -f --filter until=24h" in commands
    assert "volume prune" not in commands
    assert f"{WARNING_PING_URL}/fail" in commands
    assert f"{PING_URL}/fail" not in commands


def test_disk_guardian_cleanup_failure_reports_critical(guard_host) -> None:
    guard_host["FAKE_DISK_PERCENT"] = "80"
    guard_host["FAKE_PRUNE_OK"] = "0"
    result = _run("disk_guardian.sh", guard_host)
    assert result.returncode == 1
    assert f"{PING_URL}/fail" in _commands(guard_host)


def test_disk_guardian_critical_truncates_large_log_and_signals_failure(
    guard_host, tmp_path: Path
) -> None:
    guard_host["FAKE_DISK_PERCENT"] = "85"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    large_log = log_dir / "container-json.log"
    with large_log.open("wb") as handle:
        handle.truncate(101 * 1024 * 1024)

    result = _run("disk_guardian.sh", guard_host)
    assert result.returncode == 1
    assert large_log.stat().st_size == 0
    assert f"{PING_URL}/fail" in _commands(guard_host)


def test_disk_guardian_escalates_when_writes_cross_85_during_cleanup(
    guard_host, tmp_path: Path
) -> None:
    percent_file = tmp_path / "disk-percent"
    percent_file.write_text("80", encoding="utf-8")
    guard_host["FAKE_DISK_PERCENT_FILE"] = str(percent_file)
    guard_host["FAKE_DISK_PERCENT_AFTER_PRUNE"] = "85"
    result = _run("disk_guardian.sh", guard_host)

    assert result.returncode == 1
    assert f"{WARNING_PING_URL}/fail" in _commands(guard_host)
    assert f"{PING_URL}/fail" in _commands(guard_host)


def test_heartbeat_reports_docker_failure_without_exposing_ping_url(guard_host) -> None:
    guard_host["FAKE_DOCKER_OK"] = "0"
    result = _run("host_heartbeat.sh", guard_host)
    assert result.returncode == 1
    assert f"{PING_URL}/fail" in _commands(guard_host)
    assert PING_URL not in result.stdout + result.stderr


def test_heartbeat_success(guard_host) -> None:
    result = _run("host_heartbeat.sh", guard_host)
    assert result.returncode == 0, result.stderr
    assert PING_URL in _commands(guard_host)
    assert "/fail" not in _commands(guard_host)


def test_docker_config_check_preserves_unrelated_settings(
    guard_host, tmp_path: Path
) -> None:
    current = tmp_path / "daemon.json"
    current.write_text(
        json.dumps({"data-root": "/data/docker", "log-opts": {"labels": "team"}}),
        encoding="utf-8",
    )
    guard_host["INFRA2_DAEMON_JSON"] = str(current)
    script = (
        ROOT / "bootstrap" / "01.dokploy_install" / "host_guard" / "configure_docker.sh"
    )
    result = subprocess.run(
        ["bash", str(script), "--check"],
        env=guard_host,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(current.read_text(encoding="utf-8")) == {
        "data-root": "/data/docker",
        "log-opts": {"labels": "team"},
    }
    candidate = json.loads(
        Path(guard_host["GUARD_CAPTURE_CONFIG"]).read_text(encoding="utf-8")
    )
    assert candidate == {
        "data-root": "/data/docker",
        "live-restore": True,
        "log-driver": "json-file",
        "log-opts": {"labels": "team", "max-size": "50m", "max-file": "3"},
    }
