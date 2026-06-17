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
def apply_alerts(c, dry_run=False):
    """Ensure every checked-in finance_report alert rule exists in SigNoz.

    Routes each rule to the shared Feishu/Lark bridge channel. Idempotent: an
    existing rule with the same alert name is left untouched.
    """
    from libs.console import error, success

    alerting = _alerting_shared()
    definitions = load_alert_definitions()

    if dry_run:
        payloads = [d.to_signoz_payload(["<channel-id>"]) for d in definitions]
        print(json.dumps(payloads, indent=2, sort_keys=True))
        return payloads

    channel_id = alerting._ensure_signoz_channel(c)
    if not channel_id:
        error("Cannot apply alert rules without a SigNoz channel id")
        return False

    all_ok = True
    for definition in definitions:
        existing = alerting._find_rule(c, definition.alert_name)
        if existing:
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
    return all_ok


@task
def apply_dashboard(c):
    """Create/update the finance_report baseline SigNoz dashboard.

    Looks up an existing dashboard by title and updates it in place; otherwise
    creates it. Idempotent.
    """
    from libs.console import error, success

    alerting = _alerting_shared()
    dashboard = load_dashboard()
    title = dashboard["title"]

    listed = alerting._signoz_request(c, method="GET", path="/api/v1/dashboards")
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
    return False


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
