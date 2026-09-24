"""Offline tests for SigNoz to Feishu alerting."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from libs.alerting import (
    AlertingError,
    BasicAuth,
    InvalidWebhookUrl,
    MAX_MESSAGE_CHARS,
    build_feishu_alert_card,
    build_feishu_app_card_payload,
    build_feishu_app_message_payload,
    build_feishu_card_payload,
    build_feishu_text_payload,
    build_signoz_channel_payload,
    build_signoz_log_alert_rule_payload,
    build_signoz_metric_alert_rule_payload,
    deliver_feishu_app_text,
    feishu_host_reachable,
    find_signoz_channel_id,
    find_signoz_rule_id,
    format_signoz_alert,
    redacted_url,
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


def test_feishu_message_truncation_respects_max_length() -> None:
    """Infra-007 alerting: truncation suffix must not exceed Feishu limit."""
    text = "x" * (MAX_MESSAGE_CHARS + 100)

    webhook_payload = build_feishu_text_payload(text)
    assert len(webhook_payload["content"]["text"]) <= MAX_MESSAGE_CHARS
    assert webhook_payload["content"]["text"].endswith("\n...[truncated]")

    app_payload = build_feishu_app_message_payload("oc_test", text)
    app_text = json.loads(app_payload["content"])["text"]
    assert len(app_text) <= MAX_MESSAGE_CHARS
    assert app_text.endswith("\n...[truncated]")


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
    assert "[FIRING] ExampleBackendDown" in text
    assert "Severity: critical" in text
    assert "example-backend" in text

    feishu_payload = build_feishu_text_payload(text)
    assert feishu_payload == {"msg_type": "text", "content": {"text": text}}


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
    text = format_signoz_alert(payload)
    card = build_feishu_alert_card(payload)
    url = f"https://github.com/wangzitian0/infra2/blob/main/docs/runbooks/infra022-p0.md{anchor}"
    assert f"Runbook: {url}" in text
    assert url in json.dumps(card)
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
    assert "[FIRING] ExampleBackendDown" in title and "🔴" in title

    blob = json.dumps(card, ensure_ascii=False)
    assert "**Status**" in blob and "FIRING" in blob
    assert "**Severity**" in blob and "critical" in blob
    assert "example-backend" in blob  # per-alert instance line
    assert "Production API health check failed" in blob  # summary

    # the only action button links to SigNoz
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions and actions[0]["actions"][0]["url"] == "https://signoz.zitian.party"


def test_alert_card_resolved_is_green_and_nonhttp_url_has_no_button() -> None:
    """Resolved → green header + ✅; a non-http externalURL (e.g. infra2://) drops the button."""
    card = build_feishu_alert_card(
        _sample_alert_payload(
            status="resolved", externalURL="infra2://platform/12.alerting"
        )
    )

    assert card["header"]["template"] == "green"
    assert "✅" in card["header"]["title"]["content"]
    assert "[RESOLVED]" in card["header"]["title"]["content"]
    assert not [e for e in card["elements"] if e.get("tag") == "action"]


def test_card_payloads_use_interactive_msg_type() -> None:
    card = build_feishu_alert_card(_sample_alert_payload())

    webhook = build_feishu_card_payload(card)
    assert webhook == {"msg_type": "interactive", "card": card}

    app = build_feishu_app_card_payload("oc_test", card)
    assert app["receive_id"] == "oc_test"
    assert app["msg_type"] == "interactive"
    assert json.loads(app["content"]) == card  # content is a JSON string


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
        channel_ids=["channel-1"],
        summary="example backend emitted error logs",
    )

    assert payload["schemaVersion"] == "v2alpha1"
    assert payload["version"] == "v5"
    assert payload["alertType"] == "LOGS_BASED_ALERT"
    assert payload["condition"]["selectedQueryName"] == "A"
    threshold = payload["condition"]["thresholds"]["spec"][0]
    assert threshold["op"] == "1"
    assert threshold["matchType"] == "1"
    assert threshold["channels"] == ["channel-1"]

    query = payload["condition"]["compositeQuery"]["builderQueries"]["A"]
    assert query["dataSource"] == "logs"
    assert query["aggregateOperator"] == "count"
    filters = query["filters"]["items"]
    assert filters[0]["key"]["key"] == "service.name"
    assert filters[0]["key"]["type"] == "resource"
    assert filters[0]["value"] == "example-backend"
    assert filters[1]["key"]["key"] == "deployment.environment.name"
    assert filters[1]["value"] == "production"
    assert filters[2]["key"]["key"] == "severity_text"
    assert filters[2]["value"] == ["ERROR", "FATAL"]
    assert payload["labels"]["service_id"] == "infra/example-backend"
    assert payload["labels"]["environment"] == "production"


def test_metric_alert_rule_uses_signoz_v5_promql_schema() -> None:
    """#1106: finance_report metric alerts render as SigNoz v5 PromQL rules."""
    payload = build_signoz_metric_alert_rule_payload(
        alert_name="FinanceReportHigh5xxRate",
        promql="sum(rate(http_server_request_count[5m]))",
        channel_ids=["channel-1"],
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
            channel_ids=["channel-1"],
            summary="bad op",
            op="abvoe",
        )

    with pytest.raises(AlertingError, match="match type"):
        build_signoz_metric_alert_rule_payload(
            alert_name="BadMatch",
            promql="sum(up)",
            channel_ids=["channel-1"],
            summary="bad match type",
            match_type="sometiems",
        )


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


