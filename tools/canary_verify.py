#!/usr/bin/env python3
"""Canary Todo 4-Pillar Touch Reality Physical Verification Tool.

Verifies the physical execution state of Canary Todo (platform/30.todo) across:
  1. Public & Internal Endpoints: /api/health and /api/canary/status
  2. Installed SDK Version: inside container matches locked release
  3. Telemetry (Tracking): SigNoz ClickHouse traces, scoped to the environment
  4. Logs: SigNoz ClickHouse structured logs, scoped to the environment
  5. Monitoring: VPS availability ledger (availability-ledger.json)
  6. Alert Bridge: platform-alerting health status and delivery mode

Usage:
  python -m tools.canary_verify [--env production|staging] [--json]

The trace and log queries come from `libs.observability.signoz_queries`. They
filter on the service, on the environment and on the freshness window.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from libs.observability.signoz_queries import (
    DEFAULT_WINDOW_SECONDS,
    logs_query,
    traces_query,
)

TELEMETRY_SERVICE_NAME = "platform-todo"
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


def _evaluate_freshness(
    count: int,
    latest_str: str,
    pillar_name: str,
    max_age_seconds: int = DEFAULT_WINDOW_SECONDS,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Fail-closed freshness evaluator for Canary telemetry.

    Validates that:
    1. count > 0
    2. latest_str is non-empty and parses as UTC datetime
    3. age <= max_age_seconds
    4. clock skew is within tolerance (age >= -60s)
    """
    details: dict[str, Any] = {
        "count": count,
        "latest_timestamp": latest_str,
        "max_age_seconds": max_age_seconds,
    }
    if count is None or count <= 0:
        return (
            False,
            f"0 {pillar_name} recorded in the last {max_age_seconds}s",
            details,
        )

    if not latest_str or not str(latest_str).strip():
        return False, f"Missing timestamp for {pillar_name}", details

    raw_ts = str(latest_str).strip()
    clean_ts = raw_ts.split(".")[0]

    try:
        latest = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except Exception as err:
        details["parse_error"] = str(err)
        return False, f"Malformed timestamp '{raw_ts}': {err}", details

    now_utc = now if now is not None else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    age_seconds = (now_utc - latest).total_seconds()
    details["age_seconds"] = age_seconds

    if age_seconds < -60:
        return (
            False,
            f"Clock skew detected for {pillar_name}: timestamp {raw_ts} is {int(-age_seconds)}s in future",
            details,
        )

    if age_seconds > max_age_seconds:
        return (
            False,
            f"Stale {pillar_name}: latest entry was at {raw_ts} (age: {int(age_seconds)}s > {max_age_seconds}s)",
            details,
        )

    fresh_label = f"{int(max(0.0, age_seconds))}s ago"
    return (
        True,
        f"{count} {pillar_name} recorded, latest: {raw_ts} (fresh: {fresh_label})",
        details,
    )


def verify_canary(
    environment: str = "production",
    expected_sdk: str | None = None,
    check_sdk: bool = True,
    max_age_seconds: int = DEFAULT_WINDOW_SECONDS,
    now: datetime | None = None,
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
    # The same environment and the same window scope both telemetry queries.
    # shlex.quote protects the SQL at the remote shell boundary.
    traces_sql = shlex.quote(
        traces_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
        )
    )
    logs_sql = shlex.quote(
        logs_query(
            service_name=TELEMETRY_SERVICE_NAME,
            environment=environment,
            window_seconds=max_age_seconds,
        )
    )

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
        f"docker exec platform-clickhouse clickhouse-client --query {traces_sql} && "
        f'echo "=== LOGS ===" && '
        f"docker exec platform-clickhouse clickhouse-client --query {logs_sql} && "
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
    tracking_ok, tracking_summary, tracking_details = _evaluate_freshness(
        trace_count,
        trace_latest,
        pillar_name="spans",
        max_age_seconds=max_age_seconds,
        now=now,
    )
    tracking_details["span_count"] = trace_count
    tracking_details["environment"] = environment
    tracking_details["window_seconds"] = max_age_seconds
    tracking_pillar = PillarResult(
        ok=tracking_ok,
        summary=tracking_summary,
        details=tracking_details,
    )

    # Parse Logs
    logs_out = sections.get("LOGS", "").strip().split("\t")
    log_count = int(logs_out[0]) if logs_out and logs_out[0].isdigit() else 0
    log_latest = logs_out[1] if len(logs_out) > 1 else ""
    logs_ok, logs_summary, logs_details = _evaluate_freshness(
        log_count,
        log_latest,
        pillar_name="log entries",
        max_age_seconds=max_age_seconds,
        now=now,
    )
    logs_details["log_count"] = log_count
    logs_details["environment"] = environment
    logs_details["window_seconds"] = max_age_seconds
    logs_pillar = PillarResult(
        ok=logs_ok,
        summary=logs_summary,
        details=logs_details,
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
    bridge_status = "missing"
    bridge_mode = ""
    bridge_summary = "Bridge response is not valid JSON"
    try:
        bridge_data = json.loads(sections.get("ALERT_BRIDGE", "{}"))
        if isinstance(bridge_data, dict):
            bridge_status = str(bridge_data.get("status", "missing"))
            bridge_mode = str(bridge_data.get("mode", ""))
            bridge_ok = bridge_status == "ok"
            bridge_summary = (
                f"Bridge {'healthy' if bridge_ok else 'unhealthy'}: "
                f"status={bridge_status}"
                + (f", mode={bridge_mode}" if bridge_mode else "")
            )
        else:
            bridge_summary = "Bridge response is not a JSON object"
    except ValueError:
        pass
    alerts_pillar = PillarResult(
        ok=bridge_ok,
        summary=bridge_summary,
        details={
            "bridge_response": sections.get("ALERT_BRIDGE", ""),
            "status": bridge_status,
            "mode": bridge_mode,
        },
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


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {raw!r}")
    return value


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
    parser.add_argument(
        "--max-age-seconds",
        type=_positive_int,
        default=DEFAULT_WINDOW_SECONDS,
        help=(
            "Query window and maximum age of the latest traces and logs, in "
            f"seconds (default: {DEFAULT_WINDOW_SECONDS})"
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_canary(
        args.env,
        expected_sdk=args.expected_sdk,
        check_sdk=not args.skip_sdk_check,
        max_age_seconds=args.max_age_seconds,
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
