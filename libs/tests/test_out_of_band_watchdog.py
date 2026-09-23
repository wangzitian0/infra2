"""Offline contract tests for the infra2 out-of-band watchdog."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ops-checks.yml"
WATCHDOG = ROOT / "tools/out_of_band_watchdog.py"
ALERTING_README = ROOT / "platform/12.alerting/README.md"
ALERTING_SSOT = ROOT / "docs/ssot/ops.observability.md"


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

    assert {"cron": "17 2 * * *"} in workflow["on"]["schedule"]
    assert "workflow_dispatch" in workflow["on"]
    assert (
        "out-of-band-watchdog"
        in workflow["on"]["workflow_dispatch"]["inputs"]["task"]["options"]
    )
    assert "ssh_targets_override" in workflow["on"]["workflow_dispatch"]["inputs"]
    # issues: per job, never workflow-wide: deploy-v2-canary runs same-repo PR code.
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    jobs = workflow["jobs"]
    assert jobs["watchdog"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "write",
    }
    assert jobs["digest"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "read",
    }
    writers = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("issues") == "write"
    ]
    assert writers == ["watchdog"]


def test_workflow_alerts_directly_and_does_not_call_the_bridge() -> None:
    """Infra-007.2: host-down alerts bypass the in-band alert bridge."""
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE" in text
    assert "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL" in text
    assert "INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET" in text
    assert "INFRA2_WATCHDOG_RETRY_MAX_ATTEMPTS" in text
    assert "INFRA2_WATCHDOG_RETRY_DELAY_SECONDS" in text
    assert "http://platform-alerting" not in text
    assert "/signoz/webhook" not in text
    assert "tools/out_of_band_watchdog.py" in text
    assert "inputs.ssh_targets_override" in text


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
    # Docker ANDs filters of different keys, so the three conditions must be
    # issued as independent `docker ps` queries to behave as an OR. Otherwise a
    # plain running+unhealthy container is silently missed.
    assert ssh_targets[2].command.count("docker ps --filter") == 3
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
        watchdog.CheckResult(
            "infra2-ssh", False, "SSH watchdog config is missing", "configuration"
        ),
        watchdog.CheckResult(
            "infra2-docker", False, "SSH watchdog config is missing", "configuration"
        ),
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
        retry_delay_seconds=0,  # the retry is asserted below; its real 60 s wait is not
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
            attempt_count=2,
        )
    ]


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
    assert "Action:" in message
    assert "Runbook:" in message
    assert "https://github/run/1" in message
    assert "secret-token" not in message


def test_host_failures_link_to_concrete_p0_runbooks() -> None:
    watchdog = _load_watchdog()
    assert watchdog._runbook_url_for_failure("host-reachability").endswith(
        "docs/runbooks/infra022-p0.md#watchdog-silent"
    )
    assert watchdog._runbook_url_for_failure("docker-runtime").endswith(
        "docs/runbooks/infra022-p0.md#container-killed"
    )


def test_main_sends_feishu_only_when_a_check_fails(monkeypatch) -> None:
    """Infra-007.2: successful checks stay quiet, failures send direct Feishu."""
    watchdog = _load_watchdog()
    sent_messages: list[str] = []

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            watchdog.CheckResult("infra2-iac-runner", True, "HTTP 200")
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [
            watchdog.CheckResult("cloudflare-worker-status", True, "fresh")
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_status_check",
        lambda _env, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: [],
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
        lambda _targets, _timeout, **_kwargs: [
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


def test_http_checks_retry_transient_failure(monkeypatch) -> None:
    """Infra-012.1: transient HTTP failure should recover within retry window."""
    watchdog = _load_watchdog()
    target = watchdog.HttpTarget(
        name="infra2-public-entrypoint",
        url="https://cloud.zitian.party",
        expected_statuses={200},
    )
    attempts = {"value": 0}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def flaky_urlopen(*_args, **_kwargs):
        attempts["value"] += 1
        if attempts["value"] == 1:
            raise watchdog.URLError("temporary dns failure")
        return FakeResponse()

    monkeypatch.setattr(watchdog, "urlopen", flaky_urlopen)
    monkeypatch.setattr(watchdog.time, "sleep", lambda _seconds: None)

    results = watchdog.run_http_checks(
        [target],
        timeout=1.0,
        max_attempts=2,
        retry_delay_seconds=0,
    )

    assert len(results) == 1
    assert results[0].ok is True
    assert "recovered_on_attempt=2" in results[0].detail
    assert results[0].attempt_count == 2


def test_main_structured_check_logs_include_attempt_count(monkeypatch) -> None:
    """Infra-012.4: structured check logs include attempt_count and timestamp."""
    watchdog = _load_watchdog()
    emitted: list[dict] = []

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            watchdog.CheckResult("infra2-iac-runner", True, "HTTP 200", attempt_count=2)
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_status_check",
        lambda _env, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog, "_emit_structured_log", lambda payload: emitted.append(dict(payload))
    )

    assert watchdog.main({"WATCHDOG_DRY_RUN": "0"}) == 0
    check_events = [row for row in emitted if row.get("event") == "watchdog.check"]
    assert len(check_events) == 1
    assert check_events[0]["attempt_count"] == 2


def test_main_uses_default_retry_values_when_env_is_blank(monkeypatch) -> None:
    """Blank retry env vars must not crash watchdog execution."""
    watchdog = _load_watchdog()
    captured: dict = {}

    def fake_run_http_checks(_targets, _timeout, **kwargs):
        captured["kwargs"] = kwargs
        return [watchdog.CheckResult("infra2-iac-runner", True, "HTTP 200")]

    monkeypatch.setattr(watchdog, "run_http_checks", fake_run_http_checks)
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_status_check",
        lambda _env, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: [],
    )

    result = watchdog.main(
        {
            "INFRA2_WATCHDOG_RETRY_MAX_ATTEMPTS": "",
            "INFRA2_WATCHDOG_RETRY_DELAY_SECONDS": "",
            "WATCHDOG_DRY_RUN": "0",
        }
    )

    assert result == 0
    assert captured["kwargs"]["max_attempts"] == 2
    assert captured["kwargs"]["retry_delay_seconds"] == 60.0


def test_main_records_delivery_failure_event_instead_of_crashing(monkeypatch) -> None:
    """Infra-012.5: delivery failures must emit fallback diagnostic event."""
    watchdog = _load_watchdog()
    emitted: list[dict] = []

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "infra2-public-entrypoint", False, "connection refused"
            )
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_status_check",
        lambda _env, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "deliver_out_of_band_alert",
        lambda _env, _message: (_ for _ in ()).throw(RuntimeError("feishu down")),
    )
    monkeypatch.setattr(
        watchdog,
        "create_delivery_fallback_issue",
        lambda _env, **_kwargs: "https://github.com/wangzitian0/infra2/issues/999",
    )
    monkeypatch.setattr(
        watchdog, "_emit_structured_log", lambda payload: emitted.append(dict(payload))
    )

    assert watchdog.main({"WATCHDOG_DRY_RUN": "0"}) == 1
    delivery_events = [
        row for row in emitted if row.get("event") == "watchdog.delivery.failure"
    ]
    assert len(delivery_events) == 1
    assert delivery_events[0]["status"] == "fail"
    assert delivery_events[0]["fallback_issue_url"].endswith("/issues/999")


def test_main_records_delivery_success_event_for_weekly_recall_audit(
    monkeypatch,
) -> None:
    """Infra-012.8: delivery success is logged so weekly digest can audit recall."""
    watchdog = _load_watchdog()
    emitted: list[dict] = []
    delivered: list[str] = []

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "infra2-public-entrypoint", False, "connection refused"
            )
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_status_check",
        lambda _env, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: [],
    )
    monkeypatch.setattr(
        watchdog,
        "deliver_out_of_band_alert",
        lambda _env, message: delivered.append(message),
    )
    monkeypatch.setattr(
        watchdog, "_emit_structured_log", lambda payload: emitted.append(dict(payload))
    )

    assert watchdog.main({"WATCHDOG_DRY_RUN": "0"}) == 1
    assert len(delivered) == 1
    delivery_events = [
        row for row in emitted if row.get("event") == "watchdog.delivery.success"
    ]
    assert delivery_events == [
        {
            "event": "watchdog.delivery.success",
            "status": "ok",
            "failure_count": 1,
            "route": "github-actions->feishu-direct",
        }
    ]


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


def test_emit_structured_log_adds_timestamp(capsys) -> None:
    """Infra-012.4: structured log helper injects timestamp when omitted."""
    watchdog = _load_watchdog()

    watchdog._emit_structured_log({"event": "watchdog.check", "name": "probe-a"})
    line = capsys.readouterr().out.strip()
    payload = json.loads(line)

    assert payload["event"] == "watchdog.check"
    assert payload["name"] == "probe-a"
    assert isinstance(payload["timestamp"], int)


def test_create_delivery_fallback_issue_returns_none_without_token() -> None:
    """Infra-012.12: fallback issue creation requires GitHub token and repo."""
    watchdog = _load_watchdog()
    issue_url = watchdog.create_delivery_fallback_issue(
        {},
        failures=[
            watchdog.CheckResult(
                "infra2-public-entrypoint",
                False,
                "connection refused",
                "host-reachability",
            )
        ],
        error="feishu down",
    )
    assert issue_url is None


def test_docs_do_not_resurrect_route_canary_in_fallback_scope() -> None:
    """#543: docs must match the code-owned fallback watchdog scope.

    The hourly synthetic-compose route canary is retired; the README must not
    advertise it as fallback coverage, and the SSOT may only mention it in the
    SOP-007 retirement note.
    """
    readme = ALERTING_README.read_text(encoding="utf-8")
    ssot = ALERTING_SSOT.read_text(encoding="utf-8")
    assert "route canary" not in readme.lower()
    assert "ops-checks.yml" in readme
    assert "已退役" in ssot.split("SOP-007:", 1)[1].split("SOP-007B", 1)[0]


def _dokploy_projects_fixture():
    return [
        {
            "name": "finance-report",
            "environments": [
                {
                    "name": "production",
                    "compose": [
                        {
                            "name": "backend",
                            "composeId": "compose-prod-backend",
                            "composeStatus": "error",
                        },
                        {"name": "frontend", "composeStatus": "done"},
                    ],
                    "applications": [
                        {"name": "worker", "applicationStatus": "running"},
                    ],
                },
                {
                    "name": "staging",
                    "compose": [
                        {
                            "name": "backend",
                            "composeId": "compose-staging-backend",
                            "composeStatus": "error",
                        },
                        {"name": "frontend", "composeStatus": "idle"},
                    ],
                },
            ],
        }
    ]


def test_dokploy_status_check_fails_closed_without_api_key() -> None:
    """#543: the retired route canary owned this signal; now this check does.

    Infra-011.13 requires missing Dokploy API credentials to classify as a
    ``configuration`` failure rather than silently skipping the status sweep.
    """
    watchdog = _load_watchdog()

    def factory(*, host):  # pragma: no cover - must not be called
        raise AssertionError("client_factory must not run without an API key")

    results = watchdog.run_dokploy_status_check({}, client_factory=factory)
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].failure_domain == "configuration"
    assert "DOKPLOY_API_KEY" in results[0].detail


def test_dokploy_status_check_flags_error_and_ignores_idle_done_running() -> None:
    """Infra-011.9: composeStatus=error is an authoritative alert source."""
    watchdog = _load_watchdog()
    captured = {}

    class FakeClient:
        def list_projects(self):
            return _dokploy_projects_fixture()

        def get_latest_deployment(self, compose_id):
            return {
                "deploymentId": f"deploy-{compose_id}",
                "status": "error",
                "logPath": f"/var/lib/dokploy/{compose_id}.log",
                "errorMessage": "image pull failed",
            }

    def factory(*, host):
        captured["host"] = host
        return FakeClient()

    results = watchdog.run_dokploy_status_check(
        {"DOKPLOY_API_KEY": "secret"},
        client_factory=factory,
    )

    assert captured["host"] == "cloud.zitian.party"
    names = {result.name: result for result in results}
    assert set(names) == {
        "dokploy-status:finance-report/production/backend",
        "dokploy-status:finance-report/staging/backend",
    }
    for result in results:
        assert result.ok is False
        assert result.failure_domain == "dokploy-deploy-status"
        assert result.detail.startswith("composeStatus=error composeId=compose-")
        assert "latest_deployment_status=error" in result.detail
        assert "latest_deployment_logPath=/var/lib/dokploy/compose-" in result.detail
        assert "latest_deployment_errorMessage=image pull failed" in result.detail


def test_dokploy_status_check_maps_prod_to_p1_and_staging_to_p2() -> None:
    """Infra-011.9: prod deploy errors page louder than staging/preview."""
    watchdog = _load_watchdog()

    prod = "dokploy-status:finance-report/production/backend"
    staging = "dokploy-status:finance-report/staging/backend"
    assert watchdog._severity_for(prod, "dokploy-deploy-status") == "P1"
    assert watchdog._severity_for(staging, "dokploy-deploy-status") == "P2"
    assert (
        watchdog._severity_for(
            "dokploy-status:app/env-pr-7/backend", "dokploy-deploy-status"
        )
        == "P2"
    )


def test_healthy_runtime_reclassifies_dokploy_error_without_silencing_it() -> None:
    watchdog = _load_watchdog()
    results = [
        watchdog.CheckResult(
            "dokploy-status:truealpha/production/postgres",
            False,
            "composeStatus=error",
            "dokploy-deploy-status",
        ),
        watchdog.CheckResult(
            "infra2-docker-health",
            True,
            "docker-health-ok",
            "docker-runtime",
        ),
    ]

    correlated = watchdog.correlate_control_plane_with_runtime(results)

    finding = correlated[0]
    assert finding.ok is False
    assert finding.failure_domain == "state-discrepancy"
    assert "runtime_evidence=infra2-docker-health:ok" in finding.detail
    assert watchdog._severity_for(finding.name, finding.failure_domain) == "P2"


def test_failed_runtime_keeps_dokploy_failure_in_deploy_domain() -> None:
    watchdog = _load_watchdog()
    results = [
        watchdog.CheckResult(
            "dokploy-status:truealpha/production/postgres",
            False,
            "composeStatus=error",
            "dokploy-deploy-status",
        ),
        watchdog.CheckResult(
            "infra2-docker-health",
            False,
            "postgres unhealthy",
            "docker-runtime",
        ),
    ]

    assert watchdog.correlate_control_plane_with_runtime(results) == results


def test_dokploy_status_check_turns_client_exception_into_alert() -> None:
    """Infra-011.9: control-plane query errors must become alerts, not crashes."""
    watchdog = _load_watchdog()

    def factory(*, host):
        raise RuntimeError("dokploy api 503")

    results = watchdog.run_dokploy_status_check(
        {"DOKPLOY_API_KEY": "secret"},
        client_factory=factory,
    )

    assert results == [
        watchdog.CheckResult(
            "infra2-dokploy-status",
            False,
            "dokploy status query raised RuntimeError: dokploy api 503",
            "dokploy-control-plane",
        )
    ]


def test_format_failure_message_shows_max_severity_and_per_line_tags() -> None:
    """Infra-011.9: header carries the worst severity, each line its own tag."""
    watchdog = _load_watchdog()
    results = [
        watchdog.CheckResult(
            name="dokploy-status:app/staging/backend",
            ok=False,
            detail="composeStatus=error",
            failure_domain="dokploy-deploy-status",
            severity="P2",
        ),
        watchdog.CheckResult(
            name="infra2-ssh",
            ok=False,
            detail="ssh exited 255",
            failure_domain="host-reachability",
            severity="P0",
        ),
        watchdog.CheckResult(
            name="infra2-worker-status",
            ok=False,
            detail="status=fail",
            failure_domain="dokploy-control-plane",
            severity="P1",
        ),
    ]

    message = watchdog.format_failure_message(results, run_url="")

    assert "Severity: P0" in message
    assert "- [P0] [host-reachability] infra2-ssh:" in message
    assert "- [P1] [dokploy-control-plane] infra2-worker-status:" in message
    assert "- [P2] [dokploy-deploy-status] dokploy-status:app/staging/backend:" in (
        message
    )


def test_docker_health_command_covers_created_and_exited_containers() -> None:
    """Infra-011.9: docker-health net must catch crashed/never-started containers."""
    watchdog = _load_watchdog()

    docker_health = next(
        target
        for target in watchdog.parse_ssh_targets("")
        if target.name == "infra2-docker-health"
    )

    assert "status=created" in docker_health.command
    assert "status=exited" in docker_health.command
    # Exit code 0 (clean one-shot completion) must be excluded from alerts.
    assert '[ "$code" = "0" ]' in docker_health.command
    assert "{{.State.ExitCode}}" in docker_health.command


# --- truealpha#876: issue trail + scheduler peer liveness ---------------------------


def _patch_checks(monkeypatch, watchdog, *, dokploy=(), peer=()) -> None:
    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "infra2-public-entrypoint", True, "HTTP 200", "host-reachability"
            )
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "cloudflare-worker-status",
                False,
                "worker status unhealthy: token=abc",
                "cloudflare-worker-health",
            )
        ],
    )
    monkeypatch.setattr(
        watchdog, "run_dokploy_status_check", lambda _env, **_kwargs: list(dokploy)
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: list(peer),
    )
    monkeypatch.setattr(watchdog, "deliver_out_of_band_alert", lambda _env, _msg: None)


def test_main_records_every_verdict_for_the_issue_trail(monkeypatch, tmp_path) -> None:
    """truealpha#876 W4: the watchdog hands the trail every verdict, green included."""
    from libs.watchdog_issue_trail import load_trail

    watchdog = _load_watchdog()
    peer = watchdog.CheckResult(
        watchdog.PEER_SCHEDULER_LIVENESS_CHECK,
        False,
        "STALE: newest scheduled run is 20.0h old",
        "peer-scheduler-liveness",
    )
    _patch_checks(monkeypatch, watchdog, peer=[peer])
    path = tmp_path / "verdicts.jsonl"

    assert (
        watchdog.main(
            {"WATCHDOG_DRY_RUN": "0", "INFRA2_WATCHDOG_VERDICTS_PATH": str(path)}
        )
        == 1
    )

    trail = load_trail(path, expected_sources=["out-of-band-watchdog"])
    assert set(trail.failing) == {
        "cloudflare-worker-status",
        watchdog.PEER_SCHEDULER_LIVENESS_CHECK,
    }
    assert trail.failing[watchdog.PEER_SCHEDULER_LIVENESS_CHECK].severity == "P1"
    # the watchdog's own redaction runs before anything leaves the step
    assert "abc" not in trail.failing["cloudflare-worker-status"].detail
    assert {"infra2-public-entrypoint", "infra2-dokploy-status"} <= trail.green
    # the status query answered, so a Dokploy unit that is absent is green
    assert trail.is_green("dokploy-status:finance-report/production/backend")


