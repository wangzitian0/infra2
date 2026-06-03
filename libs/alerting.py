"""Alerting helpers for SigNoz to Feishu delivery."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

FEISHU_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
MAX_ALERT_LINES = 8
MAX_MESSAGE_CHARS = 3500


class AlertingError(Exception):
    """Base alerting error."""


class InvalidWebhookUrl(AlertingError):
    """Raised when a Feishu webhook URL is unsafe or unsupported."""


class FeishuDeliveryError(AlertingError):
    """Raised when Feishu webhook delivery fails."""


@dataclass(frozen=True)
class BasicAuth:
    username: str
    password: str

    def header_value(self) -> str:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


def validate_feishu_webhook_url(url: str) -> str:
    """Validate and return a Feishu/Lark custom bot webhook URL."""
    candidate = (url or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise InvalidWebhookUrl("Feishu webhook URL must use https")
    if parsed.hostname not in FEISHU_WEBHOOK_HOSTS:
        allowed = ", ".join(sorted(FEISHU_WEBHOOK_HOSTS))
        raise InvalidWebhookUrl(f"Feishu webhook host must be one of: {allowed}")
    if not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise InvalidWebhookUrl("Feishu webhook path must be a custom bot hook")
    return candidate


def build_feishu_text_payload(text: str) -> dict[str, Any]:
    """Build a Feishu custom bot text payload."""
    message = text.strip() or "SigNoz alert"
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - 14].rstrip() + "\n...[truncated]"
    return {"msg_type": "text", "content": {"text": message}}


def format_signoz_alert(payload: dict[str, Any]) -> str:
    """Convert an Alertmanager/SigNoz webhook payload into readable text."""
    status = str(payload.get("status") or "unknown").upper()
    common_labels = _dict(payload.get("commonLabels"))
    common_annotations = _dict(payload.get("commonAnnotations"))
    group_labels = _dict(payload.get("groupLabels"))
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        alerts = []

    alert_name = (
        common_labels.get("alertname")
        or group_labels.get("alertname")
        or "SigNoz alert"
    )
    severity = common_labels.get("severity") or "unknown"
    summary = common_annotations.get("summary") or common_annotations.get("info") or ""

    lines = [
        f"[{status}] {alert_name}",
        f"Severity: {severity}",
        f"Alerts: {len(alerts)}",
    ]
    if summary:
        lines.append(f"Summary: {_one_line(summary)}")

    external_url = payload.get("externalURL")
    if isinstance(external_url, str) and external_url:
        lines.append(f"SigNoz: {external_url}")

    for index, alert in enumerate(alerts[:MAX_ALERT_LINES], start=1):
        if not isinstance(alert, dict):
            continue
        labels = _dict(alert.get("labels"))
        annotations = _dict(alert.get("annotations"))
        instance = labels.get("instance") or labels.get("service") or labels.get("job")
        alert_summary = annotations.get("summary") or annotations.get("description")
        detail = f"{index}. {labels.get('alertname') or alert_name}"
        if instance:
            detail += f" on {instance}"
        if alert_summary:
            detail += f" - {_one_line(alert_summary)}"
        lines.append(detail)

    if len(alerts) > MAX_ALERT_LINES:
        lines.append(f"...and {len(alerts) - MAX_ALERT_LINES} more alerts")

    return "\n".join(lines)


def build_signoz_channel_payload(
    *,
    channel_name: str,
    bridge_url: str,
    send_resolved: bool = True,
    basic_auth: BasicAuth | None = None,
) -> dict[str, Any]:
    """Build the SigNoz /api/v1/channels payload for the bridge webhook."""
    config: dict[str, Any] = {
        "send_resolved": bool(send_resolved),
        "url": bridge_url,
    }
    if basic_auth:
        config["http_config"] = {
            "basic_auth": {
                "username": basic_auth.username,
                "password": basic_auth.password,
            }
        }
    return {"name": channel_name, "webhook_configs": [config]}


def deliver_feishu_text(webhook_url: str, text: str, timeout: float = 10.0) -> dict[str, Any]:
    """Send a text message to Feishu and return the decoded response."""
    safe_url = validate_feishu_webhook_url(webhook_url)
    body = json.dumps(build_feishu_text_payload(text)).encode("utf-8")
    request = Request(
        safe_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            response_body = response.read().decode("utf-8")
    except OSError as exc:
        raise FeishuDeliveryError("Feishu webhook delivery failed") from exc

    try:
        decoded = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu webhook returned invalid JSON") from exc

    code = decoded.get("code")
    if code not in (None, 0):
        message = decoded.get("msg") or decoded.get("message") or "unknown error"
        raise FeishuDeliveryError(f"Feishu webhook rejected message: {message}")
    return decoded


def redacted_url(url: str) -> str:
    """Return a webhook URL without the secret token."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/open-apis/bot/v2/hook/***"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())
