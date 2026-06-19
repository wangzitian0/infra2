#!/usr/bin/env python3
"""Low-frequency real delivery proof for the alert bridge."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from libs.infra_probes import post_alert_bridge_payload


DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_STATE_FILE = "/tmp/alert_delivery_canary_state.json"
DEFAULT_BRIDGE_URL = "http://platform-alerting:8080/signoz/webhook"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _load_env_file(Path(os.getenv("ALERTING_ENV_FILE", "/secrets/.env")))
    state_path = Path(os.getenv("ALERT_DELIVERY_CANARY_STATE_FILE", DEFAULT_STATE_FILE))
    interval_seconds = int(
        os.getenv(
            "ALERT_DELIVERY_CANARY_INTERVAL_SECONDS",
            str(DEFAULT_INTERVAL_SECONDS),
        )
    )
    now = time.time()
    state = _load_state(state_path)
    force = args.force or os.getenv("ALERT_DELIVERY_CANARY_FORCE", "0") == "1"

    if not force and not _due(state, now, interval_seconds):
        result = {
            "status": "delivery-canary-ok",
            "mode": "suppressed",
            "last_success_at": state.get("last_success_at"),
        }
        _print_result(result, as_json=args.json)
        return 0

    nonce = os.getenv("ALERT_DELIVERY_CANARY_NONCE", "").strip() or uuid.uuid4().hex
    payload = build_payload(nonce)
    response = post_alert_bridge_payload(
        os.getenv("ALERT_BRIDGE_URL", DEFAULT_BRIDGE_URL),
        payload,
        username=os.getenv("BRIDGE_BASIC_AUTH_USERNAME", ""),
        password=os.getenv("BRIDGE_BASIC_AUTH_PASSWORD", ""),
        timeout=float(os.getenv("ALERT_DELIVERY_CANARY_TIMEOUT_SECONDS", "10")),
    )
    state.update({"last_success_at": now, "last_nonce": nonce})
    _save_state(state_path, state)
    result = {
        "status": "delivery-canary-ok",
        "mode": "sent",
        "nonce": nonce,
        "bridge_status": response.get("status", "accepted"),
    }
    _print_result(result, as_json=args.json)
    return 0


def build_payload(nonce: str) -> dict:
    return {
        "status": "firing",
        "commonLabels": {
            "alertname": "InfraAlertBridgeDeliveryCanary",
            "severity": "info",
            "team": "infra",
        },
        "commonAnnotations": {
            "summary": "Infra alert bridge delivery canary",
            "description": (
                "Low-frequency synthetic alert proving alert bridge to Feishu "
                f"delivery. nonce={nonce}"
            ),
        },
        "groupLabels": {"alertname": "InfraAlertBridgeDeliveryCanary"},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "InfraAlertBridgeDeliveryCanary",
                    "service": "platform-alerting",
                    "severity": "info",
                    "failure_domain": "alert-delivery-proof",
                },
                "annotations": {
                    "summary": "Infra alert bridge delivery canary",
                    "description": f"synthetic real-send proof nonce={nonce}",
                },
            }
        ],
        "externalURL": "infra2://platform/12.alerting/alert-delivery-canary",
    }


def _due(state: dict, now: float, interval_seconds: int) -> bool:
    last_success = float(state.get("last_success_at") or 0)
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
