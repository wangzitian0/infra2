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
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libs.alerting import (
    _iter_signoz_items,
    build_signoz_log_alert_rule_payload,
    build_signoz_metric_alert_rule_payload,
    signoz_feishu_channel_name,
    signoz_rule_channel_problems,
)

# Repository root: libs/observability/dashboards.py -> repo root is parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

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
    service_id: str
    environment: str
    severity: str = "error"
    threshold: int = 0
    match_type: str = "at_least_once"
    eval_window: str = "5m0s"
    frequency: str = "1m"

    def to_signoz_payload(self, channel_names: list[str]) -> dict[str, Any]:
        """Render this definition into a SigNoz threshold-rule payload.

        ``channel_names`` are notification-channel NAMES: SigNoz routes by name.
        """
        return build_signoz_log_alert_rule_payload(
            alert_name=self.alert_name,
            service_name=self.service_name,
            channel_names=channel_names,
            summary=self.summary,
            severity=self.severity,
            threshold=self.threshold,
            match_type=self.match_type,
            eval_window=self.eval_window,
            frequency=self.frequency,
            service_id=self.service_id,
            environment=self.environment,
        )


@dataclass(frozen=True)
class MetricAlertDefinition:
    """Declarative PromQL metric alert rule definition."""

    alert_name: str
    promql: str
    summary: str
    service_id: str
    environment: str
    severity: str = "warning"
    threshold: float = 0
    threshold_unit: str = ""
    op: str = "above"
    match_type: str = "at_least_once"
    eval_window: str = "5m0s"
    frequency: str = "1m"
    service_name: str = "finance-report-backend"
    group_by: list[str] | None = None

    def to_signoz_payload(self, channel_names: list[str]) -> dict[str, Any]:
        """Render this definition into a SigNoz metric threshold-rule payload.

        ``channel_names`` are notification-channel NAMES: SigNoz routes by name.
        """
        return build_signoz_metric_alert_rule_payload(
            alert_name=self.alert_name,
            promql=self.promql,
            channel_names=channel_names,
            summary=self.summary,
            severity=self.severity,
            threshold=self.threshold,
            threshold_unit=self.threshold_unit,
            op=self.op,
            match_type=self.match_type,
            eval_window=self.eval_window,
            frequency=self.frequency,
            service_name=self.service_name,
            group_by=self.group_by,
            service_id=self.service_id,
            environment=self.environment,
        )


