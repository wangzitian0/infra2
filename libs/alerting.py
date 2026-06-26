"""Alerting helpers for SigNoz to Feishu delivery."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

FEISHU_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"
SIGNOZ_ALERT_SCHEMA_VERSION = "v2alpha1"
SIGNOZ_ALERT_VERSION = "v5"
MAX_ALERT_LINES = 8
MAX_MESSAGE_CHARS = 3500
TRUNCATION_SUFFIX = "\n...[truncated]"


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
    if not parsed.path.startswith(FEISHU_WEBHOOK_PATH_PREFIX):
        raise InvalidWebhookUrl("Feishu webhook path must be a custom bot hook")
    token = parsed.path[len(FEISHU_WEBHOOK_PATH_PREFIX) :]
    if not token or "/" in token:
        raise InvalidWebhookUrl("Feishu webhook token must be a non-empty path segment")
    return candidate


def build_feishu_text_payload(text: str) -> dict[str, Any]:
    """Build a Feishu custom bot text payload."""
    message = text.strip() or "SigNoz alert"
    if len(message) > MAX_MESSAGE_CHARS:
        message = _truncate_message(message)
    return {"msg_type": "text", "content": {"text": message}}


def build_feishu_app_message_payload(chat_id: str, text: str) -> dict[str, Any]:
    """Build a Feishu OpenAPI text message payload."""
    message = text.strip() or "SigNoz alert"
    if len(message) > MAX_MESSAGE_CHARS:
        message = _truncate_message(message)
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


def build_signoz_log_alert_rule_payload(
    *,
    alert_name: str,
    service_name: str,
    channel_ids: list[str],
    summary: str,
    severity: str = "error",
    threshold: int = 0,
    eval_window: str = "5m0s",
    frequency: str = "1m",
) -> dict[str, Any]:
    """Build a SigNoz v2 threshold rule for OTEL log error count alerts."""
    return {
        "alert": _required("alert_name", alert_name),
        "alertType": "LOGS_BASED_ALERT",
        "ruleType": "threshold_rule",
        "condition": {
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": severity,
                        "target": int(threshold),
                        "matchType": "1",
                        "op": "1",
                        "channels": [
                            channel_id for channel_id in channel_ids if channel_id
                        ],
                        "targetUnit": "",
                    }
                ],
            },
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "unit": "",
                "builderQueries": {
                    "A": {
                        "dataSource": "logs",
                        "queryName": "A",
                        "aggregateOperator": "count",
                        "aggregateAttribute": _signoz_attribute("", "", ""),
                        "aggregations": [{"expression": "count() "}],
                        "filters": {
                            "op": "AND",
                            "items": [
                                {
                                    "id": "service.name--string--resource",
                                    "key": _signoz_attribute(
                                        "service.name", "string", "resource"
                                    ),
                                    "op": "=",
                                    "value": service_name,
                                },
                                {
                                    "id": "severity_text--string--tag",
                                    "key": _signoz_attribute(
                                        "severity_text", "string", "tag"
                                    ),
                                    "op": "in",
                                    "value": ["ERROR", "FATAL"],
                                },
                            ],
                        },
                        "groupBy": [],
                        "expression": "A",
                        "disabled": False,
                        "stepInterval": 60,
                        "having": [],
                        "limit": None,
                        "orderBy": [],
                        "legend": "",
                        "reduceTo": "sum",
                    }
                },
                "promQueries": {},
                "chQueries": {},
            },
            "selectedQueryName": "A",
            "alertOnAbsent": False,
            "requireMinPoints": False,
        },
        "evaluation": {
            "kind": "rolling",
            "spec": {"evalWindow": eval_window, "frequency": frequency},
        },
        "labels": {"severity": severity, "service": service_name, "team": "infra"},
        "annotations": {"description": summary, "summary": summary},
        "notificationSettings": {
            "groupBy": [],
            "renotify": {"enabled": False, "interval": "30m", "alertStates": []},
            "usePolicy": False,
        },
        "version": SIGNOZ_ALERT_VERSION,
        "schemaVersion": SIGNOZ_ALERT_SCHEMA_VERSION,
        "source": "infra2/platform/12.alerting",
        "disabled": False,
    }


def build_signoz_metric_alert_rule_payload(
    *,
    alert_name: str,
    promql: str,
    channel_ids: list[str],
    summary: str,
    service_name: str = "finance-report-backend",
    severity: str = "warning",
    threshold: float = 0,
    threshold_unit: str = "",
    op: str = "above",
    match_type: str = "at_least_once",
    eval_window: str = "5m0s",
    frequency: str = "1m",
    group_by: list[str] | None = None,
) -> dict[str, Any]:
    """Build a SigNoz v5 PromQL rule for metric alerts."""
    return {
        "alert": _required("alert_name", alert_name),
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "promql_rule",
        "condition": {
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": severity,
                        "target": float(threshold),
                        "matchType": _signoz_match_type(match_type),
                        "op": _signoz_threshold_op(op),
                        "channels": [
                            channel_id for channel_id in channel_ids if channel_id
                        ],
                        "targetUnit": threshold_unit,
                    }
                ],
            },
            "compositeQuery": {
                "queryType": "promql",
                "panelType": "graph",
                "unit": threshold_unit,
                "queries": [
                    {
                        "type": "promql",
                        "spec": {
                            "name": "A",
                            "query": _required("promql", promql),
                            "legend": "",
                            "disabled": False,
                        },
                    }
                ],
            },
            "selectedQueryName": "A",
            "alertOnAbsent": False,
            "requireMinPoints": True,
        },
        "evaluation": {
            "kind": "rolling",
            "spec": {"evalWindow": eval_window, "frequency": frequency},
        },
        "labels": {
            "severity": severity,
            "service": service_name,
            "team": "infra",
        },
        "annotations": {"description": summary, "summary": summary},
        "notificationSettings": {
            "groupBy": group_by or [],
            "renotify": {"enabled": False, "interval": "30m", "alertStates": []},
            "usePolicy": False,
        },
        "version": SIGNOZ_ALERT_VERSION,
        "schemaVersion": SIGNOZ_ALERT_SCHEMA_VERSION,
        "source": "infra2/platform/12.alerting",
        "disabled": False,
    }


def find_signoz_channel_id(channels_response: Any, channel_name: str) -> str | None:
    """Find a SigNoz notification channel id by name across known response shapes."""
    for channel in _iter_signoz_items(channels_response, collection_keys=("channels",)):
        if not isinstance(channel, dict):
            continue
        if channel.get("name") != channel_name:
            continue
        channel_id = channel.get("id") or channel.get("channelId")
        return str(channel_id) if channel_id else None
    return None


def find_signoz_rule_id(rules_response: Any, alert_name: str) -> str | None:
    """Find a SigNoz rule id by alert name across known response shapes."""
    for rule in _iter_signoz_items(rules_response, collection_keys=("rules", "items")):
        if not isinstance(rule, dict):
            continue
        if rule.get("alert") != alert_name and rule.get("name") != alert_name:
            continue
        rule_id = rule.get("id") or rule.get("ruleId")
        return str(rule_id) if rule_id else None
    return None


def deliver_feishu_text(
    webhook_url: str, text: str, timeout: float = 10.0
) -> dict[str, Any]:
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


def deliver_infra2_report(text: str, env: "Mapping[str, str] | None" = None) -> bool:
    """Post a daily-report message to the shared 'infra2 reports' Lark group via the infra2
    Feishu app bot. The single delivery entry every periodic REPORT reconciler uses (DNS drift,
    config drift, …) so the channel + credentials live in one place, not per-tool.

    Reads ``INFRA2_REPORTS_FEISHU_{APP_ID,APP_SECRET,CHAT_ID}`` (+ optional ``_API_BASE``).
    Returns True if delivered, False if not configured (so a not-yet-wired reconciler no-ops
    cleanly instead of erroring). A configured-but-failing delivery raises (a broken report
    path must be visible, not silently green).
    """
    e = os.environ if env is None else env
    app_id = e.get("INFRA2_REPORTS_FEISHU_APP_ID", "")
    app_secret = e.get("INFRA2_REPORTS_FEISHU_APP_SECRET", "")
    chat_id = e.get("INFRA2_REPORTS_FEISHU_CHAT_ID", "")
    if not (app_id and app_secret and chat_id):
        return False
    deliver_feishu_app_text(
        app_id=app_id,
        app_secret=app_secret,
        chat_id=chat_id,
        text=text,
        api_base=e.get("INFRA2_REPORTS_FEISHU_API_BASE", "https://open.feishu.cn"),
    )
    return True


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


def feishu_host_reachable(url: str, timeout: float = 3.0) -> bool:
    """Best-effort TCP reachability check to the Feishu/Lark host (port 443).

    Proves the bridge can *reach* Feishu without POSTing anything — so a
    "lark 畅通" probe can run every minute without spamming the real alert
    channel. Returns True iff a TCP connection to (host, 443) opens. Never
    raises; an unparseable/empty URL or any socket error returns False.
    """
    import socket

    host = urlparse((url or "").strip()).hostname
    if not host:
        return False
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


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


def deliver_out_of_band_text(
    env: Mapping[str, str], text: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    """Deliver text through the out-of-band Feishu webhook/app path.

    Shared by the weekly digest, the positive stability report, and the Google
    Drive sync token-expiry alert so every out-of-band sender selects the
    delivery mode identically. Prefers ``INFRA2_OUT_OF_BAND_*`` settings (used in
    GitHub Actions) and falls back to the generic ``ALERT_DELIVERY_MODE`` /
    ``FEISHU_*`` names.
    """
    mode = (
        env.get("INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE")
        or env.get("ALERT_DELIVERY_MODE")
        or "feishu_webhook"
    ).strip()
    if mode == "feishu_app":
        return deliver_feishu_app_text(
            app_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_ID")
            or env.get("FEISHU_APP_ID", ""),
            app_secret=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET")
            or env.get("FEISHU_APP_SECRET", ""),
            chat_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID")
            or env.get("FEISHU_CHAT_ID", ""),
            api_base=env.get("INFRA2_OUT_OF_BAND_FEISHU_API_BASE")
            or env.get("FEISHU_API_BASE", "https://open.feishu.cn"),
            text=text,
            timeout=timeout,
        )
    webhook_url = (
        env.get("INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL")
        or env.get("FEISHU_WEBHOOK_URL")
        or ""
    ).strip()
    if not webhook_url:
        raise InvalidFeishuAppConfig(
            "Feishu webhook URL or app credentials are required for out-of-band delivery"
        )
    return deliver_feishu_text(webhook_url, text, timeout=timeout)


def _required(name: str, value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidFeishuAppConfig(f"{name} is required")
    return candidate


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _signoz_attribute(key: str, data_type: str, attribute_type: str) -> dict[str, str]:
    return {
        "id": f"{key}--{data_type}--{attribute_type}" if key else "--",
        "key": key,
        "dataType": data_type,
        "type": attribute_type,
    }


def _signoz_threshold_op(op: str) -> str:
    mapping = {
        "above": "1",
        "below": "2",
        "equal": "3",
        "not_equal": "4",
    }
    try:
        return mapping[op]
    except KeyError as exc:
        raise AlertingError(f"Unsupported SigNoz threshold op: {op!r}") from exc


def _signoz_match_type(match_type: str) -> str:
    mapping = {
        "at_least_once": "1",
        "all_times": "2",
        "all_the_times": "2",
        "on_average": "3",
        "in_total": "4",
        "last": "5",
    }
    try:
        return mapping[match_type]
    except KeyError as exc:
        raise AlertingError(f"Unsupported SigNoz match type: {match_type!r}") from exc


def _iter_signoz_items(
    response: Any, *, collection_keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []

    data = response.get("data", response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if any(key in data for key in ("name", "alert", "id", "channelId", "ruleId")):
            return [data]
        for key in collection_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        nested = data.get("data")
        if isinstance(nested, list):
            return nested
    return []


def _truncate_message(message: str) -> str:
    return (
        message[: MAX_MESSAGE_CHARS - len(TRUNCATION_SUFFIX)].rstrip()
        + TRUNCATION_SUFFIX
    )
