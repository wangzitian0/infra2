"""Shared tasks for alerting bridge."""

from __future__ import annotations

import json
import shlex
from io import StringIO

from invoke import task

from libs.alerting import (
    BasicAuth,
    build_signoz_channel_payload,
    build_signoz_log_alert_rule_payload,
    find_signoz_channel_id,
    find_signoz_rule_id,
)
from libs.common import get_env, service_domain, with_env_suffix


def _bridge_url(env: dict[str, str | None]) -> str:
    return f"http://{with_env_suffix('platform-alerting', env)}:8080/signoz/webhook"


def _channel_name(env: dict[str, str | None]) -> str:
    return f"infra2-feishu-alerts-{env.get('ENV', 'production')}"


def _deploy_env(env: dict[str, str | None]) -> str:
    return env.get("ENV") or env.get("DEPLOY_ENV") or "production"


def _signoz_context():
    from libs.console import error
    from libs.env import get_secrets

    env = get_env()
    deploy_env = _deploy_env(env)
    signoz_domain = service_domain("signoz", env)
    if not signoz_domain:
        error("Cannot determine SigNoz domain")
        return None

    signoz_secrets = get_secrets("platform", "signoz", deploy_env)
    api_key = signoz_secrets.get("api_key")
    if not api_key:
        error("Missing SigNoz api_key in Vault; run signoz.shared.create-api-key first")
        return None

    return {"env": env, "deploy_env": deploy_env, "domain": signoz_domain, "api_key": api_key}


def _signoz_request(c, *, method: str, path: str, payload: dict | None = None):
    context = _signoz_context()
    if not context:
        return {"ok": False, "data": None, "status": 0, "body": ""}

    api_key_header = shlex.quote(f"SIGNOZ-API-KEY: {context['api_key']}")
    body_args = ""
    if payload is not None:
        body_args = f" --data-raw {shlex.quote(json.dumps(payload))}"

    result = c.run(
        f"curl -sS -X {shlex.quote(method)} "
        f"'https://{context['domain']}{path}' "
        f"-H {api_key_header} "
        "-H 'Content-Type: application/json' "
        f"-w '\\n%{{http_code}}'"
        f"{body_args}",
        warn=True,
        hide=True,
    )

    raw = result.stdout or ""
    try:
        body, status_text = raw.rsplit("\n", 1)
        status = int(status_text.strip())
    except ValueError:
        body = raw
        status = 0

    decoded = None
    if body.strip():
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            decoded = None

    return {
        "ok": result.ok and 200 <= status < 300,
        "data": decoded,
        "status": status,
        "body": body,
    }


def _ensure_signoz_channel(c) -> str | None:
    from libs.console import success, warning

    env = get_env()
    channel_name = _channel_name(env)

    listed = _signoz_request(c, method="GET", path="/api/v1/channels")
    channel_id = find_signoz_channel_id(listed["data"], channel_name)
    if channel_id:
        success(f"SigNoz Feishu channel already exists: {channel_name}")
        return channel_id

    auth = None
    from libs.env import get_secrets

    alerting_secrets = get_secrets("platform", "alerting", _deploy_env(env))
    username = alerting_secrets.get("BRIDGE_BASIC_AUTH_USERNAME") or ""
    password = alerting_secrets.get("BRIDGE_BASIC_AUTH_PASSWORD") or ""
    if username or password:
        auth = BasicAuth(username, password)

    payload = build_signoz_channel_payload(
        channel_name=channel_name,
        bridge_url=_bridge_url(env),
        basic_auth=auth,
    )
    created = _signoz_request(c, method="POST", path="/api/v1/channels", payload=payload)
    channel_id = find_signoz_channel_id(created["data"], channel_name)
    if channel_id:
        success(f"SigNoz Feishu channel created: {channel_name}")
        return channel_id

    listed = _signoz_request(c, method="GET", path="/api/v1/channels")
    channel_id = find_signoz_channel_id(listed["data"], channel_name)
    if channel_id:
        success(f"SigNoz Feishu channel ensured: {channel_name}")
        return channel_id

    warning(
        f"Could not resolve SigNoz channel id for {channel_name}; "
        f"last status={created['status']}"
    )
    return None


def _find_rule(c, alert_name: str) -> str | None:
    listed = _signoz_request(c, method="GET", path="/api/v1/rules")
    return find_signoz_rule_id(listed["data"], alert_name)


@task
def status(c):
    """Check the internal Feishu alert bridge health."""
    from libs.console import error, success

    env = get_env()
    host = env["VPS_HOST"]
    container = with_env_suffix("platform-alerting", env)
    python_code = (
        'import urllib.request; '
        'urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3).read()'
    )
    remote_cmd = shlex.quote(
        f"docker exec {container} python -c {shlex.quote(python_code)}"
    )
    result = c.run(
        f"ssh root@{host} {remote_cmd}",
        warn=True,
        hide=True,
    )
    if result.ok:
        success("alerting bridge: ready")
    else:
        error("alerting bridge: not ready")
    return {
        "is_ready": result.ok,
        "details": "Health check passed" if result.ok else "Health check failed",
        "alerting_bridge": result.ok,
    }


