"""Backward-compatibility shim — the implementation lives in `libs.observability.dashboards`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.observability.dashboards import (
    ALERT_RULES_FILE,
    AlertDefinition,
    DASHBOARD_FILE,
    FINANCE_REPORT_OBSERVABILITY_DIR,
    LogErrorAlertDefinition,
    MetricAlertDefinition,
    OPENPANEL_ANALYTICS_FILE,
    ObservabilityDefinitionError,
    REPO_ROOT,
    ROUTING_PROBLEM_KINDS,
    RoutingProblem,
    SIGNOZ_DASHBOARD_VERSION,
    StoredDashboard,
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

__all__ = [
    "ALERT_RULES_FILE",
    "AlertDefinition",
    "DASHBOARD_FILE",
    "FINANCE_REPORT_OBSERVABILITY_DIR",
    "LogErrorAlertDefinition",
    "MetricAlertDefinition",
    "OPENPANEL_ANALYTICS_FILE",
    "ObservabilityDefinitionError",
    "REPO_ROOT",
    "ROUTING_PROBLEM_KINDS",
    "RoutingProblem",
    "SIGNOZ_DASHBOARD_VERSION",
    "StoredDashboard",
    "build_dashboard_import_payload",
    "check_alert_routing",
    "check_dashboard_state",
    "find_stored_dashboards",
    "load_alert_definitions",
    "load_dashboard",
    "load_openpanel_analytics",
    "render_alert_payloads",
    "require_rule_channel",
]
