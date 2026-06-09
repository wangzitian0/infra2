"""Offline contract tests for the infra2 out-of-band watchdog."""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/out-of-band-watchdog.yml"
WATCHDOG = ROOT / "tools/out_of_band_watchdog.py"
ALERTING_README = ROOT / "platform/12.alerting/README.md"
ALERTING_SSOT = ROOT / "docs/ssot/ops.alerting.md"


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("out_of_band_watchdog", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    sys.modules["out_of_band_watchdog"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_workflow_runs_daily_and_can_be_dispatched() -> None:
    """#209: GitHub watchdog is a daily audit with manual dispatch."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["on"]["schedule"] == [{"cron": "17 2 * * *"}]
    assert "workflow_dispatch" in workflow["on"]
    assert "ssh_targets_override" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert workflow["permissions"] == {"contents": "read"}


def test_workflow_alerts_directly_and_does_not_call_the_bridge() -> None:
    """Infra-007.2: host-down alerts bypass the in-band alert bridge."""
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE" in text
    assert "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL" in text
    assert "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET" in text
    assert "http://platform-alerting" not in text
    assert "/signoz/webhook" not in text
    assert "tools/out_of_band_watchdog.py" in text
    assert "inputs.ssh_targets_override" in text


def test_workflow_configures_dokploy_route_canary_signal() -> None:
    """Infra-011.9: out-of-band watchdog also probes Dokploy route liveness."""
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "DOKPLOY_API_KEY" in text
    assert "DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID" in text
    assert "DOKPLOY_ROUTE_CANARY_DOKPLOY_HOST" in text
    assert "DOKPLOY_ROUTE_CANARY_TIMEOUT_SECONDS" in text
    assert "python -m pip install httpx python-dotenv rich" in text


def test_default_targets_cover_public_host_and_bridge_health() -> None:
    """Infra-007.2/Infra-007.3: defaults cover host reachability and bridge."""
    watchdog = _load_watchdog()

    http_targets = watchdog.parse_http_targets("")
    assert [target.name for target in http_targets] == [
        "infra2-public-entrypoint",
        "cloudflare-worker-health",
    ]
    assert http_targets[0].url == "https://cloud.zitian.party"
    assert http_targets[1].url.endswith("/health")

    ssh_targets = watchdog.parse_ssh_targets("")
    assert [target.name for target in ssh_targets] == [
        "infra2-ssh",
        "infra2-docker",
        "infra2-docker-health",
        "infra2-alert-bridge",
    ]
    assert ssh_targets[0].command == "echo infra2-ssh-ok"
    assert "docker info" in ssh_targets[1].command
    assert "health=unhealthy" in ssh_targets[2].command
    assert "health=starting" in ssh_targets[2].command
    assert "status=restarting" in ssh_targets[2].command
    assert "docker inspect" in ssh_targets[2].command
    assert "{{.Config.Image}}" in ssh_targets[2].command
    assert "{{.State.Status}}" in ssh_targets[2].command
    assert ssh_targets[2].expected_text == "docker-health-ok"
    assert "docker exec platform-alerting" in ssh_targets[3].command
    assert "127.0.0.1:8080/health" in ssh_targets[3].command
    assert ssh_targets[3].expected_text == "healthy"


def test_ssh_checks_report_missing_configuration_as_configuration_failure() -> None:
    """Infra-011.9: missing SSH config should not masquerade as host downtime."""
    watchdog = _load_watchdog()

    results = watchdog.run_ssh_checks(None, watchdog.parse_ssh_targets(""))

    assert results == [
        watchdog.CheckResult("infra2-ssh", False, "SSH watchdog config is missing", "configuration"),
        watchdog.CheckResult("infra2-docker", False, "SSH watchdog config is missing", "configuration"),
        watchdog.CheckResult(
            "infra2-docker-health",
            False,
            "SSH watchdog config is missing",
            "configuration",
        ),
        watchdog.CheckResult(
            "infra2-alert-bridge",
            False,
            "SSH watchdog config is missing",
            "configuration",
        ),
    ]


def test_worker_status_check_detects_missing_token_and_empty_config() -> None:
    """#209: GitHub audit must verify Worker cron/KV-backed effective config."""
    watchdog = _load_watchdog()

    assert watchdog.run_worker_status_check({}, timeout=1)[0] == watchdog.CheckResult(
        "cloudflare-worker-status",
        False,
        "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN is missing",
        "configuration",
    )


def test_worker_status_check_accepts_fresh_nonempty_status(monkeypatch) -> None:
    """#209: authenticated Worker status is a first-class audit signal."""
    watchdog = _load_watchdog()
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return (
                b'{"ok":true,"lastRun":{"ageSeconds":1800,'
                b'"routeTargetCount":8,"heartbeatTargetCount":2}}'
            )

    def fake_urlopen(request, *, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(watchdog, "urlopen", fake_urlopen)

    results = watchdog.run_worker_status_check(
        {
            "INFRA2_WATCHDOG_WORKER_STATUS_URL": "",
            "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "status-token",
        },
        timeout=3,
    )

    assert results == [
        watchdog.CheckResult(
            "cloudflare-worker-status",
            True,
            "worker last-run fresh: age=1800s",
        )
    ]
    assert captured == {
        "authorization": "Bearer status-token",
        "timeout": 3,
        "url": watchdog.DEFAULT_WORKER_STATUS_URL,
    }


def test_worker_status_check_reports_last_run_failure_context(monkeypatch) -> None:
    """#209: unhealthy Worker status must expose whether cron checks failed."""
    watchdog = _load_watchdog()

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return (
                b'{"ok":false,"lastRun":{"ok":false,"ageSeconds":890,'
                b'"failureCount":2,"routeTargetCount":8,'
                b'"heartbeatTargetCount":2,'
                b'"deliveryError":"feishu delivery failed"}}'
            )

    monkeypatch.setattr(watchdog, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    results = watchdog.run_worker_status_check(
        {"INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "status-token"},
        timeout=3,
    )

    assert results == [
        watchdog.CheckResult(
            "cloudflare-worker-status",
            False,
            (
                "worker status unhealthy: age=890 last_run_ok=False failures=2 "
                "routes=8 heartbeats=2 delivery_error=feishu delivery failed"
            ),
            "cloudflare-worker-health",
        )
    ]


def test_dokploy_route_canary_check_fails_closed_without_config() -> None:
    """Infra-011.9: missing canary config is an alert, not a silent gap."""
    watchdog = _load_watchdog()

    assert watchdog.run_dokploy_route_canary_check({}, ssh_config=None) == [
        watchdog.CheckResult(
            "infra2-dokploy-route-canary",
            False,
            "DOKPLOY_API_KEY is missing",
            "configuration",
        )
    ]
    assert watchdog.run_dokploy_route_canary_check(
        {"DOKPLOY_API_KEY": "secret"},
        ssh_config=None,
    ) == [
        watchdog.CheckResult(
            "infra2-dokploy-route-canary",
            False,
            "DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID is missing",
            "configuration",
        )
    ]


def test_dokploy_route_canary_check_uses_run_scoped_default_host_and_compose() -> None:
    """Infra-011.9: default OOB canaries must not overwrite another run's labels."""
    watchdog = _load_watchdog()
    captured = {}

    def fake_runner(config, client):
        captured["config"] = config
        captured["client"] = client
        return SimpleNamespace(
            status="pass",
            failure_domain="",
            compose_id="cmp-canary",
            public_url=f"https://{config.host}",
            steps=[],
        )

    results = watchdog.run_dokploy_route_canary_check(
        {
            "DOKPLOY_API_KEY": "secret",
            "DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID": "env-1",
            "GITHUB_RUN_ID": "123",
        },
        ssh_config=None,
        runner=fake_runner,
        client_factory=lambda *, host: {"host": host},
    )

    assert results == [
        watchdog.CheckResult(
            "infra2-dokploy-route-canary",
            True,
            (
                "status=pass failure_domain=none compose_id=cmp-canary "
                "public_url=https://route-canary-watchdog.zitian.party"
            ),
        )
    ]
    assert captured["config"].host == "route-canary-watchdog.zitian.party"
    assert captured["config"].compose_name == "dokploy-route-canary-watchdog"
    assert captured["config"].nonce == "123"
    assert captured["config"].repair_stale_compose is True


def test_dokploy_route_canary_check_reports_worker_failure_domain() -> None:
    """Infra-011.9: worker/deployment-record failures page through watchdog."""
    watchdog = _load_watchdog()
    captured = {}

    def fake_runner(config, client):
        captured["config"] = config
        captured["client"] = client
        return SimpleNamespace(
            status="fail",
            failure_domain="dokploy-worker-or-deployment-record",
            compose_id="cmp-canary",
            public_url="https://route-canary.example.com",
            steps=[
                SimpleNamespace(
                    name="deployment-record",
                    status="fail",
                    detail="deploy request did not produce a new record",
                )
            ],
        )

    def fake_client_factory(*, host):
        captured["host"] = host
        return object()

    results = watchdog.run_dokploy_route_canary_check(
        {
            "DOKPLOY_API_KEY": "secret",
            "DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID": "env-1",
            "DOKPLOY_ROUTE_CANARY_HOST": "route-canary.example.com",
            "DOKPLOY_ROUTE_CANARY_TIMEOUT_SECONDS": "30",
            "GITHUB_RUN_ID": "123",
        },
        ssh_config=watchdog.SshConfig(
            host="vps.example.com",
            user="root",
            port=22,
            key_path="/tmp/key",
        ),
        runner=fake_runner,
        client_factory=fake_client_factory,
    )

    assert results == [
        watchdog.CheckResult(
            "infra2-dokploy-route-canary",
            False,
            (
                "status=fail failure_domain=dokploy-worker-or-deployment-record "
                "compose_id=cmp-canary public_url=https://route-canary.example.com "
                "failed_steps=deployment-record:fail:deploy request did not produce a new record"
            ),
            "dokploy-worker-or-deployment-record",
        )
    ]
    assert captured["host"] == "cloud.zitian.party"
    assert captured["config"].ssh_host == "vps.example.com"
    assert captured["config"].compose_name == "dokploy-route-canary-watchdog"
    assert captured["config"].timeout_seconds == 30


def test_dokploy_route_canary_check_uses_stable_default_name_and_timeout() -> None:
    """Infra-011.9: OOB canary defaults match the stable guarded test asset."""
    watchdog = _load_watchdog()
    captured = {}

    def fake_runner(config, client):
        captured["config"] = config
        captured["client"] = client
        return SimpleNamespace(
            status="pass",
            failure_domain="",
            compose_id="cmp-canary",
            public_url="https://route-canary-watchdog.zitian.party",
            steps=[],
        )

    results = watchdog.run_dokploy_route_canary_check(
        {
            "DOKPLOY_API_KEY": "secret",
            "DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID": "env-1",
        },
        ssh_config=None,
        runner=fake_runner,
        client_factory=lambda *, host: object(),
    )

    assert results[0].ok is True
    assert captured["config"].host == "route-canary-watchdog.zitian.party"
    assert captured["config"].compose_name == "dokploy-route-canary-watchdog"
    assert captured["config"].timeout_seconds == 180


def test_custom_ssh_targets_preserve_mandatory_docker_health() -> None:
    """Infra-011.2: GitHub variable drift must not remove Docker health checks."""
    watchdog = _load_watchdog()

    ssh_targets = watchdog.parse_ssh_targets("infra2-custom|echo custom-ok|custom-ok")
    names = [target.name for target in ssh_targets]

    assert "infra2-docker-health" in names
    assert "infra2-custom" in names


def test_base64_ssh_target_commands_support_manual_diagnostics() -> None:
    """Infra-011.9: Manual SSH diagnostics can include shell separators safely."""
    watchdog = _load_watchdog()
    command = "echo dokploy; docker logs --tail 20 dokploy 2>&1 | tail -20"
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")

    assert watchdog._decode_ssh_command(f"base64:{encoded}") == command


def test_iac_runner_is_not_a_default_whole_host_health_check() -> None:
    """Infra-007.2: IaC Runner is service-level, not whole-host health."""
    watchdog = _load_watchdog()

    default_targets = "\n".join(
        [target.url for target in watchdog.parse_http_targets("")]
        + [target.command for target in watchdog.parse_ssh_targets("")]
    )

    assert "iac.zitian.party" not in default_targets
    assert "iac-runner" not in default_targets


def test_failure_message_is_out_of_band_and_redacts_secrets() -> None:
    """Infra-007.2: Feishu text is actionable and does not leak secrets."""
    watchdog = _load_watchdog()
    results = [
        watchdog.CheckResult(
            name="infra2-iac-runner",
            ok=False,
            detail="GET https://iac.zitian.party/health failed: timed out",
            failure_domain="host-diagnostics",
        ),
        watchdog.CheckResult(
            name="infra2-alert-bridge",
            ok=False,
            detail="ssh command did not contain expected text: secret-token",
            failure_domain="alert-bridge",
        ),
    ]

    message = watchdog.format_failure_message(results, run_url="https://github/run/1")

    assert "[OUT-OF-BAND] Infra2 watchdog failed" in message
    assert "Route: GitHub Actions -> Feishu direct" in message
    assert "[host-diagnostics] infra2-iac-runner" in message
    assert "[alert-bridge] infra2-alert-bridge" in message
    assert "https://github/run/1" in message
    assert "secret-token" not in message


def test_main_sends_feishu_only_when_a_check_fails(monkeypatch) -> None:
    """Infra-007.2: successful checks stay quiet, failures send direct Feishu."""
    watchdog = _load_watchdog()
    sent_messages: list[str] = []

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout: [
            watchdog.CheckResult("infra2-iac-runner", True, "HTTP 200")
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout: [
            watchdog.CheckResult("cloudflare-worker-status", True, "fresh")
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_route_canary_check",
        lambda _env, ssh_config: [
            watchdog.CheckResult("infra2-dokploy-route-canary", True, "pass")
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "deliver_out_of_band_alert",
        lambda _env, text: sent_messages.append(text),
    )

    assert (
        watchdog.main(
            {
                "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/token",
                "WATCHDOG_DRY_RUN": "0",
            }
        )
        == 0
    )
    assert sent_messages == []

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout: [
            watchdog.CheckResult("infra2-iac-runner", False, "connection refused")
        ],
    )

    assert (
        watchdog.main(
            {
                "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/token",
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "wangzitian0/infra2",
                "GITHUB_RUN_ID": "123",
                "WATCHDOG_DRY_RUN": "0",
            }
        )
        == 1
    )
    assert len(sent_messages) == 1
    assert "OUT-OF-BAND" in sent_messages[0]


def test_out_of_band_delivery_supports_existing_feishu_app_mode(monkeypatch) -> None:
    """Infra-007.2: watchdog can reuse existing direct Feishu app credentials."""
    watchdog = _load_watchdog()
    calls = []

    monkeypatch.setattr(
        watchdog,
        "deliver_feishu_app_text",
        lambda **kwargs: calls.append(kwargs),
    )

    watchdog.deliver_out_of_band_alert(
        {
            "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_app",
            "INFRA2_OUT_OF_BAND_FEISHU_APP_ID": "cli_test",
            "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET": "secret",
            "INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID": "oc_test",
            "INFRA2_OUT_OF_BAND_FEISHU_API_BASE": "https://open.feishu.cn",
        },
        "hello",
    )

    assert calls == [
        {
            "app_id": "cli_test",
            "app_secret": "secret",
            "chat_id": "oc_test",
            "api_base": "https://open.feishu.cn",
            "text": "hello",
        }
    ]


def test_webhook_mode_error_mentions_primary_and_fallback_env_names() -> None:
    """Infra-007.2: missing webhook config is diagnosable in CI logs."""
    watchdog = _load_watchdog()

    try:
        watchdog.deliver_out_of_band_alert(
            {"INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE": "feishu_webhook"},
            "hello",
        )
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected missing webhook configuration to fail")

    assert "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL" in message
    assert "FEISHU_WEBHOOK_URL" in message


def test_docs_state_that_github_fallback_includes_route_canary() -> None:
    """Infra-011.9: docs must match the code-owned fallback watchdog scope."""
    assert "Dokploy route canary" in ALERTING_README.read_text(encoding="utf-8")
    assert "Dokploy route canary" in ALERTING_SSOT.read_text(encoding="utf-8")
