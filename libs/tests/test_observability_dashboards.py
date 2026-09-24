"""Offline tests for finance_report observability config-as-code (#373)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

from libs.observability_dashboards import (
    ALERT_RULES_FILE,
    DASHBOARD_FILE,
    ObservabilityDefinitionError,
    build_dashboard_import_payload,
    load_alert_definitions,
    load_dashboard,
    load_openpanel_analytics,
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
    def _agg(wid: str) -> tuple[str, str]:
        q = widgets[wid]["query"]["builder"]["queryData"][0]
        return q["aggregateOperator"], q["aggregateAttribute"]["key"]

    assert _agg("be-latency-p50") == ("p50", "durationNano")
    assert _agg("be-latency-p95") == ("p95", "durationNano")
    assert _agg("be-latency-p99") == ("p99", "durationNano")

    # fe-page-load is p75 of documentLoad span duration
    assert _agg("fe-page-load") == ("p75", "durationNano")
    page_load_filters = widgets["fe-page-load"]["query"]["builder"]["queryData"][0][
        "filters"
    ]["items"]
    assert any(
        f["key"]["key"] == "name" and f["value"] == "documentLoad"
        for f in page_load_filters
    )

    # the error-rate panel counts only SERVER spans (matches its description)
    err_filters = widgets["be-error-rate-5xx"]["query"]["builder"]["queryData"][0][
        "filters"
    ]["items"]
    assert any(
        f["key"]["key"] == "kind_string" and f["value"] == "Server" for f in err_filters
    )
    assert any(f["key"]["key"] == "hasError" for f in err_filters)


def test_every_widget_is_filterable_by_deployment_environment() -> None:
    """The deployment_environment dropdown only filters a panel if the panel's query
    references it — assert EVERY widget carries the `$deployment_environment` filter so
    the dashboard variable is real, not decorative (CR #391)."""
    for w in load_dashboard()["widgets"]:
        items = w["query"]["builder"]["queryData"][0]["filters"]["items"]
        assert any(
            f["key"]["key"] == "deployment.environment"
            and f["value"] == "$deployment_environment"
            for f in items
        ), f"widget {w['id']} is not filterable by deployment_environment"


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


def test_dashboard_import_payload_wraps_in_data_envelope() -> None:
    """#373: dashboard apply body matches the SigNoz /api/v1/dashboards envelope."""
    payload = build_dashboard_import_payload()
    assert set(payload.keys()) == {"data"}
    assert payload["data"]["title"] == load_dashboard()["title"]


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


def test_apply_alerts_raises_nonzero_exit_when_rule_create_fails(monkeypatch) -> None:
    """#1106 regression: SigNoz 400s must fail the GitHub apply workflow."""
    module, Exit = _load_fr_observability_tasks(monkeypatch)

    class Definition:
        alert_name = "FinanceReportHigh5xxRate"

        def to_signoz_payload(self, channel_ids):
            return {"alert": self.alert_name, "channels": channel_ids}

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
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [Definition()])

    with pytest.raises(Exit) as exc:
        module.apply_alerts(object())

    assert exc.value.code == 1
    assert calls == [
        ("GET", "/api/v1/rules", None),
        (
            "POST",
            "/api/v1/rules",
            {
                "alert": "FinanceReportHigh5xxRate",
                "channels": ["chan-1"],
                "labels": {"source": "infra2/finance_report-alerts"},
            },
        ),
    ]


class _Def:
    alert_name = "FinanceReportHigh5xxRate"

    def to_signoz_payload(self, channel_ids):
        return {"alert": self.alert_name, "channels": channel_ids}


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

    fake_console = types.ModuleType("libs.console")
    fake_console.error = lambda *_args, **_kwargs: None
    fake_console.success = lambda *_args, **_kwargs: None
    monkeypatch.setitem(
        sys.modules,
        "platform.12.alerting.shared",
        types.SimpleNamespace(_signoz_request=fake_request),
    )
    monkeypatch.setitem(sys.modules, "libs.console", fake_console)
    monkeypatch.setattr(module, "load_dashboard", lambda: {"title": "Dashboard"})

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
