"""Offline tests for finance_report observability config-as-code (#373)."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
import types
from pathlib import Path

import invoke  # noqa: F401
import pytest

from common.alerting import (
    AlertingError,
    build_signoz_channel_payload,
    signoz_feishu_channel_name,
)
from common.observability_dashboards import (
    ALERT_RULES_FILE,
    ALERT_RULES_FILES,
    DASHBOARD_FILE,
    DASHBOARD_FILES,
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
    north_star = spec["funnels"][1]
    assert north_star["name"] == "Upload → Review → Report (North Star)"
    assert north_star["steps"] == [
        "upload_started",
        "upload_succeeded",
        "review_approved",
        "report_generated",
    ]
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
    funnel_steps = {step for f in spec["funnels"] for step in f["steps"]}
    used = funnel_steps | set(spec["events_board"]["events"])
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
    "FinanceReportHigh5xxRateStable": (
        "promql_rule",
        "critical",
        0.05,
        "1",
        "5m0s",
        "%",
    ),
    "FinanceReportP95LatencyHigh": (
        "promql_rule",
        "error",
        3000.0,
        "2",
        "10m0s",
        "ms",
    ),
    "FinanceReportP95LatencyHighStable": (
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
    "TrueAlphaAppErrorLogs": ("threshold_rule", "error", 10, "4", "15m0s", ""),
    "TrueAlphaDataEngineErrorLogs": ("threshold_rule", "error", 10, "4", "15m0s", ""),
    "TrueAlphaHigh5xxRate": ("promql_rule", "critical", 0.05, "1", "5m0s", "%"),
    "TrueAlphaHigh5xxRateStable": (
        "promql_rule",
        "critical",
        0.05,
        "1",
        "5m0s",
        "%",
    ),
    "TrueAlphaP95LatencyHigh": (
        "promql_rule",
        "error",
        3000.0,
        "2",
        "10m0s",
        "ms",
    ),
    "TrueAlphaP95LatencyHighStable": (
        "promql_rule",
        "error",
        3000.0,
        "2",
        "10m0s",
        "ms",
    ),
    "TrueAlphaAppTelemetryAbsent": (
        "promql_rule",
        "error",
        0.0,
        "1",
        "5m0s",
        "",
    ),
    "PlatformAlertingErrorLogs": ("threshold_rule", "error", 5, "4", "15m0s", ""),
    "PlatformTodoErrorLogs": ("threshold_rule", "error", 10, "4", "15m0s", ""),
    "PlatformTodoProbeFailures": ("promql_rule", "error", 0.0, "1", "5m0s", "count"),
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


# What the services export under the names SigNoz stores with
# DOT_METRICS_ENABLED: OTel instrument names as-is, histogram buckets as
# "<name>.bucket". Label values are listed where a rule filters on them.
_APP_METRICS: dict[str, dict[str, set[str]]] = {
    "http.server.request.count": {
        "http.response.status_code_class": {"1xx", "2xx", "3xx", "4xx", "5xx"},
        "http.route": {"/health", "/ping", "/ping/toggle", "/api/statements"},
    },
    # One name, two producers (infra2#1465). The custom histogram of finance_report is in
    # milliseconds and carries `http.response.status_code_class`; the stable histogram of
    # FastAPI (>= 0.162) is in seconds and carries `http.response.status_code` and no class.
    "http.server.request.duration.bucket": {
        "http.response.status_code_class": {"1xx", "2xx", "3xx", "4xx", "5xx"},
        "http.response.status_code": {"200", "307", "404", "500", "502", "503"},
    },
    "http.server.request.duration.count": {
        "http.response.status_code": {"200", "307", "404", "500", "502", "503"},
        "http.route": {"/health", "/livez", "/readyz", "/mcp", "/api/statements"},
    },
    "http.server.duration.count": {
        "http.status_code": {"200", "500", "502", "503"},
        "http.target": {"/health", "/api/v1/system/health", "/api/v1/trade"},
    },
    "http.server.duration.bucket": {},
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
    "canary.probe.outcomes": {"canary.status": {"pass", "fail", "unconfigured"}},
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
            services = [
                val for key, op, val in matchers if key == "service.name" and op == "="
            ]
            pattern = [
                val for key, op, val in matchers if key == "service.name" and op == "=~"
            ]
            if pattern:
                # #1431: the parse metrics come from the backend and the worker process.
                assert services == [], alert_name
                assert len(pattern) == 1, alert_name
                matched = {
                    name
                    for name in ("finance-report-backend", "finance-report-worker")
                    if re.fullmatch(pattern[0], name)
                }
                assert matched == {"finance-report-backend", "finance-report-worker"}
                assert not re.fullmatch(pattern[0], "finance-report-frontend")
            else:
                assert services in [
                    ["finance-report-backend"],
                    ["truealpha-app"],
                    ["platform-todo"],
                ], (
                    alert_name,
                    services,
                )
            assert ("deployment.environment.name", "=", "production") in matchers
            value_filters += [
                (alert_name, metric, label, value)
                for label, op, value in matchers
                if op == "=~" and label != "service.name"
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


@pytest.mark.parametrize(
    ("rule", "five_xx_matcher"),
    [
        ("FinanceReportHigh5xxRate", ("http.response.status_code_class", "=", "5xx")),
        (
            "FinanceReportHigh5xxRateStable",
            ("http.response.status_code", "=~", "^5..$"),
        ),
    ],
)
def test_5xx_rule_needs_a_minimum_5xx_count_and_ignores_probe_traffic(
    rule: str, five_xx_matcher: tuple[str, str, str]
) -> None:
    """#906: the old ratio divided by clamp_min(total_rate, 1) and required every point
    of the window, so a low-traffic outage never fired; healthy /health probes also
    diluted a user-facing outage below 5%. Every selector now excludes the probe
    routes, the ratio is not clamped, and at least 5 5xx must exist in the window."""
    promql = _promql_rules()[rule]
    selectors = _selectors(promql)
    ratio, guard = promql.split(" and on() ", 1)

    assert "clamp_min" not in promql
    assert re.fullmatch(r"\(sum\(increase\(\{[^{}]*\}\[5m\]\)\) >= 5\)", guard)
    assert re.fullmatch(r"\(sum\(increase\(.*\)\) / sum\(increase\(.*\)\)\)", ratio)
    assert len(selectors) == 3
    for _metric, matchers in selectors:
        assert _PROBE_ROUTE_EXCLUSION in matchers
    five_xx = [m for _metric, m in selectors if five_xx_matcher in m]
    if rule.endswith("Stable"):
        # `.count` of the stable histogram has the name of finance_report's custom
        # histogram too (#1465): every selector, the denominator included, must require
        # the label that only the stable series carry.
        assert all(
            any(label == "http.response.status_code" for label, _op, _v in matchers)
            for _metric, matchers in selectors
        )
    assert len(five_xx) == 2  # the ratio's numerator and the count guard


@pytest.mark.parametrize(
    ("rule", "metric", "scale"),
    [
        ("FinanceReportP95LatencyHigh", "http.server.request.duration.bucket", ""),
        (
            "FinanceReportP95LatencyHighStable",
            "http.server.request.duration.bucket",
            " \\* 1000",
        ),
    ],
)
def test_p95_rule_pages_only_on_a_sustained_non_probe_breach(
    rule: str, metric: str, scale: str
) -> None:
    """#906 live check (2026-09-24, 24 h): with /health included the p95 was mostly the
    health endpoint's own latency (p50 1412 ms, max 10 s), and 1500 ms / at_least_once
    would have fired in 197 five-minute windows a day. The rule excludes the probe
    routes and must hold above 3000 ms at every point of a 10 minute window.

    SigNoz drops NaN points before all_times, and the p95 is NaN in any minute with no
    non-probe request, so a lone slow request would pass "all remaining points above".
    `>= 0` drops the NaN and `or on() vector(0)` puts a 0 in its place, so a quiet
    minute breaks the streak instead of vanishing from it."""
    payload = _rendered_rules()[rule]
    promql = payload["condition"]["compositeQuery"]["queries"][0]["spec"]["query"]
    (threshold,) = payload["condition"]["thresholds"]["spec"]
    selectors = _selectors(promql)

    assert threshold["matchType"] == "2"  # all_times
    assert threshold["target"] == 3000.0
    assert payload["evaluation"]["spec"]["evalWindow"] == "10m0s"
    assert [name for name, _ in selectors] == [metric]
    assert _PROBE_ROUTE_EXCLUSION in selectors[0][1]
    assert re.fullmatch(
        r"\(histogram_quantile\(0\.95, sum by \(le\) \(rate\(\{[^{}]*\}\[5m\]\)\)\)"
        + scale
        + r" >= 0\)"
        r" or on\(\) vector\(0\)",
        promql,
    )


def _matchers(rule: str) -> list[tuple[str, str, str]]:
    return [
        m for _metric, matchers in _selectors(_promql_rules()[rule]) for m in matchers
    ]


def test_finance_report_p95_rules_cannot_read_each_others_series() -> None:
    """#1465: the custom histogram of finance_report (milliseconds) and the stable one of
    FastAPI (seconds) have the same metric name. They differ by label: only the custom one
    carries `http.response.status_code_class`; only the stable one carries
    `http.response.status_code`. Each rule must require its own label, or a rolling deploy
    that runs both builds mixes two units and two bucket sets in one quantile."""
    legacy = _matchers("FinanceReportP95LatencyHigh")
    stable = _matchers("FinanceReportP95LatencyHighStable")

    assert ("http.response.status_code_class", "=~", "^.+$") in legacy
    assert not [m for m in legacy if m[0] == "http.response.status_code"]
    assert ("http.response.status_code", "=~", "^.+$") in stable
    assert not [m for m in stable if m[0] == "http.response.status_code_class"]


@pytest.mark.parametrize(
    ("legacy", "stable"),
    [
        ("FinanceReportP95LatencyHigh", "FinanceReportP95LatencyHighStable"),
        ("TrueAlphaP95LatencyHigh", "TrueAlphaP95LatencyHighStable"),
    ],
)
def test_the_stable_latency_rule_scales_seconds_to_the_millisecond_threshold(
    legacy: str, stable: str
) -> None:
    """#1465: the stable histogram is in seconds; the threshold is 3000 ms. Without the
    factor the rule compares 4 (seconds) with 3000 and never fires."""
    rules = _promql_rules()
    payloads = _rendered_rules()

    assert " * 1000 >= 0)" in rules[stable]
    assert "* 1000" not in rules[legacy]
    for name in (legacy, stable):
        (threshold,) = payloads[name]["condition"]["thresholds"]["spec"]
        assert threshold["target"] == 3000.0
        assert threshold["targetUnit"] == "ms"


def test_truealpha_stable_rules_read_only_the_stable_http_metric() -> None:
    """#1465: the legacy rules read `http.server.duration` (contrib instrumentor, ms,
    `http.target`, `http.status_code`); the stable rules read FastAPI's
    `http.server.request.duration` with `http.route` and `http.response.status_code`."""
    legacy_labels = {"http.target", "http.status_code"}
    for rule, metric in (
        ("TrueAlphaHigh5xxRateStable", "http.server.request.duration.count"),
        ("TrueAlphaP95LatencyHighStable", "http.server.request.duration.bucket"),
    ):
        selectors = _selectors(_promql_rules()[rule])
        assert selectors and {name for name, _ in selectors} == {metric}, rule
        assert not legacy_labels & {
            label for _n, ms in selectors for label, _o, _v in ms
        }
        assert ("deployment.environment.name", "=", "production") in selectors[0][1]
    assert ("http.response.status_code", "=~", "^5..$") in _matchers(
        "TrueAlphaHigh5xxRateStable"
    )
    for rule in ("TrueAlphaHigh5xxRate", "TrueAlphaP95LatencyHigh"):
        assert {name for name, _ in _selectors(_promql_rules()[rule])} <= {
            "http.server.duration.count",
            "http.server.duration.bucket",
        }


def test_backend_telemetry_absence_is_expressed_in_promql() -> None:
    """#906: alertOnAbsent is a no-op on SigNoz PromQL rules, so rules that
    must fire on missing data say so in the query."""
    absent = {
        name: promql
        for name, promql in _promql_rules().items()
        if "absent_over_time(" in promql
    }

    assert set(absent) == {
        "FinanceReportBackendTelemetryAbsent",
        "TrueAlphaAppTelemetryAbsent",
    }
    fr_promql = absent["FinanceReportBackendTelemetryAbsent"]
    assert fr_promql.startswith("absent_over_time(") and fr_promql.endswith("[10m])")
    assert [metric for metric, _ in _selectors(fr_promql)] == [
        "http.server.request.count"
    ]

    ta_promql = absent["TrueAlphaAppTelemetryAbsent"]
    assert ta_promql.startswith("absent_over_time(") and ta_promql.endswith("[15m])")
    assert [metric for metric, _ in _selectors(ta_promql)] == [
        "http.server.duration.count"
    ]


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

    # #1307, #1481: AI/OCR and core business telemetry widgets are present and query
    # metrics in SigNoz v5 builder format (metricName + spaceAggregation/timeAggregation).
    for wid in (
        "ai-provider-latency-p95",
        "statement-parse-outcomes",
        "reconciliation-match-outcomes",
        "financial-invariant-violations",
    ):
        assert wid in widgets, f"missing AI/business telemetry widget {wid}"
        qdata = widgets[wid]["query"]["builder"]["queryData"][0]
        assert qdata["dataSource"] == "metrics"

    def _metric_agg(wid: str) -> dict:
        (aggregation,) = widgets[wid]["query"]["builder"]["queryData"][0][
            "aggregations"
        ]
        return aggregation

    assert _metric_agg("ai-provider-latency-p95") == {
        "metricName": "finance.ai_provider.latency.bucket",
        "spaceAggregation": "p95",
    }
    assert _metric_agg("statement-parse-outcomes") == {
        "metricName": "finance.statement_parse.outcome",
        "timeAggregation": "increase",
        "spaceAggregation": "sum",
    }
    assert _metric_agg("reconciliation-match-outcomes") == {
        "metricName": "finance.reconciliation.match.outcome",
        "timeAggregation": "increase",
        "spaceAggregation": "sum",
    }
    assert _metric_agg("financial-invariant-violations") == {
        "metricName": "finance.invariant.violation",
        "timeAggregation": "increase",
        "spaceAggregation": "sum",
    }


def _filter(widget_id: str, widgets: dict[str, dict]) -> str:
    return widgets[widget_id]["query"]["builder"]["queryData"][0]["filter"][
        "expression"
    ]


# The legacy resource key, not followed by `.name`. SSOT core.environments §4.5: queries
# use `deployment.environment.name` (#1434). `$deployment_environment` has an underscore.
LEGACY_ENVIRONMENT_KEY = re.compile(r"deployment\.environment(?!\.name)")


ENVIRONMENT_FILTER = "deployment.environment.name IN $deployment_environment"
_SERVICE_FILTER = re.compile(r"service\.name\s*(?:=\s*'([^']+)'|IN\s*\[([^\]]*)\])")
# Every service a dashboard may query, with the metrics it exports (a service.name typo
# or a metric the service lacks draws an empty panel). truealpha-* and platform-* were read from SigNoz on
# 2026-11-10 (read-only): truealpha-app uses the old HTTP semantic convention, the
# platform services the new one, truealpha-data-engine exports logs and traces only.
# finance-report-* are the names its dashboard already queried (#1307).
_OLD_HTTP = {"http.server.duration.bucket", "http.server.duration.count"}
_NEW_HTTP = {
    "http.server.request.duration.bucket",
    "http.server.request.duration.count",
}
SERVICE_METRICS = {
    "truealpha-app": _OLD_HTTP | {"http.server.active_requests"},
    "truealpha-data-engine": set(),
    "platform-alerting": _NEW_HTTP | {"http.server.active_requests"},
    "platform-todo": _NEW_HTTP
    | {
        "http.server.active_requests",
        "canary.probe.duration_ms.bucket",
        "canary.probe.outcomes",
    },
    "finance-report-backend": {
        "finance.ai_provider.latency.bucket",
        "finance.statement_parse.outcome",
        "finance.reconciliation.match.outcome",
        "finance.invariant.violation",
    },
    "finance-report-worker": {
        "finance.ai_provider.latency.bucket",
        "finance.statement_parse.outcome",
    },
    "finance-report-frontend": set(),
}
all_dashboards = pytest.mark.parametrize(
    "path", DASHBOARD_FILES, ids=lambda p: p.relative_to(ROOT).as_posix()
)


def _queries(dashboard: dict):
    for widget in dashboard["widgets"]:
        for query in widget["query"]["builder"]["queryData"]:
            yield widget["id"], query


def _services(expression: str) -> set[str]:
    return {
        name
        for one, many in _SERVICE_FILTER.findall(expression)
        for name in ([one] if one else re.findall(r"'([^']+)'", many))
    }


def _metric_names(query: dict) -> list[str]:
    """`metricName` (SigNoz v5 metrics aggregation) or the `p95(name)` expression form."""
    return [
        a.get("metricName") or re.fullmatch(r"\w+\((.+)\)", a["expression"]).group(1)
        for a in query["aggregations"]
    ]


def _environment_problems(dashboard: dict) -> list[str]:
    problems = [
        f"widget {wid} lacks {ENVIRONMENT_FILTER}"
        for wid, query in _queries(dashboard)
        if ENVIRONMENT_FILTER not in query["filter"]["expression"]
    ]
    if LEGACY_ENVIRONMENT_KEY.search(json.dumps(dashboard)):
        problems.append("legacy key deployment.environment (no .name)")
    return problems


def _service_problems(dashboard: dict) -> list[str]:
    problems = []
    for wid, query in _queries(dashboard):
        services = _services(query["filter"]["expression"])
        if not services or not services <= SERVICE_METRICS.keys():
            problems.append(f"widget {wid} queries unknown services {sorted(services)}")
            continue
        exported = set().union(*(SERVICE_METRICS[s] for s in services))
        names = _metric_names(query) if query["dataSource"] == "metrics" else []
        problems += [
            f"widget {wid}: metric {name} is not exported by {sorted(services)}"
            for name in names
            if name not in exported
        ]
    return problems


def test_dashboard_files_list_every_dashboard_json_and_titles_differ() -> None:
    """apply-dashboard upserts by title: a file outside DASHBOARD_FILES is never applied,
    and two files with one title would overwrite each other."""
    on_disk = {
        found
        for layer in ("finance_report", "truealpha", "platform")
        for found in (ROOT / layer).rglob("dashboard.json")
    }
    assert on_disk == set(DASHBOARD_FILES)
    titles = [load_dashboard(path)["title"] for path in DASHBOARD_FILES]
    assert len(set(titles)) == len(titles)


@all_dashboards
def test_every_widget_is_filterable_by_deployment_environment(path) -> None:
    """The deployment_environment dropdown only filters a panel if the panel's query
    references it — assert EVERY query of EVERY dashboard filters on
    `deployment.environment.name` and no key is the legacy one, so the variable is real,
    not decorative (CR #391, #1434)."""
    assert _environment_problems(load_dashboard(path)) == []


@all_dashboards
def test_every_widget_names_its_service_and_a_metric_it_exports(path) -> None:
    """A service.name typo or a metric the service does not export draws an empty panel
    that reads as a healthy service (GREEN-WHILE-EMPTY)."""
    assert _service_problems(load_dashboard(path)) == []


@all_dashboards
def test_the_dashboard_checks_fail_on_the_change_each_one_guards(path) -> None:
    def first(dashboard: dict) -> dict:
        return dashboard["widgets"][0]["query"]["builder"]["queryData"][0]

    legacy = load_dashboard(path)
    expression = first(legacy)["filter"]["expression"]
    first(legacy)["filter"]["expression"] = expression.replace(
        "deployment.environment.name IN", "deployment.environment IN"
    )
    problems = _environment_problems(legacy)
    assert any(legacy["widgets"][0]["id"] in p for p in problems)
    assert any("legacy key" in p for p in problems)

    typo = load_dashboard(path)
    first(typo)["filter"]["expression"] = re.sub(
        r"service\.name (=|IN) ('[^']+'|\[[^\]]*\])", "service.name = 'x'", expression
    )
    assert any("unknown services ['x']" in p for p in _service_problems(typo))

    metric = load_dashboard(path)
    queries = [q for _, q in _queries(metric) if q["dataSource"] == "metrics"]
    if queries:  # the finance_report logs/traces panels carry no metric
        queries[0]["aggregations"] = [{"metricName": "not.an.exported.metric"}]
        assert any("not.an.exported" in p for p in _service_problems(metric))


@all_dashboards
def test_env_variable_query_targets_the_live_v2_logs_schema(path) -> None:
    """The deployment.environment.name dropdown must query the table/columns that actually
    exist on the SigNoz instance (logs v2: distributed_logs_v2 + resources_string),
    not the retired v1 schema (distributed_logs + stringTagMap) which UNKNOWN_TABLEs."""
    variable = load_dashboard(path)["variables"]["deployment_environment"]
    query = variable["queryValue"]
    assert "distributed_logs_v2" in query
    assert "resources_string['deployment.environment.name']" in query
    assert not LEGACY_ENVIRONMENT_KEY.search(json.dumps(variable))
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
            if query["dataSource"] == "metrics":
                assert query["aggregations"] and all(
                    a.get("metricName") and a.get("spaceAggregation")
                    for a in query["aggregations"]
                )
            else:
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
        (
            lambda d: next(
                w for w in d["widgets"] if w["id"] == "ai-provider-latency-p95"
            )["query"]["builder"]["queryData"][0].update(
                aggregations=[{"expression": "p95(finance.ai_provider.latency)"}]
            ),
            "metricName",
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


def _load_fr_observability_tasks(monkeypatch, all_dashboards=False):
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
    if not all_dashboards:  # the apply tests below drive the one finance_report file
        module.DASHBOARD_FILES = (DASHBOARD_FILE,)
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

    # The real module supplies the retry contract (transient statuses, delays, wait).
    alerting = _load_real_alerting()
    waits: list[float] = []
    monkeypatch.setattr(alerting, "_ensure_signoz_channel", lambda _c: "chan-1")
    monkeypatch.setattr(alerting, "_signoz_request", fake_request)
    monkeypatch.setattr(alerting, "_sleep", waits.append)
    fake_console = types.ModuleType("common.console")
    fake_console.error = lambda *_args, **_kwargs: None
    fake_console.success = lambda *_args, **_kwargs: None
    fake_console.info = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "platform.12.alerting.shared", alerting)
    monkeypatch.setitem(sys.modules, "common.console", fake_console)
    monkeypatch.setattr(module, "load_alert_definitions", lambda: [_Def()])

    with pytest.raises(Exit) as exc:
        module.apply_alerts(object())

    assert exc.value.code == 1
    assert waits == []  # a 400 is not transient: one POST, no wait
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/v1/rules"),
        ("POST", "/api/v1/rules"),
    ]
    posted = calls[1][2]
    assert posted["alert"] == "FinanceReportHigh5xxRate"
    assert posted["labels"]["source"] == "infra2/finance_report-alerts"
    assert posted["condition"]["thresholds"]["spec"][0]["channels"] == ["chan-1"]


