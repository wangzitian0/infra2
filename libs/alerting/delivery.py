"""HTTP delivery and reachability checks for Feishu webhooks and app messages."""
# alert-delivery-exempt: defines deliver_infra2_report primitive

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request

import libs.alerting
from libs.alerting.card import (
    build_feishu_app_card_payload,
    build_feishu_app_message_payload,
    build_feishu_card_payload,
    build_feishu_text_payload,
    feishu_request_body,
)
from libs.alerting.types import (
    FEISHU_WEBHOOK_HOSTS,
    FEISHU_WEBHOOK_PATH_PREFIX,
    JSON_UTF8,
    REPORT_ONLY_ENVIRONMENTS,
    REPORT_TITLE_PREFIX,
    FeishuDeliveryError,
    InvalidFeishuAppConfig,
    InvalidWebhookUrl,
    _required,
)

INFRA2_REPORTS_ENV = (
    "INFRA2_REPORTS_FEISHU_APP_ID",
    "INFRA2_REPORTS_FEISHU_APP_SECRET",
    "INFRA2_REPORTS_FEISHU_CHAT_ID",
)


def is_report_only_environment(value: str | None) -> bool:
    """True only for an environment known to have no pager: staging or a preview slot.

    The value is normalized first (``libs.common.normalize_env_name``: ``stg`` is
    staging, ``PRODUCTION `` and unset are production). Preview slots are ``preview``
    or ``<kind>-<value>`` for the preview kinds (``pr-5``, ``branch-main``, ...).
    Anything else — production, an unknown name, garbage — pages: an environment
    that cannot be recognised must fail loud, not quiet.
    """
    from libs.core.constants import PREVIEW_KINDS
    from libs.core.environ import normalize_env_name

    raw = (value or "").strip().lower().replace("-", "_")
    try:
        name = normalize_env_name(raw)
    except ValueError:  # e.g. a "/" in it: not an environment this estate names
        return False
    if name in REPORT_ONLY_ENVIRONMENTS:
        return True
    return any(name.startswith(f"{kind}_") for kind in (*PREVIEW_KINDS, "preview"))


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


def deliver_feishu_text(
    webhook_url: str, text: str, timeout: float = 10.0
) -> dict[str, Any]:
    """Send a text message to Feishu (custom bot) and return the decoded response."""
    return _deliver_feishu_webhook(
        webhook_url, build_feishu_text_payload(text), timeout=timeout
    )


def deliver_feishu_card(
    webhook_url: str, card: dict[str, Any], timeout: float = 10.0
) -> dict[str, Any]:
    """Send an interactive card to Feishu (custom bot) and return the decoded response."""
    return _deliver_feishu_webhook(
        webhook_url, build_feishu_card_payload(card), timeout=timeout
    )


def _deliver_feishu_webhook(
    webhook_url: str, payload: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    safe_url = validate_feishu_webhook_url(webhook_url)
    request = Request(
        safe_url,
        data=feishu_request_body(payload),
        headers={"Content-Type": JSON_UTF8},
        method="POST",
    )
    try:
        with libs.alerting.urlopen(request, timeout=timeout) as response:  # noqa: S310
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
    safe_chat_id = _required("FEISHU_CHAT_ID", chat_id)
    return _deliver_feishu_app_message(
        app_id=app_id,
        app_secret=app_secret,
        message_payload=build_feishu_app_message_payload(safe_chat_id, text),
        api_base=api_base,
        timeout=timeout,
    )


def deliver_feishu_app_card(
    *,
    app_id: str,
    app_secret: str,
    chat_id: str,
    card: dict[str, Any],
    api_base: str = "https://open.feishu.cn",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send an interactive card to a Feishu chat using app bot OpenAPI."""
    safe_chat_id = _required("FEISHU_CHAT_ID", chat_id)
    return _deliver_feishu_app_message(
        app_id=app_id,
        app_secret=app_secret,
        message_payload=build_feishu_app_card_payload(safe_chat_id, card),
        api_base=api_base,
        timeout=timeout,
    )


def _deliver_feishu_app_message(
    *,
    app_id: str,
    app_secret: str,
    message_payload: dict[str, Any],
    api_base: str,
    timeout: float,
) -> dict[str, Any]:
    base = validate_feishu_api_base(api_base)
    safe_app_id = _required("FEISHU_APP_ID", app_id)
    safe_app_secret = _required("FEISHU_APP_SECRET", app_secret)

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
        message_payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )


def deliver_infra2_report(text: str, env: Mapping[str, str] | None = None) -> bool:
    """Post a daily-report message to the shared 'infra2 reports' Lark group via the infra2
    Feishu app bot. The single delivery entry every periodic REPORT reconciler uses (DNS drift,
    config drift, …) so the channel + credentials live in one place, not per-tool.

    Reads ``INFRA2_REPORTS_ENV`` (+ optional ``INFRA2_REPORTS_FEISHU_API_BASE``).
    Returns True if delivered, False if not configured (so a not-yet-wired reconciler no-ops
    cleanly instead of erroring). A configured-but-failing delivery raises (a broken report
    path must be visible, not silently green).

    Every report starts with ``REPORT_TITLE_PREFIX`` (#905), added here once for all
    senders, so a report never reads like a page.
    """
    e = os.environ if env is None else env
    app_id, app_secret, chat_id = (e.get(name, "") for name in INFRA2_REPORTS_ENV)
    if not (app_id and app_secret and chat_id):
        return False
    libs.alerting.deliver_feishu_app_text(
        app_id=app_id,
        app_secret=app_secret,
        chat_id=chat_id,
        text=as_report(text),
        api_base=e.get("INFRA2_REPORTS_FEISHU_API_BASE", "https://open.feishu.cn"),
    )
    return True


def as_report(text: str) -> str:
    """``text`` with the ``[报告]`` header (#905), added once."""
    stripped = text.lstrip()
    return (
        stripped
        if stripped.startswith(REPORT_TITLE_PREFIX)
        else (REPORT_TITLE_PREFIX + stripped)
    )


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    request_headers = {"Content-Type": JSON_UTF8}
    request_headers.update(headers or {})
    request = Request(
        url,
        data=feishu_request_body(payload),
        headers=request_headers,
        method="POST",
    )
    try:
        with libs.alerting.urlopen(request, timeout=timeout) as response:  # noqa: S310
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
        return libs.alerting.deliver_feishu_app_text(
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
    return libs.alerting.deliver_feishu_text(webhook_url, text, timeout=timeout)
