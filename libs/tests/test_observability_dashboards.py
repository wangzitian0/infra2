"""Offline tests for finance_report observability config-as-code (#373)."""

from __future__ import annotations

import importlib.util
import json
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
)

ROOT = Path(__file__).resolve().parents[2]
OBS_DIR = ROOT / "finance_report" / "finance_report" / "observability"


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
    query = payload["condition"]["compositeQuery"]["builderQueries"]["A"]
    filters = query["filters"]["items"]
    assert filters[0]["value"] == "finance-report-backend"
    assert filters[1]["value"] == ["ERROR", "FATAL"]


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
    for wid in ("be-throughput", "be-error-rate-5xx", "be-latency-p50", "be-latency-p95", "be-latency-p99"):
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
    page_load_filters = widgets["fe-page-load"]["query"]["builder"]["queryData"][0]["filters"]["items"]
    assert any(f["key"]["key"] == "name" and f["value"] == "documentLoad" for f in page_load_filters)

    # the error-rate panel counts only SERVER spans (matches its description)
    err_filters = widgets["be-error-rate-5xx"]["query"]["builder"]["queryData"][0]["filters"]["items"]
    assert any(f["key"]["key"] == "kind_string" and f["value"] == "Server" for f in err_filters)
    assert any(f["key"]["key"] == "hasError" for f in err_filters)


def test_every_widget_is_filterable_by_deployment_environment() -> None:
    """The deployment_environment dropdown only filters a panel if the panel's query
    references it — assert EVERY widget carries the `$deployment_environment` filter so
    the dashboard variable is real, not decorative (CR #391)."""
    for w in load_dashboard()["widgets"]:
        items = w["query"]["builder"]["queryData"][0]["filters"]["items"]
        assert any(
            f["key"]["key"] == "deployment.environment" and f["value"] == "$deployment_environment"
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


def test_apply_tasks_are_invoke_tasks() -> None:
    """#373: invoke exposes apply + print tasks for alerts and dashboard."""
    fake_invoke = types.ModuleType("invoke")
    fake_invoke.task = lambda func=None, **_kwargs: func if func else (lambda f: f)
    sys.modules.setdefault("invoke", fake_invoke)

    path = OBS_DIR / "shared_tasks.py"
    spec = importlib.util.spec_from_file_location("fr_observability_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name in ("apply_alerts", "apply_dashboard", "print_alerts", "print_dashboard"):
        assert hasattr(module, name), name


def test_ssot_documents_alert_and_dashboard_apply_path() -> None:
    """#373: ops docs document how the alert + dashboard are applied."""
    alerting = (ROOT / "docs/ssot/ops.alerting.md").read_text(encoding="utf-8")
    observability = (ROOT / "docs/ssot/ops.observability.md").read_text(
        encoding="utf-8"
    )

    assert "FinanceReportBackendErrorLogs" in alerting
    assert "fr-observability.shared.apply-alerts" in alerting
    assert "fr-observability.shared.apply-dashboard" in observability
