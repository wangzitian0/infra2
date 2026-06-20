#!/usr/bin/env python3
"""Synthetic write-then-query probes for observability backends."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
import uuid

from tools.openpanel_clients import openpanel_env


DEFAULT_INTERVAL_SECONDS = 5 * 60
DEFAULT_STATE_FILE = "/tmp/observability_roundtrip_probe_state.json"
DEFAULT_QUERY_WAIT_SECONDS = 30
DEFAULT_QUERY_POLL_SECONDS = 2

SIGNOZ_SERVICE_NAME = "infra-observability-canary"
SIGNOZ_LOG_MESSAGE = "infra-signoz-roundtrip-canary"
OPENPANEL_EVENT_NAME = "infra_observability_roundtrip_canary"


@dataclass(frozen=True)
class ProbeResult:
    backend: str
    nonce: str
    count: int
    mode: str = "sent"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=("signoz", "openpanel"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--nonce", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _load_env_file(Path(os.getenv("ALERTING_ENV_FILE", "/secrets/.env")))
    state_path = Path(os.getenv("OBS_ROUNDTRIP_STATE_FILE", DEFAULT_STATE_FILE))
    interval_seconds = int(
        os.getenv("OBS_ROUNDTRIP_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS))
    )
    force = args.force or os.getenv("OBS_ROUNDTRIP_FORCE", "0") == "1"
    now = time.time()
    state = _load_state(state_path)

    if not force and not _due(state, args.backend, now, interval_seconds):
        result = {
            "status": "roundtrip-ok",
            "backend": args.backend,
            "mode": "suppressed",
            "last_success_at": state.get(args.backend, {}).get("last_success_at"),
        }
        _print_result(result, as_json=args.json)
        return 0

    nonce = args.nonce.strip() or uuid.uuid4().hex
    try:
        result = (
            run_signoz_roundtrip(nonce)
            if args.backend == "signoz"
            else run_openpanel_roundtrip(nonce)
        )
    except Exception as exc:  # noqa: BLE001 - command probe should emit one clear error.
        print(f"roundtrip-failed backend={args.backend} error={exc}", file=sys.stderr)
        return 1

    state[args.backend] = {"last_success_at": now, "last_nonce": nonce}
    _save_state(state_path, state)
    _print_result(
        {
            "status": "roundtrip-ok",
            "backend": result.backend,
            "mode": result.mode,
            "nonce": result.nonce,
            "count": result.count,
        },
        as_json=args.json,
    )
    return 0


def run_signoz_roundtrip(nonce: str) -> ProbeResult:
    collector_url = os.getenv(
        "SIGNOZ_OTLP_LOGS_URL",
        "http://platform-signoz-otel-collector:4318/v1/logs",
    )
    clickhouse_url = os.getenv(
        "SIGNOZ_CLICKHOUSE_URL",
        "http://platform-clickhouse:8123",
    )
    deploy_env = _deploy_env()
    _post_json(collector_url, _signoz_log_payload(nonce, deploy_env))
    query = (
        "SELECT count() FROM signoz_logs.distributed_logs_v2 "
        f"WHERE resources_string['service.name'] = {_ch_string(SIGNOZ_SERVICE_NAME)} "
        f"AND body LIKE {_ch_string('%' + nonce + '%')} "
        # distributed_logs_v2.timestamp is UInt64 nanoseconds, not DateTime64.
        # Comparing it against a DateTime64 bound makes ClickHouse coerce the
        # nanosecond integer into a decimal and overflow (Code 407,
        # DECIMAL_OVERFLOW) -> HTTP 500 -> the probe fails on every cycle.
        # Convert the bound to nanoseconds so both sides are UInt64.
        "AND timestamp >= toUnixTimestamp64Nano(now64(3) - INTERVAL 10 MINUTE)"
    )
    count = _wait_for_count(clickhouse_url, query)
    return ProbeResult(backend="signoz", nonce=nonce, count=count)


def run_openpanel_roundtrip(nonce: str) -> ProbeResult:
    api_url = os.getenv(
        "OPENPANEL_ROUNDTRIP_API_URL",
        "http://platform-openpanel-api:3000",
    ).rstrip("/")
    clickhouse_url = os.getenv(
        "OPENPANEL_CLICKHOUSE_URL",
        "http://platform-openpanel-ch:8123",
    )
    deploy_env = _deploy_env()
    client_id = _openpanel_client_id(deploy_env)
    headers = {"openpanel-client-id": client_id}
    client_secret = os.getenv("OPENPANEL_CLIENT_SECRET", "").strip()
    if client_secret:
        headers["openpanel-client-secret"] = client_secret
    event_name = os.getenv("OPENPANEL_ROUNDTRIP_EVENT_NAME", OPENPANEL_EVENT_NAME)
    _post_json(
        f"{api_url}/track",
        {
            "type": "track",
            "payload": {
                "name": event_name,
                "properties": {
                    "source": "infra-canary",
                    "deployment_environment": deploy_env,
                    "infra_nonce": nonce,
                },
            },
        },
        headers=headers,
    )
    query = (
        "SELECT count() FROM openpanel.events "
        f"WHERE name = {_ch_string(event_name)} "
        f"AND properties['infra_nonce'] = {_ch_string(nonce)} "
        "AND created_at >= now64(3) - INTERVAL 10 MINUTE"
    )
    count = _wait_for_count(clickhouse_url, query)
    return ProbeResult(backend="openpanel", nonce=nonce, count=count)


def _signoz_log_payload(nonce: str, deploy_env: str) -> dict:
    now_nanos = int(time.time() * 1_000_000_000)
    body = f"{SIGNOZ_LOG_MESSAGE} nonce={nonce}"
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        _string_attr("service.name", SIGNOZ_SERVICE_NAME),
                        _string_attr("deployment.environment", deploy_env),
                        _string_attr("infra.canary.nonce", nonce),
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "infra2.observability_roundtrip_probe"},
                        "logRecords": [
                            {
                                "timeUnixNano": str(now_nanos),
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": body},
                                "attributes": [
                                    _string_attr("infra_nonce", nonce),
                                    _string_attr("probe", "signoz-roundtrip"),
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _string_attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _openpanel_client_id(deploy_env: str) -> str:
    configured = os.getenv("OPENPANEL_CLIENT_ID", "").strip()
    if configured:
        return configured
    client_id = openpanel_env(deploy_env).get("OPENPANEL_CLIENT_ID", "")
    if not client_id:
        raise RuntimeError(f"no OpenPanel client id for environment {deploy_env!r}")
    return client_id


def _wait_for_count(clickhouse_url: str, query: str) -> int:
    deadline = time.monotonic() + float(
        os.getenv("OBS_ROUNDTRIP_QUERY_WAIT_SECONDS", str(DEFAULT_QUERY_WAIT_SECONDS))
    )
    poll_seconds = float(
        os.getenv("OBS_ROUNDTRIP_QUERY_POLL_SECONDS", str(DEFAULT_QUERY_POLL_SECONDS))
    )
    last_count = 0
    while True:
        last_count = _clickhouse_count(clickhouse_url, query)
        if last_count > 0:
            return last_count
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"round-trip nonce not observed; last_count={last_count}"
            )
        time.sleep(max(0.1, poll_seconds))


def _clickhouse_count(base_url: str, query: str) -> int:
    body = _clickhouse_query(base_url, query).strip()
    if not body:
        return 0
    first = body.splitlines()[0].strip()
    try:
        return int(first)
    except ValueError as exc:
        raise RuntimeError(
            f"unexpected ClickHouse count response: {body[:200]!r}"
        ) from exc


def _clickhouse_query(base_url: str, query: str) -> str:
    request = urllib.request.Request(
        _normalize_url(base_url),
        data=query.encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(  # noqa: S310 - internal Docker-network probe URL.
        request,
        timeout=float(os.getenv("OBS_ROUNDTRIP_HTTP_TIMEOUT_SECONDS", "10")),
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def _post_json(
    url: str, payload: dict, *, headers: dict[str, str] | None = None
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(  # noqa: S310 - internal Docker-network probe URL.
        request,
        timeout=float(os.getenv("OBS_ROUNDTRIP_HTTP_TIMEOUT_SECONDS", "10")),
    ) as response:
        response_body = response.read().decode("utf-8", errors="replace")
    if not response_body:
        return {}
    try:
        return json.loads(response_body)
    except json.JSONDecodeError:
        return {"raw": response_body}


def _normalize_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"invalid URL: {base_url!r}")
    return base_url


def _ch_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _deploy_env() -> str:
    raw = (
        os.getenv("OBS_ROUNDTRIP_ENV")
        or os.getenv("INFRA_PROBE_HEARTBEAT_ENV")
        or os.getenv("ENV")
        or os.getenv("DEPLOY_ENV")
        or "production"
    )
    return {"prod": "production"}.get(raw.strip().lower(), raw.strip().lower())


def _due(state: dict, backend: str, now: float, interval_seconds: int) -> bool:
    last_success = float(state.get(backend, {}).get("last_success_at") or 0)
    return last_success <= 0 or now - last_success >= max(1, interval_seconds)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), _unquote(value.strip()))


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _print_result(result: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return
    detail = " ".join(f"{key}={value}" for key, value in result.items())
    print(detail)


if __name__ == "__main__":
    raise SystemExit(main())
