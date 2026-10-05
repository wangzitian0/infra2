"""Offline tests for finance_report observability config-as-code (#373)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

from libs.alerting import (
    AlertingError,
    build_signoz_channel_payload,
    signoz_feishu_channel_name,
)
from libs.observability_dashboards import (
    ALERT_RULES_FILE,
    DASHBOARD_FILE,
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

ROOT = Path(__file__).resolve().parents[2]
OBS_DIR = ROOT / "finance_report" / "finance_report" / "observability"
APPLY_OBSERVABILITY_WORKFLOW = (
    ROOT / ".github" / "workflows" / "apply-observability.yml"
)
SIGNOZ_ALERT_PROBE = ROOT / "tools" / "signoz_alert_rule_probe.py"


def test_openpanel_analytics_spec_is_valid_and_funnel_is_well_formed() -> None:
    """D: the OpenPanel analytics intent (funnels + events board) parses, the activation
    funnel has >=2 ordered steps, and the events board is non-empty. (OpenPanel has no
    write API/MCP, so this spec is the source of truth for the SOP-006 manual build.)"""
    spec = load_openpanel_analytics()
    funnel = spec["funnels"][0]
    assert funnel["name"] == "Upload → Report"
    assert funnel["steps"] == ["upload_started", "upload_succeeded", "report_generated"]
    assert spec["events_board"]["events"]


def test_openpanel_funnel_steps_match_the_fe_event_taxonomy() -> None:
    """The funnel steps + board events must be real event names — guard against a typo
    drifting from the FE ANALYTICS_EVENTS / BE emitter taxonomy. (Names are asserted
    against the documented canonical set; the FE source lives in the app repo.)"""
    canonical = {
        "screen_view",
        "signup",
        "upload_started",
        "upload_succeeded",
        "upload_failed",
        "report_generated",
        "review_approved",
    }
    spec = load_openpanel_analytics()
    used = set(spec["funnels"][0]["steps"]) | set(spec["events_board"]["events"])
    assert used <= canonical, f"unknown event name(s): {used - canonical}"


def test_invalid_openpanel_analytics_raises() -> None:
    """A missing/malformed spec fails loudly instead of silently."""
    with pytest.raises(ObservabilityDefinitionError):
        load_openpanel_analytics(OBS_DIR / "does-not-exist-openpanel.json")


def test_openpanel_malformed_funnel_step_or_event_raise(tmp_path) -> None:
    """CR (#394): a funnel step / board event that isn't a non-empty string fails closed with
    ObservabilityDefinitionError instead of a later TypeError in callers."""
    import json

    base = {
        "funnels": [{"name": "f", "steps": ["a", "b"]}],
        "events_board": {"events": ["a"]},
    }

    def _write(obj):
        path = tmp_path / "openpanel.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    for obj in (
        {**base, "funnels": [{"name": "f", "steps": ["a", None]}]},  # null step
        {**base, "funnels": [{"name": "f", "steps": ["a", ""]}]},  # blank step
        {**base, "funnels": [{"name": "f", "steps": ["a", {}]}]},  # non-string step
        {**base, "events_board": {"events": [None]}},  # null event
        {**base, "events_board": {"events": [""]}},  # blank event
    ):
        with pytest.raises(ObservabilityDefinitionError):
            load_openpanel_analytics(_write(obj))

    # the well-formed base still loads
    assert load_openpanel_analytics(_write(base))["funnels"][0]["name"] == "f"


def test_definition_files_are_checked_in_and_parse() -> None:
    """#373: alert + dashboard definitions exist and are valid JSON."""
    assert ALERT_RULES_FILE.exists()
    assert DASHBOARD_FILE.exists()
    json.loads(ALERT_RULES_FILE.read_text(encoding="utf-8"))
    json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))


def test_finance_report_backend_error_logs_rule_is_defined() -> None:
    """#373: the FinanceReportBackendErrorLogs rule the app references exists as code."""
    definitions = load_alert_definitions()
    by_name = {d.alert_name: d for d in definitions}
    assert "FinanceReportBackendErrorLogs" in by_name
    rule = by_name["FinanceReportBackendErrorLogs"]
    assert rule.service_name == "finance-report-backend"
    assert rule.severity == "error"


def test_alert_definition_renders_signoz_log_rule_routed_to_channel() -> None:
    """#373: definition renders to a SigNoz v2 threshold rule wired to a channel."""
    rule = load_alert_definitions()[0]
    payload = rule.to_signoz_payload(["chan-1"])

    assert payload["alert"] == "FinanceReportBackendErrorLogs"
    assert payload["alertType"] == "LOGS_BASED_ALERT"
    assert payload["schemaVersion"] == "v2alpha1"
    threshold = payload["condition"]["thresholds"]["spec"][0]
    assert threshold["channels"] == ["chan-1"]
    (query,) = payload["condition"]["compositeQuery"]["queries"]
    assert query["type"] == "builder_query"
    assert query["spec"]["signal"] == "logs"
    assert query["spec"]["filter"]["expression"] == (
        "resource.service.name = 'finance-report-backend' AND "
        "resource.deployment.environment.name = 'production' AND "
        "severity_text IN ['ERROR', 'CRITICAL', 'FATAL']"
    )
    assert payload["labels"]["service_id"] == "finance_report/app"


# #906: what each checked-in rule must render to. (ruleType, severity, target,
# SigNoz matchType, evalWindow, targetUnit). matchType: "1" at_least_once,
# "2" all_times, "4" in_total.
_EXPECTED_RULES = {
    "FinanceReportBackendErrorLogs": ("threshold_rule", "error", 10, "4", "15m0s", ""),
    "FinanceReportHigh5xxRate": ("promql_rule", "critical", 0.05, "1", "5m0s", "%"),
    "FinanceReportP95LatencyHigh": (
        "promql_rule",
        "error",
        3000.0,
        "2",
        "10m0s",
        "ms",
    ),
    "FinanceReportStatementParseFailureSpike": (
        "promql_rule",
        "error",
        3.0,
        "1",
        "5m0s",
        "count",
    ),
    "FinanceReportRateLimitSaturation": (
        "promql_rule",
        "warning",
        10.0,
        "1",
        "5m0s",
        "count",
    ),
    "FinanceReportAsyncTaskFailures": (
        "promql_rule",
        "error",
        0.0,
        "1",
        "5m0s",
        "count",
    ),
    "FinanceReportBackendTelemetryAbsent": (
        "promql_rule",
        "error",
        0.0,
        "1",
        "5m0s",
        "",
    ),
}
# docs/ssot/ops.observability.md §3: the severity label is the P level.
_P_LEVEL_BY_SEVERITY = {"critical": "P0", "error": "P1", "warning": "P2"}


def _rendered_rules() -> dict[str, dict]:
    return {
        d.alert_name: d.to_signoz_payload(["chan-1"]) for d in load_alert_definitions()
    }


def test_the_catalog_holds_exactly_the_reviewed_rules() -> None:
    """#906: adding, dropping or renaming a rule has to update the reviewed table."""
    assert set(_rendered_rules()) == set(_EXPECTED_RULES)


@pytest.mark.parametrize("alert_name", sorted(_EXPECTED_RULES))
def test_rule_renders_its_reviewed_threshold_window_and_severity(alert_name) -> None:
    """#906: every rule renders the threshold, window, severity and absence choice
    recorded in the PR's per-rule table; alertOnAbsent is off on all of them (PromQL
    rules: SigNoz ignores it; the error-log count: no data is the healthy state)."""
    rule_type, severity, target, match_type, eval_window, unit = _EXPECTED_RULES[
        alert_name
    ]
    payload = _rendered_rules()[alert_name]

    assert payload["ruleType"] == rule_type
    (threshold,) = payload["condition"]["thresholds"]["spec"]
    assert threshold["name"] == severity
    assert threshold["target"] == target
    assert threshold["op"] == "1"
    assert threshold["matchType"] == match_type
    assert threshold["targetUnit"] == unit
    assert threshold["channels"] == ["chan-1"]
    assert payload["evaluation"]["spec"] == {
        "evalWindow": eval_window,
        "frequency": "1m",
    }
    assert payload["labels"]["severity"] == severity
    assert payload["condition"]["alertOnAbsent"] is False


@pytest.mark.parametrize("alert_name", sorted(_EXPECTED_RULES))
def test_rule_summary_names_the_p_level_of_its_severity_label(alert_name) -> None:
    """#906: the label decides routing and the summary is what a human reads; the P95
    rule said `warning` in one and `P1` in the other. §3: critical=P0, error=P1,
    warning=P2."""
    payload = _rendered_rules()[alert_name]

    expected = _P_LEVEL_BY_SEVERITY[payload["labels"]["severity"]]
    assert re.findall(r"Severity (P\d)\b", payload["annotations"]["summary"]) == [
        expected
    ]