@task
def print_channel_payload(c, username="", password=""):
    """Print the SigNoz webhook channel payload for Feishu alerting."""
    env = get_env()
    auth = BasicAuth(username, password) if username or password else None
    payload = build_signoz_channel_payload(
        channel_name=_channel_name(env),
        bridge_url=_bridge_url(env),
        basic_auth=auth,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


@task
def create_signoz_channel(c, username="", password=""):
    """Create the SigNoz webhook notification channel that targets the bridge."""
    from libs.console import error, success, warning
    from libs.env import get_secrets

    env = get_env()
    deploy_env = env.get("ENV") or "production"
    signoz_domain = service_domain("signoz", env)
    if not signoz_domain:
        error("Cannot determine SigNoz domain")
        return False

    signoz_secrets = get_secrets("platform", "signoz", deploy_env)
    api_key = signoz_secrets.get("api_key")
    if not api_key:
        error("Missing SigNoz api_key in Vault; run signoz.shared.create-api-key first")
        return False

    if not username and not password:
        alerting_secrets = get_secrets("platform", "alerting", deploy_env)
        username = alerting_secrets.get("BRIDGE_BASIC_AUTH_USERNAME") or ""
        password = alerting_secrets.get("BRIDGE_BASIC_AUTH_PASSWORD") or ""

    auth = BasicAuth(username, password) if username or password else None
    payload = build_signoz_channel_payload(
        channel_name=_channel_name(env),
        bridge_url=_bridge_url(env),
        basic_auth=auth,
    )
    api_key_header = shlex.quote(f"SIGNOZ-API-KEY: {api_key}")
    payload_json = shlex.quote(json.dumps(payload))
    result = c.run(
        f"curl -sS -X POST 'https://{signoz_domain}/api/v1/channels' "
        f"-H {api_key_header} "
        "-H 'Content-Type: application/json' "
        f"--data-raw {payload_json}",
        warn=True,
        hide=True,
    )
    if result.ok:
        success(f"SigNoz Feishu channel ensured: {payload['name']}")
        return True
    warning("SigNoz channel creation failed or already exists")
    return False


@task
def print_log_error_rule_payload(
    c,
    alert_name,
    service_name,
    channel_id="channel-id",
    summary="",
    severity="error",
    threshold=0,
    eval_window="5m0s",
    frequency="1m",
):
    """Print a reusable SigNoz OTEL log error alert rule payload."""
    summary = summary or f"{service_name} emitted ERROR/FATAL logs in the last 5 minutes"
    payload = build_signoz_log_alert_rule_payload(
        alert_name=alert_name,
        service_name=service_name,
        channel_ids=[channel_id],
        summary=summary,
        severity=severity,
        threshold=threshold,
        eval_window=eval_window,
        frequency=frequency,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


@task
def ensure_log_error_rule(
    c,
    alert_name,
    service_name,
    summary="",
    severity="error",
    threshold=0,
    eval_window="5m0s",
    frequency="1m",
    dry_run=False,
):
    """Ensure a reusable SigNoz OTEL log error alert rule routes to Feishu."""
    from libs.console import error, success

    channel_id = _ensure_signoz_channel(c)
    if not channel_id:
        error("Cannot create alert rule without a SigNoz channel id")
        return False

    summary = summary or f"{service_name} emitted ERROR/FATAL logs in the last 5 minutes"
    payload = build_signoz_log_alert_rule_payload(
        alert_name=alert_name,
        service_name=service_name,
        channel_ids=[channel_id],
        summary=summary,
        severity=severity,
        threshold=threshold,
        eval_window=eval_window,
        frequency=frequency,
    )
    if dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload

    existing_rule_id = _find_rule(c, alert_name)
    if existing_rule_id:
        success(f"SigNoz alert rule already exists: {alert_name}")
        return True

    created = _signoz_request(c, method="POST", path="/api/v1/rules", payload=payload)
    if created["ok"]:
        rule_id = find_signoz_rule_id(created["data"], alert_name)
        suffix = f" ({rule_id})" if rule_id else ""
        success(f"SigNoz alert rule created: {alert_name}{suffix}")
        return True

    error(
        f"Failed to create SigNoz alert rule: {alert_name}",
        f"status={created['status']} body={created['body'][:500]}",
    )
    return False


@task
def test_feishu(c, message="Infra2 Feishu alert bridge test"):
    """Send a test message through the deployed bridge."""
    from libs.console import error, success

    env = get_env()
    host = env["VPS_HOST"]
    container = with_env_suffix("platform-alerting", env)
    payload = json.dumps(
        {
            "status": "firing",
            "commonLabels": {
                "alertname": "Infra2FeishuBridgeTest",
                "severity": "warning",
            },
            "commonAnnotations": {"summary": message},
            "alerts": [],
        }
    )
    python_code = (
        "import sys, urllib.request; "
        "data=sys.stdin.buffer.read(); "
        'req=urllib.request.Request("http://127.0.0.1:8080/signoz/webhook", '
        'data=data, headers={"Content-Type":"application/json"}, method="POST"); '
        "print(urllib.request.urlopen(req, timeout=10).status)"
    )
    remote_cmd = shlex.quote(
        f"docker exec -i {container} python -c {shlex.quote(python_code)}"
    )
    result = c.run(
        f"ssh root@{host} {remote_cmd}",
        in_stream=StringIO(payload),
        warn=True,
        hide=True,
    )
    if result.ok and "202" in result.stdout:
        success("Feishu alert bridge test message accepted")
        return True
    error("Feishu alert bridge test failed")
    return False