def test_main_does_not_green_the_dokploy_family_when_the_query_failed(
    monkeypatch, tmp_path
) -> None:
    from libs.watchdog_issue_trail import load_trail

    watchdog = _load_watchdog()
    failed = watchdog.CheckResult(
        watchdog.DOKPLOY_STATUS_CHECK, False, "raised", "dokploy-control-plane"
    )
    _patch_checks(monkeypatch, watchdog, dokploy=[failed])
    path = tmp_path / "verdicts.jsonl"

    watchdog.main({"WATCHDOG_DRY_RUN": "1", "INFRA2_WATCHDOG_VERDICTS_PATH": str(path)})

    trail = load_trail(path, expected_sources=["out-of-band-watchdog"])
    assert watchdog.DOKPLOY_STATUS_CHECK in trail.failing
    assert not trail.is_green("dokploy-status:finance-report/production/backend")


def test_main_runs_the_peer_check_with_the_watchdog_retry_settings(monkeypatch) -> None:
    watchdog = _load_watchdog()
    _patch_checks(monkeypatch, watchdog)
    captured = {}

    def fake_peer(env, timeout, **kwargs):
        captured.update(kwargs, timeout=timeout)
        return []

    monkeypatch.setattr(watchdog, "run_peer_scheduler_liveness_check", fake_peer)
    watchdog.main(
        {
            "WATCHDOG_DRY_RUN": "1",
            "INFRA2_WATCHDOG_HTTP_TIMEOUT": "7",
            "INFRA2_WATCHDOG_RETRY_MAX_ATTEMPTS": "3",
            "INFRA2_WATCHDOG_RETRY_DELAY_SECONDS": "5",
        }
    )
    assert captured == {"timeout": 7.0, "max_attempts": 3, "retry_delay_seconds": 5.0}


