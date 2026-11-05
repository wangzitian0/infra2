"""Create and clean up a disabled SigNoz alert-rule canary.

The canary proves that the checked-in finance_report PromQL alert payload is
accepted by the live SigNoz API before the real alert catalog is reconciled.
It uses the same payload builder as ``fr-observability.shared.apply-alerts``.

The canary reports "passed" only after it reads the cleanup back (#1435):
the rule must be absent from ``GET /api/v1/rules``, and no route policy that
SigNoz created for the rule may remain in ``GET /api/v1/route_policies``.
A DELETE status alone proves nothing. SigNoz v0.105.1 has no
``DELETE /api/v2/rules/{id}`` route, and its web catch-all answers that request
with ``200`` and ``index.html``.
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

from common.alerting import (  # noqa: E402
    find_signoz_channel_id,
    find_signoz_rule_id,
    signoz_feishu_channel_name,
)
from common.observability_dashboards import load_alert_definitions  # noqa: E402

# Cleanup retry bounds: DELETE, read back, and repeat at most this many times.
DELETE_ATTEMPTS = 3
DELETE_RETRY_DELAY_SECONDS = 1.0


def main(env: dict[str, str] | None = None) -> int:
    current_env = env or dict(os.environ)
    api_key = current_env.get("SIGNOZ_API_KEY", "").strip()
    if not api_key:
        print("SIGNOZ_API_KEY is required", file=sys.stderr)
        return 2

    base_url = _signoz_base_url(current_env)
    channel_name = current_env.get(
        "SIGNOZ_CANARY_CHANNEL_NAME", ""
    ).strip() or signoz_feishu_channel_name(current_env.get("ENV"))
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
    if not find_signoz_channel_id(channel_response["data"], channel_name):
        print(f"SigNoz channel not found: {channel_name}", file=sys.stderr)
        return 1

    alert_name = _canary_alert_name(current_env)
    # Bind by channel NAME: SigNoz routes a rule to the receiver of that name (#973).
    payload = _build_canary_payload(alert_name, channel_name)
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
        cleanup_problem = _delete_rule(base_url, api_key, rule_id, alert_name)
        print(
            "created canary rule, but stored rule does not expose the v5 PromQL "
            "queries[] envelope",
            file=sys.stderr,
        )
        if cleanup_problem:
            print(cleanup_problem, file=sys.stderr)
        return 1

    cleanup_problem = _delete_rule(base_url, api_key, rule_id, alert_name)
    if cleanup_problem:
        print(cleanup_problem, file=sys.stderr)
        return 1

    print(
        f"SigNoz alert canary passed: {alert_name} ({rule_id}); "
        "rule and route policy read back as removed"
    )
    return 0


def _signoz_base_url(env: dict[str, str]) -> str:
    internal_domain = env.get("INTERNAL_DOMAIN", "").strip() or "zitian.party"
    return f"https://signoz.{internal_domain}"


def _canary_alert_name(env: dict[str, str]) -> str:
    suffix = env.get("GITHUB_RUN_ID") or str(int(time.time()))
    return f"CanarySigNozPromqlPayload-{suffix}"


def _build_canary_payload(alert_name: str, channel_name: str) -> dict[str, Any]:
    definitions = {d.alert_name: d for d in load_alert_definitions()}
    source = definitions["FinanceReportHigh5xxRate"].to_signoz_payload([channel_name])
    payload = json.loads(json.dumps(source))
    payload["alert"] = alert_name
    payload["disabled"] = True
    payload["source"] = "infra2/tools/signoz_alert_rule_probe.py"
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


def _delete_rule(
    base_url: str,
    api_key: str,
    rule_id: str,
    alert_name: str,
    *,
    attempts: int = DELETE_ATTEMPTS,
    delay_seconds: float = DELETE_RETRY_DELAY_SECONDS,
) -> str | None:
    """Delete the canary rule, then prove by read-back that the cleanup happened.

    Return ``None`` only when the rule is absent from the rule list and no route
    policy of the rule remains. Otherwise return one line that names the rule id
    and name. A DELETE status never counts as proof (#1435): the status of each
    DELETE goes into the failure line, and the read-back decides.

    ``/api/v1/rules/{id}`` comes first because it is the only DELETE route of the
    pinned SigNoz v0.105.1. The v2 path stays for a later SigNoz version.
    """
    paths = (
        f"/api/v1/rules/{quote(rule_id, safe='')}",
        f"/api/v2/rules/{quote(rule_id, safe='')}",
    )
    total_attempts = max(1, attempts)
    last_delete = "no DELETE was sent"
    last_read = "no read-back was made"
    for attempt in range(total_attempts):
        for path in paths:
            response = _signoz_request(base_url, api_key, method="DELETE", path=path)
            last_delete = f"DELETE {path} answered status={response['status']}"
            state, detail = _rule_listing_state(base_url, api_key, rule_id, alert_name)
            if state == "absent":
                return _route_policy_leftover(base_url, api_key, rule_id, alert_name)
            last_read = detail
        if attempt < total_attempts - 1:
            time.sleep(delay_seconds)
    return (
        f"canary rule {alert_name} ({rule_id}) was NOT removed after "
        f"{total_attempts} attempts: {last_read}; last request: {last_delete}"
    )


def _rule_listing_state(
    base_url: str, api_key: str, rule_id: str, alert_name: str
) -> tuple[str, str]:
    """Read ``GET /api/v1/rules``: ``("absent" | "present" | "unreadable", detail)``.

    A failed request or a response that is not a list counts as ``unreadable``,
    never as ``absent``.
    """
    listed = _signoz_request(base_url, api_key, method="GET", path="/api/v1/rules")
    if not listed["ok"]:
        return "unreadable", _read_failure("rule list", listed)
    rules = _listed_items(listed["data"], collection_keys=("rules", "items"))
    if rules is None:
        return "unreadable", "rule list response is not a list of rules"
    for rule in rules:
        listed_id = rule.get("id") or rule.get("ruleId")
        listed_name = rule.get("alert") or rule.get("name")
        if str(listed_id) == rule_id or listed_name == alert_name:
            return "present", "the rule is still in the rule list"
    return "absent", ""


def _route_policy_leftover(
    base_url: str, api_key: str, rule_id: str, alert_name: str
) -> str | None:
    """Return a failure line when a route policy of the deleted rule remains.

    SigNoz v0.105.1 stores the route policy of a rule under ``name == rule id``,
    and ``DELETE /api/v1/rules/{id}`` removes it in the same transaction. The
    probe still reads ``GET /api/v1/route_policies``, so an orphan fails loudly.
    """
    policies_response = _signoz_request(
        base_url, api_key, method="GET", path="/api/v1/route_policies"
    )
    prefix = f"canary rule {alert_name} ({rule_id}) is gone, but"
    if not policies_response["ok"]:
        return f"{prefix} {_read_failure('route policy list', policies_response)}"
    policies = _listed_items(policies_response["data"], collection_keys=("items",))
    if policies is None:
        return (
            f"{prefix} the route policy list response is not a list of route policies"
        )
    leftover_ids = [
        str(policy.get("id"))
        for policy in policies
        if policy.get("name") == rule_id or rule_id in str(policy.get("expression", ""))
    ]
    if leftover_ids:
        return f"{prefix} route policy {', '.join(leftover_ids)} still exists for it"
    return None


def _read_failure(what: str, response: dict[str, Any]) -> str:
    return (
        f"the {what} could not be read: status={response.get('status')} "
        f"body={str(response.get('body', ''))[:200]}"
    )


def _listed_items(
    response: Any, *, collection_keys: tuple[str, ...]
) -> list[dict[str, Any]] | None:
    """Return the items of a SigNoz list response, or ``None`` if it is no list.

    ``None`` keeps an unknown response shape from reading as an empty list.
    """
    data = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(data, dict):
        for key in collection_keys:
            if key in data:
                data = data[key]
                break
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


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
