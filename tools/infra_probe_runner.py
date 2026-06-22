#!/usr/bin/env python3
"""Run infra service probes and alert through the internal bridge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import traceback
import time
from pathlib import Path
from typing import NamedTuple
from urllib.request import Request, urlopen

from libs.infra_probes import (
    HTTP_PROBE_HEADERS,
    build_probe_alert_payload,
    failed_results,
    parse_probe_specs,
    post_alert_bridge_payload,
    run_probes,
)


DEFAULT_PROBE_SPECS = """
alert-bridge-http|http|http://platform-alerting:8080/health|200|critical|5
"""
DEFAULT_PROBE_INTERVAL_SECONDS = 60
DEFAULT_RENOTIFY_SECONDS = 1800
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_THRESHOLD = 2
DEFAULT_STATE_FILE = "/tmp/infra_probe_runner_state.json"
# A probe that has never once succeeded is treated as a broken/misconfigured probe, not a
# real outage — routed to this distinct, warning-severity alert so it cannot page critical.
MISCONFIGURED_ALERT_NAME = "InfraProbeMisconfigured"
MISCONFIGURED_SEVERITY = "warning"


def _log_send(stream_key: str, results: list, severity_override: str | None) -> None:
    """Structured line on every real send, so the alerting loop is observable (it was a
    black box — failures-only, nothing on a successful send)."""
    failures = failed_results(results)
    status = "firing" if failures else "resolved"
    if severity_override:
        severity = severity_override
    else:
        severity = failures[0].spec.severity if failures else "info"
    names = ",".join(sorted(r.spec.name for r in failures)) or "-"
    print(
        f"probe-runner send stream={stream_key} status={status} "
        f"severity={severity} failures={len(failures)} probes={names}",
        flush=True,
    )


def _cascades_to_failing_root(
    name: str, dep_of: dict[str, str], failed_names: set[str]
) -> bool:
    """True if `name` is a cascade symptom to suppress: its `depends_on` chain leads to a
    failing ROOT. False — i.e. keep alerting — when the immediate dependency is healthy (a
    real independent failure) OR the chain forms a cycle (no root → fail closed). A probe
    with no `depends_on` is itself a root and is never suppressed.
    """
    cur = dep_of.get(name)
    if not cur:
        return False  # no dependency -> this IS a root, always page
    seen = {name}
    while cur:
        if cur not in failed_names:
            return False  # dependency healthy -> independent failure, do not suppress
        if cur in seen:
            return False  # cycle -> no real root -> fail closed (alert)
        seen.add(cur)
        cur = dep_of.get(cur)
    return True  # reached a failing node with no failing dependency = the root


class ProbeGroup(NamedTuple):
    name: str
    raw_specs: str
    alert_name: str
    external_url: str


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _load_env_file(Path(os.getenv("ALERTING_ENV_FILE", "/secrets/.env")))
    interval = int(
        os.getenv("INFRA_PROBE_INTERVAL_SECONDS", str(DEFAULT_PROBE_INTERVAL_SECONDS))
    )
    renotify_seconds = int(
        os.getenv("INFRA_PROBE_RENOTIFY_SECONDS", str(DEFAULT_RENOTIFY_SECONDS))
    )
    failure_threshold = int(
        os.getenv("INFRA_PROBE_FAILURE_THRESHOLD", str(DEFAULT_FAILURE_THRESHOLD))
    )
    recovery_threshold = int(
        os.getenv("INFRA_PROBE_RECOVERY_THRESHOLD", str(DEFAULT_RECOVERY_THRESHOLD))
    )
    state_path = Path(os.getenv("INFRA_PROBE_STATE_FILE", DEFAULT_STATE_FILE))
    # Mirror run_once's gate: dry-run must not emit heartbeat/state (no misleading
    # liveness signal during local/debug runs).
    dry_run = os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1"

    while True:
        # Liveness-first heartbeat (#369): prove the runner is alive at the START of
        # every iteration, BEFORE running probes. Crash/OOM/hang during a probe cycle
        # then surfaces as heartbeat staleness within one interval — independent of how
        # long the probe cycle takes. The post-probe heartbeat below still carries the
        # ok/failure status. Heartbeat writes are throttled watchdog-side, so the extra
        # ping per loop costs no KV quota.
        if args.loop and not dry_run:
            _post_heartbeat(ok=True, detail="probe loop iteration starting")
        try:
            exit_code = run_once(
                as_json=args.json,
                renotify_seconds=renotify_seconds,
                failure_threshold=failure_threshold,
                recovery_threshold=recovery_threshold,
                state_path=state_path,
            )
        except Exception as exc:  # noqa: BLE001 - looped probes must keep running.
            print(f"infra probe iteration failed: {exc}", flush=True)
            traceback.print_exc()
            _post_heartbeat(ok=False, detail=f"iteration failed: {exc}")
            exit_code = 1
        if not args.loop:
            return exit_code
        time.sleep(interval)


def _deploy_env() -> str:
    """Resolve the deploy environment with the same precedence the heartbeat uses
    (the runner sets INFRA_PROBE_HEARTBEAT_ENV, not always ENV), normalized."""
    return (
        (
            os.getenv("INFRA_PROBE_HEARTBEAT_ENV")
            or os.getenv("ENV")
            or os.getenv("DEPLOY_ENV")
            or "production"
        )
        .strip()
        .lower()
    )


def _host_specs_for_env(specs: list) -> list:
    """Host `resource` probes describe the single shared host, so only the
    production runner should run them — otherwise the prod and staging runners
    (co-located on that host) both alert on the same CPU/mem/disk threshold.
    """
    if _deploy_env() == "production":
        return specs
    return [spec for spec in specs if spec.kind != "resource"]


def run_once(
    *,
    as_json: bool = False,
    renotify_seconds: int | None = None,
    failure_threshold: int | None = None,
    recovery_threshold: int | None = None,
    state_path: Path | None = None,
) -> int:
    groups = _probe_groups()
    state_path = state_path or Path(
        os.getenv("INFRA_PROBE_STATE_FILE", DEFAULT_STATE_FILE)
    )
    renotify_seconds = (
        renotify_seconds
        if renotify_seconds is not None
        else int(
            os.getenv("INFRA_PROBE_RENOTIFY_SECONDS", str(DEFAULT_RENOTIFY_SECONDS))
        )
    )
    failure_threshold = (
        failure_threshold
        if failure_threshold is not None
        else int(
            os.getenv("INFRA_PROBE_FAILURE_THRESHOLD", str(DEFAULT_FAILURE_THRESHOLD))
        )
    )
    recovery_threshold = (
        recovery_threshold
        if recovery_threshold is not None
        else int(
            os.getenv("INFRA_PROBE_RECOVERY_THRESHOLD", str(DEFAULT_RECOVERY_THRESHOLD))
        )
    )
    state = _load_state(state_path)
    now = time.time()
    any_failures = False
    json_results: dict[str, list[dict]] = {}
    dry_run = os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1"
    ever_succeeded = set(state.get("ever_succeeded", []))

    for group in groups:
        specs = _host_specs_for_env(parse_probe_specs(group.raw_specs))
        results = run_probes(specs)
        failures = failed_results(results)
        any_failures = any_failures or bool(failures)
        json_results[group.name] = [result.to_dict() for result in results]

        # Learn which probes have EVER passed (this cycle's passes included).
        for result in results:
            if result.ok:
                ever_succeeded.add(result.spec.name)

        # Cascade suppression: a probe whose declared `depends_on` chain reaches a failing
        # ROOT is a downstream symptom — suppress its alert and page the root only (page the
        # deepest failed node, not the cascade). E.g. signoz-roundtrip fails because the otel
        # collector is down -> page the collector, mute the round-trip.
        # A `depends_on` CYCLE (incl. self-dependency) has no root, so its members are NOT
        # suppressed (fail closed -> alert) — otherwise a cycle of all-failing probes would
        # silently swallow every page.
        failed_names = {r.spec.name for r in failures}
        dep_of = {r.spec.name: r.spec.depends_on for r in failures if r.spec.depends_on}
        cascaded = [
            r
            for r in failures
            if _cascades_to_failing_root(r.spec.name, dep_of, failed_names)
        ]
        cascaded_names = {r.spec.name for r in cascaded}
        for r in cascaded:
            print(
                f"probe-runner cascade-suppressed probe={r.spec.name} "
                f"root={r.spec.depends_on} (both failing)",
                flush=True,
            )
        results = [r for r in results if r.spec.name not in cascaded_names]
        failures = [r for r in failures if r.spec.name not in cascaded_names]

        # Split failures into two streams. ONLY `command` probes are eligible for the
        # misconfigured lane: they run code (the round-trips) that can be broken by a bug
        # so they may NEVER pass even against a healthy backend (the signoz-roundtrip
        # 500-storm). A `command` probe that has never once succeeded is therefore far more
        # likely a broken probe than an outage that began the instant the runner booted, so
        # route it to a quiet warning-severity `InfraProbeMisconfigured` lane instead of
        # paging critical. `http`/`tcp` liveness probes are NOT eligible — their failure is
        # always a real target failure — and every command round-trip is backed by an
        # `http` liveness probe that still carries the real-outage critical signal. Each
        # stream dedups independently.
        misconfig = [
            r
            for r in failures
            if r.spec.kind == "command" and r.spec.name not in ever_succeeded
        ]
        misconfig_names = {r.spec.name for r in misconfig}
        regression_results = [r for r in results if r.spec.name not in misconfig_names]

        streams = (
            (group.name, regression_results, group.alert_name, None),
            (
                f"{group.name}:misconfigured",
                misconfig,
                MISCONFIGURED_ALERT_NAME,
                MISCONFIGURED_SEVERITY,
            ),
        )
        for stream_key, stream_results, alert_name, severity_override in streams:
            payload = build_probe_alert_payload(
                stream_results,
                alert_name=alert_name,
                external_url=group.external_url,
                severity_override=severity_override,
            )
            if dry_run:
                if failed_results(stream_results):
                    _send_payload(payload)
                continue
            if _should_send(
                stream_key,
                stream_results,
                state,
                now,
                renotify_seconds,
                failure_threshold,
                recovery_threshold,
            ):
                if _maintenance_active(now):
                    continue
                _send_payload(payload)
                _record_sent(stream_key, stream_results, state, now)
                _log_send(stream_key, stream_results, severity_override)

    state["ever_succeeded"] = sorted(ever_succeeded)
    if as_json:
        print(json.dumps(json_results, indent=2))
    if not dry_run:
        if _maintenance_active(now):
            _post_heartbeat(ok=True, detail="probe loop suppressed during maintenance")
        else:
            _post_heartbeat(ok=not any_failures)
        _save_state(state_path, state)
    return 1 if any_failures else 0


def _probe_groups() -> list[ProbeGroup]:
    groups = [
        ProbeGroup(
            name="infra-service",
            raw_specs=os.getenv("INFRA_PROBE_SPECS", DEFAULT_PROBE_SPECS),
            alert_name="InfraServiceProbeFailed",
            external_url="infra2://platform/12.alerting/infra-probes",
        )
    ]
    public_specs = os.getenv("PUBLIC_ROUTE_PROBE_SPECS", "").strip()
    if public_specs:
        groups.append(
            ProbeGroup(
                name="public-route",
                raw_specs=public_specs,
                alert_name="InfraPublicRouteProbeFailed",
                external_url="infra2://platform/12.alerting/public-route-probes",
            )
        )
    return groups


def _send_payload(payload: dict) -> None:
    if os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1":
        print(json.dumps(payload, indent=2))
        return

    bridge_url = os.getenv(
        "ALERT_BRIDGE_URL",
        "http://platform-alerting:8080/signoz/webhook",
    )
    post_alert_bridge_payload(
        bridge_url,
        payload,
        username=os.getenv("BRIDGE_BASIC_AUTH_USERNAME", ""),
        password=os.getenv("BRIDGE_BASIC_AUTH_PASSWORD", ""),
    )


def _should_send(
    group_name: str,
    results: list,
    state: dict,
    now: float,
    renotify_seconds: int,
    failure_threshold: int,
    recovery_threshold: int,
) -> bool:
    failures = failed_results(results)
    group_state = state.setdefault("groups", {}).setdefault(group_name, {})
    if not failures:
        group_state["failure_count"] = 0
        if not group_state.get("active"):
            _record_resolved(group_name, state)
            return False
        recovery_count = int(group_state.get("recovery_count") or 0) + 1
        group_state["recovery_count"] = recovery_count
        if recovery_count < max(1, recovery_threshold):
            return False
        _record_resolved(group_name, state)
        return True

    fingerprint = _failure_fingerprint(results)
    pending_fingerprint = str(group_state.get("pending_fingerprint") or "")
    failure_count = (
        int(group_state.get("failure_count") or 0) + 1
        if fingerprint == pending_fingerprint
        else 1
    )
    group_state["pending_fingerprint"] = fingerprint
    group_state["failure_count"] = failure_count
    group_state["recovery_count"] = 0
    if failure_count < max(1, failure_threshold) and not group_state.get("active"):
        return False

    last_fingerprint = str(group_state.get("fingerprint") or "")
    last_alert_at = float(group_state.get("last_alert_at") or 0)
    return (
        not group_state.get("active")
        or fingerprint != last_fingerprint
        or now - last_alert_at >= renotify_seconds
    )


def _record_sent(group_name: str, results: list, state: dict, now: float) -> None:
    failures = failed_results(results)
    state.setdefault("groups", {})[group_name] = {
        "active": bool(failures),
        "fingerprint": _failure_fingerprint(results) if failures else "",
        "pending_fingerprint": _failure_fingerprint(results) if failures else "",
        "failure_count": int(
            state.get("groups", {}).get(group_name, {}).get("failure_count") or 0
        ),
        "recovery_count": 0,
        "last_alert_at": now,
    }


def _record_resolved(group_name: str, state: dict) -> None:
    state.setdefault("groups", {})[group_name] = {
        "active": False,
        "fingerprint": "",
        "pending_fingerprint": "",
        "failure_count": 0,
        "recovery_count": 0,
        "last_alert_at": 0,
    }


def _failure_fingerprint(results: list) -> str:
    failures = [
        {
            "name": result.spec.name,
            "kind": result.spec.kind,
            "observed": result.observed,
            "summary": result.summary,
        }
        for result in failed_results(results)
    ]
    encoded = json.dumps(failures, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": {}}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"infra probe state write failed: {exc}", flush=True)


def _post_heartbeat(*, ok: bool, detail: str = "") -> None:
    heartbeat_url = os.getenv("INFRA_PROBE_HEARTBEAT_URL", "").strip()
    if not heartbeat_url:
        return

    payload = {
        "env": _deploy_env(),
        "name": os.getenv("INFRA_PROBE_HEARTBEAT_NAME", "infra-probe-runner"),
        "ok": ok,
        "detail": detail or ("probe loop completed" if ok else "probe loop failed"),
        "timestamp": int(time.time()),
    }
    headers = {**HTTP_PROBE_HEADERS, "Content-Type": "application/json"}
    token = os.getenv("INFRA_PROBE_HEARTBEAT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        heartbeat_url,
        data=json.dumps(payload, sort_keys=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(  # noqa: S310 - operator-configured watchdog endpoint.
            request,
            timeout=float(os.getenv("INFRA_PROBE_HEARTBEAT_TIMEOUT", "5")),
        ) as response:
            response.read(1024)
    except OSError as exc:
        print(f"infra probe heartbeat failed: {exc}", flush=True)


def _maintenance_active(now: float | None = None) -> bool:
    raw_until = os.getenv("INFRA_PROBE_MAINTENANCE_UNTIL", "").strip()
    if not raw_until:
        return False
    try:
        until = float(raw_until)
    except ValueError:
        return False
    return (time.time() if now is None else now) < until


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")


if __name__ == "__main__":
    raise SystemExit(main())