def _peer_factory(verdicts, seen):
    from libs.scheduler_peer_liveness import PeerVerdict

    answers = iter(verdicts)

    def factory(token, *, timeout):
        seen.append((token, timeout))
        return object()

    def evaluate(_getter, *, now, bound_cap):
        seen.append(("evaluate", bound_cap))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return PeerVerdict(*answer)

    return factory, evaluate


def test_peer_check_green_and_red_verdicts(monkeypatch) -> None:
    """truealpha#876: the peer verdict becomes one watchdog check."""
    watchdog = _load_watchdog()
    seen: list = []
    factory, evaluate = _peer_factory(
        [
            ("OK", "fresh"),
            ("STALE", "20.0h old"),
            ("DISABLED", "state='disabled_inactivity'"),
        ],
        seen,
    )
    monkeypatch.setattr(watchdog, "evaluate_peer_liveness", evaluate)
    env = {"GITHUB_TOKEN": " tok "}

    (green,) = watchdog.run_peer_scheduler_liveness_check(
        env, 4.0, getter_factory=factory
    )
    assert green.ok and green.name == "truealpha-scheduler-liveness"
    assert green.detail == "OK: fresh"
    assert seen[:2] == [("tok", 4.0), ("evaluate", None)]

    (stale,) = watchdog.run_peer_scheduler_liveness_check(
        env, getter_factory=factory, retry_delay_seconds=0
    )
    assert not stale.ok and stale.failure_domain == "peer-scheduler-liveness"
    assert stale.attempt_count == 1  # a verdict is not retried
    (disabled,) = watchdog.run_peer_scheduler_liveness_check(
        env, getter_factory=factory
    )
    assert disabled.detail.startswith("DISABLED")
    assert watchdog._severity_for(stale.name, stale.failure_domain) == "P1"
    assert "scheduler-liveness" in watchdog._suggested_action_for_failure(
        stale.name, stale.failure_domain
    )
    assert watchdog._runbook_url_for_failure(stale.failure_domain).endswith(
        "truealpha/actions/workflows/scheduler-liveness.yml"
    )


