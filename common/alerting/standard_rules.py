"""The one table of standard SigNoz alert rules, and the per-environment expansion.

A standard rule is the same for every service apart from its names and threshold: an
error-log count. Production and staging both get it. Staging is report only: the bridge
reads the ``environment`` label (``common.alerting.card.is_report_payload``).
5xx, P95 and telemetry-absent PromQL differs per service, so those rules stay in the
service catalogs and use the ``{env}`` placeholder until one SDK metric name exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from common.observability.signoz_queries import ERROR_LOG_LEVELS

ENVIRONMENTS = ("production", "staging")
ENV_PLACEHOLDER = "{env}"
STAGING_NAME_SUFFIX = "Staging"
STANDARD_RUNBOOK_URL = (
    "https://github.com/wangzitian0/infra2/blob/main/docs/ssot/ops.observability.md"
    "#sop-004--sop-004b--sop-004c-应用-otel-错误告警--全栈告警目录-config-as-code"
    "373--1106--1389"
)


def _error_logs(
    alert_name: str, service_id: str, service_name: str, threshold: int
) -> dict[str, Any]:
    return {
        "signal": "logs",
        "alert_name": alert_name,
        "service_id": service_id,
        "service_name": service_name,
        "severity": "error",
        "threshold": threshold,
        "match_type": "in_total",
        "eval_window": "15m0s",
        "frequency": "1m",
        "summary": (
            f"Severity P1 (15m total > {threshold}): {service_name} emitted more than "
            f"{threshold} {'/'.join(ERROR_LOG_LEVELS)} log lines in 15 minutes."
        ),
    }


STANDARD_RULES: tuple[dict[str, Any], ...] = (
    _error_logs(
        "FinanceReportBackendErrorLogs",
        "finance_report/app",
        "finance-report-backend",
        10,
    ),
    _error_logs("TrueAlphaAppErrorLogs", "truealpha/app", "truealpha-app", 10),
    _error_logs(
        "TrueAlphaDataEngineErrorLogs",
        "truealpha/data_engine",
        "truealpha-data-engine",
        10,
    ),
    _error_logs(
        "PlatformAlertingErrorLogs", "platform/alerting", "platform-alerting", 5
    ),
    _error_logs("PlatformTodoErrorLogs", "platform/todo", "platform-todo", 10),
)


def expand_rules(
    rules: Iterable[dict[str, Any]], environment: str
) -> list[dict[str, Any]]:
    """Copy ``rules`` for ``environment``: fill ``{env}``, name staging rules apart."""
    suffix = STAGING_NAME_SUFFIX if environment == "staging" else ""
    expanded = []
    for raw in rules:
        if not isinstance(raw, dict):  # the loader rejects it with the file name
            expanded.append(raw)
            continue
        rule = {**raw, "environment": environment}
        rule["alert_name"] = f"{raw.get('alert_name', '')}{suffix}"
        if environment == "staging" and rule.get("summary"):
            rule["summary"] = f"[staging, report only] {rule['summary']}"
        if "promql" in rule:
            rule["promql"] = str(rule["promql"]).replace(ENV_PLACEHOLDER, environment)
        expanded.append(rule)
    return expanded
