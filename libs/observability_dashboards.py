"""Load and validate checked-in observability definitions (alerts + dashboards).

Infra-007 / #373: SigNoz alert rules and dashboards are config-as-code. The
canonical definitions live next to the application they describe
(``finance_report/finance_report/observability/``) as plain JSON so that they are
reviewable in a PR and can be applied idempotently by an invoke task instead of a
one-off manual click in the SigNoz UI.

This module is intentionally side-effect free: it only reads and validates the
definition files and turns the declarative alert spec into the SigNoz rule payload
via :func:`libs.alerting.build_signoz_log_alert_rule_payload`. The apply path
(curl against the SigNoz API) lives in the component ``shared_tasks.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libs.alerting import build_signoz_log_alert_rule_payload

# Repository root: libs/observability_dashboards.py -> repo root is parents[1].
REPO_ROOT = Path(__file__).resolve().parents[1]

# Canonical location of the finance_report observability definitions.
FINANCE_REPORT_OBSERVABILITY_DIR = (
    REPO_ROOT / "finance_report" / "finance_report" / "observability"
)
ALERT_RULES_FILE = FINANCE_REPORT_OBSERVABILITY_DIR / "alert_rules.json"
DASHBOARD_FILE = FINANCE_REPORT_OBSERVABILITY_DIR / "dashboard.json"
OPENPANEL_ANALYTICS_FILE = FINANCE_REPORT_OBSERVABILITY_DIR / "openpanel_analytics.json"


class ObservabilityDefinitionError(Exception):
    """Raised when a checked-in alert or dashboard definition is invalid."""


@dataclass(frozen=True)
class LogErrorAlertDefinition:
    """Declarative SigNoz OTEL log-error alert rule definition.

    This is the config-as-code source of truth for an app error-log alert. It is
    deliberately a small, reviewable shape; the full SigNoz v2 rule payload is
    derived from it so the schema details stay in one tested builder.
    """

    alert_name: str
    service_name: str
    summary: str
    severity: str = "error"
    threshold: int = 0
    eval_window: str = "5m0s"
    frequency: str = "1m"

    def to_signoz_payload(self, channel_ids: list[str]) -> dict[str, Any]:
        """Render this definition into a SigNoz threshold-rule payload."""
        return build_signoz_log_alert_rule_payload(
            alert_name=self.alert_name,
            service_name=self.service_name,
            channel_ids=channel_ids,
            summary=self.summary,
            severity=self.severity,
            threshold=self.threshold,
            eval_window=self.eval_window,
            frequency=self.frequency,
        )


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise ObservabilityDefinitionError(f"Definition file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ObservabilityDefinitionError(
            f"Definition file is not valid JSON: {path} ({exc})"
        ) from exc


def load_alert_definitions(
    path: Path = ALERT_RULES_FILE,
) -> list[LogErrorAlertDefinition]:
    """Load and validate the checked-in log-error alert definitions."""
    data = _load_json(path)
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not rules:
        raise ObservabilityDefinitionError(
            f"Alert definition must contain a non-empty 'rules' list: {path}"
        )

    definitions: list[LogErrorAlertDefinition] = []
    seen: set[str] = set()
    for raw in rules:
        if not isinstance(raw, dict):
            raise ObservabilityDefinitionError(f"Each rule must be an object: {path}")
        alert_name = str(raw.get("alert_name") or "").strip()
        service_name = str(raw.get("service_name") or "").strip()
        if not alert_name or not service_name:
            raise ObservabilityDefinitionError(
                f"Each rule needs alert_name and service_name: {path}"
            )
        if alert_name in seen:
            raise ObservabilityDefinitionError(
                f"Duplicate alert_name '{alert_name}' in {path}"
            )
        seen.add(alert_name)
        summary = str(
            raw.get("summary")
            or f"{service_name} emitted ERROR/FATAL logs in the last 5 minutes"
        )
        raw_threshold = raw.get("threshold", 0)
        try:
            threshold = int(raw_threshold)
        except (TypeError, ValueError) as exc:
            raise ObservabilityDefinitionError(
                f"Invalid 'threshold' {raw_threshold!r} for alert '{alert_name}' in "
                f"{path}: must be an integer."
            ) from exc
        definitions.append(
            LogErrorAlertDefinition(
                alert_name=alert_name,
                service_name=service_name,
                summary=summary,
                severity=str(raw.get("severity") or "error"),
                threshold=threshold,
                eval_window=str(raw.get("eval_window") or "5m0s"),
                frequency=str(raw.get("frequency") or "1m"),
            )
        )
    return definitions


def load_dashboard(path: Path = DASHBOARD_FILE) -> dict[str, Any]:
    """Load and validate the checked-in SigNoz dashboard definition."""
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ObservabilityDefinitionError(
            f"Dashboard definition must be a JSON object: {path}"
        )

    title = str(data.get("title") or "").strip()
    if not title:
        raise ObservabilityDefinitionError(f"Dashboard needs a title: {path}")

    widgets = data.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        raise ObservabilityDefinitionError(
            f"Dashboard needs a non-empty 'widgets' list: {path}"
        )

    for widget in widgets:
        if not isinstance(widget, dict) or not str(widget.get("title") or "").strip():
            raise ObservabilityDefinitionError(
                f"Each dashboard widget needs a title: {path}"
            )
    return data


def build_dashboard_import_payload(path: Path = DASHBOARD_FILE) -> dict[str, Any]:
    """Wrap a dashboard definition in the SigNoz /api/v1/dashboards body."""
    return {"data": load_dashboard(path)}


def load_openpanel_analytics(path: Path = OPENPANEL_ANALYTICS_FILE) -> dict[str, Any]:
    """Load + validate the checked-in OpenPanel analytics intent (funnels + events board).

    OpenPanel has no write API/MCP for funnels/dashboards, so this is config-as-code
    *intent* (applied manually per the SOP-006 runbook) rather than an auto-applied
    artifact. Validation keeps the spec well-formed and the funnel non-degenerate so the
    runbook and any future query tooling have a trustworthy source.
    """
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ObservabilityDefinitionError(f"OpenPanel analytics must be a JSON object: {path}")

    funnels = data.get("funnels")
    if not isinstance(funnels, list) or not funnels:
        raise ObservabilityDefinitionError(f"OpenPanel analytics needs a non-empty 'funnels' list: {path}")
    for funnel in funnels:
        steps = funnel.get("steps") if isinstance(funnel, dict) else None
        if not isinstance(steps, list) or len(steps) < 2:
            raise ObservabilityDefinitionError(
                f"Each funnel needs a name and >=2 steps: {path}"
            )
        if not str(funnel.get("name") or "").strip():
            raise ObservabilityDefinitionError(f"Each funnel needs a name: {path}")

    board = data.get("events_board")
    if not isinstance(board, dict) or not isinstance(board.get("events"), list) or not board["events"]:
        raise ObservabilityDefinitionError(
            f"OpenPanel analytics needs an 'events_board' with a non-empty 'events' list: {path}"
        )
    return data
