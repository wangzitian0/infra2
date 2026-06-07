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

from libs.infra_probes import (
    build_probe_alert_payload,
    failed_results,
    parse_probe_specs,
    post_alert_bridge_payload,
    run_probes,
)


DEFAULT_PROBE_SPECS = """
alert-bridge-http|http|http://platform-alerting:8080/health|200|critical|5
"""
DEFAULT_PROBE_INTERVAL_SECONDS = 600
DEFAULT_RENOTIFY_SECONDS = 3600
DEFAULT_STATE_FILE = "/tmp/infra_probe_runner_state.json"


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
    state_path = Path(os.getenv("INFRA_PROBE_STATE_FILE", DEFAULT_STATE_FILE))

    while True:
        try:
            exit_code = run_once(
                as_json=args.json,
                renotify_seconds=renotify_seconds,
                state_path=state_path,
            )
        except Exception as exc:  # noqa: BLE001 - looped probes must keep running.
            print(f"infra probe iteration failed: {exc}", flush=True)
            traceback.print_exc()
            exit_code = 1
        if not args.loop:
            return exit_code
        time.sleep(interval)


def run_once(
    *,
    as_json: bool = False,
    renotify_seconds: int | None = None,
    state_path: Path | None = None,
) -> int:
    groups = _probe_groups()
    state_path = state_path or Path(os.getenv("INFRA_PROBE_STATE_FILE", DEFAULT_STATE_FILE))
    renotify_seconds = (
        renotify_seconds
        if renotify_seconds is not None
        else int(os.getenv("INFRA_PROBE_RENOTIFY_SECONDS", str(DEFAULT_RENOTIFY_SECONDS)))
    )
    state = _load_state(state_path)
    now = time.time()
    any_failures = False
    json_results: dict[str, list[dict]] = {}
    dry_run = os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1"

    for group in groups:
        specs = parse_probe_specs(group.raw_specs)
        results = run_probes(specs)
        failures = failed_results(results)
        any_failures = any_failures or bool(failures)
        json_results[group.name] = [result.to_dict() for result in results]

        payload = build_probe_alert_payload(
            results,
            alert_name=group.alert_name,
            external_url=group.external_url,
        )
        if dry_run and failures:
            _send_payload(payload)
        elif _should_send(group.name, results, state, now, renotify_seconds):
            _send_payload(payload)
            _record_sent(group.name, results, state, now)
        elif not failures:
            _record_resolved(group.name, state)

    if as_json:
        print(json.dumps(json_results, indent=2))
    if not dry_run:
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
) -> bool:
    failures = failed_results(results)
    group_state = state.setdefault("groups", {}).get(group_name, {})
    if not failures:
        return bool(group_state.get("active"))

    fingerprint = _failure_fingerprint(results)
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
        "last_alert_at": now,
    }


def _record_resolved(group_name: str, state: dict) -> None:
    state.setdefault("groups", {})[group_name] = {
        "active": False,
        "fingerprint": "",
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
    encoded = json.dumps(failures, sort_keys=True, separators=(",", ":")).encode("utf-8")
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


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