def _patch_console(monkeypatch, **overrides):
    fake = types.ModuleType("common.console")
    for name in ("error", "success", "info", "warning"):
        setattr(fake, name, overrides.get(name, lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "common.console", fake)
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

    real = _load_real_alerting()  # the retry and deadline contract come from the module
    return types.SimpleNamespace(
        _ensure_signoz_channel=lambda _c: "chan-1",
        _signoz_request=fake_request,
        _monotonic=real._monotonic,
        _APPLY_ALERTS_DEADLINE_SECONDS=real._APPLY_ALERTS_DEADLINE_SECONDS,
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


def test_delete_rule_falls_through_to_v2_when_v1_leaves_the_rule(monkeypatch) -> None:
    """#1435: _delete_rule tries the v2 path when the v1 DELETE leaves the rule listed."""
    module = _load_signoz_alert_probe()
    listed_rules = [{"id": "test-rule-id", "alert": "Canary"}]
    calls = []

    def fake_request(_base_url, _api_key, *, method, path, payload=None):
        calls.append((method, path))
        if method == "DELETE" and path == "/api/v1/rules/test-rule-id":
            return {"ok": True, "status": 200, "body": "{}", "data": {}}
        if method == "DELETE" and path == "/api/v2/rules/test-rule-id":
            listed_rules.clear()
            return {"ok": True, "status": 200, "body": "{}", "data": {}}
        if (method, path) == ("GET", "/api/v1/rules"):
            data = {"status": "success", "data": {"rules": list(listed_rules)}}
            return {"ok": True, "status": 200, "body": "{}", "data": data}
        if (method, path) == ("GET", "/api/v1/route_policies"):
            data = {"status": "success", "data": []}
            return {"ok": True, "status": 200, "body": "{}", "data": data}
        return {"ok": False, "status": 500, "body": "{}", "data": None}

    monkeypatch.setattr(module, "_signoz_request", fake_request)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    problem = module._delete_rule(
        "https://signoz.example", "key", "test-rule-id", "Canary"
    )

    assert problem is None
    assert calls.index(("DELETE", "/api/v1/rules/test-rule-id")) < calls.index(
        ("DELETE", "/api/v2/rules/test-rule-id")
    )


def test_delete_rule_accepts_a_rule_that_is_already_gone(monkeypatch) -> None:
    """#1435: DELETE answers 404 for an absent rule. The list read-back confirms it."""
    module = _load_signoz_alert_probe()

    def fake_request(_base_url, _api_key, *, method, path, payload=None):
        if method == "DELETE":
            return {"ok": False, "status": 404, "body": "{}", "data": None}
        data = {"status": "success", "data": {"rules": []}}
        if path == "/api/v1/route_policies":
            data = {"status": "success", "data": []}
        return {"ok": True, "status": 200, "body": "{}", "data": data}

    monkeypatch.setattr(module, "_signoz_request", fake_request)

    assert (
        module._delete_rule("https://signoz.example", "key", "test-rule-id", "Canary")
        is None
    )


def test_delete_rule_rejects_a_404_delete_while_the_rule_is_still_listed(
    monkeypatch,
) -> None:
    """#1435: a DELETE status is no proof. A listed rule stays a failure."""
    module = _load_signoz_alert_probe()

    def fake_request(_base_url, _api_key, *, method, path, payload=None):
        if method == "DELETE":
            return {"ok": False, "status": 404, "body": "{}", "data": None}
        rules = [{"id": "test-rule-id", "alert": "Canary"}]
        data = {"status": "success", "data": {"rules": rules}}
        return {"ok": True, "status": 200, "body": "{}", "data": data}

    monkeypatch.setattr(module, "_signoz_request", fake_request)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    problem = module._delete_rule(
        "https://signoz.example", "key", "test-rule-id", "Canary"
    )

    assert problem is not None
    assert "test-rule-id" in problem
    assert "Canary" in problem
    assert "status=404" in problem


# ---------------------------------------------------------------------------
# #1435: the canary must read the rule back after DELETE


def _load_signoz_alert_probe():
    spec = importlib.util.spec_from_file_location(
        "signoz_alert_rule_probe_under_test", SIGNOZ_ALERT_PROBE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CANARY_ENV = {"SIGNOZ_API_KEY": "key", "GITHUB_RUN_ID": "1435", "ENV": "production"}
_CANARY_RULE_NAME = "CanarySigNozPromqlPayload-1435"
_CANARY_RULE_ID = "0199aaaa-1435-7000-8000-000000000001"
_CANARY_POLICY_ID = "0199aaaa-1435-7000-8000-0000000000a1"


class _FakeSigNozV01051:
    """In-memory model of the SigNoz v0.105.1 API that the canary probe calls.

    Facts taken from the SigNoz v0.105.1 source:
    - ``DELETE /api/v1/rules/{id}`` is the only rule DELETE route. There is no
      ``/api/v2/rules/{id}`` route, so the web catch-all answers a DELETE there with
      ``200`` and the HTML of ``index.html`` and changes nothing.
    - ``POST /api/v1/rules`` stores a route policy whose ``name`` is the rule id.
    - ``DELETE /api/v1/rules/{id}`` removes the rule and its route policies together.
    - ``GET /api/v1/rules`` lists ``{"data": {"rules": [...]}}``;
      ``GET /api/v1/route_policies`` lists ``{"data": [...]}``.

    ``v1_delete`` selects what the v1 DELETE does:
    ``removes`` (rule and policy), ``ignores`` (answers OK, changes nothing) or
    ``keeps_policy`` (removes the rule only). The first ``ignored_deletes`` v1 DELETE
    calls answer OK and change nothing, whatever ``v1_delete`` says.
    """

    def __init__(
        self,
        *,
        v1_delete: str = "removes",
        ignored_deletes: int = 0,
        rules_unreadable_after_delete: bool = False,
        policies_unreadable: bool = False,
        stores_legacy_rule_shape: bool = False,
        html_list: str | None = None,
    ) -> None:
        self.v1_delete = v1_delete
        self.ignored_deletes = ignored_deletes
        self.rules_unreadable_after_delete = rules_unreadable_after_delete
        self.policies_unreadable = policies_unreadable
        self.stores_legacy_rule_shape = stores_legacy_rule_shape
        self.html_list = html_list
        self.rules: dict[str, dict] = {}
        self.policies: dict[str, dict] = {}
        self.v1_delete_calls = 0
        self.v2_delete_calls = 0
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _reply(status: int, data=None, body: str | None = None) -> dict:
        text = body if body is not None else json.dumps(data)
        return {
            "ok": 200 <= status < 300,
            "status": status,
            "body": text,
            "data": data,
        }

    def request(self, _base_url, _api_key, *, method, path, payload=None):
        self.calls.append((method, path))
        if (method, path) == ("GET", "/api/v1/channels"):
            channel = {"id": "chan-id", "name": "infra2-feishu-alerts-production"}
            return self._reply(200, {"status": "success", "data": [channel]})
        if (method, path) == ("POST", "/api/v1/rules"):
            rule = json.loads(json.dumps(payload))
            if self.stores_legacy_rule_shape:
                rule["condition"]["compositeQuery"].pop("queries", None)
            rule["id"] = _CANARY_RULE_ID
            self.rules[_CANARY_RULE_ID] = rule
            self.policies[_CANARY_POLICY_ID] = {
                "id": _CANARY_POLICY_ID,
                "name": _CANARY_RULE_ID,
                "expression": f'threshold.name == "x" && ruleId == "{_CANARY_RULE_ID}"',
            }
            created = {"id": _CANARY_RULE_ID, "alert": rule["alert"]}
            return self._reply(200, {"status": "success", "data": created})
        if (method, path) == ("GET", "/api/v1/rules"):
            if self.rules_unreadable_after_delete and self.v1_delete_calls:
                return self._reply(500, {"status": "error"}, body="database locked")
            if self.html_list == "rules" and self.v1_delete_calls:
                return self._reply(200, None, body="<!doctype html><html></html>")
            listing = {"rules": list(self.rules.values())}
            return self._reply(200, {"status": "success", "data": listing})
        if (method, path) == ("GET", "/api/v1/route_policies"):
            if self.policies_unreadable:
                return self._reply(500, {"status": "error"}, body="database locked")
            if self.html_list == "policies":
                return self._reply(200, None, body="<!doctype html><html></html>")
            data = {"status": "success", "data": list(self.policies.values())}
            return self._reply(200, data)
        if method == "DELETE" and path.startswith("/api/v2/rules/"):
            self.v2_delete_calls += 1
            return self._reply(200, None, body="<!doctype html><html></html>")
        if method == "DELETE" and path.startswith("/api/v1/rules/"):
            rule_id = path.rsplit("/", 1)[1]
            self.v1_delete_calls += 1
            if rule_id not in self.rules:
                return self._reply(404, {"status": "error", "errorType": "not_found"})
            ignored = self.v1_delete == "ignores" or (
                self.v1_delete_calls <= self.ignored_deletes
            )
            if not ignored:
                del self.rules[rule_id]
                if self.v1_delete == "removes":
                    self.policies = {
                        key: value
                        for key, value in self.policies.items()
                        if value["name"] != rule_id
                    }
            return self._reply(200, {"status": "success", "data": "rule deleted"})
        return self._reply(404, None, body="not found")


def _run_canary(monkeypatch, capsys, fake: _FakeSigNozV01051) -> tuple[int, str, str]:
    module = _load_signoz_alert_probe()
    monkeypatch.setattr(module, "_signoz_request", fake.request)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    code = module.main(dict(_CANARY_ENV))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_canary_passes_when_signoz_deletes_the_rule_and_its_route_policy(
    monkeypatch, capsys
) -> None:
    """#1435: a server that deletes for real passes, and nothing stays behind."""
    fake = _FakeSigNozV01051(v1_delete="removes")

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 0, err
    assert f"SigNoz alert canary passed: {_CANARY_RULE_NAME}" in out
    assert fake.rules == {}
    assert fake.policies == {}


def test_canary_fails_when_delete_answers_ok_but_the_rule_stays(
    monkeypatch, capsys
) -> None:
    """#1435: production answered OK to DELETE and kept the rule. That must fail."""
    fake = _FakeSigNozV01051(v1_delete="ignores")

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 1
    assert "passed" not in out
    assert _CANARY_RULE_ID in err
    assert _CANARY_RULE_NAME in err
    assert 2 <= fake.v1_delete_calls <= 5  # retried, and the retry is bounded
    assert _CANARY_RULE_ID in fake.rules  # the rule really is still there


def test_canary_retries_delete_until_the_rule_is_gone(monkeypatch, capsys) -> None:
    """#1435: a DELETE that is ignored once is retried, then verified by read-back."""
    fake = _FakeSigNozV01051(v1_delete="removes", ignored_deletes=1)

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 0, err
    assert "SigNoz alert canary passed" in out
    assert fake.v1_delete_calls == 2
    assert fake.rules == {}
    assert fake.policies == {}


def test_canary_fails_when_the_rule_list_cannot_be_read_after_delete(
    monkeypatch, capsys
) -> None:
    """#1435: a read error after DELETE is a failure. It must not count as gone."""
    fake = _FakeSigNozV01051(v1_delete="removes", rules_unreadable_after_delete=True)

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 1
    assert "passed" not in out
    assert _CANARY_RULE_ID in err
    assert _CANARY_RULE_NAME in err


def test_canary_fails_when_the_route_policy_survives_the_rule_delete(
    monkeypatch, capsys
) -> None:
    """#1435: the rule is gone but its route policy is not. That must fail by id."""
    fake = _FakeSigNozV01051(v1_delete="keeps_policy")

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 1
    assert "passed" not in out
    assert _CANARY_POLICY_ID in err
    assert _CANARY_RULE_ID in err
    assert fake.rules == {}
    assert _CANARY_POLICY_ID in fake.policies


def test_canary_fails_when_route_policies_cannot_be_read(monkeypatch, capsys) -> None:
    """#1435: a read error on the route policy list is a failure, not a pass."""
    fake = _FakeSigNozV01051(v1_delete="removes", policies_unreadable=True)

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 1
    assert "passed" not in out
    assert _CANARY_RULE_ID in err
    assert "route polic" in err


@pytest.mark.parametrize("html_list", ["rules", "policies"])
def test_canary_fails_when_a_list_read_answers_ok_without_a_list(
    monkeypatch, capsys, html_list: str
) -> None:
    """#1435: a 200 that is not a list (for example the web page) is not "empty"."""
    fake = _FakeSigNozV01051(v1_delete="removes", html_list=html_list)

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 1
    assert "passed" not in out
    assert _CANARY_RULE_ID in err
    assert "not a list" in err


def test_canary_names_the_leftover_rule_when_the_v5_check_fails(
    monkeypatch, capsys
) -> None:
    """#1435: the v5 failure path also reports a cleanup that did not happen."""
    fake = _FakeSigNozV01051(v1_delete="ignores", stores_legacy_rule_shape=True)

    code, out, err = _run_canary(monkeypatch, capsys, fake)

    assert code == 1
    assert "passed" not in out
    assert "v5 PromQL" in err
    assert "NOT removed" in err
    assert _CANARY_RULE_ID in err


def test_routing_check_flags_stale_canary_probe_rules() -> None:
    """Flag lingering canary probe rules as stale when not in expected catalog."""
    canary_by_label = {
        "id": "canary-1",
        "alert": "UnknownCanaryAlert",
        "labels": {"canary": "true"},
    }
    canary_by_prefix = {
        "id": "canary-2",
        "alert": "CanarySigNozPromqlPayload-12345",
        "labels": {},
    }
    problems = check_alert_routing(
        {"data": {"rules": [canary_by_label, canary_by_prefix]}},
        {"data": [{"id": "c1", "name": "chan-1"}]},
        channel_name="chan-1",
        expected_alerts=[],
    )
    by_subject = {p.subject: p for p in problems if p.kind == "stale"}
    assert "UnknownCanaryAlert" in by_subject
    assert "CanarySigNozPromqlPayload-12345" in by_subject
    assert "stale canary probe rule" in by_subject["UnknownCanaryAlert"].detail
    assert (
        "stale canary probe rule"
        in by_subject["CanarySigNozPromqlPayload-12345"].detail
    )


def test_apply_observability_workflow_exposes_canary_mode() -> None:
    """#1106 regression: CI can prove alert payloads before real catalog apply."""
    workflow = APPLY_OBSERVABILITY_WORKFLOW.read_text(encoding="utf-8")

    assert "mode:" in workflow
    assert "- canary" in workflow
    assert "tools/signoz_alert_rule_probe.py" in workflow
    assert "inputs.mode == 'canary'" in workflow
    assert "inputs.mode == 'apply'" in workflow


def _apply_observability_document() -> dict:
    """Return the parsed apply-observability.yml."""
    import yaml

    return yaml.safe_load(APPLY_OBSERVABILITY_WORKFLOW.read_text(encoding="utf-8"))


def _apply_observability_triggers() -> dict:
    """Return the `on` block. PyYAML reads the key `on` as the boolean True."""
    document = _apply_observability_document()
    return document["on"] if "on" in document else document[True]


def _apply_observability_job() -> dict:
    """Return the parsed `apply` job of apply-observability.yml."""
    return _apply_observability_document()["jobs"]["apply"]


def _apply_observability_step(name: str) -> dict:
    """Return the one step of the `apply` job with this exact name."""
    matches = [s for s in _apply_observability_job()["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def test_apply_observability_mode_input_options_are_exact() -> None:
    """#1435: the mode input offers apply, canary and prune-alerts, in this order."""
    mode_input = _apply_observability_triggers()["workflow_dispatch"]["inputs"]["mode"]

    assert mode_input["options"] == ["apply", "canary", "prune-alerts"]
    assert mode_input["default"] == "apply"


def test_apply_observability_triggers_permissions_and_concurrency_are_exact() -> None:
    """#1435: the new mode adds no trigger, no write permission, no queue change."""
    document = _apply_observability_document()

    assert set(_apply_observability_triggers()) == {"workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"] == {
        "group": "apply-observability",
        "cancel-in-progress": False,
    }


def test_apply_observability_step_names_are_exact_and_in_order() -> None:
    """#1435: an added or removed step changes this list."""
    names = [step["name"] for step in _apply_observability_job()["steps"]]

    assert names == [
        "Checkout",
        "Set up Python",
        "Install uv",
        "Preflight — SIGNOZ_API_KEY present",
        "Canary — SigNoz alert rule payload",
        "Apply alert rules (idempotent)",
        "Apply dashboard (idempotent)",
    ]


def test_apply_observability_alerts_step_prunes_only_in_prune_alerts_mode() -> None:
    """#1435: prune-alerts runs apply-alerts with --prune; other modes pass no flag."""
    step = _apply_observability_step("Apply alert rules (idempotent)")

    assert step == {
        "name": "Apply alert rules (idempotent)",
        "if": (
            "${{ github.event_name != 'workflow_dispatch' || inputs.mode == 'apply' "
            "|| inputs.mode == 'prune-alerts' }}"
        ),
        "env": {
            "PRUNE_FLAG": "${{ inputs.mode == 'prune-alerts' && '--prune' || '' }}"
        },
        "run": (
            "uv run --locked python -m invoke fr-observability.shared.apply-alerts "
            "$PRUNE_FLAG"
        ),
    }


def test_apply_observability_dashboard_step_never_runs_in_prune_alerts_mode() -> None:
    """#1435: a prune run must not push dashboards the owner did not approve."""
    step = _apply_observability_step("Apply dashboard (idempotent)")

    assert step == {
        "name": "Apply dashboard (idempotent)",
        "if": (
            "${{ github.event_name != 'workflow_dispatch' || inputs.mode == 'apply' }}"
        ),
        "run": (
            "uv run --locked python -m invoke fr-observability.shared.apply-dashboard"
        ),
    }


def test_apply_observability_job_environment_stays_production() -> None:
    """#1435: the new mode adds no unattended path; every run waits for the owner."""
    assert _apply_observability_job()["environment"] == "production"


def test_apply_observability_run_name_shows_the_mode_and_the_release_tag() -> None:
    """#1435: the approver must see the mode and the release tag in the run title."""
    run_name = _apply_observability_document().get("run-name")

    assert isinstance(run_name, str), "run-name is missing from the workflow"
    assert run_name == (
        "Apply Observability (${{ inputs.mode }} ${{ github.ref_name }})"
    )


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
    for dashboard_file in DASHBOARD_FILES:  # #1464
        assert dashboard_file.relative_to(ROOT).as_posix() in observability


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
        and not str(path.relative_to(ROOT)).startswith("common/alerting")
        and path != ROOT / "common" / "alerting.py"
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
    from common import observability_dashboards as obs

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


def test_apply_dashboard_applies_every_checked_in_dashboard_once(monkeypatch) -> None:
    """#1464: one apply creates each DASHBOARD_FILES dashboard, a second only updates."""
    module, _ = _load_fr_observability_tasks(monkeypatch, all_dashboards=True)
    calls, rows, _ = _install_dashboards(monkeypatch, [])

    assert module.apply_dashboard(object()) is True
    module.apply_dashboard(object())

    count = len(DASHBOARD_FILES)
    assert [m for m, _ in _writes(calls)] == ["POST"] * count + ["PUT"] * count
    posted = [body for method, _, body in calls if method == "POST"]
    assert posted == [build_dashboard_import_payload(p) for p in DASHBOARD_FILES]
    assert len(rows) == count
    printed = module.print_dashboard(object())
    assert printed == posted


def test_verify_dashboard_task_names_the_dashboard_missing_from_signoz(
    monkeypatch,
) -> None:
    module, Exit = _load_fr_observability_tasks(monkeypatch, all_dashboards=True)
    stored = [
        {"id": f"d{n}", "data": load_dashboard(path)}
        for n, path in enumerate(DASHBOARD_FILES)
    ]
    _, rows, messages = _install_dashboards(monkeypatch, stored)
    assert module.verify_dashboard(object()) is True

    missing = rows.pop(f"d{len(DASHBOARD_FILES) - 1}")["data"]["title"]
    with pytest.raises(Exit) as exc:
        module.verify_dashboard(object())
    assert exc.value.code == 1
    assert any(missing in message for message in messages["error"])


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
    import common.security.store as env_store

    monkeypatch.setattr(env_store, "get_secrets", lambda *a, **k: {})
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


def test_apply_observability_workflow_watches_every_alert_rules_file() -> None:
    """Ensure apply-observability.yml has no on.push trigger and reconcile-iac-inputs.yml
    watches every alert_rules.json and dashboard.json and invokes apply-alerts / apply-dashboard."""
    import fnmatch
    import yaml

    wf = yaml.safe_load(APPLY_OBSERVABILITY_WORKFLOW.read_text(encoding="utf-8"))
    on_block = wf.get("on") or wf.get(True)
    assert "push" not in on_block
    assert "workflow_dispatch" in on_block

    reconcile_wf = yaml.safe_load(
        (ROOT / ".github/workflows/reconcile-iac-inputs.yml").read_text(
            encoding="utf-8"
        )
    )
    reconcile_steps = reconcile_wf["jobs"]["reconcile"]["steps"]
    obs_step = next(
        s
        for s in reconcile_steps
        if s.get("name") == "Reconcile SigNoz observability (alerts + dashboard)"
    )
    assert obs_step["env"]["DEPLOY_ENV"] == "production"
    run_script = obs_step["run"]
    non_comment_lines = [
        line for line in run_script.splitlines() if not line.lstrip().startswith("#")
    ]
    non_comment_script = "\n".join(non_comment_lines)
    assert "fr-observability.shared.apply-alerts" in non_comment_script
    assert "fr-observability.shared.apply-dashboard" in non_comment_script

    train_patterns = [
        pat.strip()
        for line in non_comment_lines
        if line.strip().endswith(")") and "|" in line and "observability" in line
        for pat in line.strip()[:-1].split("|")
    ]
    expected_patterns = [
        "finance_report/finance_report/observability/*",
        "truealpha/truealpha/observability/*",
        "platform/12.alerting/alert_rules.json",
        "platform/12.alerting/dashboard.json",
        "platform/30.todo/dashboard.json",
        "common/alerting/*",
        "common/observability_dashboards.py",
        "tools/signoz_alert_rule_probe.py",
    ]
    assert train_patterns == expected_patterns

    for target in (*ALERT_RULES_FILES, *DASHBOARD_FILES):
        rel = target.relative_to(ROOT).as_posix()
        assert any(fnmatch.fnmatch(rel, pat) for pat in train_patterns), (
            f"{rel} is not matched by any observability pattern in reconcile-iac-inputs.yml: {train_patterns}"
        )


# --- #1550: bounded retry of one transient SigNoz API failure ---------------------


class _FakeCurl:
    """`c` stand-in. Each `run` call answers with the next curl result.

    After the last answer, `run` repeats it, so an extra attempt shows as a call
    count and not as an IndexError.
    """

    def __init__(self, *answers: tuple[str, bool]):
        self.answers = answers
        self.commands: list[str] = []
        self.kwargs: list[dict] = []

    def run(self, command, **kwargs):
        index = min(len(self.commands), len(self.answers) - 1)
        self.commands.append(command)
        self.kwargs.append(kwargs)
        stdout, ok = self.answers[index]
        return types.SimpleNamespace(stdout=stdout, ok=ok)


def _answer(status: int, body: str = "") -> tuple[str, bool]:
    """One curl result: the body, a newline, then the `-w` status line."""
    return f"{body}\n{status}", True


_CURL_FAILED = ("", False)  # no output at all: connection error or timeout


def _load_real_alerting():
    spec = importlib.util.spec_from_file_location(
        "alerting_retry_under_test", ROOT / "platform/12.alerting/shared_tasks.py"
    )
    assert spec and spec.loader
    alerting = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alerting)
    return alerting


def _real_signoz_request(monkeypatch):
    """The real `_signoz_request`, with no sleep and no secret lookup.

    Returns `(alerting, waits)`; `waits` records each requested pause in seconds.
    """
    alerting = _load_real_alerting()
    waits: list[float] = []
    monkeypatch.setattr(
        alerting,
        "_signoz_context",
        lambda: {"domain": "signoz.example.test", "api_key": "test-api-key-1550"},
    )
    monkeypatch.setattr(alerting, "_sleep", waits.append)
    return alerting, waits


def test_signoz_request_get_retries_one_transient_520_then_returns_the_200(
    monkeypatch,
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(
        _answer(520, "error code: 520"), _answer(200, '{"status": "success"}')
    )

    result = alerting._signoz_request(c, method="GET", path="/api/v1/rules")

    assert result == {
        "ok": True,
        "data": {"status": "success"},
        "status": 200,
        "body": '{"status": "success"}',
    }
    assert len(c.commands) == 2
    assert waits == [2]
    # Every attempt runs the same command, and `c.run` never raises on a curl failure.
    assert c.commands[0] == c.commands[1]
    assert c.kwargs == [{"warn": True, "hide": True}] * 2
    # Each curl call is bounded, so a hung connection ends as status 0 (retryable).
    tokens = shlex.split(c.commands[0])
    assert tokens[tokens.index("--connect-timeout") + 1] == "10"
    assert tokens[tokens.index("--max-time") + 1] == "30"


def test_signoz_request_put_returns_the_last_520_after_three_attempts(
    monkeypatch,
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(_answer(520, "first"), _answer(520, "second"), _answer(520, "third"))
    payload = {"alert": "A", "labels": {"source": "test"}}

    result = alerting._signoz_request(
        c, method="PUT", path="/api/v1/rules/rule-1", payload=payload
    )

    assert result == {"ok": False, "data": None, "status": 520, "body": "third"}
    assert len(c.commands) == 3
    assert waits == [2, 5]
    # A retried PUT sends the same payload each time.
    assert len(set(c.commands)) == 1
    assert shlex.quote(json.dumps(payload)) in c.commands[0]


@pytest.mark.parametrize("answer", [_answer(520, "error code: 520"), _CURL_FAILED])
def test_signoz_request_post_never_retries(monkeypatch, answer) -> None:
    """A lost POST response can hide a rule that SigNoz already created."""
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(answer, _answer(200, "{}"))

    result = alerting._signoz_request(
        c, method="POST", path="/api/v1/rules", payload={"alert": "A"}
    )

    assert result["status"] == (520 if answer is not _CURL_FAILED else 0)
    assert result["ok"] is False
    assert len(c.commands) == 1
    assert waits == []


@pytest.mark.parametrize("status", [200, 400, 404, 500])
def test_signoz_request_get_returns_a_non_transient_status_at_once(
    monkeypatch, status
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(_answer(status, "body"), _answer(200, "{}"))

    result = alerting._signoz_request(c, method="GET", path="/api/v1/rules")

    assert result["status"] == status
    assert result["body"] == "body"
    assert len(c.commands) == 1
    assert waits == []


@pytest.mark.parametrize("status", [520, 524])
def test_signoz_request_get_retries_the_edges_of_the_transient_range(
    monkeypatch, status
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(_answer(status), _answer(200, "{}"))

    result = alerting._signoz_request(c, method="GET", path="/api/v1/rules")

    assert result["status"] == 200
    assert len(c.commands) == 2
    assert waits == [2]


def test_signoz_transient_statuses_are_the_gateway_errors_and_status_zero() -> None:
    """Status 0 is a curl failure. 502 to 504 and 520 to 524 are gateway and edge errors."""
    alerting = _load_real_alerting()

    assert alerting._SIGNOZ_TRANSIENT_STATUSES == frozenset(
        {0, 502, 503, 504, 520, 521, 522, 523, 524}
    )
    assert alerting._SIGNOZ_RETRY_DELAYS_SECONDS == (2, 5)


@pytest.mark.parametrize(
    "unreadable",
    [_CURL_FAILED, ("curl: (28) timed out", False), ("body\nnot-a-number", True)],
)
def test_signoz_request_delete_retries_unreadable_curl_output_then_returns_the_200(
    monkeypatch, unreadable
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(unreadable, _answer(200, "{}"))

    result = alerting._signoz_request(c, method="DELETE", path="/api/v1/rules/rule-1")

    assert result["status"] == 200
    assert result["ok"] is True
    assert len(c.commands) == 2
    assert waits == [2]


def test_signoz_request_put_recovers_on_the_third_attempt(monkeypatch) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(_answer(503), _answer(502), _answer(200, '{"id": "rule-1"}'))

    result = alerting._signoz_request(
        c, method="PUT", path="/api/v1/rules/rule-1", payload={"alert": "A"}
    )

    assert result["status"] == 200
    assert result["data"] == {"id": "rule-1"}
    assert len(c.commands) == 3
    assert waits == [2, 5]


def test_signoz_request_retry_matches_the_method_in_any_letter_case(
    monkeypatch,
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    c = _FakeCurl(_answer(520), _answer(200, "{}"))

    result = alerting._signoz_request(c, method="get", path="/api/v1/rules")

    assert result["status"] == 200
    assert len(c.commands) == 2
    assert waits == [2]


def test_signoz_request_retry_logs_only_the_status_method_path_and_delay(
    monkeypatch, capsys
) -> None:
    alerting, _ = _real_signoz_request(monkeypatch)
    c = _FakeCurl(_answer(520), _answer(502), _answer(200, "{}"))

    alerting._signoz_request(
        c, method="PUT", path="/api/v1/rules/rule-1", payload={"alert": "secret-rule"}
    )

    shown = capsys.readouterr()
    assert shown.out.splitlines() == [
        "SigNoz API 520 for PUT /api/v1/rules/rule-1; retry 1/2 in 2s",
        "SigNoz API 502 for PUT /api/v1/rules/rule-1; retry 2/2 in 5s",
    ]
    assert shown.err == ""


def test_signoz_request_without_a_context_makes_no_call_and_no_wait(
    monkeypatch,
) -> None:
    alerting, waits = _real_signoz_request(monkeypatch)
    monkeypatch.setattr(alerting, "_signoz_context", lambda: None)
    c = _FakeCurl(_answer(520))

    result = alerting._signoz_request(c, method="GET", path="/api/v1/rules")

    assert result == {"ok": False, "data": None, "status": 0, "body": ""}
    assert c.commands == []
    assert waits == []


# --- #1550: re-create an alert rule safely after a transient create failure ---------
#
# `apply_alerts` re-applies a rule by DELETE, then POST. In run 42815407435 the DELETE
# worked and the POST answered 520, so the rule stayed missing. A lost POST response can
# also hide a rule that SigNoz already stored, so the retry looks before it POSTs again.

_INCIDENT_RULE = "FinanceReportRateLimitSaturation"
_STALE_RULE = "FinanceReportReconciliationAnomaly"


class _LossyCreateSigNoz(_FakeSigNoz):
    """`_FakeSigNoz` that answers each POST of one rule from a script.

    A script entry is `(status, stored)`. Status 200 creates the rule. Any other
    status fails the POST. With `stored` set, SigNoz keeps the rule although the
    caller sees the failure: a lost response. When `relist_answer` is set, it
    replaces the answer of the first list after a failed POST. `events` records each
    POST of a rule and each list of the rules, in order.
    """

    def __init__(self, rule_name, script, *, relist_answer=None, **kwargs):
        super().__init__(**kwargs)
        self.rule_name = rule_name
        self.script = list(script)
        self.relist_answer = relist_answer
        self.posted: list[str] = []  # alert name of every POST, in order
        self.events: list[tuple] = []
        self._relist_pending = False

    def request(self, _c, *, method, path, payload=None):
        if (method, path) == ("GET", "/api/v1/rules"):
            self.events.append(("GET",))
            if self._relist_pending and self.relist_answer is not None:
                self._relist_pending = False
                self.calls.append((method, path, payload))
                return self.relist_answer
        if (method, path) == ("POST", "/api/v1/rules"):
            self.events.append(("POST", payload["alert"]))
            self.posted.append(payload["alert"])
            if payload["alert"] == self.rule_name and self.script:
                status, stored = self.script.pop(0)
                if status != 200:
                    if stored:
                        super().request(_c, method=method, path=path, payload=payload)
                    else:
                        self.calls.append((method, path, payload))
                    self._relist_pending = True
                    return {
                        "ok": False,
                        "data": None,
                        "status": status,
                        "body": f"error code: {status}",
                    }
        return super().request(_c, method=method, path=path, payload=payload)


def _apply_alerts_over(monkeypatch, fake, clock=None):
    """`apply_alerts` over `fake`, on the real alerting module.

    Returns a namespace: `module` (the task module), `apply` (the task), `Exit`,
    `waits` (seconds), `errors` (the `(message, detail)` pair of each error line),
    `successes` and `infos` (messages). A wait is also recorded in `fake.events`, as
    `("WAIT", seconds)`. With `clock` (a dict with key `now`), the task reads it as
    its monotonic clock.
    """
    task, alerting, Exit, _ = _install_real_alerting(monkeypatch, fake)
    run = types.SimpleNamespace(
        module=task,
        apply=task.apply_alerts,
        Exit=Exit,
        waits=[],
        errors=[],
        successes=[],
        infos=[],
    )
    if clock is not None:
        monkeypatch.setattr(alerting, "_monotonic", lambda: clock["now"])

    def sleep(seconds):
        run.waits.append(seconds)
        fake.events.append(("WAIT", seconds))

    monkeypatch.setattr(alerting, "_sleep", sleep)
    _patch_console(
        monkeypatch,
        error=lambda *a, **k: run.errors.append(a),
        success=lambda *a, **k: run.successes.append(a[0]),
        info=lambda *a, **k: run.infos.append(a[0]),
    )
    return run


def _stored_names(fake) -> list[str]:
    return sorted(rule["alert"] for rule in fake.rules.values())


def _failed_line(status: int) -> tuple[str, str]:
    return (
        f"Failed to apply SigNoz alert rule: {_INCIDENT_RULE}",
        f"status={status} body=error code: {status}",
    )


def test_apply_alerts_create_posts_again_after_a_lost_create_that_stored_nothing(
    monkeypatch,
) -> None:
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE, [(520, False)], rules=_prod_before_this_fix()
    )
    run = _apply_alerts_over(monkeypatch, fake)

    assert run.apply(object()) is True

    assert fake.posted.count(_INCIDENT_RULE) == 2
    assert run.waits == [2]
    assert run.errors == []
    assert f"SigNoz alert rule updated: {_INCIDENT_RULE}" in run.successes
    assert _stored_names(fake).count(_INCIDENT_RULE) == 1
    # The order is: failed POST, wait, list the rules, POST again.
    first = fake.events.index(("POST", _INCIDENT_RULE))
    assert fake.events[first : first + 4] == [
        ("POST", _INCIDENT_RULE),
        ("WAIT", 2),
        ("GET",),
        ("POST", _INCIDENT_RULE),
    ]
    assert (
        f"SigNoz create of {_INCIDENT_RULE} got 520; wait 2s, "
        "check the rule list, then retry"
    ) in run.infos


@pytest.mark.parametrize(("existed", "verb"), [(True, "updated"), (False, "created")])
def test_apply_alerts_create_does_not_post_again_when_the_rule_is_already_listed(
    monkeypatch, existed, verb
) -> None:
    rules = [
        rule
        for rule in _prod_before_this_fix()
        if existed or rule["alert"] != _INCIDENT_RULE
    ]
    fake = _LossyCreateSigNoz(_INCIDENT_RULE, [(520, True)], rules=rules)
    run = _apply_alerts_over(monkeypatch, fake)

    assert run.apply(object()) is True

    assert fake.posted.count(_INCIDENT_RULE) == 1
    assert run.waits == [2]
    assert run.errors == []
    assert (
        f"SigNoz alert rule {verb} after a lost response: {_INCIDENT_RULE}"
        in run.successes
    )
    assert _stored_names(fake).count(_INCIDENT_RULE) == 1


@pytest.mark.parametrize(
    ("stored", "posts", "lost_lines"),
    [
        (False, 2, []),
        (
            True,
            1,
            [f"SigNoz alert rule updated after a lost response: {_INCIDENT_RULE}"],
        ),
    ],
    ids=["no-new-rule-is-listed", "a-new-rule-is-listed"],
)
def test_apply_alerts_create_counts_only_a_rule_that_is_new_since_the_first_list(
    monkeypatch, stored, posts, lost_lines
) -> None:
    """A same-name rule that SigNoz listed before the first POST is not a lost create."""
    old_copy = {"id": "old-copy", "alert": _INCIDENT_RULE, "labels": {}}
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE, [(520, stored)], rules=[*_prod_before_this_fix(), old_copy]
    )
    run = _apply_alerts_over(monkeypatch, fake)

    assert run.apply(object()) is True

    assert fake.posted.count(_INCIDENT_RULE) == posts
    assert [m for m in run.successes if "lost response" in m] == lost_lines
    assert "old-copy" in fake.rules  # the task never touched the old copy


def test_apply_alerts_create_fails_naming_the_rule_after_three_lost_creates(
    monkeypatch,
) -> None:
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE, [(520, False)] * 3, rules=_prod_before_this_fix()
    )
    run = _apply_alerts_over(monkeypatch, fake)

    with pytest.raises(run.Exit) as exc:
        run.apply(object())

    assert exc.value.code == 1
    assert fake.posted.count(_INCIDENT_RULE) == 3
    assert run.waits == [2, 5]
    assert run.errors == [_failed_line(520)]
    assert [m for m in run.infos if m.startswith("SigNoz create of")] == [
        f"SigNoz create of {_INCIDENT_RULE} got 520; wait 2s, "
        "check the rule list, then retry",
        f"SigNoz create of {_INCIDENT_RULE} got 520; wait 5s, "
        "check the rule list, then retry",
    ]
    assert _INCIDENT_RULE not in _stored_names(fake)


@pytest.mark.parametrize("status", [400, 404])
def test_apply_alerts_create_does_not_retry_a_non_transient_status(
    monkeypatch, status
) -> None:
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE, [(status, False)], rules=_prod_before_this_fix()
    )
    run = _apply_alerts_over(monkeypatch, fake)

    with pytest.raises(run.Exit) as exc:
        run.apply(object())

    assert exc.value.code == 1
    assert fake.posted.count(_INCIDENT_RULE) == 1
    assert run.waits == []
    assert run.errors == [_failed_line(status)]


def test_apply_alerts_create_retries_a_create_that_got_no_status(monkeypatch) -> None:
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE, [(0, False)], rules=_prod_before_this_fix()
    )
    run = _apply_alerts_over(monkeypatch, fake)

    assert run.apply(object()) is True

    assert fake.posted.count(_INCIDENT_RULE) == 2
    assert run.waits == [2]
    assert run.errors == []


def _list_answer(body) -> dict:
    """An HTTP-OK list answer whose decoded body is `body`."""
    return {"ok": True, "data": body, "status": 200, "body": "{}"}


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "data": None, "status": 520, "body": "error code: 520"},
        _list_answer(None),
        _list_answer("<html>index</html>"),
        _list_answer({}),
        _list_answer([]),
        _list_answer({"status": "success"}),
        _list_answer({"data": {"rules": None}}),
        _list_answer({"status": "success", "data": {}}),
    ],
    ids=[
        "http-error",
        "no-data",
        "a-string",
        "empty-object",
        "empty-list",
        "status-only",
        "rules-null",
        "empty-data-object",
    ],
)
def test_apply_alerts_create_never_posts_again_when_the_list_cannot_prove_absence(
    monkeypatch, answer
) -> None:
    """Only a recognised rule collection proves a rule absent. Else a second POST could
    duplicate a rule that SigNoz stored."""
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE,
        [(520, True)],
        relist_answer=answer,
        rules=_prod_before_this_fix(),
    )
    run = _apply_alerts_over(monkeypatch, fake)

    with pytest.raises(run.Exit) as exc:
        run.apply(object())

    assert exc.value.code == 1
    assert fake.posted.count(_INCIDENT_RULE) == 1
    assert run.waits == [2]
    assert run.errors == [
        (
            f"SigNoz rule state unknown after a failed create: {_INCIDENT_RULE}",
            "the rule list is unreadable, so the task sends no second create",
        ),
        _failed_line(520),
    ]
    assert _stored_names(fake).count(_INCIDENT_RULE) == 1


def test_apply_alerts_create_posts_again_when_the_list_holds_an_empty_rules_collection(
    monkeypatch,
) -> None:
    """An empty list under `rules` is a valid answer: the rule really is absent."""
    fake = _LossyCreateSigNoz(
        _INCIDENT_RULE,
        [(520, False)],
        relist_answer=_list_answer({"data": {"rules": []}}),
        rules=_prod_before_this_fix(),
    )
    run = _apply_alerts_over(monkeypatch, fake)

    assert run.apply(object()) is True

    assert fake.posted.count(_INCIDENT_RULE) == 2
    assert run.errors == []
    assert f"SigNoz alert rule updated: {_INCIDENT_RULE}" in run.successes
    assert _stored_names(fake).count(_INCIDENT_RULE) == 1


def test_apply_alerts_create_failure_of_one_rule_does_not_stop_the_other_rules(
    monkeypatch,
) -> None:
    order = [definition.alert_name for definition in load_alert_definitions()]
    first = order[0]
    fake = _LossyCreateSigNoz(first, [(400, False)], rules=_prod_before_this_fix())
    run = _apply_alerts_over(monkeypatch, fake)

    with pytest.raises(run.Exit) as exc:
        run.apply(object())

    assert exc.value.code == 1
    assert fake.posted == order
    assert run.waits == []
    assert [message for message, _ in run.errors] == [
        f"Failed to apply SigNoz alert rule: {first}"
    ]
    assert _stored_names(fake) == sorted({*order[1:], _STALE_RULE})


class _SlowPostSigNoz(_FakeSigNoz):
    """`_FakeSigNoz` where each rule POST moves the fake clock by `seconds`."""

    def __init__(self, clock, seconds, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.seconds = seconds
        self.posted: list[str] = []  # alert name of every POST, in order

    def request(self, _c, *, method, path, payload=None):
        answer = super().request(_c, method=method, path=path, payload=payload)
        if (method, path) == ("POST", "/api/v1/rules"):
            self.posted.append(payload["alert"])
            self.clock["now"] += self.seconds
        return answer


def _three_rule_apply(monkeypatch, seconds_per_post):
    """`apply_alerts` for the first three catalog rules, each POST taking `seconds`."""
    order = [definition.alert_name for definition in load_alert_definitions()][:3]
    clock = {"now": 0.0}
    fake = _SlowPostSigNoz(clock, seconds_per_post, rules=_prod_before_this_fix())
    run = _apply_alerts_over(monkeypatch, fake, clock=clock)
    definitions = load_alert_definitions()[:3]
    monkeypatch.setattr(run.module, "load_alert_definitions", lambda: definitions)
    return order, fake, run


@pytest.mark.parametrize(("seconds", "applied"), [(1201, 1), (1200, 2)])
def test_apply_alerts_stops_before_the_next_delete_after_the_deadline(
    monkeypatch, seconds, applied
) -> None:
    order, fake, run = _three_rule_apply(monkeypatch, seconds)

    with pytest.raises(run.Exit) as exc:
        run.apply(object())

    assert exc.value.code == 1
    assert fake.posted == order[:applied]
    deleted = {path for method, path, _ in fake.calls if method == "DELETE"}
    assert deleted == {
        f"/api/v{version}/rules/old-{n}"
        for version in (1, 2)
        for n in range(1, applied + 1)
    }
    assert run.errors == [
        (
            "Stopped after 1200 s: not applied, the live rule is unchanged: "
            + ", ".join(order[applied:]),
        )
    ]
    # The skipped rules keep their live definition.
    assert {rule["id"] for rule in fake.rules.values()} >= {
        f"old-{n}" for n in range(applied + 1, 4)
    }


def test_apply_alerts_applies_every_rule_when_the_clock_stays_under_the_deadline(
    monkeypatch,
) -> None:
    order, fake, run = _three_rule_apply(monkeypatch, 100)

    assert run.apply(object()) is True

    assert fake.posted == order
    assert run.errors == []
