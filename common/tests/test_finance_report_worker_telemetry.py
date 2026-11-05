"""The finance_report Prefect worker exports telemetry under its own identity (#1431).

The worker parses statements, so the parse metrics and the async-failure counter come
from its process. Before #1431 it had no OTLP endpoint and the parse panels and P1
alerts read an empty series. These tests resolve the compose against the env that the
production promote path issues, then check the alerts and panels admit that identity.
"""

from __future__ import annotations

import json

from common.deploy import promote as dp
from common.deploy.contract import SERVICES
from common.observability_dashboards import load_alert_definitions, load_dashboard
from common.tests.compose_env import container_env
from common.tests.test_deploy_primitive import (
    FIXED_FINANCE_REPORT_COMPOSE,
    FULL_SHA,
    FakeDokploy,
    _stub_public_health,  # noqa: F401  fixture: no live HTTP in deploy()
    _stub_schema_gate,  # noqa: F401  fixture: no SSH in deploy()
    _stub_secret_provisioning,  # noqa: F401  fixture: no Vault in deploy()
)

WORKER = "finance-report-worker"
BACKEND = "finance-report-backend"
COLLECTOR = "http://platform-signoz-otel-collector:4318"
#: Metrics that the worker process emits (statement parse and its async task).
WORKER_METRICS = ("finance.statement_parse.outcome", "finance.async_parse.failure")


def _pushed_env() -> dict[str, str]:
    client = FakeDokploy()
    dp.deploy(
        "staging",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        iac_ref="b" * 40,
    )
    return client.updated[0][1]


def _attributes(raw: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in raw.split(","))


def test_worker_container_gets_endpoint_and_its_own_issued_identity() -> None:
    worker = container_env(FIXED_FINANCE_REPORT_COMPOSE, "prefect-worker", _pushed_env())
    assert worker["OTEL_EXPORTER_OTLP_ENDPOINT"] == COLLECTOR
    assert worker["OTEL_SERVICE_NAME"] == WORKER
    attributes = _attributes(worker["OTEL_RESOURCE_ATTRIBUTES"])
    assert attributes["service.name"] == WORKER
    assert attributes["infra.component"] == "worker"
    assert attributes["infra.service.id"] == "finance_report/app"
    assert attributes["deployment.environment.name"] == "staging"
    assert attributes["deployment.environment"] == "staging"


def test_backend_identity_stays_the_backend_identity() -> None:
    backend = container_env(FIXED_FINANCE_REPORT_COMPOSE, "backend", _pushed_env())
    attributes = _attributes(backend["OTEL_RESOURCE_ATTRIBUTES"])
    assert backend["OTEL_SERVICE_NAME"] == BACKEND
    assert attributes["service.name"] == BACKEND
    assert attributes["infra.component"] == "app"


def test_only_services_with_a_worker_identity_are_issued_one() -> None:
    assert SERVICES["finance_report/app"].worker_identity_service_name == WORKER
    assert SERVICES["truealpha/app"].worker_identity_service_name == ""


def test_dashboard_has_no_north_star_panel() -> None:
    dashboard = load_dashboard()
    assert "confidence-north-star" not in {w["id"] for w in dashboard["widgets"]}
    assert "confidence-north-star" not in {item["i"] for item in dashboard["layout"]}
    assert "confidence_north_star" not in json.dumps(dashboard)


def test_dashboard_layout_has_no_gap_after_the_removal() -> None:
    layout = load_dashboard()["layout"]
    ids = [item["i"] for item in layout]
    assert len(ids) == len(set(ids))
    cells = [(item["x"], item["y"]) for item in layout]
    assert len(cells) == len(set(cells)), "two panels share one grid cell"
    rows = sorted({item["y"] for item in layout})
    assert rows == list(range(0, 6 * len(rows), 6)), "a grid row is empty"
    per_row = [sum(1 for item in layout if item["y"] == y) for y in rows]
    assert per_row[:-1] == [2] * (len(rows) - 1), "a grid row has a hole"


def test_worker_metric_panels_count_worker_series() -> None:
    widgets = {w["id"]: w for w in load_dashboard()["widgets"]}
    for widget_id in ("ai-provider-latency-p95", "statement-parse-outcomes"):
        expression = widgets[widget_id]["query"]["builder"]["queryData"][0]["filter"][
            "expression"
        ]
        assert WORKER in expression, widget_id
        assert BACKEND in expression, widget_id


def test_parse_alerts_count_worker_series() -> None:
    rules = {rule["alert_name"]: rule for rule in _rules()}
    for name in (
        "FinanceReportStatementParseFailureSpike",
        "FinanceReportAsyncTaskFailures",
    ):
        promql = rules[name]["promql"]
        assert WORKER in promql, name
        assert BACKEND in promql, name


def test_worker_metrics_are_named_by_the_alerts_that_this_covers() -> None:
    promql = " ".join(rule.get("promql", "") for rule in _rules())
    for metric in WORKER_METRICS:
        assert metric in promql


def _rules() -> list[dict]:
    from common.observability_dashboards import ALERT_RULES_FILE

    return json.loads(ALERT_RULES_FILE.read_text(encoding="utf-8"))["rules"]
