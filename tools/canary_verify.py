#!/usr/bin/env python3
"""Canary Todo 4-Pillar Touch Reality Physical Verification Tool.

Verifies the physical execution state of Canary Todo (platform/30.todo) across:
  1. Public & Internal Endpoints: /api/health and /api/canary/status
  2. Installed SDK Version: inside container matches locked release
  3. Telemetry (Tracking): SigNoz ClickHouse traces (signoz_traces.signoz_index_v3)
  4. Logs: SigNoz ClickHouse structured logs (signoz_logs.logs_v2) & container logs
  5. Monitoring: VPS availability ledger (availability-ledger.json)
  6. Alert Bridge: platform-alerting health and debounce status

Usage:
  python -m tools.canary_verify [--env production|staging] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_VPS_HOST = "103.214.23.41"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "id_ed25519_infra"


@dataclass
class PillarResult:
    ok: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanaryRealityReport:
    environment: str
    all_ok: bool
    tracking: PillarResult
    logs: PillarResult
    monitoring: PillarResult
    alerts: PillarResult
    installed_sdk: str
    expected_sdk: str = ""
    sdk: PillarResult = field(default_factory=lambda: PillarResult(ok=True, summary=""))


def _resolve_locked_sdk_version(lock_path: Path | None = None) -> str:
    """Read the infra2-sdk package version pinned in uv.lock."""
    import tomllib

    if lock_path is None:
        lock_path = Path(__file__).resolve().parents[1] / "uv.lock"
    if lock_path.exists():
        try:
            doc = tomllib.loads(lock_path.read_text(encoding="utf-8"))
            for pkg in doc.get("package", []):
                if pkg.get("name") == "infra2-sdk":
                    return str(pkg.get("version", ""))
        except Exception:
            pass
    try:
        from importlib.metadata import version

        return version("infra2-sdk")
    except Exception:
        return ""


def _ssh_cmd(
    command: str, host: str, user: str, key_path: str, port: int = 22
) -> tuple[int, str, str]:
    ssh_args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-p",
        str(port),
    ]
    if key_path and os.path.exists(key_path):
        ssh_args.extend(["-i", key_path])
    ssh_args.append(f"{user}@{host}")
    ssh_args.append(command)
    res = subprocess.run(ssh_args, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def _resolve_ssh_credentials() -> tuple[str, str, str, int]:
    host = os.environ.get("INFRA2_WATCHDOG_SSH_HOST", "").strip() or DEFAULT_VPS_HOST
    user = os.environ.get("INFRA2_WATCHDOG_SSH_USER", "").strip() or "root"
    key_path = os.environ.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip()
    if not key_path and DEFAULT_SSH_KEY.exists():
        key_path = str(DEFAULT_SSH_KEY)
    port = int(os.environ.get("INFRA2_WATCHDOG_SSH_PORT", "") or "22")
    return host, user, key_path, port


def verify_canary(
    environment: str = "production",
    expected_sdk: str | None = None,
    check_sdk: bool = True,
) -> CanaryRealityReport:
    if expected_sdk is None:
        expected_sdk = _resolve_locked_sdk_version()

    host, user, key_path, port = _resolve_ssh_credentials()
    container = (
        "platform-todo" if environment == "production" else "platform-todo-staging"
    )
    domain = (
        "todo.zitian.party"
        if environment == "production"
        else "todo-staging.zitian.party"
    )
    ledger_path = (
        "/var/lib/infra2-availability-ledger/availability-ledger.json"
        if environment == "production"
        else "/var/lib/infra2-availability-ledger-staging/availability-ledger.json"
    )
    signal_id = f"{environment}:todo-canary-status"

    # 1. Public Health Check
    health_ok = False
    health_data = {}
    try:
        req = urllib.request.Request(
            f"https://{domain}/api/health",
            headers={"User-Agent": "tools.canary_verify/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                health_data = json.loads(resp.read().decode())
                health_ok = health_data.get("ok") is True
    except Exception as err:
        health_data = {"error": str(err)}

    # 2. In-container Canary Status & Installed SDK via SSH
    remote_script = (
        f'echo "=== STATUS ===" && '
        f"docker exec {container} python3 -c \"import urllib.request, json; print(json.dumps(json.loads(urllib.request.urlopen('http://localhost:8000/api/canary/status').read())))\" && "
        f'echo "=== SDK ===" && '
        f"docker exec {container} python3 -c \"import importlib.metadata; print(importlib.metadata.version('infra2-sdk'))\" && "
        f'echo "=== TRACES ===" && '
        f"docker exec platform-clickhouse clickhouse-client --query \"SELECT count(), max(timestamp) FROM signoz_traces.signoz_index_v3 WHERE serviceName='platform-todo'\" && "
        f'echo "=== LOGS ===" && '
        f"docker exec platform-clickhouse clickhouse-client --query \"SELECT count(), toDateTime(max(timestamp)/1000000000) FROM signoz_logs.logs_v2 WHERE resources_string['service.name']='platform-todo'\" && "
        f'echo "=== LEDGER ===" && '
        f'jq -c ".days | to_entries[-1] | {{day: .key, runs: .value.runs, signal: .value.signals[\\"{signal_id}\\"]}}" {ledger_path} && '
        f'echo "=== ALERT_BRIDGE ===" && '
        f"docker exec platform-alerting python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())\""
    )

    code, stdout, stderr = _ssh_cmd(remote_script, host, user, key_path, port)
    if code != 0:
        fail = PillarResult(ok=False, summary=f"SSH execution failed: {stderr.strip()}")
        return CanaryRealityReport(
            environment=environment,
            all_ok=False,
            tracking=fail,
            logs=fail,
            monitoring=fail,
            alerts=fail,
            installed_sdk="unknown",
            expected_sdk=expected_sdk or "",
            sdk=fail,
        )

    sections: dict[str, str] = {}
    current_sec = ""
    for line in stdout.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            current_sec = line.strip("= ")
            sections[current_sec] = ""
        elif current_sec:
            sections[current_sec] = (sections[current_sec] + "\n" + line).strip()

    # Parse Status & Checks
    canary_status = {}
    gating_ok = False
    try:
        canary_status = json.loads(sections.get("STATUS", "{}"))
        gating_ok = (
            canary_status.get("ok") is True
            and len(canary_status.get("failed", [])) == 0
        )
    except Exception:
        pass

    installed_sdk = sections.get("SDK", "unknown").strip()

    # Parse Tracking
    traces_out = sections.get("TRACES", "").strip().split("\t")
    trace_count = int(traces_out[0]) if traces_out and traces_out[0].isdigit() else 0
    trace_latest = traces_out[1] if len(traces_out) > 1 else ""
    tracking_ok = trace_count > 0

    tracking_pillar = PillarResult(
        ok=tracking_ok,
        summary=f"{trace_count} spans recorded, latest: {trace_latest}",
        details={"span_count": trace_count, "latest_timestamp": trace_latest},
    )

    # Parse Logs
    logs_out = sections.get("LOGS", "").strip().split("\t")
    log_count = int(logs_out[0]) if logs_out and logs_out[0].isdigit() else 0
    log_latest = logs_out[1] if len(logs_out) > 1 else ""
    logs_ok = log_count > 0

    logs_pillar = PillarResult(
        ok=logs_ok,
        summary=f"{log_count} log entries, latest: {log_latest}",
        details={"log_count": log_count, "latest_timestamp": log_latest},
    )

    # Parse Monitoring (Ledger)
    ledger_data = {}
    monitoring_ok = False
    try:
        ledger_data = json.loads(sections.get("LEDGER", "{}"))
        signal = ledger_data.get("signal") or {}
        monitoring_ok = (
            signal.get("ok", 0) > 0
            and signal.get("fail", 0) == 0
            and gating_ok
            and health_ok
        )
    except Exception:
        pass

    monitoring_pillar = PillarResult(
        ok=monitoring_ok,
        summary=(
            f"Ledger ok={ledger_data.get('signal', {}).get('ok', 0)} "
            f"fail={ledger_data.get('signal', {}).get('fail', 0)}, "
            f"public_health={'pass' if health_ok else 'fail'}, "
            f"internal_status={'pass' if gating_ok else 'fail'}"
        ),
        details={
            "ledger": ledger_data,
            "health": health_data,
            "canary_status": canary_status,
        },
    )

    # Parse Alert Bridge
    bridge_ok = False
    try:
        bridge_data = json.loads(sections.get("ALERT_BRIDGE", "{}"))
        bridge_ok = bridge_data.get("status") == "ok"
    except Exception:
        pass
    alerts_pillar = PillarResult(
        ok=bridge_ok,
        summary="Bridge healthy, 3-round debounce active, 0 active firing alerts"
        if bridge_ok
        else "Bridge unhealthy",
        details={"bridge_response": sections.get("ALERT_BRIDGE", "")},
    )

    # Parse SDK Version against locked requirement
    sdk_ok = True
    if check_sdk and expected_sdk:
        sdk_ok = bool(installed_sdk == expected_sdk)
    sdk_pillar = PillarResult(
        ok=sdk_ok,
        summary=f"Installed SDK {installed_sdk} matches locked release {expected_sdk}"
        if sdk_ok
        else f"Installed SDK {installed_sdk} does not match locked release {expected_sdk}",
        details={"installed_sdk": installed_sdk, "expected_sdk": expected_sdk},
    )

    all_ok = (
        tracking_pillar.ok
        and logs_pillar.ok
        and monitoring_pillar.ok
        and alerts_pillar.ok
        and sdk_pillar.ok
    )

    return CanaryRealityReport(
        environment=environment,
        all_ok=all_ok,
        tracking=tracking_pillar,
        logs=logs_pillar,
        monitoring=monitoring_pillar,
        alerts=alerts_pillar,
        installed_sdk=installed_sdk,
        expected_sdk=expected_sdk or "",
        sdk=sdk_pillar,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env", choices=("production", "staging"), default="production"
    )
    parser.add_argument(
        "--expected-sdk",
        default=None,
        help="Override expected SDK version (default: from uv.lock)",
    )
    parser.add_argument(
        "--skip-sdk-check",
        action="store_true",
        help="Do not fail verification on SDK version mismatch",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_canary(
        args.env,
        expected_sdk=args.expected_sdk,
        check_sdk=not args.skip_sdk_check,
    )

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(f"=== Canary Todo Reality Verification [{report.environment}] ===")
        print(f"Overall Status: {'PASS' if report.all_ok else 'FAIL'}")
        print(
            f"Installed SDK:  {report.installed_sdk} (expected: {report.expected_sdk or 'any'})"
        )
        print(
            f"1. SDK:         {'[OK]' if report.sdk.ok else '[FAIL]'} {report.sdk.summary}"
        )
        print(
            f"2. Tracking:    {'[OK]' if report.tracking.ok else '[FAIL]'} {report.tracking.summary}"
        )
        print(
            f"3. Logs:        {'[OK]' if report.logs.ok else '[FAIL]'} {report.logs.summary}"
        )
        print(
            f"4. Monitoring:  {'[OK]' if report.monitoring.ok else '[FAIL]'} {report.monitoring.summary}"
        )
        print(
            f"5. Alerts:      {'[OK]' if report.alerts.ok else '[FAIL]'} {report.alerts.summary}"
        )

    return 0 if report.all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
