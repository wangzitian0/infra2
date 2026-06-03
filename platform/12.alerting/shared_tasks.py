"""Shared tasks for alerting bridge."""

from __future__ import annotations

import json
import shlex

from invoke import task

from libs.alerting import BasicAuth, build_signoz_channel_payload
from libs.common import get_env, service_domain, with_env_suffix


def _bridge_url(env: dict[str, str | None]) -> str:
    return f"http://{with_env_suffix('platform-alerting', env)}:8080/signoz/webhook"


def _channel_name(env: dict[str, str | None]) -> str:
    return f"infra2-feishu-alerts-{env.get('ENV', 'production')}"


@task
def status(c):
    """Check the internal Feishu alert bridge health."""
    from libs.console import error, success

    env = get_env()
    host = env["VPS_HOST"]
    container = with_env_suffix("platform-alerting", env)
    result = c.run(
        f"ssh root@{host} 'docker exec {container} python -c "
        "\"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()\"'",
        warn=True,
        hide=True,
    )
    if result.ok:
        success("alerting bridge: ready")
    else:
        error("alerting bridge: not ready")
    return {"alerting_bridge": result.ok}


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
    payload_json = shlex.quote(json.dumps(payload))
    result = c.run(
        f"curl -sS -X POST 'https://{signoz_domain}/api/v1/channels' "
        f"-H 'SIGNOZ-API-KEY: {api_key}' "
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
    result = c.run(
        f"ssh root@{host} 'docker exec -i {container} python -c "
        "\"import sys, urllib.request; data=sys.stdin.buffer.read(); "
        "req=urllib.request.Request('http://127.0.0.1:8080/signoz/webhook', "
        "data=data, headers={'Content-Type':'application/json'}, method='POST'); "
        "print(urllib.request.urlopen(req, timeout=10).status)\"' "
        f"<<'EOF'\n{payload}\nEOF",
        warn=True,
        hide=True,
    )
    if result.ok and "202" in result.stdout:
        success("Feishu alert bridge test message accepted")
        return True
    error("Feishu alert bridge test failed")
    return False