def test_peer_check_unreadable_is_red_after_one_retry(monkeypatch) -> None:
    watchdog = _load_watchdog()
    seen: list = []
    factory, evaluate = _peer_factory(
        [
            RuntimeError("boom"),
            ("UNVERIFIABLE", "HTTP 403"),
            ("UNVERIFIABLE", "x"),
            ("OK", "fresh"),
        ],
        seen,
    )
    monkeypatch.setattr(watchdog, "evaluate_peer_liveness", evaluate)
    sleeps: list = []
    monkeypatch.setattr(watchdog.time, "sleep", sleeps.append)

    (red,) = watchdog.run_peer_scheduler_liveness_check(
        {}, getter_factory=factory, max_attempts=2, retry_delay_seconds=9
    )
    assert not red.ok and red.detail == "UNVERIFIABLE: HTTP 403"
    assert red.attempt_count == 2 and sleeps == [9]

    (recovered,) = watchdog.run_peer_scheduler_liveness_check(
        {}, getter_factory=factory, max_attempts=2, retry_delay_seconds=0
    )
    assert recovered.ok and recovered.attempt_count == 2


def test_peer_check_bound_cap_is_passed_and_validated(monkeypatch) -> None:
    from datetime import timedelta

    watchdog = _load_watchdog()
    seen: list = []
    factory, evaluate = _peer_factory([("STALE", "capped")], seen)
    monkeypatch.setattr(watchdog, "evaluate_peer_liveness", evaluate)

    (drill,) = watchdog.run_peer_scheduler_liveness_check(
        {"INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "0"}, getter_factory=factory
    )
    assert not drill.ok
    assert ("evaluate", timedelta(0)) in seen

    (bad,) = watchdog.run_peer_scheduler_liveness_check(
        {"INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "soon"}, getter_factory=factory
    )
    assert not bad.ok and bad.failure_domain == "configuration"
    assert "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS" in bad.detail


def test_workflow_records_the_watchdog_verdicts_as_issues() -> None:
    """truealpha#876 W4: the watchdog job's last step is the issue trail."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["watchdog"]
    names = [step.get("name") for step in job["steps"]]
    trail = job["steps"][-1]
    assert trail["name"] == "Record the verdicts as GitHub issues"
    assert (
        names.index("Run watchdog")
        < names.index(
            "Runner health — 1Password service account and deploy prerequisites"
        )
        < names.index(trail["name"])
    )
    assert trail["if"] == "${{ !cancelled() }}"
    assert trail["run"] == "python tools/watchdog_issue_trail.py"
    assert job["env"]["INFRA2_WATCHDOG_VERDICTS_PATH"].endswith(
        "watchdog-verdicts.jsonl"
    )
    assert (
        "inputs.ssh_targets_override"
        in job["env"]["INFRA2_WATCHDOG_SSH_TARGETS_OVERRIDDEN"]
    )
    health = job["steps"][
        names.index(
            "Runner health — 1Password service account and deploy prerequisites"
        )
    ]
    assert (
        "RUNNER_HEALTH_SOURCE" in health["run"] and "record_verdicts" in health["run"]
    )
    # the Feishu page is still delivered, after the verdict is recorded
    assert health["run"].index("record(False") < health["run"].index(
        "deliver_out_of_band_alert("
    )


def test_the_drill_input_reaches_the_tool_only_through_env() -> None:
    """Untrusted dispatch text is never interpolated into a `run:` script."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
    assert dispatch["peer_liveness_bound_cap_hours"]["default"] == ""
    job = workflow["jobs"]["watchdog"]
    assert (
        "inputs.peer_liveness_bound_cap_hours"
        in job["env"]["INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS"]
    )
    for body in workflow["jobs"].values():
        for step in body["steps"]:
            assert "peer_liveness_bound_cap_hours" not in step.get("run", "")
            assert "inputs.ssh_targets_override" not in step.get("run", "")