# What finance-report-backend exports (finance_report
# apps/backend/src/observability/telemetry_metrics.py) under the names SigNoz stores
# with DOT_METRICS_ENABLED: the OTel instrument name as-is, a histogram's buckets as
# "<name>.bucket". Label values are listed where a rule filters on them.
_APP_METRICS: dict[str, dict[str, set[str]]] = {
    "http.server.request.count": {
        "http.response.status_code_class": {"1xx", "2xx", "3xx", "4xx", "5xx"},
        "http.route": {"/health", "/ping", "/ping/toggle", "/api/statements"},
    },
    "http.server.request.duration.bucket": {},
    "finance.statement_parse.outcome": {"outcome": {"success", "failure"}},
    "finance.reconciliation.match.outcome": {
        "outcome": {
            "auto_accepted",
            "pending_review",
            "accepted",
            "rejected",
            "superseded",
        }
    },
    "finance.rate_limit.rejected": {},
    "finance.async_parse.failure": {},
}
_SELECTOR = re.compile(r"\{([^{}]*)\}")
_MATCHER = re.compile(r'"([^"]+)"\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


def _selectors(promql: str) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """(metric, [(label, op, value)]) for every `{"metric", …}` selector in a query."""
    found = []
    for body in _SELECTOR.findall(promql):
        name = re.match(r'\s*"([^"]+)"\s*(?:,|$)', body)
        assert name, f"selector without a quoted metric name: {{{body}}}"
        found.append((name.group(1), _MATCHER.findall(body[name.end() :])))
    return found


def _promql_rules() -> dict[str, str]:
    return {
        name: payload["condition"]["compositeQuery"]["queries"][0]["spec"]["query"]
        for name, payload in _rendered_rules().items()
        if payload["ruleType"] == "promql_rule"
    }


def test_promql_selectors_reach_series_the_app_exports() -> None:
    """#906: SigNoz matches metric_name and label keys exactly as written, so every
    selector of every PromQL rule in the catalog must name a metric the app exports,
    pin the production backend, and use label values the app can emit.
    `outcome=~"failed|error|anomaly"` matched none of the reconciliation outcomes,
    so that rule could never fire."""
    rules = _promql_rules()
    value_filters = []

    assert rules
    for alert_name, promql in rules.items():
        selectors = _selectors(promql)
        assert selectors, alert_name
        for metric, matchers in selectors:
            assert metric in _APP_METRICS, (alert_name, metric)
            assert ("service.name", "=", "finance-report-backend") in matchers
            assert ("deployment.environment.name", "=", "production") in matchers
            value_filters += [
                (alert_name, metric, label, value)
                for label, op, value in matchers
                if op == "=~"
            ]

    assert value_filters  # the parse-failure outcome filter at least
    for alert_name, metric, label, value in value_filters:
        emitted = _APP_METRICS[metric].get(label, set())
        assert any(re.fullmatch(value, v) for v in emitted), (alert_name, value)


def test_promql_regex_matchers_are_anchored() -> None:
    """#906: SigNoz's PromQL adapter turns a regex matcher into ClickHouse `match()`,
    which is a substring search; `!~"/health"` would also drop `/api/x/health-report`.
    Explicit ^…$ gives the anchored Prometheus meaning on both engines."""
    regexes = [
        (name, value)
        for name, promql in _promql_rules().items()
        for _metric, matchers in _selectors(promql)
        for _label, op, value in matchers
        if op in {"=~", "!~"}
    ]

    assert regexes
    for name, value in regexes:
        assert value.startswith("^") and value.endswith("$"), (name, value)


# The probe routes the 5xx ratio and the p95 leave out (finance_report health/system routers).
_PROBE_ROUTE_EXCLUSION = ("http.route", "!~", "^(/health|/ping|/ping/toggle)$")


def test_5xx_rule_needs_a_minimum_5xx_count_and_ignores_probe_traffic() -> None:
    """#906: the old ratio divided by clamp_min(total_rate, 1) and required every point
    of the window, so a low-traffic outage never fired; healthy /health probes also
    diluted a user-facing outage below 5%. Every selector now excludes the probe
    routes, the ratio is not clamped, and at least 5 5xx must exist in the window."""
    promql = _promql_rules()["FinanceReportHigh5xxRate"]
    selectors = _selectors(promql)
    ratio, guard = promql.split(" and on() ", 1)

    assert "clamp_min" not in promql
    assert re.fullmatch(r"\(sum\(increase\(\{[^{}]*\}\[5m\]\)\) >= 5\)", guard)
    assert re.fullmatch(r"\(sum\(increase\(.*\)\) / sum\(increase\(.*\)\)\)", ratio)
    assert len(selectors) == 3
    for _metric, matchers in selectors:
        assert _PROBE_ROUTE_EXCLUSION in matchers
    five_xx = [
        m
        for _metric, m in selectors
        if ("http.response.status_code_class", "=", "5xx") in m
    ]
    assert len(five_xx) == 2  # the ratio's numerator and the count guard


def test_p95_rule_pages_only_on_a_sustained_non_probe_breach() -> None:
    """#906 live check (2026-09-24, 24 h): with /health included the p95 was mostly the
    health endpoint's own latency (p50 1412 ms, max 10 s), and 1500 ms / at_least_once
    would have fired in 197 five-minute windows a day. The rule excludes the probe
    routes and must hold above 3000 ms at every point of a 10 minute window.

    SigNoz drops NaN points before all_times, and the p95 is NaN in any minute with no
    non-probe request, so a lone slow request would pass "all remaining points above".
    `>= 0` drops the NaN and `or on() vector(0)` puts a 0 in its place, so a quiet
    minute breaks the streak instead of vanishing from it."""
    payload = _rendered_rules()["FinanceReportP95LatencyHigh"]
    promql = payload["condition"]["compositeQuery"]["queries"][0]["spec"]["query"]
    (threshold,) = payload["condition"]["thresholds"]["spec"]
    selectors = _selectors(promql)

    assert threshold["matchType"] == "2"  # all_times
    assert threshold["target"] == 3000.0
    assert payload["evaluation"]["spec"]["evalWindow"] == "10m0s"
    assert [metric for metric, _ in selectors] == [
        "http.server.request.duration.bucket"
    ]
    assert _PROBE_ROUTE_EXCLUSION in selectors[0][1]
    assert re.fullmatch(
        r"\(histogram_quantile\(0\.95, sum by \(le\) \(rate\(\{[^{}]*\}\[5m\]\)\)\) >= 0\)"
        r" or on\(\) vector\(0\)",
        promql,
    )


def test_backend_telemetry_absence_is_expressed_in_promql() -> None:
    """#906: alertOnAbsent is a no-op on SigNoz PromQL rules, so the one rule that
    must fire on missing data says so in the query: no request-count sample from the
    production backend for 10 minutes."""
    absent = {
        name: promql
        for name, promql in _promql_rules().items()
        if "absent_over_time(" in promql
    }

    assert list(absent) == ["FinanceReportBackendTelemetryAbsent"]
    promql = absent["FinanceReportBackendTelemetryAbsent"]
    assert promql.startswith("absent_over_time(") and promql.endswith("[10m])")
    assert [metric for metric, _ in _selectors(promql)] == ["http.server.request.count"]


def test_metric_alert_definition_renders_signoz_v5_payload() -> None:
    """#1106: metric alerts render to SigNoz v5 PromQL rules routed to Lark."""
    payload = _rendered_rules()["FinanceReportHigh5xxRate"]

    assert payload["alertType"] == "METRIC_BASED_ALERT"
    assert payload["ruleType"] == "promql_rule"
    assert payload["schemaVersion"] == "v2alpha1"
    composite = payload["condition"]["compositeQuery"]
    assert composite["queryType"] == "promql"
    assert "builderQueries" not in composite
    assert "promQueries" not in composite
    (query,) = composite["queries"]
    assert query["type"] == "promql"
    assert query["spec"]["name"] == "A"
    assert payload["labels"]["service"] == "finance-report-backend"


def test_dashboard_covers_backend_and_frontend_signals() -> None:
    """#373: dashboard covers BE error rate + latency and FE web-vitals + exceptions."""
    dashboard = load_dashboard()
    assert dashboard["title"]
    widget_titles = " ".join(w["title"].lower() for w in dashboard["widgets"])

    assert "error" in widget_titles
    assert "latency" in widget_titles
    assert "web-vitals" in widget_titles or "web vitals" in widget_titles
    assert "exception" in widget_titles

    raw = DASHBOARD_FILE.read_text(encoding="utf-8")
    assert "finance-report-backend" in raw
    assert "finance-report-frontend" in raw


def test_dashboard_has_full_backend_red_and_fe_page_load() -> None:
    """The baseline rounds out to RED for the backend (Rate / Errors / Duration
    p50·p95·p99) plus a FE page-load widget, not just error+p95."""
    widgets = {w["id"]: w for w in load_dashboard()["widgets"]}
    # Rate, Errors, and the three Duration percentiles
    for wid in (
        "be-throughput",
        "be-error-rate-5xx",
        "be-latency-p50",
        "be-latency-p95",
        "be-latency-p99",
    ):
        assert wid in widgets, f"missing RED widget {wid}"
    assert "fe-page-load" in widgets

    # the latency widgets request the right aggregate on durationNano (p50/p95/p99)
    def _agg(wid: str) -> str:
        (aggregation,) = widgets[wid]["query"]["builder"]["queryData"][0][
            "aggregations"
        ]
        return aggregation["expression"]

    assert _agg("be-latency-p50") == "p50(durationNano)"
    assert _agg("be-latency-p95") == "p95(durationNano)"
    assert _agg("be-latency-p99") == "p99(durationNano)"

    # fe-page-load is p75 of documentLoad span duration
    assert _agg("fe-page-load") == "p75(durationNano)"
    assert "name = 'documentLoad'" in _filter("fe-page-load", widgets)

    # the error-rate panel counts only SERVER spans flagged hasError (matches its text)
    err_filter = _filter("be-error-rate-5xx", widgets)
    assert "kind_string = 'Server'" in err_filter
    assert "hasError = true" in err_filter


def _filter(widget_id: str, widgets: dict[str, dict]) -> str:
    return widgets[widget_id]["query"]["builder"]["queryData"][0]["filter"][
        "expression"
    ]


def test_every_widget_is_filterable_by_deployment_environment() -> None:
    """The deployment_environment dropdown only filters a panel if the panel's query
    references it — assert EVERY widget carries the `$deployment_environment` filter so
    the dashboard variable is real, not decorative (CR #391)."""
    for w in load_dashboard()["widgets"]:
        expression = w["query"]["builder"]["queryData"][0]["filter"]["expression"]
        assert "deployment.environment IN $deployment_environment" in expression, (
            f"widget {w['id']} is not filterable by deployment_environment"
        )


def test_env_variable_query_targets_the_live_v2_logs_schema() -> None:
    """The deployment.environment dropdown must query the table/columns that actually
    exist on the SigNoz instance (logs v2: distributed_logs_v2 + resources_string),
    not the retired v1 schema (distributed_logs + stringTagMap) which UNKNOWN_TABLEs."""
    query = load_dashboard()["variables"]["deployment_environment"]["queryValue"]
    assert "distributed_logs_v2" in query
    assert "resources_string" in query
    # the retired v1 identifiers must not reappear
    assert "stringTagMap" not in query
    assert "signoz_logs.distributed_logs " not in query + " "


def test_dashboard_body_is_the_dashboard_map_signoz_stores() -> None:
    """#934: SigNoz v0.105.1 decodes the POST/PUT body straight into the dashboard map
    (`PostableDashboard = StorableDashboardData = map[string]interface{}`, handler.go
    `Create`) and stores it verbatim, so title/widgets/layout must be top-level keys.
    The old test pinned `{"data": …}` -- our code checked against itself -- and every
    apply stored an untitled, widget-less wrapper."""
    payload = build_dashboard_import_payload()

    assert payload["title"] == "Finance Report — Backend & Frontend"
    assert "data" not in payload
    assert isinstance(payload["widgets"], list) and payload["widgets"]
    assert isinstance(payload["layout"], list) and payload["layout"]
    assert payload == json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))