AlertDefinition = LogErrorAlertDefinition | MetricAlertDefinition


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
) -> list[AlertDefinition]:
    """Load and validate the checked-in alert definitions."""
    data = _load_json(path)
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not rules:
        raise ObservabilityDefinitionError(
            f"Alert definition must contain a non-empty 'rules' list: {path}"
        )

    definitions: list[AlertDefinition] = []
    service_id = str(data.get("service_id") or "").strip()
    environment = str(data.get("environment") or "").strip()
    seen: set[str] = set()
    for raw in rules:
        if not isinstance(raw, dict):
            raise ObservabilityDefinitionError(f"Each rule must be an object: {path}")
        alert_name = str(raw.get("alert_name") or "").strip()
        signal = str(raw.get("signal") or "logs").strip()
        service_name = str(raw.get("service_name") or "").strip()
        if not alert_name:
            raise ObservabilityDefinitionError(f"Each rule needs alert_name: {path}")
        if signal not in {"logs", "metrics"}:
            raise ObservabilityDefinitionError(
                f"Invalid signal {signal!r} for alert '{alert_name}' in {path}"
            )
        if not service_name:
            raise ObservabilityDefinitionError(
                f"Alert '{alert_name}' needs service_name: {path}"
            )
        if alert_name in seen:
            raise ObservabilityDefinitionError(
                f"Duplicate alert_name '{alert_name}' in {path}"
            )
        seen.add(alert_name)
        if "alert_on_absent" in raw:
            raise ObservabilityDefinitionError(
                f"Alert '{alert_name}' sets alert_on_absent in {path}: SigNoz ignores "
                "it on PromQL rules, and an error-log count has no data when healthy. "
                "Express 'no data is an outage' in the query with absent_over_time()."
            )
        raw_threshold = raw.get("threshold", 0)
        if signal == "metrics":
            summary = _required_text(raw.get("summary"), "summary", alert_name, path)
            promql = _required_text(raw.get("promql"), "promql", alert_name, path)
            threshold = _float_threshold(raw_threshold, alert_name, path)
            match_type = str(raw.get("match_type") or "at_least_once")
            _require_dotted_metric_selectors(promql, alert_name, path)
            _reject_in_total_over_range_vectors(promql, match_type, alert_name, path)
            _require_alert_identity(service_id, environment, path)
            definitions.append(
                MetricAlertDefinition(
                    alert_name=alert_name,
                    promql=promql,
                    summary=summary,
                    service_id=service_id,
                    environment=environment,
                    severity=str(raw.get("severity") or "warning"),
                    threshold=threshold,
                    threshold_unit=str(raw.get("threshold_unit") or ""),
                    op=str(raw.get("op") or "above"),
                    match_type=match_type,
                    eval_window=str(raw.get("eval_window") or "5m0s"),
                    frequency=str(raw.get("frequency") or "1m"),
                    service_name=service_name,
                    group_by=_string_list(
                        raw.get("group_by"), "group_by", alert_name, path
                    ),
                )
            )
            continue

        summary = _required_text(
            raw.get("summary")
            or f"{service_name} emitted ERROR/FATAL logs in the last 5 minutes",
            "summary",
            alert_name,
            path,
        )
        try:
            threshold = int(raw_threshold)
        except (TypeError, ValueError) as exc:
            raise ObservabilityDefinitionError(
                f"Invalid 'threshold' {raw_threshold!r} for alert '{alert_name}' in "
                f"{path}: must be an integer."
            ) from exc
        _require_alert_identity(service_id, environment, path)
        definitions.append(
            LogErrorAlertDefinition(
                alert_name=alert_name,
                service_name=service_name,
                summary=summary,
                service_id=service_id,
                environment=environment,
                severity=str(raw.get("severity") or "error"),
                threshold=threshold,
                match_type=str(raw.get("match_type") or "at_least_once"),
                eval_window=str(raw.get("eval_window") or "5m0s"),
                frequency=str(raw.get("frequency") or "1m"),
            )
        )
    return definitions


def require_rule_channel(payload: dict[str, Any], channel_name: str) -> dict[str, Any]:
    """Return ``payload`` if SigNoz would route it to ``channel_name``, else raise.

    A rule that ends up with no channel is accepted or rejected by SigNoz depending on
    its version and is never delivered; this makes that a definition error before
    anything is applied (#973).
    """
    problems = signoz_rule_channel_problems(payload, channel_name)
    if problems:
        raise ObservabilityDefinitionError(
            f"Alert '{payload.get('alert')}' would not be delivered to channel "
            f"{channel_name!r}: " + "; ".join(problems)
        )
    return payload


def render_alert_payloads(
    channel_name: str | None = None,
    *,
    deploy_env: str | None = None,
    path: Path = ALERT_RULES_FILE,
) -> list[dict[str, Any]]:
    """Render every checked-in alert rule bound to the Feishu bridge channel.

    The channel defaults to :func:`libs.alerting.signoz_feishu_channel_name`, the same
    definition ``platform/12.alerting`` creates the channel under. Each payload is
    checked with :func:`require_rule_channel` after rendering.
    """
    name = channel_name or signoz_feishu_channel_name(deploy_env)
    return [
        require_rule_channel(definition.to_signoz_payload([name]), name)
        for definition in load_alert_definitions(path)
    ]


@dataclass(frozen=True)
class RoutingProblem:
    """One reason the managed rules do not all reach the channel.

    ``kind`` says what to do about it: ``channel`` (the channel is not listed exactly
    once), ``missing`` (a catalog rule is absent or duplicated: run ``apply-alerts``),
    ``unbound`` (a rule exists but SigNoz would not deliver it to the channel: run
    ``apply-alerts``), ``stale`` (a managed rule that is no longer in the catalog: run
    ``apply-alerts --prune``).
    """

    kind: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.subject}: {self.detail}"


ROUTING_PROBLEM_KINDS = ("channel", "missing", "unbound", "stale")


