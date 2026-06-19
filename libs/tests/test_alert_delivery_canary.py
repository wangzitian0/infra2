"""Tests for the low-frequency alert delivery canary."""

from __future__ import annotations

import json
import sys

from tools import alert_delivery_canary as canary


def test_build_payload_is_alertmanager_compatible() -> None:
    payload = canary.build_payload("nonce-1")

    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "InfraAlertBridgeDeliveryCanary"
    assert payload["commonLabels"]["severity"] == "info"
    assert payload["alerts"][0]["labels"]["service"] == "platform-alerting"
    assert "nonce-1" in payload["alerts"][0]["annotations"]["description"]


def test_main_sends_when_due_and_records_success(monkeypatch, tmp_path, capsys) -> None:
    state_path = tmp_path / "delivery-state.json"
    posted: list[dict] = []

    monkeypatch.setattr(sys, "argv", ["alert_delivery_canary.py"])
    monkeypatch.setattr(canary.time, "time", lambda: 1234.0)
    monkeypatch.setenv("ALERT_DELIVERY_CANARY_STATE_FILE", str(state_path))
    monkeypatch.setenv("ALERT_DELIVERY_CANARY_NONCE", "nonce-2")
    monkeypatch.setenv("ALERT_BRIDGE_URL", "http://bridge.example/signoz/webhook")
    monkeypatch.setenv("BRIDGE_BASIC_AUTH_USERNAME", "user")
    monkeypatch.setenv("BRIDGE_BASIC_AUTH_PASSWORD", "pass")

    def fake_post(url, payload, **kwargs):
        posted.append({"url": url, "payload": payload, "kwargs": kwargs})
        return {"status": "accepted"}

    monkeypatch.setattr(canary, "post_alert_bridge_payload", fake_post)

    assert canary.main() == 0

    out = capsys.readouterr().out
    assert "delivery-canary-ok" in out
    assert "mode=sent" in out
    assert posted[0]["url"] == "http://bridge.example/signoz/webhook"
    assert posted[0]["kwargs"]["username"] == "user"
    assert posted[0]["payload"]["commonLabels"]["alertname"] == (
        "InfraAlertBridgeDeliveryCanary"
    )
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "last_nonce": "nonce-2",
        "last_success_at": 1234.0,
    }


def test_main_suppresses_until_interval(monkeypatch, tmp_path, capsys) -> None:
    state_path = tmp_path / "delivery-state.json"
    state_path.write_text(
        json.dumps({"last_success_at": 1000.0, "last_nonce": "old"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["alert_delivery_canary.py"])
    monkeypatch.setattr(canary.time, "time", lambda: 1100.0)
    monkeypatch.setenv("ALERT_DELIVERY_CANARY_STATE_FILE", str(state_path))
    monkeypatch.setenv("ALERT_DELIVERY_CANARY_INTERVAL_SECONDS", "3600")
    monkeypatch.setattr(
        canary,
        "post_alert_bridge_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not send")
        ),
    )

    assert canary.main() == 0

    out = capsys.readouterr().out
    assert "delivery-canary-ok" in out
    assert "mode=suppressed" in out