def test_dashboard_widgets_are_the_ones_signoz_counts_and_draws() -> None:
    """#934: what SigNoz does with the stored map. `GetWidgetIds` counts a widget only
    if it has `query` and a string `id`; the UI draws only widgets that have a `layout`
    entry. Both must cover every checked-in widget."""
    payload = build_dashboard_import_payload()

    counted = [
        w["id"]
        for w in payload["widgets"]
        if w.get("query") is not None and isinstance(w.get("id"), str)
    ]
    drawn = [entry["i"] for entry in payload["layout"]]
    assert counted == [w["id"] for w in payload["widgets"]]
    assert sorted(drawn) == sorted(counted)
    for entry in payload["layout"]:
        assert all(isinstance(entry[key], int) for key in ("x", "y", "w", "h"))


def test_dashboard_is_stored_in_the_v5_query_shape_so_create_equals_update() -> None:
    """#934: `Create` runs SigNoz's v4->v5 migration unless `version == "v5"`, `Update`
    never does. A v4 file would be stored migrated on the first apply and raw on every
    later one; and the migration drops a bare `count` aggregation. The file is v5 and
    every query says what it aggregates."""
    payload = build_dashboard_import_payload()

    assert payload["version"] == "v5"
    for widget in payload["widgets"]:
        for query in widget["query"]["builder"]["queryData"]:
            assert not {"aggregateOperator", "aggregateAttribute", "filters"} & set(
                query
            )
            assert query["aggregations"] and all(
                a["expression"] for a in query["aggregations"]
            )
            assert query["filter"]["expression"]


def _dashboard_with(tmp_path: Path, mutate) -> Path:
    data = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_the_unmutated_dashboard_copy_loads(tmp_path) -> None:
    """The fixture the rejection tests below mutate is valid, so each rejection is
    caused by the one field that test changes."""
    assert load_dashboard(_dashboard_with(tmp_path, lambda d: None))["widgets"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(version="v4"), "version"),
        (lambda d: d.update(version="v6"), "version"),
        (lambda d: d.pop("layout"), "layout"),
        (lambda d: d["layout"].pop(), "layout must place each widget"),
        (lambda d: d["layout"][0].update(i="not-a-widget"), "layout must place"),
        (lambda d: d["layout"][0].pop("w"), "layout entry"),
        (
            lambda d: d["widgets"][0]["query"]["builder"]["queryData"][0].update(
                aggregateOperator="count"
            ),
            "v4 query keys",
        ),
        (
            lambda d: d["widgets"][0]["query"]["builder"]["queryData"][0].pop(
                "aggregations"
            ),
            "no aggregations",
        ),
        (
            lambda d: d["widgets"][0]["query"]["builder"]["queryData"][0].pop("filter"),
            "filter.expression",
        ),
        (lambda d: d["widgets"][1].update(id=d["widgets"][0]["id"]), "unique id"),
    ],
)
def test_dashboard_that_signoz_would_store_broken_is_rejected(
    tmp_path, mutate, message
) -> None:
    """#934: the loader refuses what SigNoz would store as an empty or half-migrated
    dashboard, instead of the apply finding out on the live instance."""
    with pytest.raises(ObservabilityDefinitionError, match=message):
        load_dashboard(_dashboard_with(tmp_path, mutate))


def test_invalid_definitions_raise() -> None:
    """#373: malformed definitions fail loudly instead of silently."""
    with pytest.raises(ObservabilityDefinitionError):
        load_alert_definitions(OBS_DIR / "does-not-exist.json")
    with pytest.raises(ObservabilityDefinitionError):
        load_dashboard(OBS_DIR / "does-not-exist.json")


def test_non_integer_threshold_raises_definition_error(tmp_path) -> None:
    """#373 review: a malformed `threshold` raises ObservabilityDefinitionError
    (not a raw ValueError) so bad definitions fail with a clear, catchable error."""
    import json

    bad = tmp_path / "alert_rules.json"
    bad.write_text(
        json.dumps(
            {"rules": [{"alert_name": "X", "service_name": "svc", "threshold": "oops"}]}
        )
    )
    with pytest.raises(ObservabilityDefinitionError, match="threshold"):
        load_alert_definitions(bad)


def test_metric_alert_requires_promql_and_numeric_threshold(tmp_path) -> None:
    """#1106: malformed metric alert definitions fail before apply."""
    import json

    missing_promql = tmp_path / "missing_promql.json"
    missing_promql.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "signal": "metrics",
                        "alert_name": "BadMetric",
                        "service_name": "svc",
                        "threshold": 1,
                        "summary": "missing query",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ObservabilityDefinitionError, match="promql"):
        load_alert_definitions(missing_promql)

    bad_threshold = tmp_path / "bad_threshold.json"
    bad_threshold.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "signal": "metrics",
                        "alert_name": "BadMetric",
                        "service_name": "svc",
                        "threshold": "oops",
                        "promql": "sum(up)",
                        "summary": "bad threshold",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ObservabilityDefinitionError, match="threshold"):
        load_alert_definitions(bad_threshold)


def test_metric_alert_requires_metric_specific_summary_and_service(tmp_path) -> None:
    """#1106 review: metric rules cannot inherit log-oriented defaults."""
    import json

    missing_summary = tmp_path / "missing_summary.json"
    missing_summary.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "signal": "metrics",
                        "alert_name": "BadMetric",
                        "service_name": "svc",
                        "threshold": 1,
                        "promql": "sum(up)",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ObservabilityDefinitionError, match="summary"):
        load_alert_definitions(missing_summary)

    missing_service = tmp_path / "missing_service.json"
    missing_service.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "signal": "metrics",
                        "alert_name": "BadMetric",
                        "threshold": 1,
                        "summary": "missing service",
                        "promql": "sum(up)",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ObservabilityDefinitionError, match="service_name"):
        load_alert_definitions(missing_service)


def _write_catalog(tmp_path: Path, rule: dict) -> Path:
    path = tmp_path / "alert_rules.json"
    path.write_text(
        json.dumps(
            {
                "service_id": "finance_report/app",
                "environment": "production",
                "rules": [rule],
            }
        ),
        encoding="utf-8",
    )
    return path


_VALID_METRIC_RULE = {
    "signal": "metrics",
    "alert_name": "GuardProbe",
    "service_name": "finance-report-backend",
    "threshold": 3,
    "match_type": "at_least_once",
    "summary": "guard probe",
    "promql": 'sum(increase({"finance.async_parse.failure","service.name"="x"}[15m]))',
}


def test_the_guard_probe_rule_itself_loads(tmp_path) -> None:
    """The fixture the guard tests mutate is valid, so each rejection below is caused
    by the one field that test changes."""
    (definition,) = load_alert_definitions(_write_catalog(tmp_path, _VALID_METRIC_RULE))

    assert definition.alert_name == "GuardProbe"


def test_bare_prometheus_style_metric_selector_is_rejected(tmp_path) -> None:
    """#906: `finance_async_parse_failure{…}` names no series in a SigNoz that stores
    dotted OTel names; the loader refuses it instead of shipping a rule that never fires."""
    rule = {
        **_VALID_METRIC_RULE,
        "promql": 'sum(increase(finance_async_parse_failure{service_name="x"}[15m]))',
    }

    with pytest.raises(ObservabilityDefinitionError, match="bare name"):
        load_alert_definitions(_write_catalog(tmp_path, rule))


def test_braces_inside_a_quoted_regex_are_not_a_bare_selector(tmp_path) -> None:
    """A `{` inside a label value is data, not a selector."""
    rule = {
        **_VALID_METRIC_RULE,
        "promql": 'sum(increase({"finance.async_parse.failure","task"=~"^a{2}$"}[15m]))',
    }

    (definition,) = load_alert_definitions(_write_catalog(tmp_path, rule))

    assert definition.promql == rule["promql"]


def test_in_total_over_a_range_vector_is_rejected(tmp_path) -> None:
    """#906: SigNoz sums every 60 s point for in_total; each point of
    increase(x[15m]) already covers 15 minutes, so one failure summed to ~15."""
    rule = {**_VALID_METRIC_RULE, "match_type": "in_total"}

    with pytest.raises(ObservabilityDefinitionError, match="in_total"):
        load_alert_definitions(_write_catalog(tmp_path, rule))


def test_alert_on_absent_key_is_rejected(tmp_path) -> None:
    """#906: SigNoz never reads alertOnAbsent on a PromQL rule, so the key would look
    like coverage and do nothing; the loader points at absent_over_time() instead."""
    rule = {**_VALID_METRIC_RULE, "alert_on_absent": True}

    with pytest.raises(ObservabilityDefinitionError, match="absent_over_time"):
        load_alert_definitions(_write_catalog(tmp_path, rule))