def check_alert_routing(
    rules_response: Any,
    channels_response: Any,
    *,
    expected_alerts: Iterable[str],
    channel_name: str,
    managed_source: str | None = None,
) -> list[RoutingProblem]:
    """Problems that stop SigNoz from delivering the managed rules to ``channel_name``.

    Reads what ``GET /api/v1/rules`` and ``GET /api/v1/channels`` returned. Empty means
    the channel exists once, every catalog rule exists once, enabled, bound to it, and
    no rule labelled ``source=managed_source`` is left over that the catalog no longer
    has. A stale managed rule is a problem even when bound: the catalog is the source of
    truth, and ``apply-alerts`` only logs it until ``--prune``.
    """
    problems: list[RoutingProblem] = []
    channels = [
        item
        for item in _iter_signoz_items(channels_response, collection_keys=("channels",))
        if isinstance(item, dict)
    ]
    named = [item for item in channels if item.get("name") == channel_name]
    if len(named) != 1:
        problems.append(
            RoutingProblem(
                "channel",
                channel_name,
                f"channel exists {len(named)} times in SigNoz (expected 1)",
            )
        )
    channel_ids = {str(item["id"]) for item in channels if item.get("id")}

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in _iter_signoz_items(rules_response, collection_keys=("rules", "items")):
        if isinstance(rule, dict):
            by_name[str(rule.get("alert") or rule.get("name"))].append(rule)

    def unbound_reasons(rule: dict[str, Any]) -> list[str]:
        reasons = signoz_rule_channel_problems(rule, channel_name)
        if rule.get("disabled"):
            reasons.append("rule is disabled")
        bound_ids = sorted(
            {
                channel
                for spec in _spec_list(rule)
                for channel in (
                    spec.get("channels")
                    if isinstance(spec.get("channels"), list)
                    else []
                )
                if isinstance(channel, str) and channel in channel_ids
            }
        )
        if bound_ids:
            reasons.append(
                f"binds channel id(s) {bound_ids}; SigNoz routes by channel name"
            )
        return reasons

    expected = set(expected_alerts)
    for name in sorted(expected):
        found = by_name.get(name, [])
        if not found:
            problems.append(RoutingProblem("missing", name, "rule is not in SigNoz"))
        elif len(found) > 1:
            problems.append(
                RoutingProblem(
                    "missing", name, f"{len(found)} rules share this name (expected 1)"
                )
            )
        for rule in found:
            problems.extend(
                RoutingProblem("unbound", name, reason)
                for reason in unbound_reasons(rule)
            )
    if managed_source:
        for name, rules in sorted(by_name.items()):
            if name in expected:
                continue
            for rule in rules:
                if (rule.get("labels") or {}).get("source") != managed_source:
                    continue
                detail = (
                    "stale managed rule, no longer in the catalog "
                    "(`apply-alerts --prune` removes it)"
                )
                reasons = unbound_reasons(rule)
                if reasons:
                    detail += "; also not delivering: " + "; ".join(reasons)
                problems.append(RoutingProblem("stale", name, detail))
    return problems


