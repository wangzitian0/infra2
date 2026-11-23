"""Offline tests for SigNoz to Feishu alerting."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from common.tests.ambient_env import clear_telemetry_env
from common.tests.compose_env import compose_services
from common.alerting import (
    AlertingError,
    BasicAuth,
    InvalidWebhookUrl,
    MAX_MESSAGE_CHARS,
    _signoz_filter_literal,
    build_feishu_alert_card,
    build_feishu_app_card_payload,
    build_feishu_card_payload,
    build_signoz_channel_payload,
    build_signoz_log_alert_rule_payload,
    build_signoz_metric_alert_rule_payload,
    deliver_feishu_app_card,
    feishu_host_reachable,
    find_signoz_channel_id,
    find_signoz_rule_id,
    format_signoz_alert,
    redacted_url,
    signoz_feishu_channel_name,
    signoz_rule_channels,
    validate_feishu_webhook_url,
)

ROOT = Path(__file__).resolve().parents[2]


def test_feishu_host_reachable_false_on_empty_or_unparseable_url() -> None:
    """'lark 畅通' probe helper: empty / hostless URL is never reachable, never raises."""
    assert feishu_host_reachable("") is False
    assert feishu_host_reachable("   ") is False
    assert feishu_host_reachable("not a url") is False


def test_feishu_host_reachable_does_a_tcp_connect_no_message(monkeypatch) -> None:
    """It proves reachability via a plain TCP connect to (host, 443) — it never POSTs
    (so the probe can run every minute without spamming the real Lark channel)."""
    import socket

    calls = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_create_connection(addr, timeout=None):
        calls.append(addr)
        return _Conn()

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    assert (
        feishu_host_reachable("https://open.feishu.cn/open-apis/bot/v2/hook/x") is True
    )
    assert calls == [("open.feishu.cn", 443)]

    def _boom(addr, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", _boom)
    assert (
        feishu_host_reachable("https://open.feishu.cn/open-apis/bot/v2/hook/x") is False
    )


def test_feishu_webhook_validation_is_https_and_host_scoped() -> None:
    """Infra-007 alerting: Feishu webhook secrets stay host-scoped and hidden."""
    valid = "https://open.feishu.cn/open-apis/bot/v2/hook/token"
    assert validate_feishu_webhook_url(valid) == valid
    assert redacted_url(valid).endswith("/open-apis/bot/v2/hook/***")

    with pytest.raises(InvalidWebhookUrl):
        validate_feishu_webhook_url("http://open.feishu.cn/open-apis/bot/v2/hook/token")
    with pytest.raises(InvalidWebhookUrl):
        validate_feishu_webhook_url("https://example.com/open-apis/bot/v2/hook/token")
    with pytest.raises(InvalidWebhookUrl):
        validate_feishu_webhook_url("https://open.feishu.cn/open-apis/bot/v2/hook/")


def test_alertmanager_payload_is_rendered_as_feishu_text() -> None:
    """Infra-007 alerting: SigNoz webhook payloads become Feishu text messages."""
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "ExampleBackendDown", "severity": "critical"},
        "commonAnnotations": {"summary": "Production API health check failed"},
        "externalURL": "https://signoz.zitian.party",
        "alerts": [
            {
                "labels": {
                    "alertname": "ExampleBackendDown",
                    "instance": "example-backend",
                },
                "annotations": {"summary": "GET /api/health returned 503"},
            }
        ],
    }

    text = format_signoz_alert(payload)
    assert text.splitlines()[0] == "🔴 [P0 告警] ExampleBackendDown · 1 项"
    assert "级别：P0" in text.splitlines()
    assert "对象：example-backend" in text.splitlines()
    assert "现象：GET /api/health returned 503" in text.splitlines()


@pytest.mark.parametrize(
    ("alert_name", "anchor"),
    [
        ("ContainerBreakdown", "#container-killed"),
        ("DeployQueueStuck", "#deployment-failed"),
    ],
)
def test_p0_alerts_link_to_existing_runbook_in_text_and_card(
    alert_name: str, anchor: str
) -> None:
    payload = _sample_alert_payload(
        commonLabels={"alertname": alert_name, "severity": "critical"}
    )
    payload["alerts"][0]["labels"]["alertname"] = alert_name
    text = format_signoz_alert(payload)
    card = build_feishu_alert_card(payload)
    url = f"https://github.com/wangzitian0/infra2/blob/main/docs/runbooks/infra022-p0.md{anchor}"
    assert f"Runbook：{url}" in text.splitlines()
    assert f"]({url})" in json.dumps(card, ensure_ascii=False)
    heading = anchor.removeprefix("#").replace("-", " ")
    headings = (ROOT / "docs/runbooks/infra022-p0.md").read_text(encoding="utf-8")
    assert f"## {heading}" in headings.lower()


def _sample_alert_payload(**over) -> dict:
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "ExampleBackendDown", "severity": "critical"},
        "commonAnnotations": {"summary": "Production API health check failed"},
        "externalURL": "https://signoz.zitian.party",
        "alerts": [
            {
                "labels": {
                    "alertname": "ExampleBackendDown",
                    "instance": "example-backend",
                },
                "annotations": {"summary": "GET /api/health returned 503"},
            }
        ],
    }
    payload.update(over)
    return payload


def test_alert_card_has_severity_colored_header_fields_and_signoz_button() -> None:
    """A firing critical alert renders a red-headed interactive card with a SigNoz button."""
    card = build_feishu_alert_card(_sample_alert_payload())

    assert card["header"]["template"] == "red"
    title = card["header"]["title"]["content"]
    assert title == "🔴 [P0 告警] ExampleBackendDown · 1 项"

    (fields,) = [e["fields"] for e in card["elements"] if "fields" in e]
    contents = [field["text"]["content"] for field in fields]
    assert contents[:4] == [
        "级别：P0",
        "对象：example-backend",  # the per-alert instance
        "现象：GET /api/health returned 503",
        "开始于：未知",
    ]
    short_flags = {
        field["text"]["content"].split("：", 1)[0]: field["is_short"]
        for field in fields
    }
    assert short_flags["级别"] is True
    assert short_flags["对象"] is True
    assert short_flags["开始于"] is True
    assert short_flags["现象"] is False

    # the only action button links to SigNoz
    (action,) = [e for e in card["elements"] if e.get("tag") == "action"]
    assert [button["url"] for button in action["actions"]] == [
        "https://signoz.zitian.party"
    ]


def test_alert_card_resolved_is_green_and_nonhttp_url_has_no_button() -> None:
    """Resolved → green header + ✅; a non-http externalURL (e.g. infra2://) drops the button."""
    card = build_feishu_alert_card(
        _sample_alert_payload(
            status="resolved", externalURL="infra2://platform/12.alerting"
        )
    )

    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "✅ [已恢复] ExampleBackendDown · 1 项"
    assert not [e for e in card["elements"] if e.get("tag") == "action"]


def test_card_payloads_use_interactive_msg_type() -> None:
    card = build_feishu_alert_card(_sample_alert_payload())

    webhook = build_feishu_card_payload(card)
    assert webhook == {"msg_type": "interactive", "card": card}

    app = build_feishu_app_card_payload("oc_test", card)
    assert app["receive_id"] == "oc_test"
    assert app["msg_type"] == "interactive"
    assert json.loads(app["content"]) == card  # content is a JSON string


def test_deliver_out_of_band_card_supports_app_webhook_and_fallback(
    monkeypatch,
) -> None:
    import common.alerting as alerting
    from common.alerting import (
        FeishuDeliveryError,
        InvalidFeishuAppConfig,
        deliver_out_of_band_card,
    )

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        alerting,
        "deliver_feishu_app_card",
        lambda **kw: calls.append(("app_card", kw["chat_id"])) or {"code": 0},
    )
    monkeypatch.setattr(
        alerting,
        "deliver_feishu_card",
        lambda url, card, **kw: calls.append(("webhook_card", url)) or {"code": 0},
    )
    monkeypatch.setattr(
        alerting.delivery,
        "_deliver_feishu_webhook",
        lambda url, payload, **kw: (
            calls.append(("webhook_text", payload["content"]["text"])) or {"code": 0}
        ),
    )

    res_app = deliver_out_of_band_card(
        {
            "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_app",
            "INFRA2_OUT_OF_BAND_FEISHU_APP_ID": "cli_x",
            "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET": "sec",
            "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID": "oc_123",
        },
        {"header": {}},
    )
    assert res_app == {"code": 0}
    assert calls[-1] == ("app_card", "oc_123")

    res_wh = deliver_out_of_band_card(
        {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/tok"},
        {"header": {}},
    )
    assert res_wh == {"code": 0}
    assert calls[-1] == (
        "webhook_card",
        "https://open.feishu.cn/open-apis/bot/v2/hook/tok",
    )

    with pytest.raises(InvalidFeishuAppConfig):
        deliver_out_of_band_card({}, {"header": {}})

    def fail_card(*_a, **_k):
        raise FeishuDeliveryError("card rejected")

    monkeypatch.setattr(alerting, "deliver_feishu_card", fail_card)
    res_fb = deliver_out_of_band_card(
        {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/tok"},
        {"header": {}},
        fallback_text="fallback summary",
    )
    assert res_fb == {"code": 0}
    assert calls[-1] == ("webhook_text", "fallback summary")
    with pytest.raises(FeishuDeliveryError):
        deliver_out_of_band_card(
            {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/tok"},
            {"header": {}},
        )


def test_signoz_channel_payload_targets_internal_bridge_with_optional_basic_auth() -> (
    None
):
    """Infra-007 alerting: SigNoz channel points to bridge, not the Feishu secret."""
    payload = build_signoz_channel_payload(
        channel_name="infra2-feishu-alerts-production",
        bridge_url="http://platform-alerting:8080/signoz/webhook",
        basic_auth=BasicAuth(username="signoz", password="secret"),
    )

    webhook = payload["webhook_configs"][0]
    assert payload["name"] == "infra2-feishu-alerts-production"
    assert webhook["url"] == "http://platform-alerting:8080/signoz/webhook"
    assert webhook["send_resolved"] is True
    assert webhook["http_config"]["basic_auth"]["username"] == "signoz"
    assert webhook["http_config"]["basic_auth"]["password"] == "secret"


def test_log_error_alert_rule_uses_signoz_v2_threshold_schema() -> None:
    """Infra-007 alerting: shared app alerts are SigNoz rule payloads."""
    payload = build_signoz_log_alert_rule_payload(
        alert_name="ExampleBackendErrorLogs",
        service_name="example-backend",
        channel_names=["channel-1"],
        summary="example backend emitted error logs",
    )

    assert payload["schemaVersion"] == "v2alpha1"
    assert payload["version"] == "v5"
    assert payload["alertType"] == "LOGS_BASED_ALERT"
    assert payload["ruleType"] == "threshold_rule"
    assert payload["condition"]["selectedQueryName"] == "A"
    threshold = payload["condition"]["thresholds"]["spec"][0]
    assert threshold["op"] == "1"
    assert threshold["matchType"] == "1"
    assert threshold["channels"] == ["channel-1"]
    assert payload["labels"]["service_id"] == "infra/example-backend"
    assert payload["labels"]["environment"] == "production"


def test_log_error_alert_rule_carries_its_query_in_the_v5_queries_envelope() -> None:
    """#906: a rule marked version v5 is evaluated only from compositeQuery.queries[].

    SigNoz v0.105 copies `queries` into the query-range request and skips its v3->v5
    migration for rules already marked v5, so the old `builderQueries` map made this
    rule run no query at all: it could never fire.
    """
    payload = build_signoz_log_alert_rule_payload(
        alert_name="ExampleBackendErrorLogs",
        service_name="example-backend",
        channel_names=["channel-1"],
        summary="example backend emitted error logs",
        environment="staging",
    )

    composite = payload["condition"]["compositeQuery"]
    assert composite["queryType"] == "builder"
    assert "builderQueries" not in composite
    assert composite["queries"] == [
        {
            "type": "builder_query",
            "spec": {
                "name": "A",
                "signal": "logs",
                "stepInterval": 60,
                "aggregations": [{"expression": "count()"}],
                "filter": {
                    "expression": (
                        "resource.service.name = 'example-backend' AND "
                        "resource.deployment.environment.name = 'staging' AND "
                        "severity_text IN ['ERROR', 'CRITICAL', 'FATAL']"
                    )
                },
                "disabled": False,
            },
        }
    ]
    assert payload["condition"]["alertOnAbsent"] is False


def test_signoz_filter_literals_escape_quotes_and_backslashes() -> None:
    """#906: a value cannot close the filter literal early and inject a condition."""
    assert (
        _signoz_filter_literal("finance-report-backend") == "'finance-report-backend'"
    )
    assert _signoz_filter_literal("o'brien") == "'o\\'brien'"
    assert _signoz_filter_literal("a\\' OR 1=1") == "'a\\\\\\' OR 1=1'"


def test_log_error_alert_rule_threshold_semantics_are_configurable() -> None:
    """#906: the error-log rule counts a burst (in_total over the window), not any error."""
    payload = build_signoz_log_alert_rule_payload(
        alert_name="BurstErrorLogs",
        service_name="example-backend",
        channel_names=["channel-1"],
        summary="burst",
        threshold=10,
        match_type="in_total",
        eval_window="15m0s",
    )

    threshold = payload["condition"]["thresholds"]["spec"][0]
    assert threshold["target"] == 10
    assert threshold["matchType"] == "4"
    assert payload["evaluation"]["spec"]["evalWindow"] == "15m0s"
    with pytest.raises(AlertingError, match="match type"):
        build_signoz_log_alert_rule_payload(
            alert_name="BadMatch",
            service_name="example-backend",
            channel_names=["channel-1"],
            summary="bad",
            match_type="sometiems",
        )


def test_metric_alert_rule_uses_signoz_v5_promql_schema() -> None:
    """#1106: finance_report metric alerts render as SigNoz v5 PromQL rules."""
    payload = build_signoz_metric_alert_rule_payload(
        alert_name="FinanceReportHigh5xxRate",
        promql="sum(rate(http_server_request_count[5m]))",
        channel_names=["channel-1"],
        summary="backend 5xx rate high",
        severity="critical",
        threshold=0.05,
        threshold_unit="%",
        match_type="all_times",
    )

    assert payload["schemaVersion"] == "v2alpha1"
    assert payload["version"] == "v5"
    assert payload["alertType"] == "METRIC_BASED_ALERT"
    assert payload["ruleType"] == "promql_rule"
    assert payload["condition"]["selectedQueryName"] == "A"
    composite = payload["condition"]["compositeQuery"]
    assert composite["queryType"] == "promql"
    assert "builderQueries" not in composite
    assert "promQueries" not in composite
    assert composite["queries"] == [
        {
            "type": "promql",
            "spec": {
                "name": "A",
                "query": "sum(rate(http_server_request_count[5m]))",
                "legend": "",
                "disabled": False,
            },
        }
    ]
    threshold = payload["condition"]["thresholds"]["spec"][0]
    assert threshold["op"] == "1"
    assert threshold["matchType"] == "2"
    assert threshold["target"] == 0.05
    assert threshold["targetUnit"] == "%"
    assert threshold["channels"] == ["channel-1"]


def test_metric_alert_rule_rejects_unknown_threshold_semantics() -> None:
    """#1106 review: typos in SigNoz threshold semantics fail closed."""
    with pytest.raises(AlertingError, match="threshold op"):
        build_signoz_metric_alert_rule_payload(
            alert_name="BadOp",
            promql="sum(up)",
            channel_names=["channel-1"],
            summary="bad op",
            op="abvoe",
        )

    with pytest.raises(AlertingError, match="match type"):
        build_signoz_metric_alert_rule_payload(
            alert_name="BadMatch",
            promql="sum(up)",
            channel_names=["channel-1"],
            summary="bad match type",
            match_type="sometiems",
        )


def test_signoz_feishu_channel_name_is_per_environment_and_defaults_to_production() -> (
    None
):
    """#973: the one name the channel is created under and every rule binds to."""
    assert signoz_feishu_channel_name() == "infra2-feishu-alerts-production"
    assert signoz_feishu_channel_name(None) == "infra2-feishu-alerts-production"
    assert signoz_feishu_channel_name("  ") == "infra2-feishu-alerts-production"
    assert signoz_feishu_channel_name("staging") == "infra2-feishu-alerts-staging"
    payload = build_signoz_channel_payload(
        channel_name=signoz_feishu_channel_name("staging"),
        bridge_url="http://platform-alerting:8080/signoz/webhook",
    )
    assert payload["name"] == "infra2-feishu-alerts-staging"


def test_signoz_rule_channels_normalises_names_and_refuses_none() -> None:
    """#973: names are stripped and de-duplicated; no usable name is an error, because
    SigNoz rejects a rule with no channel and never delivers one bound to a non-name."""
    assert signoz_rule_channels([" a ", "b", "a", ""]) == ["a", "b"]
    for bad in ([], [""], [None], [1], "a"):
        with pytest.raises(AlertingError):
            signoz_rule_channels(bad)


def test_signoz_api_response_helpers_find_channel_and_rule_ids() -> None:
    """Infra-007 alerting: SigNoz API parsing tolerates common envelopes."""
    channels_response = {
        "status": "success",
        "data": {"channels": [{"name": "infra2-feishu-alerts-production", "id": "c1"}]},
    }
    rules_response = {
        "data": {"rules": [{"alert": "ExampleBackendErrorLogs", "id": "r1"}]}
    }

    assert (
        find_signoz_channel_id(channels_response, "infra2-feishu-alerts-production")
        == "c1"
    )
    assert find_signoz_rule_id(rules_response, "ExampleBackendErrorLogs") == "r1"
    assert find_signoz_channel_id({"data": []}, "missing") is None


def test_feishu_app_delivery_fetches_token_then_sends_message(monkeypatch) -> None:
    """Infra-007 alerting: app bot mode uses tenant token and chat_id."""
    requests = []

    class FakeResponse:
        def __init__(self, payload: dict):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "tenant_access_token": "tenant-token"})
        return FakeResponse({"code": 0, "data": {"message_id": "om_test"}})

    monkeypatch.setattr("common.alerting.urlopen", fake_urlopen)

    result = deliver_feishu_app_card(
        app_id="cli_test",
        app_secret="secret",
        chat_id="oc_test",
        card={"header": {}},
    )

    assert result["data"]["message_id"] == "om_test"
    assert len(requests) == 2
    send_request, _timeout = requests[1]
    assert "receive_id_type=chat_id" in send_request.full_url
    assert send_request.headers["Authorization"] == "Bearer tenant-token"


def test_alerting_platform_service_contract_files_exist() -> None:
    """Infra-007 alerting: bridge service has deploy, Vault, and docs surfaces."""
    base = ROOT / "platform/12.alerting"
    required = [
        "Dockerfile",
        "README.md",
        "app.py",
        "compose.yaml",
        "deploy.py",
        "secrets.ctmpl",
        "shared_tasks.py",
        "vault-agent.hcl",
        "vault-policy.hcl",
    ]
    for name in required:
        assert (base / name).exists(), name

    compose = (base / "compose.yaml").read_text(encoding="utf-8")
    assert "platform-alerting-vault-agent${ENV_SUFFIX}" in compose
    assert "platform-alerting${ENV_SUFFIX}" in compose
    assert "while [ ! -s /secrets/.env ]" in compose
    assert 'max_wait="300"' in compose
    assert "condition: service_healthy" not in compose
    assert "open-apis/bot/v2/hook" not in compose
    assert "secrets:/secrets:ro" in compose

    deploy = (base / "deploy.py").read_text(encoding="utf-8")
    # PR-E: the manifest-driven supply (common/security/supply.py) replaced the bespoke
    # 1Password → Vault copy; the Deployer hook is what pre_compose calls.
    assert "apply_secret_supply" in deploy
    assert "_sync_1password_to_vault" not in deploy
    assert 'credential_type="root_vars"' not in deploy
    assert "INFRA_PROBE_HEARTBEAT_URL" in deploy
    assert "INFRA_PROBE_HEARTBEAT_TOKEN" in deploy

    ctmpl = (base / "secrets.ctmpl").read_text(encoding="utf-8")
    assert "INFRA_PROBE_HEARTBEAT_URL" in ctmpl
    assert "INFRA_PROBE_HEARTBEAT_TOKEN" in ctmpl


# import root -> the pip-installable name the Dockerfile must list. common.observability.probes
# gained real infra2_sdk.runtime.postgres/s3 imports in #600 (psycopg/boto3-backed
# probes) without anyone updating this Dockerfile's now-false "stdlib-only" comment —
# the image shipped, and the probe-runner crash-looped on the first postgres/s3
# ProbeSpec dispatch in staging. Scanning actual imports (not a hardcoded list of
# "known" third-party names) is the point: a NEW import here must be added to this
# map or the test fails, so the Dockerfile can never again silently drift stale.
_IMPORT_TO_PIP_NAME = {
    "httpx": "httpx",
    "dotenv": "python-dotenv",
    "rich": "rich",
    "invoke": "invoke",
    "infra2_sdk": "infra2-sdk",
    "psycopg": "psycopg",
    "boto3": "boto3",
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "uvicorn": "uvicorn",
}


def _third_party_import_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    first_party = {"common", "tools", "__future__"}
    return {
        r for r in roots if r not in first_party and r not in sys.stdlib_module_names
    }


def test_alerting_dockerfile_installs_every_import_the_packaged_code_needs() -> None:
    """Infra-007 alerting / #600 regression: the image's pip install line must cover
    every third-party import reachable from what it actually COPYs and runs
    (app.py's entrypoint, and infra_probe_runner.py's import of common.observability.probes) —
    caught live via a staging ModuleNotFoundError crash loop, not by any test."""
    base = ROOT / "platform/12.alerting"
    dockerfile = (base / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install" in dockerfile, "expected a pip install step in the Dockerfile"

    required_roots: set[str] = set()
    for path in (
        base / "app.py",
        ROOT / "common/observability/probes.py",
        ROOT / "common/observability/infra_probe_runner.py",
    ):
        required_roots |= _third_party_import_roots(path.read_text(encoding="utf-8"))

    missing_mapping = required_roots - set(_IMPORT_TO_PIP_NAME)
    assert not missing_mapping, (
        f"unmapped third-party import(s) {missing_mapping} in platform/12.alerting's "
        "packaged code — add each to _IMPORT_TO_PIP_NAME above with its pip package name"
    )

    unsatisfied = {
        root for root in required_roots if _IMPORT_TO_PIP_NAME[root] not in dockerfile
    }
    assert not unsatisfied, (
        f"platform/12.alerting/Dockerfile's pip install does not cover: "
        f"{[_IMPORT_TO_PIP_NAME[r] for r in sorted(unsatisfied)]} — "
        f"required by an import in app.py / common/observability/probes.py / "
        f"common/observability/infra_probe_runner.py"
    )


def test_alerting_ssot_catalog_includes_dokploy_control_plane() -> None:
    """Infra-007 alerting: Dokploy control-plane alerts are explicitly cataloged."""
    ssot = (ROOT / "docs/ssot/ops.observability.md").read_text(encoding="utf-8")

    assert "Dokploy" in ssot
    assert "deployment control-plane" in ssot
    assert "app health alerts remain app-owned" in ssot


def test_alerting_shared_tasks_are_invoke_tasks() -> None:
    """Infra-007 alerting: invoke exposes channel payload and test-send tasks."""
    fake_invoke = types.ModuleType("invoke")
    fake_invoke.task = lambda func=None, **_kwargs: func if func else (lambda f: f)
    sys.modules.setdefault("invoke", fake_invoke)

    path = ROOT / "platform/12.alerting/shared_tasks.py"
    spec = importlib.util.spec_from_file_location("alerting_shared_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "status")
    assert hasattr(module, "print_channel_payload")
    assert hasattr(module, "ensure_log_error_rule")
    assert hasattr(module, "print_log_error_rule_payload")
    assert hasattr(module, "test_feishu")

    source = path.read_text(encoding="utf-8")
    assert 'shlex.quote(f"SIGNOZ-API-KEY: {api_key}")' in source
    assert "-H {api_key_header}" in source
    assert '"is_ready": result.ok' in source
    assert "finance_report" not in source
    assert "FinanceReport" not in source


def test_alerting_app_request_guards_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Infra-007 alerting: HTTP auth/body guards match review feedback."""
    clear_telemetry_env(monkeypatch)
    path = ROOT / "platform/12.alerting/app.py"
    spec = importlib.util.spec_from_file_location("alerting_app_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._parse_content_length("1") == 1
    with pytest.raises(module.RequestBodyError) as missing:
        module._parse_content_length(None)
    assert missing.value.status_code == 411
    assert missing.value.payload == {"status": "length_required"}

    with pytest.raises(module.RequestBodyError) as invalid:
        module._parse_content_length("abc")
    assert invalid.value.status_code == 400
    assert invalid.value.payload == {"status": "invalid_content_length"}

    with pytest.raises(module.RequestBodyError) as empty:
        module._parse_content_length("0")
    assert empty.value.status_code == 400
    assert empty.value.payload == {"status": "empty_payload"}

    with pytest.raises(module.RequestBodyError) as oversized:
        module._parse_content_length(str(module.MAX_BODY_BYTES + 1))
    assert oversized.value.status_code == 413
    assert oversized.value.payload == {"status": "payload_too_large"}

    source = path.read_text(encoding="utf-8")
    assert "secrets.compare_digest(header, expected)" in source


# ---------------------------------------------------------------------------
# #903: delivery=report routing through the bridge


def _bridge_under_test(monkeypatch, *, mode: str = "feishu_app", report_chat: str = ""):
    """The real bridge handler on an ephemeral local port, with Feishu delivery
    captured instead of sent. Returns (post, sent, stop)."""

    clear_telemetry_env(monkeypatch)
    path = ROOT / "platform/12.alerting/app.py"
    spec = importlib.util.spec_from_file_location("alerting_app_routing", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for key in ("BRIDGE_BASIC_AUTH_USERNAME", "BRIDGE_BASIC_AUTH_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALERT_DELIVERY_MODE", mode)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("FEISHU_CHAT_ID", "oc_pager")
    monkeypatch.setenv(
        "FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
    )
    if report_chat:
        monkeypatch.setenv("FEISHU_REPORT_CHAT_ID", report_chat)
    else:
        monkeypatch.delenv("FEISHU_REPORT_CHAT_ID", raising=False)

    sent: list[dict] = []

    def app_card(*, chat_id, card, **_kwargs):
        sent.append(
            {"chat": chat_id, "title": card["header"]["title"]["content"], "card": card}
        )
        return {"code": 0}

    def webhook_card(url, card, **_kwargs):
        sent.append(
            {
                "chat": "webhook",
                "title": card["header"]["title"]["content"],
                "card": card,
            }
        )
        return {"code": 0}

    monkeypatch.setattr(module, "deliver_feishu_app_card", app_card)
    monkeypatch.setattr(module, "deliver_feishu_card", webhook_card)
    from starlette.testclient import TestClient

    client = TestClient(module.fastapi_app)

    def post(payload: dict) -> int:
        body = json.dumps(payload).encode("utf-8")
        response = client.post(
            "/signoz/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        return response.status_code

    def stop() -> None:
        pass

    return post, sent, stop


def _payload(alertname: str, *, report: bool) -> dict:
    from common.alerting import mark_report_payload

    payload = {
        "status": "firing",
        "commonLabels": {"alertname": alertname, "severity": "warning"},
        "alerts": [{"status": "firing", "labels": {"alertname": alertname}}],
    }
    return mark_report_payload(payload) if report else payload


def test_bridge_sends_a_report_to_the_report_chat_and_a_page_to_the_pager(
    monkeypatch,
) -> None:
    """#903: `delivery=report` goes to FEISHU_REPORT_CHAT_ID; everything else still
    reaches the pager chat. #905: the report is titled `[报告]` there too, the page is
    not."""
    post, sent, stop = _bridge_under_test(monkeypatch, report_chat="oc_reports")
    try:
        assert post(_payload("InfraProbeChronic", report=True)) == 202
        assert post(_payload("InfraServiceProbeFailed", report=False)) == 202
    finally:
        stop()

    assert [s["chat"] for s in sent] == ["oc_reports", "oc_pager"]
    assert [s["title"] for s in sent] == [
        "[报告] InfraProbeChronic · 1 项",
        "🟡 [P2 告警] InfraServiceProbeFailed · 1 项",
    ]


def test_staging_signoz_payload_is_a_report(monkeypatch) -> None:
    """#1649: a SigNoz firing labelled `environment=staging` goes to the report chat
    as a report; the same payload with `environment=production` is a page."""
    from common.alerting import is_report_payload

    def firing(environment: str) -> dict:
        labels = {
            "alertname": "PlatformTodoErrorLogs",
            "severity": "error",
            "environment": environment,
        }
        return {
            "status": "firing",
            "commonLabels": labels,
            "alerts": [{"status": "firing", "labels": labels}],
        }

    assert is_report_payload(firing("staging")) is True
    assert is_report_payload(firing("production")) is False
    post, sent, stop = _bridge_under_test(monkeypatch, report_chat="oc_reports")
    try:
        assert post(firing("staging")) == 202
        assert post(firing("production")) == 202
    finally:
        stop()

    assert [s["chat"] for s in sent] == ["oc_reports", "oc_pager"]
    assert sent[0]["title"].startswith("[报告]")
    assert not sent[1]["title"].startswith("[报告]")


def test_bridge_without_a_report_chat_marks_reports_in_the_pager_chat(
    monkeypatch,
) -> None:
    """#903: with FEISHU_REPORT_CHAT_ID unset nothing is lost — the report lands in the
    pager chat, titled [报告] so it does not read as a page. A webhook is bound to
    one chat, so webhook mode always takes this fallback."""
    for mode, pager in (("feishu_app", "oc_pager"), ("feishu_webhook", "webhook")):
        post, sent, stop = _bridge_under_test(monkeypatch, mode=mode)
        try:
            assert post(_payload("ContainerBreakdownChronic", report=True)) == 202
            assert post(_payload("ContainerBreakdown", report=False)) == 202
        finally:
            stop()

        assert [s["chat"] for s in sent] == [pager, pager], mode
        assert sent[0]["title"] == "[报告] ContainerBreakdownChronic · 1 项", mode
        assert sent[1]["title"] == "🟡 [P2 告警] ContainerBreakdown · 1 项", mode


def test_only_staging_and_preview_environments_are_report_only() -> None:
    """#903 review: the report-only decision is an allowlist over the normalized
    environment. `prod`, `PRODUCTION `, unset and garbage page; staging and the
    preview slots report."""
    from common.alerting import is_report_only_environment

    pages = [None, "", "production", "PRODUCTION ", "prod", "garbage", "x/y", "pr"]
    reports = [
        "staging",
        " STAGING",
        "stg",
        "preview",
        "pr-5",
        "branch-main",
    ]
    assert [v for v in pages if is_report_only_environment(v)] == []
    assert [v for v in reports if not is_report_only_environment(v)] == []


# ---------------------------------------------------------------------------
# #905 review: the bridge never drops a page, and budgets for its delivery mode


def _unrenderable(starts_at) -> dict:
    return {
        "status": "firing",
        "commonLabels": {
            "alertname": "InfraServiceProbeFailed",
            "severity": "critical",
        },
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "service_id": "platform/vault",
                    "component": "vault-http",
                    "environment": "production",
                },
                "annotations": {"summary": "vault-http probe failed"},
                "startsAt": starts_at,
            }
        ],
    }


def test_the_bridge_delivers_a_page_with_a_malformed_time(monkeypatch) -> None:
    """Epoch milliseconds, a far-future time and JSON Infinity used to raise in the
    renderer, and the bridge only caught AlertingError: the page was lost."""
    post, sent, stop = _bridge_under_test(monkeypatch)
    try:
        for starts_at in (1_790_236_800_000, "9999-12-31T23:59:59Z", float("inf")):
            assert post(_unrenderable(starts_at)) == 202
    finally:
        stop()

    assert [s["title"] for s in sent] == [
        "🔴 [P0 告警] InfraServiceProbeFailed · production · 1 项"
    ] * 3


def test_the_bridge_delivers_a_page_it_cannot_render(monkeypatch) -> None:
    """Any renderer failure still pages, as the minimal card."""
    import common.alerting as alerting

    def broken(*_args, **_kwargs):
        raise KeyError("renderer bug")

    monkeypatch.setattr(alerting, "pager_message_from_payload", broken)
    post, sent, stop = _bridge_under_test(monkeypatch)
    try:
        assert post(_unrenderable("2026-09-24T08:00:00Z")) == 202
    finally:
        stop()

    (page,) = sent
    assert page["title"] == "🔴 [P0 告警] InfraServiceProbeFailed · 简化卡片"
    body = "\n".join(e["text"]["content"] for e in page["card"]["elements"])
    assert (
        "P0 · production · platform/vault · vault-http · vault-http probe failed"
        in body
    )


def test_the_bridge_budgets_the_card_for_its_delivery_mode(monkeypatch) -> None:
    """Production and staging run the app bot (30 KB); the webhook takes 20 KB. The
    bridge sizes each card for the mode it sends through."""
    from common.alerting import card_body_bytes, card_budget

    chinese = "服务不可用" * 200
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "ContainerBreakdown", "severity": "critical"},
        "alerts": [
            {
                "labels": {
                    "service_id": f"infra/s{index}",
                    "environment": "production",
                },
                "annotations": {"symptom": chinese, "log_tail": chinese},
            }
            for index in range(40)
        ],
    }
    sizes = {}
    for mode in ("feishu_app", "feishu_webhook"):
        post, sent, stop = _bridge_under_test(monkeypatch, mode=mode)
        try:
            assert post(payload) == 202
        finally:
            stop()
        (page,) = sent
        sizes[mode] = card_body_bytes(page["card"], mode)

    assert sizes["feishu_webhook"] <= card_budget("feishu_webhook")
    # the app bot's card uses the room it has, which a webhook would not take
    assert (
        card_budget("feishu_webhook") < sizes["feishu_app"] <= card_budget("feishu_app")
    )


def test_alerting_fastapi_endpoints_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify alerting service FastAPI endpoints (/livez, /readyz, /health, /signoz/webhook)."""
    import importlib.util
    from starlette.testclient import TestClient

    clear_telemetry_env(monkeypatch)
    path = ROOT / "platform/12.alerting/app.py"
    spec = importlib.util.spec_from_file_location("alerting_fastapi_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("ALERT_DELIVERY_MODE", "feishu_app")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("FEISHU_CHAT_ID", "oc_pager")
    monkeypatch.delenv("BRIDGE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("BRIDGE_BASIC_AUTH_PASSWORD", raising=False)

    monkeypatch.setattr(module, "_feishu_reachable", lambda: True)
    monkeypatch.setattr(module, "deliver_feishu_app_card", lambda **kwargs: {"code": 0})

    client = TestClient(module.fastapi_app)

    # 1. /livez
    live_res = client.get("/livez")
    assert live_res.status_code == 200
    assert live_res.json() == {"status": "alive", "service": "platform-alerting"}

    # 2. /readyz
    ready_res = client.get("/readyz")
    assert ready_res.status_code == 200
    assert ready_res.json()["status"] == "ready"

    # 3. /health is the SDK report (#1209); the delivery config is its one check
    health_res = client.get("/health")
    assert health_res.status_code == 200
    health = health_res.json()
    assert health["status"] == "healthy"
    assert health["service"] == "platform-alerting"
    assert health["reasons"] == []
    assert health["checks"]["database"]["status"] == "present"
    assert health["checks"]["database"]["detail"] == "delivery config valid"

    # 4. /health/feishu
    feishu_res = client.get("/health/feishu")
    assert feishu_res.status_code == 200
    assert feishu_res.json()["status"] == "ok"
    assert feishu_res.json()["reachable"] is True

    # 5. POST /signoz/webhook
    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "TestAlert", "severity": "warning"},
        "alerts": [{"status": "firing", "labels": {"alertname": "TestAlert"}}],
    }
    hook_res = client.post("/signoz/webhook", json=payload)
    assert hook_res.status_code == 202
    assert hook_res.json()["status"] == "accepted"

    # 6. Basic-Auth rejection on POST /signoz/webhook (401)
    monkeypatch.setenv("BRIDGE_BASIC_AUTH_USERNAME", "test_u")
    monkeypatch.setenv("BRIDGE_BASIC_AUTH_PASSWORD", "test_p")
    unauth_res = client.post("/signoz/webhook", json=payload)
    assert unauth_res.status_code == 401
    assert unauth_res.json() == {"status": "unauthorized"}

    # With valid basic auth header (202)
    import base64

    valid_auth = "Basic " + base64.b64encode(b"test_u:test_p").decode("ascii")
    auth_ok_res = client.post(
        "/signoz/webhook",
        json=payload,
        headers={"Authorization": valid_auth},
    )
    assert auth_ok_res.status_code == 202

    monkeypatch.delenv("BRIDGE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("BRIDGE_BASIC_AUTH_PASSWORD", raising=False)

    # 7. Empty payload (400)
    empty_res = client.post("/signoz/webhook", content=b"")
    assert empty_res.status_code == 400
    assert empty_res.json()["status"] == "empty_payload"

    # 8. Payload too large (413)
    huge_res = client.post(
        "/signoz/webhook",
        content=b"x" * (module.MAX_BODY_BYTES + 1),
        headers={"Content-Length": str(module.MAX_BODY_BYTES + 1)},
    )
    assert huge_res.status_code == 413
    assert huge_res.json()["status"] == "payload_too_large"

    # 9. Invalid JSON (400)
    invalid_json_res = client.post("/signoz/webhook", content=b"not valid json")
    assert invalid_json_res.status_code == 400
    assert invalid_json_res.json()["status"] == "invalid_json"

    # 10. Non-dict JSON payload (400)
    non_dict_res = client.post("/signoz/webhook", content=b'["array", "not", "dict"]')
    assert non_dict_res.status_code == 400
    assert non_dict_res.json()["status"] == "invalid_payload"

    # 11. Delivery failure (502)
    def _fail_deliver(**kwargs: object) -> object:
        raise module.AlertingError("mock delivery failure")

    monkeypatch.setattr(module, "deliver_feishu_app_card", _fail_deliver)
    fail_res = client.post("/signoz/webhook", json=payload)
    assert fail_res.status_code == 502
    assert fail_res.json()["status"] == "delivery_failed"


def _load_alerting_app(name: str, monkeypatch: pytest.MonkeyPatch):
    import importlib.util

    clear_telemetry_env(monkeypatch)

    spec = importlib.util.spec_from_file_location(
        name, ROOT / "platform/12.alerting/app.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alerting_health_and_readyz_fail_on_a_bad_delivery_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad delivery config must stop the bridge from reporting healthy (#1209).

    The probe runner, the container healthcheck and the out-of-band watchdog read only the
    status code of /health. The SDK report gives 503 and names the reason.
    """
    from starlette.testclient import TestClient

    module = _load_alerting_app("alerting_health_failure_test", monkeypatch)
    client = TestClient(module.fastapi_app)
    monkeypatch.setenv("ALERT_DELIVERY_MODE", "no-such-mode")

    health_res = client.get("/health")
    assert health_res.status_code == 503
    health = health_res.json()
    assert health["status"] == "unhealthy"
    assert health["checks"]["database"]["status"] == "absent"
    assert health["reasons"] == [
        "database: Unsupported ALERT_DELIVERY_MODE: no-such-mode"
    ]

    ready_res = client.get("/readyz")
    assert ready_res.status_code == 503
    assert ready_res.json()["status"] == "not_ready"


def test_alerting_health_route_is_the_sdk_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge keeps the SDK /health and holds no copy of its own (#1209)."""
    module = _load_alerting_app("alerting_one_health_route_test", monkeypatch)
    (route,) = [
        route
        for route in module.fastapi_app.router.routes
        if getattr(route, "path", None) == "/health"
    ]
    assert route.endpoint.__module__ == "infra2_sdk.fastapi"


def test_alerting_webhook_delivery_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page in flight must not stop /health from answering (#1222).

    The webhook handler is `async def`. A direct call to the sync `_deliver` holds the event
    loop for the whole Feishu call (10 s timeout). The container healthcheck (interval 30 s,
    timeout 5 s, retries 3) can then restart the bridge while it delivers a page.

    Both requests go through ONE ASGI client on ONE event loop. A `TestClient` per request
    gives each request its own loop in its own thread, so a blocked loop would go unseen.

    The fake delivery blocks its thread until /health has answered, as `urlopen` blocks on
    a socket. It gives up after `block_seconds`. With a blocked loop /health cannot answer
    in that time, so the delivery sees no answer and the test fails. The wait uses an event
    and not a fixed sleep: the result does not depend on how fast the machine is.
    """
    import asyncio
    import threading

    import httpx

    module = _load_alerting_app("alerting_event_loop_test", monkeypatch)
    for key in ("BRIDGE_BASIC_AUTH_USERNAME", "BRIDGE_BASIC_AUTH_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALERT_DELIVERY_MODE", "feishu_webhook")
    monkeypatch.setenv(
        "FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
    )

    block_seconds = 3.0
    delivery_started = threading.Event()
    health_answered = threading.Event()
    seen: dict[str, object] = {}

    def blocking_deliver(card, *, report=False):
        delivery_started.set()
        seen["health_answered_during_delivery"] = health_answered.wait(block_seconds)
        return {"code": 0}

    monkeypatch.setattr(module, "_deliver", blocking_deliver)

    payload = {
        "status": "firing",
        "commonLabels": {"alertname": "HighCPU", "severity": "critical"},
        "alerts": [{"status": "firing", "labels": {"alertname": "HighCPU"}}],
    }
    body = json.dumps(payload).encode("utf-8")

    async def scenario():
        transport = httpx.ASGITransport(app=module.fastapi_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://alerting.test"
        ) as client:

            async def webhook():
                return await client.post(
                    "/signoz/webhook",
                    content=body,
                    headers={"Content-Type": "application/json"},
                )

            async def health():
                # Ask only after the delivery is in flight. A blocked loop cannot run
                # this poll, so a delivery that holds the loop is seen at its latest.
                deadline = asyncio.get_running_loop().time() + 30
                while not delivery_started.is_set():
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.01)
                response = await client.get("/health")
                health_answered.set()
                return response

            return await asyncio.gather(webhook(), health())

    webhook_response, health_response = asyncio.run(scenario())

    assert health_response.status_code == 200, health_response.text
    assert seen["health_answered_during_delivery"] is True, (
        "the delivery held the event loop: /health answered only after the delivery ended"
    )
    assert webhook_response.status_code == 202, webhook_response.text
    assert webhook_response.json() == {"status": "accepted", "feishu": {"code": 0}}


# The bridge pages people. A bad OTEL value must be logged and must never stop it (#1209).
# `strict_otel=True` would raise while the module loads, so the test loads it in a child
# process, the way the container does, with values the SDK refuses in strict mode.
_FAIL_OPEN_SCRIPT = textwrap.dedent(
    """
    import importlib.util, json, os, sys

    sys.path.append(sys.argv[1])
    from starlette.testclient import TestClient

    spec = importlib.util.spec_from_file_location("alerting_fail_open", sys.argv[2])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._feishu_reachable = lambda: True
    client = TestClient(module.fastapi_app)
    answers = {
        path: client.get(path).status_code
        for path in ("/livez", "/health", "/health/feishu")
    }
    sys.stdout.write(json.dumps(answers) + "\\n")
    sys.stdout.flush()
    sys.stderr.flush()
    # the OTLP exporter points at a closed port: a normal exit would wait on its flush
    os._exit(0)
    """
)


@pytest.mark.parametrize(
    ("environment", "attributes", "logged"),
    [
        ("bogus", "service.name=platform-alerting", True),
        ("staging", "this-is-not-a-pair", False),
        ("bogus", "this-is-not-a-pair", True),
    ],
    ids=["unknown-env", "malformed-attributes", "both"],
)
def test_alerting_bridge_serves_with_a_bad_otel_value(
    environment: str, attributes: str, logged: bool
) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OTEL_") and key not in ("ENV", "ENVIRONMENT")
    }
    env.update(
        {
            "ENV": environment,
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
            "OTEL_SERVICE_NAME": "platform-alerting",
            "OTEL_RESOURCE_ATTRIBUTES": attributes,
            "ALERT_DELIVERY_MODE": "feishu_app",
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "app-secret",
            "FEISHU_CHAT_ID": "oc_pager",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _FAIL_OPEN_SCRIPT,
            str(ROOT),
            str(ROOT / "platform/12.alerting/app.py"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 0, (
        f"the bridge stopped on a bad OTEL value\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert json.loads(lines[-1]) == {
        "/livez": 200,
        "/health": 200,
        "/health/feishu": 200,
    }
    if logged:
        assert "Could not initialize OpenTelemetry" in result.stderr, result.stderr


def test_alerting_bridge_health_timeout_is_below_the_container_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK default of 10 s is above the 3 s the container healthcheck waits. A slow
    check would then make the healthcheck fail on its own timeout, with no reason (#1209)."""
    import infra2_sdk.fastapi as sdk_fastapi

    seen: list[dict] = []
    original = sdk_fastapi.init_service

    def recording(app, name, **kwargs):
        seen.append(kwargs)
        return original(app, name, **kwargs)

    monkeypatch.setattr(sdk_fastapi, "init_service", recording)
    _load_alerting_app("alerting_health_timeout_test", monkeypatch)

    (kwargs,) = seen
    timeout = kwargs.get("check_timeout_seconds")
    assert isinstance(timeout, (int, float)), (
        "the bridge must pass check_timeout_seconds"
    )
    healthcheck = compose_services(ROOT / "platform/12.alerting/compose.yaml")[
        "feishu-alert-bridge"
    ]["healthcheck"]
    command = " ".join(healthcheck["test"])
    (urlopen_timeout,) = re.findall(
        r"urlopen\('http://127\.0\.0\.1:8080/health', timeout=(\d+)\)", command
    )
    docker_timeout = float(healthcheck["timeout"].removesuffix("s"))
    assert 0 < timeout < float(urlopen_timeout) <= docker_timeout, (
        timeout,
        urlopen_timeout,
        docker_timeout,
    )


def test_breakdown_watch_rebreak_suppression_ceiling_escalates() -> None:
    """Escalate alert when re-break count exceeds max_suppressed_rebreaks ceiling."""
    from common.observability.watchers.breakdown_watch import (
        Breakdown,
        RE_BREAK_SUPPRESSION_CEILING,
        evaluate,
    )
    from infra2_sdk.observability import ConsecutiveObservationState

    name = "test-worker"
    b = Breakdown(
        container=name,
        state="restarting",
        reason="crashloop",
        detail="exit 1",
    )
    chronic: dict = {}
    chronic_context: dict = {}
    container_state: dict = {}
    resolved_at = {name: 100.0}
    now = 200.0
    stay_resolved_seconds = 3600
    failure_threshold = 3

    for i in range(1, RE_BREAK_SUPPRESSION_CEILING + 1):
        state = container_state.setdefault(name, ConsecutiveObservationState())
        state.bad_streak = failure_threshold - 1
        state.active = False
        result = evaluate(
            name=name,
            b=b,
            container_state=container_state,
            resolved_at=resolved_at,
            chronic=chronic,
            chronic_context=chronic_context,
            now=now,
            wall=now,
            failure_threshold=failure_threshold,
            recovery_threshold=5,
            renotify=0,
            stay_resolved_seconds=stay_resolved_seconds,
            max_suppressed_rebreaks=RE_BREAK_SUPPRESSION_CEILING,
        )
        assert result is None, f"re-break #{i} must be suppressed"
        assert chronic[name] == i

    state = container_state[name]
    state.bad_streak = failure_threshold - 1
    state.active = False
    escalated = evaluate(
        name=name,
        b=b,
        container_state=container_state,
        resolved_at=resolved_at,
        chronic=chronic,
        chronic_context=chronic_context,
        now=now,
        wall=now,
        failure_threshold=failure_threshold,
        recovery_threshold=5,
        renotify=0,
        stay_resolved_seconds=stay_resolved_seconds,
        max_suppressed_rebreaks=RE_BREAK_SUPPRESSION_CEILING,
    )
    assert escalated is not None, "re-break exceeding ceiling must escalate"
    assert escalated.container == name
    assert name not in chronic
    assert name not in chronic_context


# AC1 of #1648: text leaves this repository only through the private fallback and the
# thread-reply helper. Each allowed line is (file, stripped text, occurrences).
_TEXT_SEND_PATTERN = (
    r"deliver_feishu_(app_)?text|deliver_out_of_band_text|build_feishu_text_payload"
    r"|msg_type.{0,12}text"
)
_ALLOWED_TEXT_SENDS = {
    # reply_feishu_app_text: the rollback follow-up under the release card (#1370)
    ("common/alerting/delivery.py", '"msg_type": "text",'): 2,
    # _deliver_text_fallback, the webhook payload
    (
        "common/alerting/delivery.py",
        '{"msg_type": "text", "content": {"text": message}},',
    ): 1,
}


def test_only_the_private_fallback_sends_text() -> None:
    """A text sender that comes back (a public one, or a worker body) fails here."""
    import collections

    found = subprocess.run(
        [
            "git",
            "grep",
            "-nE",
            _TEXT_SEND_PATTERN,
            "--",
            "common",
            "tools",
            "cloudflare",
        ]
        + [":!common/tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert found.returncode in (0, 1), found.stderr
    seen: collections.Counter[tuple[str, str]] = collections.Counter()
    for line in found.stdout.splitlines():
        path, _number, text = line.split(":", 2)
        seen[(path, text.strip())] += 1
    assert dict(seen) == _ALLOWED_TEXT_SENDS


def test_the_text_fallback_cuts_to_the_message_limit_and_is_private(
    monkeypatch,
) -> None:
    from common.alerting import delivery

    assert not hasattr(delivery, "deliver_out_of_band_text")
    sent: list[dict] = []
    monkeypatch.setattr(
        delivery,
        "_deliver_feishu_webhook",
        lambda url, payload, **kw: sent.append(payload),
    )
    env = {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/tok"}
    delivery._deliver_text_fallback(env, "x" * (MAX_MESSAGE_CHARS + 100), timeout=1.0)

    text = sent[0]["content"]["text"]
    assert len(text) <= MAX_MESSAGE_CHARS
    assert text.endswith("\n...[truncated]")


def _assert_card_standard(card: dict) -> None:
    """#1648 card standard: a title of at most 40 characters, a symptom first, at most
    12 body lines, at most one link."""
    title = card["header"]["title"]["content"]
    body = "\n".join(
        element["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "div" and "text" in element
    )
    lines = [line for line in body.splitlines() if line.strip()]
    assert 0 < len(title) <= 40
    assert 0 < len(lines) <= 12
    assert len(re.findall(r"https?://", body)) <= 1
    assert card["header"]["template"] in {"red", "orange", "yellow", "green", "blue"}


def test_text_to_card_follows_the_card_standard() -> None:
    from common.alerting import text_to_card

    message = "\n".join(
        ["🚨 production platform-todo canary is failing for 3 runs in a row"]
        + [f"detail {n}" for n in range(30)]
        + ["Run: https://github.com/wangzitian0/infra2/actions/runs/1"]
    )
    card = text_to_card(message, template="red")

    _assert_card_standard(card)
    body = card["elements"][0]["text"]["content"]
    assert body.splitlines()[0] == message.splitlines()[0]  # a cut title keeps its line
    assert "…及另外" in body
    assert "fields" not in json.dumps(card)  # no empty KPI block


def test_report_card_without_kpi_fields_has_no_kpi_block() -> None:
    from common.alerting import build_feishu_report_card

    card = build_feishu_report_card(
        title="t", template="blue", kpi_fields=(), sections=["body"]
    )
    assert [element["tag"] for element in card["elements"]] == ["div"]


def test_deliver_infra2_report_sends_a_report_card(monkeypatch) -> None:
    import common.alerting as alerting

    sent: list[dict] = []
    monkeypatch.setattr(
        alerting, "deliver_feishu_app_card", lambda **kw: sent.append(kw) or {}
    )
    env = {
        "INFRA2_REPORTS_FEISHU_APP_ID": "a",
        "INFRA2_REPORTS_FEISHU_APP_SECRET": "b",
        "INFRA2_REPORTS_FEISHU_CHAT_ID": "oc_reports",
    }
    assert alerting.deliver_infra2_report(
        "DNS drift report · zitian.party\nin sync", env
    )

    (call,) = sent
    assert call["chat_id"] == "oc_reports"
    _assert_card_standard(call["card"])
    assert call["card"]["header"]["template"] == "blue"
    assert call["card"]["header"]["title"]["content"].startswith("[报告] ")


def test_alerting_openapi_schema_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1648 AC18: /openapi.json declares typed schemas and HTTPBasic security."""
    from starlette.testclient import TestClient

    module = _load_alerting_app("alerting_openapi_test", monkeypatch)
    resp = TestClient(module.fastapi_app).get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()

    schemas = spec["components"]["schemas"]
    security_schemes = spec["components"]["securitySchemes"]
    assert "HTTPBasic" in security_schemes
    assert security_schemes["HTTPBasic"]["scheme"] == "basic"

    webhook_op = spec["paths"]["/signoz/webhook"]["post"]
    assert webhook_op["requestBody"]["required"] is True
    req_schema_ref = webhook_op["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    assert req_schema_ref == "#/components/schemas/SigNozAlertPayload"
    assert "SigNozAlertPayload" in schemas
    assert {"HTTPBasic": []} in webhook_op["security"]
    for code in ("202", "400", "401", "411", "413", "502"):
        assert code in webhook_op["responses"], code
        assert (
            "$ref"
            in webhook_op["responses"][code]["content"]["application/json"]["schema"]
        )

    feishu_op = spec["paths"]["/health/feishu"]["get"]
    for code in ("200", "503"):
        assert code in feishu_op["responses"], code
        assert (
            "$ref"
            in feishu_op["responses"][code]["content"]["application/json"]["schema"]
        )


# #1816: the finance_report alert rules against the pinned finance_report sources. The
# routes come from its openapi.json, the failure counters from its telemetry_metrics.py.
_ROUTE_MATCHER = re.compile(r'"http\.route"\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_SELECTOR_METRIC = re.compile(r'\{\s*"([^"]+)"')


def _finance_report_alert_rules() -> list[dict]:
    from common.observability_dashboards import ALERT_RULES_FILE

    return json.loads(ALERT_RULES_FILE.read_text(encoding="utf-8"))["rules"]


def _route_tokens(op: str, value: str) -> list[str]:
    """Split one `http.route` matcher into regex tokens: `^(a|b)$` gives `a` and `b`."""
    if op in {"=", "!="}:
        return [re.escape(value)]
    body = value.removeprefix("^").removesuffix("$")
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    return body.split("|")


def _route_token_problems(rules: list[dict], routes: set[str]) -> list[str]:
    """Each `http.route` token must match one active route template or probe path."""
    return [
        f"{rule['alert_name']}: route token {token!r} matches no active route"
        for rule in rules
        for op, value in _ROUTE_MATCHER.findall(rule.get("promql", ""))
        for token in _route_tokens(op, value)
        if not any(re.fullmatch(token, route) for route in routes)
    ]


def _probe_exclusion_problems(rules: list[dict], probes: frozenset[str]) -> list[str]:
    """Each `http.route!~` exclusion must leave out every probe path."""
    return [
        f"{rule['alert_name']}: route exclusion keeps probe {probe}"
        for rule in rules
        for op, value in _ROUTE_MATCHER.findall(rule.get("promql", ""))
        if op == "!~"
        for probe in sorted(probes)
        if not re.fullmatch(value, probe)
    ]


def _unalerted_counters(rules: list[dict], counters: set[str]) -> list[str]:
    """Counters that no rule selects as its metric."""
    selected = {
        metric
        for rule in rules
        for metric in _SELECTOR_METRIC.findall(rule.get("promql", ""))
    }
    return sorted(counters - selected)


def _active_routes() -> set[str]:
    from infra2_sdk.runtime.otel import PROBE_PATHS

    from common.tests import finance_report_sources as fr

    return fr.pinned_openapi_routes() | set(PROBE_PATHS)


def test_finance_report_route_tokens_match_active_openapi_or_probe_routes() -> None:
    """#1816: a retired route in an exclusion excludes nothing. The renamed
    `POST /statements` then polluted the interactive p95 alert."""
    rules = _finance_report_alert_rules()
    tokens = [
        token
        for rule in rules
        for op, value in _ROUTE_MATCHER.findall(rule.get("promql", ""))
        for token in _route_tokens(op, value)
    ]
    assert len(tokens) == 22  # 3 probes in 3 5xx selectors, 11 + 2 p95 tokens
    assert _route_token_problems(rules, _active_routes()) == []


def test_finance_report_route_exclusions_leave_out_every_sdk_probe_path() -> None:
    """#1816: /livez and /readyz were missing from the exclusions."""
    from infra2_sdk.runtime.otel import PROBE_PATHS

    rules = _finance_report_alert_rules()
    exclusions = [
        value
        for rule in rules
        for op, value in _ROUTE_MATCHER.findall(rule.get("promql", ""))
        if op == "!~"
    ]
    assert len(exclusions) == 4  # 3 in FinanceReportHigh5xxRate, 1 in the p95 rule
    assert _probe_exclusion_problems(rules, PROBE_PATHS) == []


def test_every_finance_report_failure_counter_has_an_alert_rule() -> None:
    """#1816: a failure counter without a rule fails in silence."""
    from common.tests import finance_report_sources as fr

    counters = fr.failure_counter_names(fr.read_pinned(fr.TELEMETRY_METRICS))
    assert {
        "finance.invariant.violation",
        "finance.outbox.dead_letter",
        "finance.market_data.sync_failure",
    } <= counters
    assert _unalerted_counters(_finance_report_alert_rules(), counters) == []


def test_finance_report_classification_rule_selects_only_failure_outcomes() -> None:
    """#1816: the outcome filter names ClassifyOutcome values of the pinned app. A stale
    label (`failure` for `failed`) matches no series, so the rule never fires."""
    from common.tests import finance_report_sources as fr

    outcomes = fr.classify_outcomes(fr.read_pinned(fr.CLASSIFICATION_RUN))
    (rule,) = [
        r
        for r in _finance_report_alert_rules()
        if r["alert_name"] == "FinanceReportClassificationFailureSpike"
    ]
    (pattern,) = re.findall(r'"outcome"=~"((?:[^"\\]|\\.)*)"', rule["promql"])
    assert {o for o in outcomes if re.fullmatch(pattern, o)} == {"failed", "timed_out"}
    assert _SELECTOR_METRIC.findall(rule["promql"]) == [
        "finance.statement_classification.outcome"
    ]


def test_finance_report_contracts_fail_on_a_stale_route_or_an_unalerted_counter() -> (
    None
):
    """Mutation proof for the three contracts above (#1816)."""
    from common.tests import finance_report_sources as fr

    rules = _finance_report_alert_rules()
    stale = [
        {
            **rule,
            "promql": rule["promql"].replace(
                "/statements/[^/]+/approvals", "/statements/[^/]+/review/approve"
            ),
        }
        for rule in rules
    ]
    assert _route_token_problems(stale, _active_routes()) == [
        "FinanceReportP95LatencyHigh: route token '/statements/[^/]+/review/approve' "
        "matches no active route"
    ]

    no_livez = [
        {**rule, "promql": rule["promql"].replace("|/livez|", "|")}
        for rule in rules
        if rule["alert_name"] == "FinanceReportP95LatencyHigh"
    ]
    assert _probe_exclusion_problems(no_livez, frozenset({"/livez", "/health"})) == [
        "FinanceReportP95LatencyHigh: route exclusion keeps probe /livez"
    ]

    source = fr.read_pinned(fr.TELEMETRY_METRICS)
    grown = source.replace(
        "_COUNTER_SPECS: tuple[tuple[str, str, str], ...] = (",
        "_COUNTER_SPECS: tuple[tuple[str, str, str], ...] = (\n"
        '    ("ledger_post_failure", "finance.ledger_post.failure", "Ledger post failures."),',
        1,
    )
    assert grown != source
    assert _unalerted_counters(rules, fr.failure_counter_names(grown)) == [
        "finance.ledger_post.failure"
    ]


# The tests that read a pinned finance_report source. When every read fails, each of them
# fails, and no other test fails on PinnedSourceUnavailable (#1816 review).
_READS_FINANCE_REPORT_SOURCES = [
    "common/tests/test_alerting.py::test_ci_reads_only_the_pinned_checkout_that_the_ci_step_names",
    "common/tests/test_alerting.py::test_every_finance_report_failure_counter_has_an_alert_rule",
    "common/tests/test_alerting.py::test_finance_report_classification_rule_selects_only_failure_outcomes",
    "common/tests/test_alerting.py::test_finance_report_contracts_fail_on_a_stale_route_or_an_unalerted_counter",
    "common/tests/test_alerting.py::test_finance_report_route_tokens_match_active_openapi_or_probe_routes",
    "common/tests/test_observability_dashboards.py::test_every_widget_names_its_service_and_a_metric_it_exports[finance_report/finance_report/observability/dashboard.json]",
    "common/tests/test_observability_dashboards.py::test_finance_report_alert_rules_select_metrics_the_pinned_app_exports",
    "common/tests/test_observability_dashboards.py::test_finance_report_metric_contract_fails_on_a_metric_the_app_does_not_export",
    "common/tests/test_observability_dashboards.py::test_finance_report_service_metrics_come_from_the_pinned_telemetry_source",
    "common/tests/test_observability_dashboards.py::test_promql_selectors_reach_series_the_app_exports",
    "common/tests/test_observability_dashboards.py::test_the_dashboard_checks_fail_on_the_change_each_one_guards[finance_report/finance_report/observability/dashboard.json]",
]

_UNREADABLE_SOURCE_PLUGIN = """
def pytest_configure(config):
    from common.tests import finance_report_sources as fr

    def unreadable(relative_path):
        raise OSError("simulated outage of " + relative_path)

    fr._read = unreadable
"""


def test_an_unreadable_finance_report_source_fails_only_its_contract_tests(
    tmp_path: Path,
) -> None:
    """#1816 review: an import never reads a pinned source. With every read failing, both
    modules still collect, and only the tests that read finance_report sources fail."""
    (tmp_path / "fr_unreadable.py").write_text(_UNREADABLE_SOURCE_PLUGIN)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), str(ROOT), os.environ.get("PYTHONPATH", "")]
        ),
        "COLUMNS": "1000",  # keep the exception name in each short summary line
    }
    files = [
        "common/tests/test_observability_dashboards.py",
        "common/tests/test_alerting.py",
    ]
    base = [sys.executable, "-m", "pytest", "-p", "fr_unreadable", "-o", "addopts="]
    base += ["-n0", "-p", "no:cacheprovider", "-q"]
    collected = subprocess.run(
        [*base, "--collect-only", *files],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert collected.returncode == 0, collected.stdout[-2000:] + collected.stderr
    assert "error" not in collected.stdout.lower().splitlines()[-1]

    this = "common/tests/test_alerting.py::" + sys._getframe().f_code.co_name
    ran = subprocess.run(
        [*base, "-rf", "--deselect", this, *files],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    summaries = {
        line.split(" ", 2)[1]: line
        for line in ran.stdout.splitlines()
        if line.startswith("FAILED ")
    }
    missing = set(_READS_FINANCE_REPORT_SOURCES) - summaries.keys()
    assert missing == set(), ran.stdout[-3000:]
    others = {
        test: line
        for test, line in summaries.items()
        if test not in _READS_FINANCE_REPORT_SOURCES
    }
    assert [t for t, line in others.items() if "PinnedSourceUnavailable" in line] == []
    assert "PinnedSourceUnavailable" in ran.stdout
    assert "simulated outage of apps/" in ran.stdout


def test_ci_reads_only_the_pinned_checkout_that_the_ci_step_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1816 review: with CI set, the reader reads only FINANCE_REPORT_PINNED_CHECKOUT at the
    pin and never fetches. A missing or unpinned path names the infra-ci step. Without CI
    and without a checkout at the pin, the reader fetches at the pinned commit."""
    from common.tests import finance_report_sources as fr

    pin = tmp_path / "pin"
    (pin / "apps/frontend").mkdir(parents=True)
    (pin / fr.OPENAPI).write_text("pinned text")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=pin, check=True, timeout=30)
    subprocess.run([*git, "add", "."], cwd=pin, check=True, timeout=30)
    subprocess.run([*git, "commit", "-qm", "pin"], cwd=pin, check=True, timeout=30)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=pin,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()

    fetched: list[str] = []
    monkeypatch.setattr(fr, "ROOT", tmp_path / "infra2")  # no repos/finance_report
    monkeypatch.setattr(fr, "pinned_sha", lambda: head)
    monkeypatch.setattr(fr, "http_get", lambda url: fetched.append(url) or b"fetched")
    monkeypatch.setenv("CI", "true")

    for configured in (None, str(tmp_path / "absent")):
        monkeypatch.setattr(fr, "_READS", {})
        if configured is None:
            monkeypatch.delenv(fr.PINNED_CHECKOUT_ENV, raising=False)
        else:
            monkeypatch.setenv(fr.PINNED_CHECKOUT_ENV, configured)
        with pytest.raises(fr.PinnedSourceUnavailable) as raised:
            fr.read_pinned(fr.OPENAPI)
        assert fr.CI_STEP in str(raised.value)

    monkeypatch.setattr(fr, "_READS", {})
    monkeypatch.setenv(fr.PINNED_CHECKOUT_ENV, str(pin))
    monkeypatch.setattr(
        fr, "pinned_sha", lambda: "0" * 40
    )  # the checkout is not at the pin
    with pytest.raises(fr.PinnedSourceUnavailable):
        fr.read_pinned(fr.OPENAPI)

    monkeypatch.setattr(fr, "_READS", {})
    monkeypatch.setattr(fr, "pinned_sha", lambda: head)
    assert fr.read_pinned(fr.OPENAPI) == "pinned text"
    assert fetched == []

    monkeypatch.delenv("CI")
    monkeypatch.delenv(fr.PINNED_CHECKOUT_ENV)
    monkeypatch.setattr(fr, "_READS", {})
    assert fr.read_pinned(fr.OPENAPI) == "fetched"
    assert fetched == [
        f"https://raw.githubusercontent.com/wangzitian0/finance_report/{head}/"
        "apps/frontend/openapi.json"
    ]