def _load_fr_observability_tasks(monkeypatch):
    fake_invoke = types.ModuleType("invoke")
    fake_invoke.task = lambda func=None, **_kwargs: func if func else (lambda f: f)

    exceptions_module = types.ModuleType("invoke.exceptions")

    class Exit(Exception):
        def __init__(self, message="", code=0):
            super().__init__(message)
            self.code = code

    exceptions_module.Exit = Exit
    monkeypatch.setitem(sys.modules, "invoke", fake_invoke)
    monkeypatch.setitem(sys.modules, "invoke.exceptions", exceptions_module)

    path = OBS_DIR / "shared_tasks.py"
    spec = importlib.util.spec_from_file_location("fr_observability_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, Exit


def test_apply_tasks_are_invoke_tasks(monkeypatch) -> None:
    """#373: invoke exposes apply + print tasks for alerts and dashboard."""
    module, _exit = _load_fr_observability_tasks(monkeypatch)

    for name in ("apply_alerts", "apply_dashboard", "print_alerts", "print_dashboard"):
        assert hasattr(module, name), name


def _real_definition(alert_name: str):
    return next(d for d in load_alert_definitions() if d.alert_name == alert_name)


class _Def:
    """The real checked-in definition, so the payload carries a real channel binding."""

    alert_name = "FinanceReportHigh5xxRate"

    def to_signoz_payload(self, channel_names):
        return _real_definition(self.alert_name).to_signoz_payload(channel_names)


def test_apply_alerts_raises_nonzero_exit_when_rule_create_fails(monkeypatch) -> None:
    """#1106 regression: SigNoz 400s must fail the GitHub apply workflow."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)

    calls = []

    def fake_request(_c, *, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET" and path == "/api/v1/rules":
            return {"ok": True, "data": {"rules": []}, "status": 200, "body": "{}"}
        return {
            "ok": False,
            "data": None,
            "status": 400,
            "body": "bad_data",
        }

    alerting = types.SimpleNamespace(
        _ensure_signoz_channel=lambda _c: "chan-1",
        _signoz_request=fake_request,
    )
    fake_console = types.ModuleType("libs.console")
    fake_console.error = lambda *_args, **_kwargs: None
    fake_console.success = lambda *_args, **_kwargs: None
    fake_console.info = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    monkeypatch.setitem(sys.modules, "libs.console", fake_console)
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [_Def()])

    with pytest.raises(Exit) as exc:
        module.apply_alerts(object())

    assert exc.value.code == 1
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/v1/rules"),
        ("POST", "/api/v1/rules"),
    ]
    posted = calls[1][2]
    assert posted["alert"] == "FinanceReportHigh5xxRate"
    assert posted["labels"]["source"] == "infra2/finance_report-alerts"
    assert posted["condition"]["thresholds"]["spec"][0]["channels"] == ["chan-1"]


def _patch_console(monkeypatch, **overrides):
    fake = types.ModuleType("libs.console")
    for name in ("error", "success", "info", "warning"):
        setattr(fake, name, overrides.get(name, lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "libs.console", fake)
    return fake


def _stateful_alerting(initial_rules, calls):
    """A SigNoz mock that mutates state on DELETE/POST, so `_delete_signoz_rule`'s
    verify-after-delete re-list reflects the deletion."""
    rules = {str(r["id"]): dict(r) for r in initial_rules}

    def fake_request(_c, *, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET" and path == "/api/v1/rules":
            return {
                "ok": True,
                "data": {"rules": list(rules.values())},
                "status": 200,
                "body": "{}",
            }
        if method == "DELETE" and path.startswith("/api/v1/rules/"):
            rules.pop(path.rsplit("/", 1)[-1], None)
            return {"ok": True, "data": None, "status": 200, "body": "{}"}
        if method == "DELETE":  # v2 is idempotent-200 even for an absent id
            return {"ok": True, "data": None, "status": 200, "body": "{}"}
        if method == "POST" and path == "/api/v1/rules":
            new_id = f"new-{payload['alert']}"
            rules[new_id] = {
                "id": new_id,
                "alert": payload["alert"],
                "labels": payload.get("labels", {}),
            }
            return {"ok": True, "data": {"id": new_id}, "status": 200, "body": "{}"}
        return {"ok": False, "data": None, "status": 400, "body": "x"}

    return types.SimpleNamespace(
        _ensure_signoz_channel=lambda _c: "chan-1", _signoz_request=fake_request
    )


def test_apply_alerts_updates_existing_rule(monkeypatch) -> None:
    """Declarative: an existing rule is deleted + recreated so a changed definition
    actually takes effect (old behaviour silently skipped it)."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls = []
    alerting = _stateful_alerting(
        [{"id": "old-1", "alert": "FinanceReportHigh5xxRate", "labels": {}}], calls
    )
    _patch_console(monkeypatch)
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [_Def()])

    assert module.apply_alerts(object()) is True
    pairs = [(m, p) for (m, p, _) in calls]
    assert ("DELETE", "/api/v1/rules/old-1") in pairs  # existing removed
    assert ("POST", "/api/v1/rules") in pairs  # then recreated => change applies


def test_apply_alerts_prune_is_log_only_by_default(monkeypatch) -> None:
    """Managed residue (canary leftover) is reported, NOT deleted, unless --prune."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls = []
    alerting = _stateful_alerting(
        [
            {
                "id": "canary-1",
                "alert": "CanarySigNozPromqlPayload-99",
                "labels": {"canary": "true"},
            }
        ],
        calls,
    )
    infos = []
    _patch_console(
        monkeypatch, info=lambda *a, **k: infos.append(" ".join(str(x) for x in a))
    )
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [_Def()])

    module.apply_alerts(object())  # prune defaults False
    assert not any(m == "DELETE" and "canary-1" in p for (m, p, _) in calls)
    assert any("would prune" in msg for msg in infos)


def test_apply_alerts_prune_flag_deletes_only_managed_rules(monkeypatch) -> None:
    """--prune deletes managed residue but never a hand-made (unmarked) rule."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls = []
    alerting = _stateful_alerting(
        [
            {
                "id": "canary-1",
                "alert": "CanarySigNozPromqlPayload-99",
                "labels": {"canary": "true"},
            },
            {"id": "human-1", "alert": "SomeoneHandMadeRule", "labels": {}},
        ],
        calls,
    )
    _patch_console(monkeypatch)
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [_Def()])

    module.apply_alerts(object(), prune=True)
    deletes = [p for (m, p, _) in calls if m == "DELETE"]
    assert any("canary-1" in p for p in deletes)  # managed residue pruned
    assert not any("human-1" in p for p in deletes)  # hand-made rule untouched


def test_apply_alerts_prune_fails_loudly_on_missing_rule_id(monkeypatch) -> None:
    """CR: a managed stale rule with no id must NOT be reported deleted (the absence-check
    would falsely 'verify' deleting `str(None)`); fail loud instead, never issue the DELETE."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    calls = []
    alerting = _stateful_alerting(
        [
            {
                "id": None,
                "alert": "CanarySigNozPromqlPayload-7",
                "labels": {"canary": "true"},
            }
        ],
        calls,
    )
    _patch_console(monkeypatch)
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [_Def()])

    with pytest.raises(Exit):
        module.apply_alerts(object(), prune=True)
    assert not any(m == "DELETE" for (m, _p, _pl) in calls)  # never deleted a None id


def test_apply_dashboard_raises_nonzero_exit_when_list_fails(monkeypatch) -> None:
    """#1106 regression: dashboard apply must not create duplicates after list errors."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)

    calls = []

    def fake_request(_c, *, method, path, payload=None):
        calls.append((method, path, payload))
        return {
            "ok": False,
            "data": None,
            "status": 503,
            "body": "unavailable",
        }

    _patch_console(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "platform.12.alerting.shared",
        types.SimpleNamespace(_signoz_request=fake_request),
    )

    with pytest.raises(Exit) as exc:
        module.apply_dashboard(object())

    assert exc.value.code == 1
    assert calls == [("GET", "/api/v1/dashboards", None)]