def _spec_list(rule: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = (rule.get("condition") or {}).get("thresholds") or {}
    specs = thresholds.get("spec") if isinstance(thresholds, dict) else None
    return (
        [spec for spec in specs if isinstance(spec, dict)]
        if isinstance(specs, list)
        else []
    )


# A PromQL string literal, and a bare (unquoted) metric selector such as `foo_bar{`.
_PROMQL_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_BARE_METRIC_SELECTOR = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*\s*\{")
# A range selector (`[5m]`) or subquery (`[5m:1m]`): one value covers a whole window.
_RANGE_SELECTOR = re.compile(r"\[\s*\d+(?:ms|[smhdwy])")


def _require_dotted_metric_selectors(promql: str, alert_name: str, path: Path) -> None:
    """Reject the Prometheus-normalized selector form (`http_server_request_count{`).

    SigNoz runs with DOT_METRICS_ENABLED (platform/11.signoz/compose.yaml) and the
    collector stores OTel metrics under their own dotted names, so its PromQL adapter
    matches `metric_name` and label keys exactly as written. A normalized name matches
    no series: the rule is accepted, evaluates to nothing, and can never fire (#906).
    The only form that reaches the data is `{"http.server.request.count", "service.name"="…"}`.
    """
    unquoted = _PROMQL_STRING.sub('""', promql)
    bare = _BARE_METRIC_SELECTOR.search(unquoted)
    if bare:
        raise ObservabilityDefinitionError(
            f"Alert '{alert_name}' in {path} selects a metric by bare name "
            f"({bare.group(0).rstrip('{').strip()!r}); SigNoz stores OTel metrics under "
            'dotted names, so write the selector as {"metric.name", "label.key"="value"}.'
        )


def _reject_in_total_over_range_vectors(
    promql: str, match_type: str, alert_name: str, path: Path
) -> None:
    """Reject ``in_total`` on a query whose points each cover a trailing window.

    SigNoz evaluates a PromQL rule as a range query with a 60 s step and ``in_total``
    sums every point. When each point is already ``increase(x[15m])`` the windows
    overlap, so one event is counted once per step it stays inside the window: a
    single failure summed to ~15 against a threshold of 3 (#906).
    """
    if match_type == "in_total" and _RANGE_SELECTOR.search(promql):
        raise ObservabilityDefinitionError(
            f"Alert '{alert_name}' in {path} uses match_type in_total on a range "
            "vector: SigNoz sums every 60s step, and each step already covers the "
            "whole window. Compare the windowed value with at_least_once instead."
        )


def _require_alert_identity(service_id: str, environment: str, path: Path) -> None:
    if not service_id or not environment:
        raise ObservabilityDefinitionError(
            f"Alert definition needs top-level service_id and environment: {path}"
        )


def _required_text(value: Any, field: str, alert_name: str, path: Path) -> str:
    text = str(value or "").strip()
    if not text:
        raise ObservabilityDefinitionError(
            f"Alert '{alert_name}' needs non-empty {field}: {path}"
        )
    return text


def _float_threshold(value: Any, alert_name: str, path: Path) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ObservabilityDefinitionError(
            f"Invalid 'threshold' {value!r} for alert '{alert_name}' in {path}: "
            "must be a number."
        ) from exc


def _string_list(
    value: Any, field: str, alert_name: str, path: Path
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ObservabilityDefinitionError(
            f"Alert '{alert_name}' field {field} must be a list of strings: {path}"
        )
    return [item.strip() for item in value]


# SigNoz v0.105.1 stores a posted dashboard verbatim, except that ``Create`` runs a
# lossy v4 -> v5 query migration whenever ``version != "v5"`` and ``Update`` never does
# (``pkg/modules/dashboard/impldashboard/handler.go``). The checked-in file therefore
# carries the v5 query shape and says so: Create and Update then store the same bytes.
SIGNOZ_DASHBOARD_VERSION = "v5"
# v4 builder-query keys the migration deletes (``pkg/transition/migrate_common.go``
# ``updateQueryData``). A widget that still has one was not converted, or only half.
_V4_QUERY_KEYS = (
    "aggregateOperator",
    "aggregateAttribute",
    "filters",
    "temporality",
    "timeAggregation",
    "spaceAggregation",
    "reduceTo",
    "seriesAggregation",
)
_LAYOUT_KEYS = ("i", "x", "y", "w", "h")


def load_dashboard(path: Path = DASHBOARD_FILE) -> dict[str, Any]:
    """Load and validate the checked-in SigNoz dashboard definition.

    The returned map is exactly what ``POST/PUT /api/v1/dashboards`` takes as its body
    (SigNoz ``PostableDashboard`` = ``UpdatableDashboard`` = the dashboard map itself),
    so the checks here are SigNoz's own expectations: a top-level ``title``, ``widgets``
    that carry v5 queries, and a ``layout`` placing every widget (the UI draws only
    widgets that have a layout entry).
    """
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ObservabilityDefinitionError(
            f"Dashboard definition must be a JSON object: {path}"
        )

    title = str(data.get("title") or "").strip()
    if not title:
        raise ObservabilityDefinitionError(f"Dashboard needs a title: {path}")

    if data.get("version") != SIGNOZ_DASHBOARD_VERSION:
        raise ObservabilityDefinitionError(
            f"Dashboard version must be {SIGNOZ_DASHBOARD_VERSION!r}, got "
            f"{data.get('version')!r}: SigNoz migrates other versions' queries on "
            f"create only, so create and update would store different dashboards: {path}"
        )

    widgets = data.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        raise ObservabilityDefinitionError(
            f"Dashboard needs a non-empty 'widgets' list: {path}"
        )

    widget_ids: list[str] = []
    for widget in widgets:
        if not isinstance(widget, dict) or not str(widget.get("title") or "").strip():
            raise ObservabilityDefinitionError(
                f"Each dashboard widget needs a title: {path}"
            )
        widget_id = str(widget.get("id") or "").strip()
        if not widget_id or widget_id in widget_ids:
            raise ObservabilityDefinitionError(
                f"Each dashboard widget needs a unique id (got {widget_id!r}): {path}"
            )
        widget_ids.append(widget_id)
        _require_v5_builder_queries(widget, widget_id, path)

    _require_layout_places_every_widget(data.get("layout"), widget_ids, path)
    return data


def _require_v5_builder_queries(
    widget: dict[str, Any], widget_id: str, path: Path
) -> None:
    query = widget.get("query")
    builder = query.get("builder") if isinstance(query, dict) else None
    queries = builder.get("queryData") if isinstance(builder, dict) else None
    if not isinstance(queries, list) or not queries:
        raise ObservabilityDefinitionError(
            f"Widget '{widget_id}' needs query.builder.queryData: {path}"
        )
    for query_data in queries:
        if not isinstance(query_data, dict):
            raise ObservabilityDefinitionError(
                f"Widget '{widget_id}' has a malformed query: {path}"
            )
        stale = [key for key in _V4_QUERY_KEYS if key in query_data]
        if stale:
            raise ObservabilityDefinitionError(
                f"Widget '{widget_id}' still uses v4 query keys {stale} in {path}; "
                "write the v5 shape (aggregations[] + filter.expression)."
            )
        aggregations = query_data.get("aggregations")
        if not isinstance(aggregations, list) or not aggregations:
            raise ObservabilityDefinitionError(
                f"Widget '{widget_id}' query has no aggregations[] in {path}: SigNoz's "
                "v4 migration drops a bare `count` aggregation, leaving a query that "
                "aggregates nothing."
            )
        filter_expression = (query_data.get("filter") or {}).get("expression")
        if not isinstance(filter_expression, str) or not filter_expression.strip():
            raise ObservabilityDefinitionError(
                f"Widget '{widget_id}' query needs filter.expression in {path}"
            )


def _require_layout_places_every_widget(
    layout: Any, widget_ids: list[str], path: Path
) -> None:
    if not isinstance(layout, list) or not layout:
        raise ObservabilityDefinitionError(
            f"Dashboard needs a non-empty 'layout': SigNoz draws only widgets with a "
            f"layout entry: {path}"
        )
    placed: list[str] = []
    for entry in layout:
        if not isinstance(entry, dict) or any(key not in entry for key in _LAYOUT_KEYS):
            raise ObservabilityDefinitionError(
                f"Each layout entry needs {_LAYOUT_KEYS}: {path}"
            )
        placed.append(str(entry["i"]))
    if sorted(placed) != sorted(widget_ids):
        raise ObservabilityDefinitionError(
            f"Dashboard layout must place each widget exactly once: layout has "
            f"{sorted(placed)}, widgets are {sorted(widget_ids)}: {path}"
        )


def build_dashboard_import_payload(path: Path = DASHBOARD_FILE) -> dict[str, Any]:
    """The ``/api/v1/dashboards`` request body: the dashboard map itself, unwrapped.

    SigNoz v0.105.1 decodes the body straight into the dashboard map and stores it
    (``PostableDashboard = StorableDashboardData = map[string]interface{}`` in
    ``pkg/types/dashboardtypes/dashboard.go``; ``Create`` in
    ``pkg/modules/dashboard/impldashboard/handler.go``). Wrapping it in ``{"data": …}``
    stored the wrapper as the dashboard: an untitled dashboard with no widgets (#934).
    """
    return load_dashboard(path)


@dataclass(frozen=True)
class StoredDashboard:
    """One dashboard row SigNoz lists under a title: its id and how its data is shaped."""

    id: str
    #: True for a row the pre-#934 apply stored: the wrapper ``{"data": dashboard}`` was
    #: saved as the dashboard, so its title sits at ``data.data.title`` and SigNoz sees
    #: an untitled dashboard with no widgets.
    legacy: bool


def find_stored_dashboards(
    dashboards_response: Any, title: str
) -> list[StoredDashboard]:
    """Every dashboard SigNoz lists with exactly ``title``, best update target first.

    ``GET /api/v1/dashboards`` returns ``[{"id", "data": <the dashboard map>, …}]``
    (``Dashboard`` in ``pkg/types/dashboardtypes/dashboard.go``), so a correctly stored
    dashboard has its title at ``data.title``. Rows stored by the pre-#934 apply nest it
    one level deeper; they are returned too (``legacy``) so an apply converts one in
    place instead of adding another copy. Correctly stored rows come first.
    """
    proper: list[StoredDashboard] = []
    legacy: list[StoredDashboard] = []
    for item in _iter_signoz_items(
        dashboards_response, collection_keys=("dashboards",)
    ):
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        dashboard_id = item.get("id") or item.get("uuid")
        if not dashboard_id:
            continue
        if data.get("title") == title:
            proper.append(StoredDashboard(str(dashboard_id), legacy=False))
        elif (
            not data.get("title")
            and isinstance(data.get("data"), dict)
            and data["data"].get("title") == title
        ):
            legacy.append(StoredDashboard(str(dashboard_id), legacy=True))
    return proper + legacy


def check_dashboard_state(
    dashboards_response: Any, dashboard: dict[str, Any]
) -> list[str]:
    """Problems with how SigNoz holds ``dashboard``; empty means one correct copy.

    Correct is: exactly one row with that title, stored unwrapped (title at the top of
    its data, not nested), at the checked-in version, with the checked-in widgets and a
    layout placing them.
    """
    title = dashboard["title"]
    stored = find_stored_dashboards(dashboards_response, title)
    problems: list[str] = []
    if len(stored) != 1:
        problems.append(
            f"{len(stored)} dashboards titled {title!r} in SigNoz (expected 1): "
            f"{[entry.id for entry in stored]}"
        )
    if not stored or stored[0].legacy:
        if stored:
            problems.append(
                f"dashboard {stored[0].id} is still stored wrapped in a data envelope"
            )
        return problems
    row = next(
        item
        for item in _iter_signoz_items(
            dashboards_response, collection_keys=("dashboards",)
        )
        if isinstance(item, dict)
        and (item.get("id") or item.get("uuid")) == stored[0].id
    )
    data = row["data"]
    if data.get("version") != dashboard.get("version"):
        problems.append(
            f"stored version {data.get('version')!r}, checked-in {dashboard.get('version')!r}"
        )
    stored_ids = [w.get("id") for w in data.get("widgets") or [] if isinstance(w, dict)]
    wanted_ids = [w["id"] for w in dashboard["widgets"]]
    if stored_ids != wanted_ids:
        problems.append(
            f"stored widgets {stored_ids} differ from checked-in {wanted_ids}"
        )
    if not data.get("layout"):
        problems.append("stored dashboard has no layout, so SigNoz draws no widget")
    return problems


def load_openpanel_analytics(path: Path = OPENPANEL_ANALYTICS_FILE) -> dict[str, Any]:
    """Load + validate the checked-in OpenPanel analytics intent (funnels + events board).

    OpenPanel has no write API/MCP for funnels/dashboards, so this is config-as-code
    *intent* (applied manually per the SOP-006 runbook) rather than an auto-applied
    artifact. Validation keeps the spec well-formed and the funnel non-degenerate so the
    runbook and any future query tooling have a trustworthy source.
    """
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ObservabilityDefinitionError(
            f"OpenPanel analytics must be a JSON object: {path}"
        )

    funnels = data.get("funnels")
    if not isinstance(funnels, list) or not funnels:
        raise ObservabilityDefinitionError(
            f"OpenPanel analytics needs a non-empty 'funnels' list: {path}"
        )
    for funnel in funnels:
        steps = funnel.get("steps") if isinstance(funnel, dict) else None
        if not isinstance(steps, list) or len(steps) < 2:
            raise ObservabilityDefinitionError(
                f"Each funnel needs a name and >=2 steps: {path}"
            )
        if not all(isinstance(step, str) and step.strip() for step in steps):
            raise ObservabilityDefinitionError(
                f"Each funnel step must be a non-empty string: {path}"
            )
        if not str(funnel.get("name") or "").strip():
            raise ObservabilityDefinitionError(f"Each funnel needs a name: {path}")

    board = data.get("events_board")
    if (
        not isinstance(board, dict)
        or not isinstance(board.get("events"), list)
        or not board["events"]
    ):
        raise ObservabilityDefinitionError(
            f"OpenPanel analytics needs an 'events_board' with a non-empty 'events' list: {path}"
        )
    if not all(isinstance(event, str) and event.strip() for event in board["events"]):
        raise ObservabilityDefinitionError(
            f"Each events_board event must be a non-empty string: {path}"
        )
    return data


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
