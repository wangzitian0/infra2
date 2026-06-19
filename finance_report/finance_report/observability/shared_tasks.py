"""Apply finance_report SigNoz observability config-as-code (#373).

These tasks turn the checked-in JSON definitions in this directory into live
SigNoz objects (an OTEL error-log alert rule wired to the shared Feishu/Lark
bridge channel, and a baseline backend+frontend dashboard). They are idempotent
and reuse the shared alerting plumbing in ``platform/12.alerting`` so the
application-agnostic bridge logic stays in one place.

Definitions are the source of truth; provisioning is a post-merge apply step:

    uv run python -m invoke fr-observability.shared.apply-alerts
    uv run python -m invoke fr-observability.shared.apply-dashboard

Dry-run / print payloads without touching SigNoz:

    uv run python -m invoke fr-observability.shared.print-alerts
    uv run python -m invoke fr-observability.shared.print-dashboard
"""

from __future__ import annotations

import json
import shlex
import sys

from invoke import task

from libs.observability_dashboards import (
    build_dashboard_import_payload,
    load_alert_definitions,
    load_dashboard,
    load_openpanel_analytics,
)


def _alerting_shared():
    """Return the loaded platform/12.alerting shared-tasks module.

    The loader registers it as ``platform.12.alerting.shared``. We reuse its
    SigNoz request + channel-ensure helpers instead of re-implementing the
    application-agnostic bridge logic here.
    """
    module = sys.modules.get("platform.12.alerting.shared")
    if module is None:
        raise RuntimeError(
            "platform/12.alerting shared tasks are not loaded; run via "
            "`uv run python -m invoke ...` from the repo root."
        )
    return module


@task
def print_alerts(c):
    """Print the SigNoz alert-rule payloads from the checked-in definitions."""
    definitions = load_alert_definitions()
    payloads = [
        definition.to_signoz_payload(["<channel-id>"]) for definition in definitions
    ]
    print(json.dumps(payloads, indent=2, sort_keys=True))
    return payloads


@task
def print_dashboard(c):
    """Print the SigNoz dashboard import payload from the checked-in definition."""
    payload = build_dashboard_import_payload()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


@task
def print_openpanel_analytics(c):
    """Validate + print the OpenPanel analytics intent (funnels + events board).

    OpenPanel has no write API/MCP for funnels/dashboards, so there is no `apply`
    here — this validates the checked-in spec and prints it for the SOP-006 manual
    build runbook. Querying these views on-demand is via the OpenPanel MCP / CLI.
    """
    spec = load_openpanel_analytics()
    print(json.dumps(spec, indent=2, sort_keys=True))
    return spec


@task
def apply_alerts(c, dry_run=False):
    """Ensure every checked-in finance_report alert rule exists in SigNoz.

    Routes each rule to the shared Feishu/Lark bridge channel. Idempotent: an
    existing rule with the same alert name is left untouched.
    """
    from invoke.exceptions import Exit

    from libs.console import error, success
    from libs.alerting import find_signoz_rule_id

    alerting = _alerting_shared()
    definitions = load_alert_definitions()

    if dry_run:
        payloads = [d.to_signoz_payload(["<channel-id>"]) for d in definitions]
        print(json.dumps(payloads, indent=2, sort_keys=True))
        return payloads

    channel_id = alerting._ensure_signoz_channel(c)
    if not channel_id:
        error("Cannot apply alert rules without a SigNoz channel id")
        raise Exit("Cannot apply alert rules without a SigNoz channel id", code=1)

    # List existing rules ONCE, then match locally — avoids an N+1 GET /api/v1/rules
    # per definition as the rule set grows.
    listed = alerting._signoz_request(c, method="GET", path="/api/v1/rules")
    if not listed["ok"]:
        error(
            "Failed to list SigNoz alert rules before apply",
            f"status={listed['status']} body={listed['body'][:500]}",
        )
        raise Exit("Failed to list SigNoz alert rules before apply", code=1)
    existing_rules = listed.get("data")

    all_ok = True
    for definition in definitions:
        if find_signoz_rule_id(existing_rules, definition.alert_name):
            success(f"SigNoz alert rule already exists: {definition.alert_name}")
            continue
        payload = definition.to_signoz_payload([channel_id])
        created = alerting._signoz_request(
            c, method="POST", path="/api/v1/rules", payload=payload
        )
        if created["ok"]:
            success(f"SigNoz alert rule created: {definition.alert_name}")
        else:
            all_ok = False
            error(
                f"Failed to create SigNoz alert rule: {definition.alert_name}",
                f"status={created['status']} body={created['body'][:500]}",
            )
    if not all_ok:
        raise Exit("Failed to create one or more SigNoz alert rules", code=1)
    return all_ok


@task
def apply_dashboard(c):
    """Create/update the finance_report baseline SigNoz dashboard.

    Looks up an existing dashboard by title and updates it in place; otherwise
    creates it. Idempotent.
    """
    from invoke.exceptions import Exit

    from libs.console import error, success

    alerting = _alerting_shared()
    dashboard = load_dashboard()
    title = dashboard["title"]

    listed = alerting._signoz_request(c, method="GET", path="/api/v1/dashboards")
    if not listed["ok"]:
        error(
            "Failed to list SigNoz dashboards before apply",
            f"status={listed['status']} body={listed['body'][:500]}",
        )
        raise Exit("Failed to list SigNoz dashboards before apply", code=1)
    existing_uuid = _find_dashboard_uuid(listed.get("data"), title)

    payload = build_dashboard_import_payload()
    if existing_uuid:
        result = alerting._signoz_request(
            c,
            method="PUT",
            path=f"/api/v1/dashboards/{shlex.quote(existing_uuid)}",
            payload=payload,
        )
        verb = "updated"
    else:
        result = alerting._signoz_request(
            c, method="POST", path="/api/v1/dashboards", payload=payload
        )
        verb = "created"

    if result["ok"]:
        success(f"SigNoz dashboard {verb}: {title}")
        return True
    error(
        f"Failed to apply SigNoz dashboard: {title}",
        f"status={result['status']} body={result['body'][:500]}",
    )
    raise Exit(f"Failed to apply SigNoz dashboard: {title}", code=1)


def _find_dashboard_uuid(dashboards_response, title: str) -> str | None:
    """Find a dashboard uuid by title across known SigNoz response shapes."""
    items = dashboards_response
    if isinstance(dashboards_response, dict):
        items = dashboards_response.get("data", dashboards_response)
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if data.get("title") == title:
            uuid = item.get("uuid") or item.get("id") or data.get("uuid")
            return str(uuid) if uuid else None
    return None