def test_signoz_alert_canary_uses_disabled_v5_promql_payload() -> None:
    """#1106 regression: live canary uses the same disabled v5 PromQL payload."""
    spec = importlib.util.spec_from_file_location(
        "signoz_alert_rule_probe_under_test", SIGNOZ_ALERT_PROBE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    payload = module._build_canary_payload("CanarySigNozPromqlPayload-test", "chan-1")

    assert payload["alert"] == "CanarySigNozPromqlPayload-test"
    assert payload["disabled"] is True
    assert payload["alertType"] == "METRIC_BASED_ALERT"
    assert payload["ruleType"] == "promql_rule"
    composite = payload["condition"]["compositeQuery"]
    assert "promQueries" not in composite
    assert composite["queries"][0]["type"] == "promql"
    threshold = payload["condition"]["thresholds"]["spec"][0]
    assert threshold["channels"] == ["chan-1"]
    assert threshold["op"] == "1"
    assert threshold["matchType"] == "1"
    assert payload["labels"]["canary"] == "true"


def test_signoz_alert_canary_retries_rule_id_resolution(monkeypatch) -> None:
    """#1106 regression: canary cleanup should wait for the created rule to list."""
    spec = importlib.util.spec_from_file_location(
        "signoz_alert_rule_probe_under_test", SIGNOZ_ALERT_PROBE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = {"count": 0}

    def fake_request(_base_url, _api_key, *, method, path, payload=None):
        calls["count"] += 1
        assert method == "GET"
        assert path == "/api/v1/rules"
        if calls["count"] == 1:
            return {"ok": True, "data": {"rules": []}, "status": 200, "body": "{}"}
        return {
            "ok": True,
            "data": {"rules": [{"alert": "Canary", "id": "rule-1"}]},
            "status": 200,
            "body": "{}",
        }

    monkeypatch.setattr(module, "_signoz_request", fake_request)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert (
        module._resolve_rule_id("https://signoz.example", "key", "Canary") == "rule-1"
    )
    assert calls["count"] == 2


def test_apply_observability_workflow_exposes_canary_mode() -> None:
    """#1106 regression: CI can prove alert payloads before real catalog apply."""
    workflow = APPLY_OBSERVABILITY_WORKFLOW.read_text(encoding="utf-8")

    assert "mode:" in workflow
    assert "- canary" in workflow
    assert "tools/signoz_alert_rule_probe.py" in workflow
    assert "inputs.mode == 'canary'" in workflow
    assert "inputs.mode == 'apply'" in workflow


def test_ssot_documents_alert_and_dashboard_apply_path() -> None:
    """#373: ops docs document how the alert + dashboard are applied."""
    alerting = (ROOT / "docs/ssot/ops.observability.md").read_text(encoding="utf-8")
    observability = (ROOT / "docs/ssot/ops.observability.md").read_text(
        encoding="utf-8"
    )

    assert "FinanceReportBackendErrorLogs" in alerting
    assert "FinanceReportHigh5xxRate" in alerting
    assert "SOP-004C" in alerting
    assert "fr-observability.shared.apply-alerts" in alerting
    assert "fr-observability.shared.apply-dashboard" in observability


# ---------------------------------------------------------------------------
# #973: every rule is bound to the Feishu channel, by name


BRIDGE_CHANNEL = "infra2-feishu-alerts-production"
CHANNEL_ID = "0199aaaa-0000-7000-8000-000000000001"  # what SigNoz lists as the id


def _spec_channels(rule: dict) -> list[list[str]]:
    return [spec["channels"] for spec in rule["condition"]["thresholds"]["spec"]]


def test_every_rendered_rule_is_bound_to_the_bridge_channel_by_name() -> None:
    """#973: SigNoz v0.105.1 routes a v2alpha1 rule through
    `condition.thresholds.spec[].channels` (BasicRuleThreshold.Channels), turned into a
    route policy when the rule is created and into alertmanager receiver NAMES at
    delivery (dispatcher.go `getOrCreateRoute`). The rendered rules carry the bridge
    channel's name there and keep `usePolicy` off so those channels are the ones used."""
    payloads = render_alert_payloads()

    assert {p["alert"] for p in payloads} == set(_EXPECTED_RULES)
    for payload in payloads:
        assert _spec_channels(payload) == [[BRIDGE_CHANNEL]], payload["alert"]
        assert payload["schemaVersion"] == "v2alpha1"
        assert payload["notificationSettings"]["usePolicy"] is False
        # the v1-schema binding is not read for v2alpha1 rules
        assert "preferredChannels" not in payload


def test_rendered_channel_is_the_one_the_bridge_creates(monkeypatch) -> None:
    """#973: one definition of the channel name. The name platform/12.alerting creates
    the channel under (print-channel-payload posts exactly that payload) is the name
    every rule binds to, for production and for any other environment."""
    fake_invoke = types.ModuleType("invoke")
    fake_invoke.task = lambda func=None, **_kwargs: func if func else (lambda f: f)
    monkeypatch.setitem(sys.modules, "invoke", fake_invoke)
    spec = importlib.util.spec_from_file_location(
        "alerting_shared_channel_name", ROOT / "platform/12.alerting/shared_tasks.py"
    )
    assert spec and spec.loader
    alerting = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alerting)

    for env_name in ("production", "staging"):
        env = {"ENV": env_name, "ENV_SUFFIX": ""}
        monkeypatch.setattr(alerting, "get_env", lambda env=env: env)
        created = alerting.print_channel_payload(object())["name"]
        rendered = render_alert_payloads(created)
        assert created == signoz_feishu_channel_name(env_name)
        assert all(_spec_channels(p) == [[created]] for p in rendered)
    assert BRIDGE_CHANNEL == signoz_feishu_channel_name("production")


def test_channel_name_has_a_single_definition() -> None:
    """#973: no second hand-written copy of the channel name to drift."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "infra2-feishu-alerts" in path.read_text(encoding="utf-8", errors="ignore")
        and not {"tests", ".venv", "repos", "oh-my-code-agent"} & set(path.parts)
        and path != ROOT / "libs" / "alerting.py"
    ]
    assert offenders == []


@pytest.mark.parametrize("channels", [[], [""], ["  "], [None]])
def test_a_rule_with_no_channel_cannot_be_rendered(channels) -> None:
    """#973: SigNoz creates a route policy from the channels and rejects none ("at least
    one channel is required"); a rule that silently dropped them is undeliverable. The
    renderer raises instead of filtering them away."""
    for definition in load_alert_definitions():
        with pytest.raises(AlertingError):
            definition.to_signoz_payload(channels)


def test_a_bare_string_is_not_a_channel_list() -> None:
    """`"abc"` iterates as `a`,`b`,`c`: refuse it rather than bind three wrong names."""
    with pytest.raises(AlertingError):
        load_alert_definitions()[0].to_signoz_payload(BRIDGE_CHANNEL)


def test_renderer_rejects_a_rule_that_ends_up_unbound(monkeypatch) -> None:
    """#973: defence in depth. Even if a rule type's builder forgot the binding, the
    renderer's own check refuses it, so the apply never POSTs an unrouted rule."""
    from libs import observability_dashboards as obs

    real = obs.MetricAlertDefinition.to_signoz_payload

    def forgetful(self, channel_names):
        payload = real(self, channel_names)
        payload["condition"]["thresholds"]["spec"][0]["channels"] = []
        return payload

    monkeypatch.setattr(obs.MetricAlertDefinition, "to_signoz_payload", forgetful)

    with pytest.raises(ObservabilityDefinitionError, match="would not be delivered"):
        render_alert_payloads()


def test_require_rule_channel_rejects_every_way_a_rule_misses_the_channel() -> None:
    """#973: the checks `require_rule_channel` makes, one mutation each."""
    base = render_alert_payloads()[0]
    assert require_rule_channel(base, BRIDGE_CHANNEL) is base

    def mutated(change):
        payload = json.loads(json.dumps(base))
        change(payload)
        return payload

    wrong = {
        "no thresholds": lambda p: p["condition"]["thresholds"].update(spec=[]),
        "empty channels": lambda p: p["condition"]["thresholds"]["spec"][0].update(
            channels=[]
        ),
        "missing channels key": lambda p: p["condition"]["thresholds"]["spec"][0].pop(
            "channels"
        ),
        "other channel": lambda p: p["condition"]["thresholds"]["spec"][0].update(
            channels=["someone-else"]
        ),
        "channel id instead of name": lambda p: p["condition"]["thresholds"]["spec"][
            0
        ].update(channels=[CHANNEL_ID]),
        "usePolicy": lambda p: p["notificationSettings"].update(usePolicy=True),
        "no notificationSettings": lambda p: p.pop("notificationSettings"),
        "v1 schema": lambda p: p.update(schemaVersion="v1"),
    }
    for label, change in wrong.items():
        with pytest.raises(ObservabilityDefinitionError):
            require_rule_channel(mutated(change), BRIDGE_CHANNEL)
        assert label


# ---- check_alert_routing: what `verify-alert-routing` decides ----------------

_CHANNELS_RESPONSE = {
    "status": "success",
    "data": [{"id": CHANNEL_ID, "name": BRIDGE_CHANNEL, "type": "webhook"}],
}
_MANAGED = "infra2/finance_report-alerts"


def _stored_rules(**overrides) -> dict:
    """What GET /api/v1/rules lists after a correct apply, one entry per catalog rule."""
    rules = []
    for index, payload in enumerate(render_alert_payloads(), start=1):
        rule = json.loads(json.dumps(payload))
        rule.update(id=f"rule-{index}", state="inactive")
        rule["labels"]["source"] = _MANAGED
        rules.append(rule)
    for rule in rules:
        rule.update(overrides.get(rule["alert"], {}))
    return {"status": "success", "data": {"rules": rules}}


def _routing_problem_objects(rules=None, channels=None, **kwargs) -> list:
    return check_alert_routing(
        rules if rules is not None else _stored_rules(),
        channels if channels is not None else _CHANNELS_RESPONSE,
        expected_alerts=_EXPECTED_RULES,
        channel_name=BRIDGE_CHANNEL,
        managed_source=_MANAGED,
        **kwargs,
    )


def _routing_problems(rules=None, channels=None, **kwargs) -> list[str]:
    return [str(p) for p in _routing_problem_objects(rules, channels, **kwargs)]


def test_routing_check_passes_when_every_rule_is_bound() -> None:
    assert _routing_problems() == []


def test_routing_check_flags_a_rule_bound_to_the_channel_id() -> None:
    """#973: binding the channel id (what the old apply wrote) is accepted by SigNoz and
    never delivered; the check names it."""
    spec = json.loads(json.dumps(_stored_rules()["data"]["rules"][0]))
    spec["condition"]["thresholds"]["spec"][0]["channels"] = [CHANNEL_ID]
    name = spec["alert"]

    problems = _routing_problems(_stored_rules(**{name: spec}))

    assert any(
        problem.startswith(f"{name}:") and "channel id" in problem
        for problem in problems
    ), problems


def test_routing_check_flags_each_way_delivery_can_be_missing() -> None:
    rules = _stored_rules()["data"]["rules"]
    first, second, third = rules[0], rules[1], rules[2]

    unbound = json.loads(json.dumps(first))
    unbound["condition"]["thresholds"]["spec"][0]["channels"] = []
    disabled = {**second, "disabled": True}
    duplicated = rules + [dict(third, id="rule-dup")]
    stale = {
        "id": "stale-1",
        "alert": "RenamedAwayRule",
        "labels": {"source": _MANAGED},
        "schemaVersion": "v2alpha1",
        "condition": {"thresholds": {"spec": [{"channels": []}]}},
        "notificationSettings": {},
    }
    hand_made = {"id": "mine", "alert": "HandMade", "labels": {}}

    def run(rule_list, channels=None):
        return _routing_problems({"data": {"rules": rule_list}}, channels)

    assert any("channels [] do not include" in p for p in run([unbound, *rules[1:]]))
    assert any("rule is disabled" in p for p in run([first, disabled, *rules[2:]]))
    assert any("rule is not in SigNoz" in p for p in run(rules[1:]))
    assert any("2 rules share this name" in p for p in run(duplicated))
    assert any(
        p.startswith("RenamedAwayRule:") and "stale managed rule" in p
        for p in run(rules + [stale])
    )
    assert not any("HandMade" in p for p in run(rules + [hand_made]))
    assert any("exists 0 times" in p for p in run(rules, {"data": []}))
    twice = {"data": _CHANNELS_RESPONSE["data"] * 2}
    assert any("exists 2 times" in p for p in run(rules, twice))


def test_routing_check_tells_stale_from_unbound() -> None:
    """#973: the post-apply procedure removes a stale managed rule with `--prune`; the
    check says which rules are stale and which are unbound, so the operator sees why it
    fails and which command fixes it. A stale rule is a failure even when it is bound
    (the catalog is the truth), and a hand-made rule is never one."""
    rules = _stored_rules()["data"]["rules"]
    unbound = json.loads(json.dumps(rules[0]))
    unbound["condition"]["thresholds"]["spec"][0]["channels"] = [CHANNEL_ID]
    stale_bound = json.loads(json.dumps(rules[1]))
    stale_bound.update(id="stale-1", alert="FinanceReportReconciliationAnomaly")
    stale_unbound = json.loads(json.dumps(unbound))
    stale_unbound.update(id="stale-2", alert="FinanceReportRenamed")
    hand_made = {"id": "mine", "alert": "HandMade", "labels": {}}

    problems = _routing_problem_objects(
        {
            "data": {
                "rules": [unbound, *rules[1:], stale_bound, stale_unbound, hand_made]
            }
        }
    )

    by_kind: dict[str, set[str]] = {}
    for problem in problems:
        by_kind.setdefault(problem.kind, set()).add(problem.subject)
    assert by_kind == {
        "unbound": {rules[0]["alert"]},
        "stale": {"FinanceReportReconciliationAnomaly", "FinanceReportRenamed"},
    }
    detail = {p.subject: p.detail for p in problems if p.kind == "stale"}
    assert "--prune" in detail["FinanceReportReconciliationAnomaly"]
    assert "also not delivering" not in detail["FinanceReportReconciliationAnomaly"]
    assert "also not delivering" in detail["FinanceReportRenamed"]


# ---- the tasks, against a fake SigNoz HTTP client ----------------------------


def _fake_signoz(routes: dict, calls: list):
    """`_signoz_request` stand-in: serves (method, path) from `routes`, 404 otherwise."""

    def request(_c, *, method, path, payload=None):
        calls.append((method, path, payload))
        handler = routes.get((method, path))
        if handler is None:
            return {"ok": False, "data": None, "status": 404, "body": "not found"}
        if callable(handler):
            return handler(payload)
        return {"ok": True, "data": handler, "status": 200, "body": "{}"}

    return types.SimpleNamespace(
        _signoz_request=request,
        _channel_name=lambda env: signoz_feishu_channel_name(env.get("ENV")),
        get_env=lambda: {"ENV": "production"},
    )


def _install_fake_signoz(monkeypatch, routes, calls):
    errors: list[str] = []
    _patch_console(monkeypatch, error=lambda *a, **k: errors.append(str(a[0])))
    monkeypatch.setitem(
        sys.modules, "platform.12.alerting.shared", _fake_signoz(routes, calls)
    )
    return errors


def test_verify_alert_routing_passes_and_only_reads(monkeypatch) -> None:
    """#973: the post-apply check is read-only (GET only) and succeeds on bound rules."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls: list = []
    _install_fake_signoz(
        monkeypatch,
        {
            ("GET", "/api/v1/rules"): _stored_rules(),
            ("GET", "/api/v1/channels"): _CHANNELS_RESPONSE,
        },
        calls,
    )

    assert module.verify_alert_routing(object()) is True
    assert {method for method, _, _ in calls} == {"GET"}


def test_verify_alert_routing_fails_on_unbound_rule(monkeypatch) -> None:
    """#973: the exact failure live SigNoz had -- rules that name no deliverable
    channel -- makes the check exit non-zero and name the rule."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    rules = _stored_rules()
    rules["data"]["rules"][0]["condition"]["thresholds"]["spec"][0]["channels"] = [
        CHANNEL_ID
    ]
    calls: list = []
    errors = _install_fake_signoz(
        monkeypatch,
        {
            ("GET", "/api/v1/rules"): rules,
            ("GET", "/api/v1/channels"): _CHANNELS_RESPONSE,
        },
        calls,
    )

    with pytest.raises(Exit) as exc:
        module.verify_alert_routing(object())

    assert exc.value.code == 1
    assert any(rules["data"]["rules"][0]["alert"] in message for message in errors)


def test_verify_alert_routing_fails_when_a_listing_fails(monkeypatch) -> None:
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    _install_fake_signoz(
        monkeypatch, {("GET", "/api/v1/channels"): _CHANNELS_RESPONSE}, []
    )

    with pytest.raises(Exit) as exc:
        module.verify_alert_routing(object())

    assert exc.value.code == 1


def _stored_channel(**overrides) -> dict:
    """SigNoz stores a channel as `data` = the receiver config JSON string."""
    receiver = build_signoz_channel_payload(
        channel_name=BRIDGE_CHANNEL,
        bridge_url="http://platform-alerting:8080/signoz/webhook",
    )
    return {
        "id": CHANNEL_ID,
        "name": BRIDGE_CHANNEL,
        "type": "webhook",
        "data": json.dumps(receiver),
        **overrides,
    }


def test_test_alert_channel_posts_the_stored_receiver_to_test_channel(
    monkeypatch,
) -> None:
    """#973: one explicit test notification: SigNoz `/api/v1/testChannel` takes the
    receiver config (same body as channel create) and sends a test alert through it."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls: list = []
    _install_fake_signoz(
        monkeypatch,
        {
            ("GET", "/api/v1/channels"): {"data": [_stored_channel()]},
            ("POST", "/api/v1/testChannel"): lambda payload: {
                "ok": True,
                "data": None,
                "status": 204,
                "body": "",
            },
        },
        calls,
    )

    assert module.test_alert_channel(object()) is True
    posts = [call for call in calls if call[0] == "POST"]
    assert [(m, p) for m, p, _ in posts] == [("POST", "/api/v1/testChannel")]
    assert posts[0][2] == json.loads(_stored_channel()["data"])
    assert posts[0][2]["name"] == BRIDGE_CHANNEL


def test_test_alert_channel_sends_nothing_without_exactly_one_channel(
    monkeypatch,
) -> None:
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    for channels in ([], [_stored_channel(), _stored_channel(id="other")]):
        calls: list = []
        _install_fake_signoz(
            monkeypatch,
            {
                ("GET", "/api/v1/channels"): {"data": channels},
                ("POST", "/api/v1/testChannel"): {},
            },
            calls,
        )
        with pytest.raises(Exit) as exc:
            module.test_alert_channel(object())
        assert exc.value.code == 1
        assert not [call for call in calls if call[0] == "POST"]


def test_test_alert_channel_fails_when_signoz_rejects_the_test(monkeypatch) -> None:
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    _install_fake_signoz(
        monkeypatch,
        {
            ("GET", "/api/v1/channels"): {"data": [_stored_channel()]},
            ("POST", "/api/v1/testChannel"): lambda payload: {
                "ok": False,
                "data": None,
                "status": 500,
                "body": "webhook unreachable",
            },
        },
        [],
    )

    with pytest.raises(Exit) as exc:
        module.test_alert_channel(object())

    assert exc.value.code == 1


def test_apply_alerts_binds_the_channel_name_not_the_id(monkeypatch) -> None:
    """#973: the apply posts every catalog rule with the channel NAME the alerting
    module resolved (ensure returns the name), checked by the renderer."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls: list = []
    alerting = _stateful_alerting([], calls)
    alerting._ensure_signoz_channel = lambda _c: BRIDGE_CHANNEL
    _patch_console(monkeypatch)
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)

    assert module.apply_alerts(object()) is True

    posted = [payload for method, _, payload in calls if method == "POST"]
    assert {p["alert"] for p in posted} == set(_EXPECTED_RULES)
    for payload in posted:
        assert _spec_channels(payload) == [[BRIDGE_CHANNEL]]


