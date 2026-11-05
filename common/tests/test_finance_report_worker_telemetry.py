"""The finance_report Prefect worker exports telemetry under its own identity (#1431).

The worker parses statements, so the parse metrics and the async-failure counter come
from its process. Before #1431 it had no OTLP endpoint and the parse panels and P1
alerts read an empty series. These tests resolve the compose against the env that the
production promote path issues, then check the alerts and panels admit that identity.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from common.deploy import promote as dp
from common.deploy.contract import SERVICES
from common.observability_dashboards import load_dashboard
from common.tests.compose_env import container_env
from common.tests.test_deploy_primitive import (
    FIXED_FINANCE_REPORT_COMPOSE,
    FULL_SHA,
    FakeDokploy,
    _stub_public_health,  # noqa: F401  fixture: no live HTTP in deploy()
    _stub_schema_gate,  # noqa: F401  fixture: no SSH in deploy()
    _stub_secret_provisioning,  # noqa: F401  fixture: no Vault in deploy()
)

ROOT = Path(__file__).resolve().parents[2]
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
    worker = container_env(
        FIXED_FINANCE_REPORT_COMPOSE, "prefect-worker", _pushed_env()
    )
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


_PANEL_SERVICES = re.compile(r"service\.name IN \[([^\]]*)\]")


def test_worker_metric_panels_count_exactly_backend_and_worker() -> None:
    """A near-miss name (`finance-report-workers`) must fail: compare the parsed list."""
    widgets = {w["id"]: w for w in load_dashboard()["widgets"]}
    for widget_id in ("ai-provider-latency-p95", "statement-parse-outcomes"):
        expression = widgets[widget_id]["query"]["builder"]["queryData"][0]["filter"][
            "expression"
        ]
        match = _PANEL_SERVICES.search(expression)
        assert match, widget_id
        names = [item.strip().strip("'") for item in match.group(1).split(",")]
        assert names == [BACKEND, WORKER], widget_id
        assert "service.name =" not in expression, widget_id


def test_parse_alerts_count_exactly_backend_and_worker() -> None:
    rules = {rule["alert_name"]: rule for rule in _rules()}
    for name in (
        "FinanceReportStatementParseFailureSpike",
        "FinanceReportAsyncTaskFailures",
    ):
        match = re.search(
            r'"service\.name"=~"((?:[^"\\]|\\.)*)"', rules[name]["promql"]
        )
        assert match, name
        pattern = match.group(1)
        assert re.fullmatch(pattern, BACKEND), name
        assert re.fullmatch(pattern, WORKER), name
        for other in ("finance-report-workers", "finance-report-frontend", "x"):
            assert not re.fullmatch(pattern, other), (name, other)


def test_worker_metrics_are_named_by_the_alerts_that_this_covers() -> None:
    promql = " ".join(rule.get("promql", "") for rule in _rules())
    for metric in WORKER_METRICS:
        assert metric in promql


def _rules() -> list[dict]:
    from common.observability_dashboards import ALERT_RULES_FILE

    return json.loads(ALERT_RULES_FILE.read_text(encoding="utf-8"))["rules"]


# --- the endpoint must not outlive the attributes (#1431 review) ------------------------

PLACEHOLDER_SHA = "c" * 40


def _sync_env(monkeypatch, environment: str) -> dict[str, str]:
    """The env that the iac-runner sync path pushes, from the real SyncPipeline step."""
    import importlib.util

    from common.core.service_identity import ServiceIdentity
    from common.deploy.sync_pipeline import SyncPipeline

    spec = importlib.util.spec_from_file_location(
        "finance_report_app_deploy_sync",
        ROOT / "finance_report/finance_report/10.app/deploy.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deployer = module.AppDeployer
    pushed: dict[str, str] = {}
    monkeypatch.setattr(deployer, "_prepare_dirs", classmethod(lambda cls, c: True))
    monkeypatch.setattr(
        deployer,
        "composing",
        classmethod(lambda cls, c, env: pushed.update(env) or "compose-id"),
    )
    pipeline = SyncPipeline(deployer=deployer, context=None)
    ctx = pipeline.ctx
    ctx.env = {"ENV": environment}
    ctx.local_hash = "h"
    ctx.deploy_ref = PLACEHOLDER_SHA
    ctx.service_id = "finance_report/app"
    ctx.runtime_identity = ServiceIdentity.build(
        "finance_report/app", environment, version=PLACEHOLDER_SHA
    )
    assert pipeline.step_prepare_and_deploy() is None
    return pushed


def _preview_env() -> dict[str, str]:
    from common.deploy.env_config import preview_alias
    from common.deploy.preview import _preview_env_vars

    return _preview_env_vars(
        preview_alias("pr", 5),
        service="finance_report/app",
        sha=FULL_SHA,
        domain="zitian.party",
        iac_ref=PLACEHOLDER_SHA,
        _now=lambda: 0,
    )


def _worker_export(pushed: dict[str, str]) -> dict[str, str]:
    return container_env(FIXED_FINANCE_REPORT_COMPOSE, "prefect-worker", pushed)


def test_sync_path_issues_the_worker_identity(monkeypatch) -> None:
    worker = _worker_export(_sync_env(monkeypatch, "staging"))
    assert worker["OTEL_EXPORTER_OTLP_ENDPOINT"] == COLLECTOR
    assert worker["OTEL_SERVICE_NAME"] == WORKER
    attributes = _attributes(worker["OTEL_RESOURCE_ATTRIBUTES"])
    assert attributes["service.name"] == WORKER
    assert attributes["infra.component"] == "worker"
    assert attributes["deployment.environment.name"] == "staging"


def test_every_deploy_path_leaves_export_off_when_it_issues_no_attributes(
    monkeypatch,
) -> None:
    """Export on with an empty attribute string crash-loops the worker: Settings() refuses
    a deployed export without a deployment.environment tag. Resolve the real compose
    against what each path pushes; the invariant is `no attributes => no endpoint`."""
    paths = {
        "promote": _pushed_env(),
        "sync": _sync_env(monkeypatch, "staging"),
        "preview": _preview_env(),
    }
    issuing = []
    for name, pushed in paths.items():
        worker = _worker_export(pushed)
        endpoint = worker["OTEL_EXPORTER_OTLP_ENDPOINT"]
        attributes = worker["OTEL_RESOURCE_ATTRIBUTES"]
        if attributes:
            issuing.append(name)
            assert "deployment.environment.name=" in attributes, name
        else:
            assert endpoint == "", name
    assert issuing == ["promote", "sync"]  # preview issues none and stays off


def test_endpoint_is_empty_when_a_path_pushes_no_worker_attributes() -> None:
    pushed = {
        k: v for k, v in _pushed_env().items() if not k.startswith("OTEL_WORKER_")
    }
    worker = _worker_export(pushed)
    assert worker["OTEL_RESOURCE_ATTRIBUTES"] == ""
    assert worker["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
