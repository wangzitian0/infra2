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

After an apply, prove delivery (read-only, then one explicit test notification):

    uv run python -m invoke fr-observability.shared.verify-alert-routing
    uv run python -m invoke fr-observability.shared.test-alert-channel
"""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

from invoke import task

from libs.alerting import AlertingError
from libs.observability_dashboards import (
    ROUTING_PROBLEM_KINDS,
    ObservabilityDefinitionError,
    build_dashboard_import_payload,
    check_alert_routing,
    check_dashboard_state,
    find_stored_dashboards,
    load_alert_definitions,
    load_dashboard,
    load_openpanel_analytics,
    render_alert_payloads,
    require_rule_channel,
)

# Label stamped on every rule this catalog owns, so `apply_alerts` can safely prune its own
# drift (renamed/leftover managed rules) without ever touching a hand-made rule.
MANAGED_ALERT_SOURCE = "infra2/finance_report-alerts"
# Residue left by the alert-rule canary (tools/signoz_alert_rule_probe.py) is also ours.
_CANARY_RULE_PREFIX = "CanarySigNozPromqlPayload-"


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


def _channel_name() -> str:
    """Name of the Feishu channel rules bind to: the one platform/12.alerting creates.

    Read from the alerting module's own ``_channel_name`` so the creator and the
    binder cannot disagree (#973); neither the apply nor the checks re-derive it.
    """
    alerting = _alerting_shared()
    return alerting._channel_name(alerting.get_env())


@task
def print_alerts(c):
    """Print the SigNoz alert-rule payloads from the checked-in definitions."""
    payloads = render_alert_payloads(_channel_name())
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
def apply_alerts(c, dry_run=False, prune=False):
    """Reconcile SigNoz alert rules to the checked-in catalog (declarative IaC).

    Two fixes over the old create-if-absent behaviour, which silently skipped any
    existing rule (so an edited threshold never took effect) and never removed drift:

    - **upsert**: every catalog rule is re-applied (delete + create) so a changed
      definition actually lands. Binds each to the shared Feishu/Lark channel by name
      and refuses to send a rule that would end up with no channel.
    - **prune**: managed rules NOT in the catalog (our ``source`` label, or leftover
      ``Canary*`` residue) are removed. **LOG-ONLY by default** — pass ``--prune`` to
      actually delete (warn-before-fail; verify the would-prune list first). A rule
      with no managed marker (e.g. hand-made in the UI) is never touched.

    Every rule is rendered and validated before the first delete, so a bad definition
    stops the apply with the live catalog untouched.
    """
    from invoke.exceptions import Exit

    from libs.console import error, info, success
    from libs.alerting import find_signoz_rule_id, _iter_signoz_items

    alerting = _alerting_shared()
    definitions = load_alert_definitions()
    catalog_names = {d.alert_name for d in definitions}

    if dry_run:
        payloads = render_alert_payloads(_channel_name())
        print(json.dumps(payloads, indent=2, sort_keys=True))
        return payloads

    # The channel NAME, not its id: SigNoz routes a rule to a channel by name (#973).
    channel_name = alerting._ensure_signoz_channel(c)
    if not channel_name:
        error("Cannot apply alert rules without the SigNoz Feishu channel")
        raise Exit("Cannot apply alert rules without the SigNoz Feishu channel", code=1)

    # Render and validate EVERY payload before the first DELETE: a rule that cannot be
    # rendered (or would end up without the channel) must stop the apply while the live
    # catalog is still whole, not after half of it has been re-created.
    rendered: list[tuple[str, dict]] = []
    try:
        for definition in definitions:
            payload = require_rule_channel(
                definition.to_signoz_payload([channel_name]), channel_name
            )
            payload.setdefault("labels", {})["source"] = MANAGED_ALERT_SOURCE
            rendered.append((definition.alert_name, payload))
    except (AlertingError, ObservabilityDefinitionError) as exc:
        error("Cannot render the alert catalog; nothing was changed", str(exc))
        raise Exit(f"Cannot render the alert catalog: {exc}", code=1) from exc

    # List existing rules ONCE, then match locally — avoids an N+1 GET /api/v1/rules.
    listed = alerting._signoz_request(c, method="GET", path="/api/v1/rules")
    if not listed["ok"]:
        error(
            "Failed to list SigNoz alert rules before apply",
            f"status={listed['status']} body={listed['body'][:500]}",
        )
        raise Exit("Failed to list SigNoz alert rules before apply", code=1)
    existing_rules = listed.get("data")

    all_ok = True
    # --- upsert: (re)write every catalog rule so a changed definition takes effect.
    # delete-then-create with verified deletion; a failed create fails the apply loudly
    # (CI non-zero) rather than silently leaving the old rule.
    for alert_name, payload in rendered:
        existing_id = find_signoz_rule_id(existing_rules, alert_name)
        if existing_id and not _delete_signoz_rule(alerting, c, str(existing_id)):
            all_ok = False
            error(f"Failed to remove existing rule before re-apply: {alert_name}")
            continue
        created = alerting._signoz_request(
            c, method="POST", path="/api/v1/rules", payload=payload
        )
        if created["ok"]:
            success(
                f"SigNoz alert rule {'updated' if existing_id else 'created'}: "
                f"{alert_name}"
            )
        else:
            all_ok = False
            error(
                f"Failed to apply SigNoz alert rule: {alert_name}",
                f"status={created['status']} body={created['body'][:500]}",
            )

    # --- prune: managed rules that are no longer in the catalog. Log-only unless --prune.
    for rule in _iter_signoz_items(existing_rules, collection_keys=("rules", "items")):
        name = rule.get("alert") or rule.get("name")
        if not name or name in catalog_names:
            continue
        labels = rule.get("labels") or {}
        managed = (
            labels.get("source") == MANAGED_ALERT_SOURCE
            or labels.get("canary") == "true"
            or str(name).startswith(_CANARY_RULE_PREFIX)
        )
        if not managed:
            continue  # never touch a rule we do not own
        rule_id = rule.get("id") or rule.get("ruleId")
        if not prune:
            info(f"would prune stale managed rule: {name} (run with --prune to delete)")
            continue
        if not rule_id:
            all_ok = False
            error(f"Cannot prune stale managed rule (missing id): {name}")
            continue
        if _delete_signoz_rule(alerting, c, str(rule_id)):
            success(f"pruned stale managed rule: {name}")
        else:
            all_ok = False
            error(f"Failed to prune stale managed rule: {name}")

    if not all_ok:
        raise Exit("Failed to reconcile one or more SigNoz alert rules", code=1)
    return all_ok


def _delete_signoz_rule(alerting, c, rule_id: str) -> bool:
    """Delete a SigNoz rule and VERIFY it is gone by re-listing.

    SigNoz's DELETE is idempotent (200 even for a wrong/absent id), so trusting the
    status code is exactly how the canary leaked rules. Try both API versions, then
    confirm the id is actually absent.
    """
    from libs.alerting import _iter_signoz_items

    # Guard against a missing/placeholder id: without it the absence-check below would
    # "verify" the deletion of a non-existent id and falsely report success.
    if not rule_id or rule_id == "None":
        return False
    rid = quote(rule_id, safe="")
    for path in (f"/api/v1/rules/{rid}", f"/api/v2/rules/{rid}"):
        alerting._signoz_request(c, method="DELETE", path=path)
    listed = alerting._signoz_request(c, method="GET", path="/api/v1/rules")
    if not listed.get("ok"):
        return False
    return not any(
        str(r.get("id") or r.get("ruleId")) == str(rule_id)
        for r in _iter_signoz_items(
            listed.get("data"), collection_keys=("rules", "items")
        )
    )


@task
def apply_dashboard(c, delete_duplicates=False):
    """Create/update the finance_report baseline SigNoz dashboard.

    Looks the dashboard up by exact title and updates that row in place; creates it
    only when none exists. A row stored by the pre-#934 apply (title nested under a
    ``data`` envelope) counts as the dashboard and is converted in place, not copied.

    Other rows with the exact title are duplicates: they are **reported, never
    deleted**, unless ``--delete-duplicates`` is passed. Deletion is limited to rows
    whose title equals ours exactly, only after the update succeeded, and is verified
    by re-listing.
    """
    from invoke.exceptions import Exit

    from libs.console import error, info, success, warning

    alerting = _alerting_shared()
    payload = build_dashboard_import_payload()
    title = payload["title"]

    listed = alerting._signoz_request(c, method="GET", path="/api/v1/dashboards")
    if not listed["ok"]:
        error(
            "Failed to list SigNoz dashboards before apply",
            f"status={listed['status']} body={listed['body'][:500]}",
        )
        raise Exit("Failed to list SigNoz dashboards before apply", code=1)
    stored = find_stored_dashboards(listed.get("data"), title)
    target, duplicates = (stored[0], stored[1:]) if stored else (None, [])

    if target:
        result = alerting._signoz_request(
            c,
            method="PUT",
            path=f"/api/v1/dashboards/{quote(target.id, safe='')}",
            payload=payload,
        )
        verb = "converted from the legacy envelope" if target.legacy else "updated"
    else:
        result = alerting._signoz_request(
            c, method="POST", path="/api/v1/dashboards", payload=payload
        )
        verb = "created"

    if not result["ok"]:
        error(
            f"Failed to apply SigNoz dashboard: {title}",
            f"status={result['status']} body={result['body'][:500]}",
        )
        raise Exit(f"Failed to apply SigNoz dashboard: {title}", code=1)
    success(f"SigNoz dashboard {verb}: {title}")

    if not duplicates:
        return True
    duplicate_ids = [entry.id for entry in duplicates]
    if not delete_duplicates:
        warning(
            f"{len(duplicates)} duplicate dashboard(s) titled {title!r} remain: "
            f"{duplicate_ids} (re-run with --delete-duplicates to remove them)"
        )
        return True

    for entry in duplicates:
        alerting._signoz_request(
            c, method="DELETE", path=f"/api/v1/dashboards/{quote(entry.id, safe='')}"
        )
    # SigNoz answers a delete it did not perform like one it did: trust the re-list.
    relisted = alerting._signoz_request(c, method="GET", path="/api/v1/dashboards")
    if not relisted["ok"]:
        error("Failed to re-list SigNoz dashboards after deleting duplicates")
        raise Exit(
            "Failed to re-list SigNoz dashboards after deleting duplicates", code=1
        )
    remaining = [
        entry.id
        for entry in find_stored_dashboards(relisted.get("data"), title)
        if entry.id in duplicate_ids
    ]
    if remaining:
        error(f"Duplicate dashboards still present after delete: {remaining}")
        raise Exit("Failed to delete duplicate SigNoz dashboards", code=1)
    info(f"deleted {len(duplicate_ids)} duplicate dashboard(s): {duplicate_ids}")
    return True


@task
def verify_dashboard(c):
    """Read-only: SigNoz holds exactly one correct copy of the checked-in dashboard."""
    from invoke.exceptions import Exit

    from libs.console import error, success

    alerting = _alerting_shared()
    dashboard = load_dashboard()
    listed = alerting._signoz_request(c, method="GET", path="/api/v1/dashboards")
    if not listed["ok"]:
        error(
            "Failed to list SigNoz dashboards",
            f"status={listed['status']} body={listed['body'][:500]}",
        )
        raise Exit("Failed to list SigNoz dashboards", code=1)
    problems = check_dashboard_state(listed.get("data"), dashboard)
    if problems:
        for problem in problems:
            error(f"dashboard: {problem}")
        raise Exit("SigNoz dashboard does not match the checked-in definition", code=1)
    success(f"SigNoz holds exactly one correct dashboard: {dashboard['title']}")
    return True


@task
def verify_alert_routing(c):
    """Read-only: every managed rule is bound to the Feishu channel, by name.

    Reads ``GET /api/v1/rules`` and ``GET /api/v1/channels`` and fails (non-zero) unless
    the channel exists once, each catalog rule exists once, is enabled and lists the
    channel name in every threshold, and no managed rule is left that the catalog no
    longer has. Each failure is printed under its kind: ``unbound``/``missing`` are fixed
    by ``apply-alerts``, ``stale`` by ``apply-alerts --prune``. It changes nothing; it
    proves the binding is stored, not that a message arrives: for that, run
    ``test-alert-channel``.
    """
    from invoke.exceptions import Exit

    from libs.console import error, success

    alerting = _alerting_shared()
    channel_name = _channel_name()
    rules = alerting._signoz_request(c, method="GET", path="/api/v1/rules")
    channels = alerting._signoz_request(c, method="GET", path="/api/v1/channels")
    for label, response in (("rules", rules), ("channels", channels)):
        if not response["ok"]:
            error(
                f"Failed to list SigNoz {label}",
                f"status={response['status']} body={response['body'][:500]}",
            )
            raise Exit(f"Failed to list SigNoz {label}", code=1)

    expected = [definition.alert_name for definition in load_alert_definitions()]
    problems = check_alert_routing(
        rules.get("data"),
        channels.get("data"),
        expected_alerts=expected,
        channel_name=channel_name,
        managed_source=MANAGED_ALERT_SOURCE,
    )
    if problems:
        counts = {
            kind: sum(1 for p in problems if p.kind == kind)
            for kind in ROUTING_PROBLEM_KINDS
        }
        error(
            "alert routing: "
            + ", ".join(f"{n} {kind}" for kind, n in counts.items() if n)
        )
        for kind in ROUTING_PROBLEM_KINDS:
            for problem in (p for p in problems if p.kind == kind):
                error(f"alert routing [{kind}]: {problem}")
        if counts["stale"]:
            error(
                "stale managed rules are removed by "
                "`fr-observability.shared.apply-alerts --prune`; "
                "missing/unbound rules by `apply-alerts`"
            )
        raise Exit("SigNoz alert rules are not all bound to the Feishu channel", code=1)
    success(f"{len(expected)} SigNoz rules are bound to channel {channel_name}")
    return True


@task
def test_alert_channel(c):
    """Send ONE test notification through the live Feishu channel (visible in Feishu).

    Posts the channel's own stored receiver config to SigNoz's ``/api/v1/testChannel``
    (``TestReceiver`` in ``pkg/alertmanager/api.go``), which sends a test alert to that
    receiver. It reaches the bridge and the Feishu chat exactly like a real alert, so
    it is explicit and never part of an apply. Confirm the card in Feishu, or the
    bridge's ``/signoz/webhook`` log line.
    """
    from invoke.exceptions import Exit

    from libs.alerting import _iter_signoz_items
    from libs.console import error, success

    alerting = _alerting_shared()
    channel_name = _channel_name()
    listed = alerting._signoz_request(c, method="GET", path="/api/v1/channels")
    if not listed["ok"]:
        error(
            "Failed to list SigNoz channels",
            f"status={listed['status']} body={listed['body'][:500]}",
        )
        raise Exit("Failed to list SigNoz channels", code=1)
    named = [
        item
        for item in _iter_signoz_items(
            listed.get("data"), collection_keys=("channels",)
        )
        if isinstance(item, dict) and item.get("name") == channel_name
    ]
    if len(named) != 1:
        error(
            f"Expected exactly one SigNoz channel named {channel_name}, found {len(named)}"
        )
        raise Exit(f"SigNoz channel {channel_name} not found exactly once", code=1)
    stored = named[0].get("data")
    try:
        receiver = json.loads(stored) if isinstance(stored, str) else stored
    except json.JSONDecodeError:
        receiver = None
    if not isinstance(receiver, dict):
        error(f"SigNoz channel {channel_name} has no readable stored receiver config")
        raise Exit("SigNoz channel config is unreadable", code=1)

    result = alerting._signoz_request(
        c, method="POST", path="/api/v1/testChannel", payload=receiver
    )
    if not result["ok"]:
        error(
            f"SigNoz test notification failed for channel {channel_name}",
            f"status={result['status']} body={result['body'][:500]}",
        )
        raise Exit("SigNoz test notification failed", code=1)
    success(
        f"Test notification sent through channel {channel_name}; "
        "confirm it in Feishu / the bridge log"
    )
    return True