# ---------------------------------------------------------------------------
# #934: dashboards are upserted by title and sent the way SigNoz stores them

DASH_TITLE = "Finance Report — Backend & Frontend"


def _proper_row(dashboard_id: str, title: str = DASH_TITLE) -> dict:
    """A GET /api/v1/dashboards row as v0.105.1 lists it: the dashboard map at `data`."""
    return {
        "id": dashboard_id,
        "createdAt": "2026-09-25T00:00:00Z",
        "locked": False,
        "data": {"title": title, "widgets": [], "version": "v5"},
    }


def _legacy_row(dashboard_id: str) -> dict:
    """A row the pre-#934 apply stored: the `{"data": dashboard}` wrapper, saved as-is."""
    return {
        "id": dashboard_id,
        "data": {
            "data": {"title": DASH_TITLE, "widgets": [{"id": "x"}]},
            "version": "v6",
        },
    }


def _stateful_dashboards(initial: list[dict], calls: list):
    """SigNoz dashboards API mock: POST adds a row, PUT replaces `data`, DELETE removes."""
    rows = {row["id"]: row for row in initial}
    counter = {"next": 0}

    def request(_c, *, method, path, payload=None):
        calls.append((method, path, payload))
        ok = {"ok": True, "status": 200, "body": "{}"}
        if (method, path) == ("GET", "/api/v1/dashboards"):
            return {**ok, "data": {"status": "success", "data": list(rows.values())}}
        if (method, path) == ("POST", "/api/v1/dashboards"):
            counter["next"] += 1
            new_id = f"new-{counter['next']}"
            rows[new_id] = {"id": new_id, "data": payload}
            return {**ok, "data": {"status": "success", "data": rows[new_id]}}
        prefix = "/api/v1/dashboards/"
        if path.startswith(prefix):
            dashboard_id = path[len(prefix) :]
            if method == "PUT" and dashboard_id in rows:
                rows[dashboard_id]["data"] = payload
                return {**ok, "data": None}
            if method == "DELETE":
                rows.pop(dashboard_id, None)
                return {**ok, "data": None}
        return {"ok": False, "data": None, "status": 404, "body": "nope"}

    return types.SimpleNamespace(_signoz_request=request), rows


