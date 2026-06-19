"""Create and clean up a disabled SigNoz alert-rule canary.

The canary proves that the checked-in finance_report PromQL alert payload is
accepted by the live SigNoz API before the real alert catalog is reconciled.
It uses the same payload builder as ``fr-observability.shared.apply-alerts``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import find_signoz_channel_id, find_signoz_rule_id  # noqa: E402
from libs.observability_dashboards import load_alert_definitions  # noqa: E402


def main(env: dict[str, str] | None = None) -> int:
    current_env = env or dict(os.environ)
    api_key = current_env.get("SIGNOZ_API_KEY", "").strip()
    if not api_key:
        print("SIGNOZ_API_KEY is required", file=sys.stderr)
        return 2

    base_url = _signoz_base_url(current_env)
    channel_name = current_env.get("SIGNOZ_CANARY_CHANNEL_NAME", "").strip() or (
        f"infra2-feishu-alerts-{current_env.get('ENV', 'production')}"
    )
    channel_response = _signoz_request(
        base_url, api_key, method="GET", path="/api/v1/channels"
    )
    if not channel_response["ok"]:
        print(
            "failed to list SigNoz channels: "
            f"status={channel_response['status']} body={channel_response['body'][:500]}",
            file=sys.stderr,
        )
        return 1
    channel_id = find_signoz_channel_id(channel_response["data"], channel_name)
    if not channel_id:
        print(f"SigNoz channel not found: {channel_name}", file=sys.stderr)
        return 1

    alert_name = _canary_alert_name(current_env)
    payload = _build_canary_payload(alert_name, channel_id)
    created = _signoz_request(
        base_url, api_key, method="POST", path="/api/v1/rules", payload=payload
    )
    if not created["ok"]:
        print(
            "failed to create SigNoz canary alert rule: "
            f"status={created['status']} body={created['body'][:1000]}",
            file=sys.stderr,
        )
        return 1

    rule_id = find_signoz_rule_id(created["data"], alert_name) or _resolve_rule_id(
        base_url, api_key, alert_name
    )
    if not rule_id:
        print(
            f"created canary rule but could not resolve rule id: {alert_name}",
            file=sys.stderr,
        )
        return 1

    listed = _signoz_request(base_url, api_key, method="GET", path="/api/v1/rules")
    if not _stored_rule_has_v5_promql_query(listed["data"], alert_name):
        _delete_rule(base_url, api_key, rule_id)
        print(
            "created canary rule, but stored rule does not expose the v5 PromQL "
            "queries[] envelope",
            file=sys.stderr,
        )
        return 1

    deleted = _delete_rule(base_url, api_key, rule_id)
    if not deleted:
        print(
            f"created canary rule {alert_name} ({rule_id}), but cleanup failed",
            file=sys.stderr,
        )
        return 1

    print(f"SigNoz alert canary passed: {alert_name} ({rule_id})")
    return 0


def _signoz_base_url(env: dict[str, str]) -> str:
    explicit = env.get("SIGNOZ_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    internal_domain = env.get("INTERNAL_DOMAIN", "").strip() or "zitian.party"
    return f"https://signoz.{internal_domain}"


def _canary_alert_name(env: dict[str, str]) -> str:
    suffix = (
        env.get("GITHUB_RUN_ID") or env.get("SIGNOZ_CANARY_ID") or str(int(time.time()))
    )
    return f"CanarySigNozPromqlPayload-{suffix}"


def _build_canary_payload(alert_name: str, channel_id: str) -> dict[str, Any]:
    definitions = {d.alert_name: d for d in load_alert_definitions()}
    source = definitions["FinanceReportHigh5xxRate"].to_signoz_payload([channel_id])
    payload = json.loads(json.dumps(source))
    payload["alert"] = alert_name
    payload["disabled"] = True
    payload["source"] = "infra2/tools/signoz_alert_rule_canary.py"
    payload["labels"] = {
        **payload.get("labels", {}),
        "severity": "info",
        "canary": "true",
    }
    payload["annotations"] = {
        "summary": "Disabled canary for finance_report SigNoz PromQL alert schema.",
        "description": (
            "This disabled rule is created and deleted by CI to prove the live "
            "SigNoz API accepts the generated PromQL alert payload."
        ),
    }
    return payload


def _resolve_rule_id(
    base_url: str,
    api_key: str,
    alert_name: str,
    *,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> str | None:
    for attempt in range(max(1, attempts)):
        listed = _signoz_request(base_url, api_key, method="GET", path="/api/v1/rules")
        if listed["ok"]:
            rule_id = find_signoz_rule_id(listed["data"], alert_name)
            if rule_id:
                return rule_id
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return None


def _stored_rule_has_v5_promql_query(rules_response: Any, alert_name: str) -> bool:
    for rule in _iter_rules(rules_response):
        if rule.get("alert") != alert_name and rule.get("name") != alert_name:
            continue
        condition = (
            rule.get("condition") if isinstance(rule.get("condition"), dict) else {}
        )
        composite = condition.get("compositeQuery")
        if not isinstance(composite, dict):
            return False
        queries = composite.get("queries")
        return (
            rule.get("alertType") == "METRIC_BASED_ALERT"
            and rule.get("ruleType") == "promql_rule"
            and isinstance(queries, list)
            and bool(queries)
            and queries[0].get("type") == "promql"
        )
    return False


def _iter_rules(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        return []
    data = response.get("data", response)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("rules", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _delete_rule(base_url: str, api_key: str, rule_id: str) -> bool:
    for path in (
        f"/api/v2/rules/{quote(rule_id, safe='')}",
        f"/api/v1/rules/{quote(rule_id, safe='')}",
    ):
        response = _signoz_request(base_url, api_key, method="DELETE", path=path)
        if response["ok"] or response["status"] == 404:
            return True
    return False


def _signoz_request(
    base_url: str,
    api_key: str,
    *,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "SIGNOZ-API-KEY": api_key,
            "User-Agent": "infra2-signoz-alert-rule-canary/1.0",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        return {"ok": False, "status": 0, "body": str(exc), "data": None}

    decoded = None
    if body.strip():
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            decoded = None
    return {
        "ok": 200 <= status < 300,
        "status": status,
        "body": body,
        "data": decoded,
    }


if __name__ == "__main__":
    raise SystemExit(main())
