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


class InvalidFeishuAppConfig(AlertingError):
    """Raised when Feishu app delivery config is incomplete or unsafe."""


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


def build_feishu_app_message_payload(chat_id: str, text: str) -> dict[str, Any]:
    """Build a Feishu OpenAPI text message payload."""
    message = text.strip() or "SigNoz alert"
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[: MAX_MESSAGE_CHARS - 14].rstrip() + "\n...[truncated]"
    return {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": message}, ensure_ascii=False),
    }


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


def deliver_feishu_app_text(
    *,
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    api_base: str = "https://open.feishu.cn",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send a text message to a Feishu chat using app bot OpenAPI."""
    base = validate_feishu_api_base(api_base)
    safe_app_id = _required("FEISHU_APP_ID", app_id)
    safe_app_secret = _required("FEISHU_APP_SECRET", app_secret)
    safe_chat_id = _required("FEISHU_CHAT_ID", chat_id)

    token_response = _post_json(
        f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        {
            "app_id": safe_app_id,
            "app_secret": safe_app_secret,
        },
        timeout=timeout,
    )
    access_token = token_response.get("tenant_access_token")
    if not access_token:
        raise FeishuDeliveryError("Feishu tenant_access_token missing in response")

    return _post_json(
        f"{base}/open-apis/im/v1/messages?receive_id_type=chat_id",
        build_feishu_app_message_payload(safe_chat_id, text),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )


def validate_feishu_api_base(api_base: str) -> str:
    """Validate Feishu/Lark OpenAPI base URL."""
    candidate = (api_base or "https://open.feishu.cn").strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise InvalidFeishuAppConfig("Feishu API base must use https")
    if parsed.hostname not in FEISHU_WEBHOOK_HOSTS:
        allowed = ", ".join(sorted(FEISHU_WEBHOOK_HOSTS))
        raise InvalidFeishuAppConfig(f"Feishu API host must be one of: {allowed}")
    return candidate


def redacted_url(url: str) -> str:
    """Return a webhook URL without the secret token."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/open-apis/bot/v2/hook/***"


def redacted_app_config(app_id: str, chat_id: str, api_base: str) -> dict[str, str]:
    """Return safe Feishu app delivery metadata."""
    redacted_app_id = f"{app_id[:8]}..." if app_id else ""
    redacted_chat_id = f"{chat_id[:8]}..." if chat_id else ""
    return {
        "api_base": validate_feishu_api_base(api_base),
        "app_id": redacted_app_id,
        "chat_id": redacted_chat_id,
    }


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            response_body = response.read().decode("utf-8")
    except OSError as exc:
        raise FeishuDeliveryError("Feishu OpenAPI request failed") from exc

    try:
        decoded = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu OpenAPI returned invalid JSON") from exc

    code = decoded.get("code")
    if code not in (None, 0):
        message = decoded.get("msg") or decoded.get("message") or "unknown error"
        raise FeishuDeliveryError(f"Feishu OpenAPI rejected message: {message}")
    return decoded


def _required(name: str, value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidFeishuAppConfig(f"{name} is required")
    return candidate


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())