def _install_dashboards(monkeypatch, initial):
    calls: list = []
    alerting, rows = _stateful_dashboards(initial, calls)
    messages: dict[str, list[str]] = {"warning": [], "info": [], "error": []}
    _patch_console(
        monkeypatch,
        **{
            level: (lambda *a, level=level, **k: messages[level].append(str(a[0])))
            for level in messages
        },
    )
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    return calls, rows, messages


def _writes(calls):
    return [(m, p) for m, p, _ in calls if m != "GET"]


def test_apply_dashboard_creates_it_unwrapped_when_absent(monkeypatch) -> None:
    """#934: the POST body is the dashboard map itself, not `{"data": …}`."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls, rows, _ = _install_dashboards(monkeypatch, [_proper_row("other", "Other")])

    assert module.apply_dashboard(object()) is True

    ((method, path, body),) = [c for c in calls if c[0] == "POST"]
    assert (method, path) == ("POST", "/api/v1/dashboards")
    assert body["title"] == DASH_TITLE and "data" not in body
    assert body == build_dashboard_import_payload()
    assert check_dashboard_state({"data": list(rows.values())}, load_dashboard()) == []


def test_apply_dashboard_updates_in_place_and_never_adds_a_copy(monkeypatch) -> None:
    """#934: applying twice leaves one dashboard: the first apply creates, the second
    finds it by its top-level title and PUTs. (The old lookup never matched the row it
    had just stored, so every apply was a POST: 23 copies.)"""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls, rows, _ = _install_dashboards(monkeypatch, [])

    module.apply_dashboard(object())
    module.apply_dashboard(object())
    module.apply_dashboard(object())

    assert [m for m, _ in _writes(calls)] == ["POST", "PUT", "PUT"]
    assert len(rows) == 1
    ((_, put_path, put_body),) = [c for c in calls if c[0] == "PUT"][:1]
    assert put_path == "/api/v1/dashboards/new-1" and put_body == (
        build_dashboard_import_payload()
    )


def test_apply_dashboard_converts_a_legacy_row_instead_of_adding_one(
    monkeypatch,
) -> None:
    """#934: prod holds rows stored as `{"data": dashboard}`. The apply finds one by its
    nested title and rewrites it in place (PUT does not run the v4 migration, so it
    stores exactly the checked-in v5 bytes); it does not POST a 24th."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls, rows, _ = _install_dashboards(
        monkeypatch, [_legacy_row("old-1"), _legacy_row("old-2")]
    )

    module.apply_dashboard(object())

    assert _writes(calls) == [("PUT", "/api/v1/dashboards/old-1")]
    assert rows["old-1"]["data"] == build_dashboard_import_payload()


def test_apply_dashboard_reports_duplicates_and_deletes_nothing_by_default(
    monkeypatch,
) -> None:
    """#934: duplicates are reported, with their ids; without the flag no DELETE runs."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls, rows, messages = _install_dashboards(
        monkeypatch,
        [_proper_row("keep"), _legacy_row("dup-1"), _proper_row("dup-2")],
    )

    assert module.apply_dashboard(object()) is True

    assert _writes(calls) == [("PUT", "/api/v1/dashboards/keep")]
    assert set(rows) == {"keep", "dup-1", "dup-2"}
    (warning,) = messages["warning"]
    assert (
        "dup-1" in warning and "dup-2" in warning and "--delete-duplicates" in warning
    )


def test_apply_dashboard_delete_duplicates_deletes_only_exact_title_extras(
    monkeypatch,
) -> None:
    """#934: with the explicit flag the extras go, and only those: not the row that was
    updated, not a dashboard with another title (even a similar one)."""
    module, _ = _load_fr_observability_tasks(monkeypatch)
    calls, rows, messages = _install_dashboards(
        monkeypatch,
        [
            _proper_row("keep"),
            _legacy_row("dup-1"),
            _proper_row("dup-2"),
            _proper_row("other", "Finance Report — Backend"),
            _proper_row("someone-else", "Hand made"),
        ],
    )

    assert module.apply_dashboard(object(), delete_duplicates=True) is True

    assert _writes(calls) == [
        ("PUT", "/api/v1/dashboards/keep"),
        ("DELETE", "/api/v1/dashboards/dup-2"),  # correctly stored copies first
        ("DELETE", "/api/v1/dashboards/dup-1"),
    ]
    assert set(rows) == {"keep", "other", "someone-else"}
    assert any("dup-1" in message for message in messages["info"])


def test_apply_dashboard_delete_duplicates_fails_if_a_duplicate_survives(
    monkeypatch,
) -> None:
    """SigNoz answers an undone delete like a done one (the rules canary leaked that
    way): the apply trusts the re-list, so a surviving duplicate is a failure."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    calls, rows, _ = _install_dashboards(
        monkeypatch, [_proper_row("keep"), _proper_row("stubborn")]
    )
    real = sys.modules["platform.12.alerting.shared"]._signoz_request

    def refuses_delete(c, *, method, path, payload=None):
        if method == "DELETE":
            calls.append((method, path, payload))
            return {"ok": True, "data": None, "status": 200, "body": "{}"}
        return real(c, method=method, path=path, payload=payload)

    sys.modules["platform.12.alerting.shared"]._signoz_request = refuses_delete

    with pytest.raises(Exit) as exc:
        module.apply_dashboard(object(), delete_duplicates=True)

    assert exc.value.code == 1
    assert "stubborn" in rows


def test_apply_dashboard_does_not_delete_when_the_update_failed(monkeypatch) -> None:
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    calls, rows, _ = _install_dashboards(
        monkeypatch, [_proper_row("keep"), _proper_row("dup")]
    )
    real = sys.modules["platform.12.alerting.shared"]._signoz_request

    def put_fails(c, *, method, path, payload=None):
        if method == "PUT":
            calls.append((method, path, payload))
            return {"ok": False, "data": None, "status": 400, "body": "bad"}
        return real(c, method=method, path=path, payload=payload)

    sys.modules["platform.12.alerting.shared"]._signoz_request = put_fails

    with pytest.raises(Exit):
        module.apply_dashboard(object(), delete_duplicates=True)

    assert not [call for call in calls if call[0] == "DELETE"]
    assert set(rows) == {"keep", "dup"}


def test_find_stored_dashboards_reads_the_v0_105_list_shape() -> None:
    """#934: lookup fixture shaped like the real list (`{"status","data":[{id,data}]}`),
    including an integration dashboard, a similarly named one and a legacy row."""
    listing = {
        "status": "success",
        "data": [
            {"id": "integration--x", "data": {"title": "Redis", "widgets": []}},
            _legacy_row("legacy"),
            _proper_row("proper"),
            _proper_row("near", DASH_TITLE + " (copy)"),
            {"id": "broken", "data": "not a map"},
            {"data": {"title": DASH_TITLE}},  # no id: cannot be addressed
        ],
    }

    found = find_stored_dashboards(listing, DASH_TITLE)

    assert [(d.id, d.legacy) for d in found] == [("proper", False), ("legacy", True)]


def test_dashboard_state_check_accepts_only_one_correct_copy() -> None:
    dashboard = load_dashboard()
    good = {"id": "g", "data": dict(dashboard)}

    assert check_dashboard_state({"data": [good]}, dashboard) == []
    assert any(
        "expected 1" in p for p in check_dashboard_state({"data": []}, dashboard)
    )
    twice = [good, {"id": "h", "data": dict(dashboard)}]
    assert any(
        "expected 1" in p for p in check_dashboard_state({"data": twice}, dashboard)
    )
    wrapped = check_dashboard_state({"data": [_legacy_row("l")]}, dashboard)
    assert any("envelope" in p for p in wrapped)
    stale_version = {"id": "g", "data": {**dashboard, "version": "v4"}}
    assert any(
        "version" in p
        for p in check_dashboard_state({"data": [stale_version]}, dashboard)
    )
    no_layout = {
        "id": "g",
        "data": {k: v for k, v in dashboard.items() if k != "layout"},
    }
    assert any(
        "layout" in p for p in check_dashboard_state({"data": [no_layout]}, dashboard)
    )
    fewer = {"id": "g", "data": {**dashboard, "widgets": dashboard["widgets"][:1]}}
    assert any(
        "widgets" in p for p in check_dashboard_state({"data": [fewer]}, dashboard)
    )


def test_verify_dashboard_task_reads_and_judges(monkeypatch) -> None:
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    dashboard = load_dashboard()
    calls, rows, _ = _install_dashboards(monkeypatch, [{"id": "g", "data": dashboard}])

    assert module.verify_dashboard(object()) is True
    assert _writes(calls) == []

    rows["dup"] = {"id": "dup", "data": dict(dashboard)}
    with pytest.raises(Exit) as exc:
        module.verify_dashboard(object())
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# #973: the REAL channel lookup, against a SigNoz whose channel id != its name

REAL_CHANNEL_ID = "019e91c3-7a52-7d3e-9a4c-0b1f5e6d8a21"


