"""Offline tests for SigNoz to Feishu alerting."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from libs.alerting import (
    BasicAuth,
    InvalidWebhookUrl,
    build_feishu_app_message_payload,
    build_feishu_text_payload,
    build_signoz_channel_payload,
    deliver_feishu_app_text,
    format_signoz_alert,
    redacted_url,
    validate_feishu_webhook_url,
)

ROOT = Path(__file__).resolve().parents[2]


def test_feishu_webhook_validation_is_https_and_host_scoped() -> None:
    """Infra-007 alerting: Feishu webhook secrets stay host-scoped and hidden."""
    valid = "https://open.feishu.cn/open-apis/bot/v2/hook/token"
    assert validate_feishu_webhook_url(valid) == valid
    assert redacted_url(valid).endswith("/open-apis/bot/v2/hook/***")

    with pytest.raises(InvalidWebhookUrl):
        validate_feishu_webhook_url("http://open.feishu.cn/open-apis/bot/v2/hook/token")
    with pytest.raises(InvalidWebhookUrl):
        validate_feishu_webhook_url("https://example.com/open-apis/bot/v2/hook/token")


def test_alertmanager_payload_is_rendered_as_feishu_text() -> None:
    """Infra-007 alerting: SigNoz webhook payloads become Feishu text messages."""
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "FinanceReportDown", "severity": "critical"},
        "commonAnnotations": {"summary": "Production API health check failed"},
        "externalURL": "https://signoz.zitian.party",
        "alerts": [
            {
                "labels": {
                    "alertname": "FinanceReportDown",
                    "instance": "finance_report-backend",
                },
                "annotations": {"summary": "GET /api/health returned 503"},
            }
        ],
    }

    text = format_signoz_alert(payload)
    assert "[FIRING] FinanceReportDown" in text
    assert "Severity: critical" in text
    assert "finance_report-backend" in text

    feishu_payload = build_feishu_text_payload(text)
    assert feishu_payload == {"msg_type": "text", "content": {"text": text}}


def test_signoz_channel_payload_targets_internal_bridge_with_optional_basic_auth() -> None:
    """Infra-007 alerting: SigNoz channel points to bridge, not the Feishu secret."""
    payload = build_signoz_channel_payload(
        channel_name="infra2-feishu-alerts-production",
        bridge_url="http://platform-alerting:8080/signoz/webhook",
        basic_auth=BasicAuth(username="signoz", password="secret"),
    )

    webhook = payload["webhook_configs"][0]
    assert payload["name"] == "infra2-feishu-alerts-production"
    assert webhook["url"] == "http://platform-alerting:8080/signoz/webhook"
    assert webhook["send_resolved"] is True
    assert webhook["http_config"]["basic_auth"]["username"] == "signoz"
    assert webhook["http_config"]["basic_auth"]["password"] == "secret"


def test_feishu_app_message_payload_stringifies_content() -> None:
    """Infra-007 alerting: Feishu app bot payload follows OpenAPI contract."""
    payload = build_feishu_app_message_payload("oc_test", "hello")
    assert payload["receive_id"] == "oc_test"
    assert payload["msg_type"] == "text"
    assert payload["content"] == '{"text": "hello"}'


def test_feishu_app_delivery_fetches_token_then_sends_message(monkeypatch) -> None:
    """Infra-007 alerting: app bot mode uses tenant token and chat_id."""
    requests = []

    class FakeResponse:
        def __init__(self, payload: dict):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "tenant-token"})
        return FakeResponse({"code": 0, "data": {"message_id": "om_test"}})

    monkeypatch.setattr("libs.alerting.urlopen", fake_urlopen)

    result = deliver_feishu_app_text(
        app_id="cli_test",
        app_secret="secret",
        chat_id="oc_test",
        text="hello",
    )

    assert result["data"]["message_id"] == "om_test"
    assert len(requests) == 2
    send_request, _timeout = requests[1]
    assert "receive_id_type=chat_id" in send_request.full_url
    assert send_request.headers["Authorization"] == "Bearer tenant-token"


def test_alerting_platform_service_contract_files_exist() -> None:
    """Infra-007 alerting: bridge service has deploy, Vault, and docs surfaces."""
    base = ROOT / "platform/12.alerting"
    required = [
        "Dockerfile",
        "README.md",
        "app.py",
        "compose.yaml",
        "deploy.py",
        "secrets.ctmpl",
        "shared_tasks.py",
        "vault-agent.hcl",
        "vault-policy.hcl",
    ]
    for name in required:
        assert (base / name).exists(), name

    compose = (base / "compose.yaml").read_text(encoding="utf-8")
    assert "platform-alerting-vault-agent${ENV_SUFFIX}" in compose
    assert "platform-alerting${ENV_SUFFIX}" in compose
    assert "open-apis/bot/v2/hook" not in compose
    assert "secrets:/secrets:ro" in compose


def test_alerting_shared_tasks_are_invoke_tasks() -> None:
    """Infra-007 alerting: invoke exposes channel payload and test-send tasks."""
    fake_invoke = types.ModuleType("invoke")
    fake_invoke.task = lambda func=None, **_kwargs: func if func else (lambda f: f)
    sys.modules.setdefault("invoke", fake_invoke)

    path = ROOT / "platform/12.alerting/shared_tasks.py"
    spec = importlib.util.spec_from_file_location("alerting_shared_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "status")
    assert hasattr(module, "print_channel_payload")
    assert hasattr(module, "test_feishu")