def test_feishu_app_message_payload_stringifies_content() -> None:
    """Infra-007 alerting: Feishu app bot payload follows OpenAPI contract."""
    payload = build_feishu_app_message_payload("oc_test", "hello")
    assert payload["receive_id"] == "oc_test"
    assert payload["msg_type"] == "text"
    assert payload["content"] == '{"text": "hello"}'


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

    monkeypatch.setattr("libs.alerting.urlopen", fake_urlopen)

    result = deliver_feishu_app_text(
        app_id="cli_test",
        app_secret="secret",
        chat_id="oc_test",
        text="hello",
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
    assert "ALERTING_SECRETS_WAIT_SECONDS:-300" in compose
    assert "condition: service_healthy" not in compose
    assert "open-apis/bot/v2/hook" not in compose
    assert "secrets:/secrets:ro" in compose

    deploy = (base / "deploy.py").read_text(encoding="utf-8")
    # PR-E: the manifest-driven supply (libs/security/supply.py) replaced the bespoke
    # 1Password → Vault copy; the Deployer hook is what pre_compose calls.
    assert "apply_secret_supply" in deploy
    assert "_sync_1password_to_vault" not in deploy
    assert 'credential_type="root_vars"' not in deploy
    assert "INFRA_PROBE_HEARTBEAT_URL" in deploy
    assert "INFRA_PROBE_HEARTBEAT_TOKEN" in deploy

    ctmpl = (base / "secrets.ctmpl").read_text(encoding="utf-8")
    assert "INFRA_PROBE_HEARTBEAT_URL" in ctmpl
    assert "INFRA_PROBE_HEARTBEAT_TOKEN" in ctmpl


# import root -> the pip-installable name the Dockerfile must list. libs.observability.probes
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
}


def _third_party_import_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    first_party = {"libs", "tools", "__future__"}
    return {
        r for r in roots if r not in first_party and r not in sys.stdlib_module_names
    }


def test_alerting_dockerfile_installs_every_import_the_packaged_code_needs() -> None:
    """Infra-007 alerting / #600 regression: the image's pip install line must cover
    every third-party import reachable from what it actually COPYs and runs
    (app.py's entrypoint, and infra_probe_runner.py's import of libs.observability.probes) —
    caught live via a staging ModuleNotFoundError crash loop, not by any test."""
    base = ROOT / "platform/12.alerting"
    dockerfile = (base / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install" in dockerfile, "expected a pip install step in the Dockerfile"

    required_roots: set[str] = set()
    for path in (
        base / "app.py",
        ROOT / "libs/observability/probes.py",
        ROOT / "tools/infra_probe_runner.py",
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
        f"required by an import in app.py / libs/observability/probes.py / "
        f"tools/infra_probe_runner.py"
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


def test_alerting_app_request_guards_are_explicit() -> None:
    """Infra-007 alerting: HTTP auth/body guards match review feedback."""
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
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

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
        sent.append({"chat": chat_id, "title": card["header"]["title"]["content"]})
        return {"code": 0}

    def webhook_card(url, card, **_kwargs):
        sent.append({"chat": "webhook", "title": card["header"]["title"]["content"]})
        return {"code": 0}

    monkeypatch.setattr(module, "deliver_feishu_app_card", app_card)
    monkeypatch.setattr(module, "deliver_feishu_card", webhook_card)
    server = ThreadingHTTPServer(("localhost", 0), module.AlertBridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(payload: dict) -> int:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"http://localhost:{server.server_address[1]}/signoz/webhook",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return response.status

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return post, sent, stop


def _payload(alertname: str, *, report: bool) -> dict:
    from libs.alerting import mark_report_payload

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
    reaches the pager chat, untouched."""
    post, sent, stop = _bridge_under_test(monkeypatch, report_chat="oc_reports")
    try:
        assert post(_payload("InfraProbeChronic", report=True)) == 202
        assert post(_payload("InfraServiceProbeFailed", report=False)) == 202
    finally:
        stop()

    assert [s["chat"] for s in sent] == ["oc_reports", "oc_pager"]
    assert not any(s["title"].startswith("[REPORT]") for s in sent)


def test_bridge_without_a_report_chat_marks_reports_in_the_pager_chat(
    monkeypatch,
) -> None:
    """#903: with FEISHU_REPORT_CHAT_ID unset nothing is lost — the report lands in the
    pager chat, titled [REPORT] so it does not read as a page. A webhook is bound to
    one chat, so webhook mode always takes this fallback."""
    for mode, pager in (("feishu_app", "oc_pager"), ("feishu_webhook", "webhook")):
        post, sent, stop = _bridge_under_test(monkeypatch, mode=mode)
        try:
            assert post(_payload("ContainerBreakdownChronic", report=True)) == 202
            assert post(_payload("ContainerBreakdown", report=False)) == 202
        finally:
            stop()

        assert [s["chat"] for s in sent] == [pager, pager], mode
        assert sent[0]["title"].startswith("[REPORT] "), mode
        assert "ContainerBreakdownChronic" in sent[0]["title"], mode
        assert not sent[1]["title"].startswith("[REPORT]"), mode


def test_only_staging_and_preview_environments_are_report_only() -> None:
    """#903 review: the report-only decision is an allowlist over the normalized
    environment. `prod`, `PRODUCTION `, unset and garbage page; staging and the
    preview slots report."""
    from libs.alerting import is_report_only_environment

    pages = [None, "", "production", "PRODUCTION ", "prod", "garbage", "x/y", "pr"]
    reports = [
        "staging",
        " STAGING",
        "stg",
        "preview",
        "pr-5",
        "branch-main",
        "commit-1ab32d5",
        "tag-v1-2-3",
    ]
    assert [v for v in pages if is_report_only_environment(v)] == []
    assert [v for v in reports if not is_report_only_environment(v)] == []