class _FakeSigNoz:
    """SigNoz HTTP mock for the real platform/12.alerting helpers.

    The channel's id is a UUID and differs from its name, as on the live instance; a
    created rule keeps the body it was posted with, as SigNoz stores it.
    """

    def __init__(self, *, with_channel=True, rules=()):
        self.channels = (
            [{"id": REAL_CHANNEL_ID, "name": BRIDGE_CHANNEL, "type": "webhook"}]
            if with_channel
            else []
        )
        self.rules = {rule["id"]: dict(rule) for rule in rules}
        self.calls: list[tuple[str, str, object]] = []
        self._counter = 0

    def request(self, _c, *, method, path, payload=None):
        self.calls.append((method, path, payload))
        ok = {"ok": True, "status": 200, "body": "{}"}
        if (method, path) == ("GET", "/api/v1/channels"):
            return {**ok, "data": {"status": "success", "data": list(self.channels)}}
        if (method, path) == ("POST", "/api/v1/channels"):
            self.channels.append(
                {"id": f"0199{len(self.channels):04d}-0000-7000-8000-00000000cafe"}
                | {"name": payload["name"], "type": "webhook"}
            )
            return {**ok, "status": 204, "data": None, "body": ""}
        if (method, path) == ("GET", "/api/v1/rules"):
            return {
                **ok,
                "data": {
                    "status": "success",
                    "data": {"rules": list(self.rules.values())},
                },
            }
        if (method, path) == ("POST", "/api/v1/rules"):
            self._counter += 1
            rule_id = f"rule-{self._counter}"
            self.rules[rule_id] = {**json.loads(json.dumps(payload)), "id": rule_id}
            return {**ok, "data": {"status": "success", "data": {"id": rule_id}}}
        if method == "DELETE" and path.startswith("/api/v1/rules/"):
            self.rules.pop(path.rsplit("/", 1)[-1], None)
            return {**ok, "data": None}
        if method == "DELETE":  # the v2 path answers 200 for any id
            return {**ok, "data": None}
        return {"ok": False, "data": None, "status": 404, "body": "not found"}

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


def _install_real_alerting(monkeypatch, fake: _FakeSigNoz):
    """The FR tasks + the real platform/12.alerting module over `fake`. Returns
    (fr_tasks, alerting, Exit, errors)."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "alerting_shared_real_under_test", ROOT / "platform/12.alerting/shared_tasks.py"
    )
    assert spec and spec.loader
    alerting = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alerting)
    monkeypatch.setattr(
        alerting, "get_env", lambda: {"ENV": "production", "ENV_SUFFIX": ""}
    )
    monkeypatch.setattr(alerting, "_signoz_request", fake.request)
    import libs.env

    monkeypatch.setattr(libs.env, "get_secrets", lambda *a, **k: {})
    errors: list[str] = []
    _patch_console(monkeypatch, error=lambda *a, **k: errors.append(str(a[0])))
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    return module, alerting, Exit, errors


def test_ensure_channel_returns_the_name_never_the_id(monkeypatch) -> None:
    """#973: rules bind to the channel NAME, so what the real lookup/creation returns
    must be the name even though SigNoz knows the channel by a different UUID."""
    assert REAL_CHANNEL_ID != BRIDGE_CHANNEL
    existing = _FakeSigNoz()
    _, alerting, _, _ = _install_real_alerting(monkeypatch, existing)
    assert alerting._ensure_signoz_channel(object()) == BRIDGE_CHANNEL

    created = _FakeSigNoz(with_channel=False)
    _, alerting, _, _ = _install_real_alerting(monkeypatch, created)
    assert alerting._ensure_signoz_channel(object()) == BRIDGE_CHANNEL
    (channel,) = created.channels
    assert channel["name"] == BRIDGE_CHANNEL and channel["id"] != BRIDGE_CHANNEL


@pytest.mark.parametrize("with_channel", [True, False])
def test_apply_alerts_binds_every_rule_to_the_channel_name_end_to_end(
    monkeypatch, with_channel
) -> None:
    """#973 regression: through the real channel lookup (found, or created during the
    apply), every rule SigNoz stores has `channels == [<channel NAME>]`, not the UUID
    SigNoz lists as the channel's id -- the exact bug that gave 156 `stage for receiver
    missing` errors a day."""
    fake = _FakeSigNoz(with_channel=with_channel)
    module, _, _, _ = _install_real_alerting(monkeypatch, fake)

    assert module.apply_alerts(object()) is True

    (channel,) = fake.channels
    assert channel["id"] != channel["name"] == BRIDGE_CHANNEL
    assert {r["alert"] for r in fake.rules.values()} == set(_EXPECTED_RULES)
    for rule in fake.rules.values():
        assert _spec_channels(rule) == [[BRIDGE_CHANNEL]], rule["alert"]
        assert channel["id"] not in json.dumps(rule)


def _prod_before_this_fix() -> list[dict]:
    """The live state #973 describes: every catalog rule bound to the channel UUID, plus
    the managed rule the catalog dropped in #906 (`FinanceReportReconciliationAnomaly`)."""
    rules = []
    for index, payload in enumerate(render_alert_payloads(REAL_CHANNEL_ID), start=1):
        rule = {**payload, "id": f"old-{index}", "state": "inactive"}
        rule["labels"] = {**payload["labels"], "source": _MANAGED}
        rules.append(rule)
    stale = {
        **rules[0],
        "id": "old-stale",
        "alert": "FinanceReportReconciliationAnomaly",
    }
    return [*rules, stale]


def test_owner_approved_sequence_reaches_a_green_verify(monkeypatch) -> None:
    """#973: the post-merge procedure, played on prod's before-state.

    verify fails (7 unbound + 1 stale) -> the merge's default apply rebinds the 7 and
    only logs the stale rule -> verify still fails, naming just the stale one ->
    `apply-alerts --prune` removes it -> verify passes."""
    fake = _FakeSigNoz(rules=_prod_before_this_fix())
    module, _, Exit, errors = _install_real_alerting(monkeypatch, fake)

    with pytest.raises(Exit):
        module.verify_alert_routing(object())
    assert sum("[unbound]" in e for e in errors) >= len(_EXPECTED_RULES)
    assert any(
        "[stale]" in e and "FinanceReportReconciliationAnomaly" in e for e in errors
    )

    errors.clear()
    assert module.apply_alerts(object()) is True  # what the workflow runs on merge
    assert "old-stale" in fake.rules  # logged, not pruned
    with pytest.raises(Exit):
        module.verify_alert_routing(object())
    assert not any("[unbound]" in e for e in errors)
    (stale_line,) = [e for e in errors if "[stale]" in e]
    assert "FinanceReportReconciliationAnomaly" in stale_line
    assert any("apply-alerts --prune" in e for e in errors)

    assert module.apply_alerts(object(), prune=True) is True
    assert "FinanceReportReconciliationAnomaly" not in {
        r["alert"] for r in fake.rules.values()
    }
    assert module.verify_alert_routing(object()) is True


def test_verify_is_red_on_the_pre_fix_state_and_green_only_after_rebinding(
    monkeypatch,
) -> None:
    """The guard behind #973: if the apply bound the channel id again, verify -- run
    against what that apply stored -- would stay red. (Here the 'apply' is the real one;
    the mutation that makes it write ids is shown red by the end-to-end test above.)"""
    fake = _FakeSigNoz(
        rules=[r for r in _prod_before_this_fix() if r["id"] != "old-stale"]
    )
    module, _, Exit, _ = _install_real_alerting(monkeypatch, fake)

    with pytest.raises(Exit):
        module.verify_alert_routing(object())
    module.apply_alerts(object())

    assert module.verify_alert_routing(object()) is True


# ---- an apply that cannot render must not touch the live catalog -------------


class _BrokenDef:
    alert_name = "FinanceReportP95LatencyHigh"

    def __init__(self, how):
        self.how = how

    def to_signoz_payload(self, channel_names):
        if self.how == "raises":
            raise AlertingError("cannot render")
        payload = _real_definition(self.alert_name).to_signoz_payload(channel_names)
        payload["condition"]["thresholds"]["spec"][0]["channels"] = []  # forgot to bind
        return payload


@pytest.mark.parametrize("how", ["raises", "unbound"])
def test_apply_alerts_validates_every_rule_before_deleting_any(
    monkeypatch, how
) -> None:
    """A rule that cannot be rendered, or would end up unbound, stops the apply while
    the live catalog is whole: the earlier (valid) rule is not deleted and re-created
    first, which would leave a half-rebound catalog."""
    live = _prod_before_this_fix()
    fake = _FakeSigNoz(rules=live)
    module, _, Exit, errors = _install_real_alerting(monkeypatch, fake)
    monkeypatch.setattr(
        module, "load_alert_definitions", lambda: [_Def(), _BrokenDef(how)]
    )

    with pytest.raises(Exit) as exc:
        module.apply_alerts(object())

    assert exc.value.code == 1
    assert fake.writes() == []
    assert set(fake.rules) == {rule["id"] for rule in live}
    assert any("nothing was changed" in e for e in errors)


# ---- legacy dashboard rows: no usable top-level title -------------------------


@pytest.mark.parametrize("top_level", [{}, {"title": ""}, {"title": None}])
def test_legacy_dashboard_row_is_found_whether_its_title_is_absent_or_empty(
    top_level,
) -> None:
    """#934: prod's 23 rows carry no top-level `title` at all, `data.data.title` holds it
    and the wrapper has `version: v5`; a row with an empty or null title is the same
    legacy shape and must be found too (the old `"title" not in data` test missed it)."""
    row = {
        "id": "legacy",
        "data": {
            **top_level,
            "data": {"title": DASH_TITLE, "widgets": [{"id": "x"}]},
            "version": "v5",
        },
    }

    found = find_stored_dashboards({"status": "success", "data": [row]}, DASH_TITLE)

    assert [(d.id, d.legacy) for d in found] == [("legacy", True)]
