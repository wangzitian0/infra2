"""Offline contract tests for the infra2 out-of-band watchdog."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/out-of-band-watchdog.yml"
WATCHDOG = ROOT / "tools/out_of_band_watchdog.py"


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("out_of_band_watchdog", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    sys.modules["out_of_band_watchdog"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_workflow_runs_every_30_minutes_and_can_be_dispatched() -> None:
    """Infra-007.1: out-of-band watchdog is a 30-minute external schedule."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["on"]["schedule"] == [{"cron": "*/30 * * * *"}]
    assert "workflow_dispatch" in workflow["on"]
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


def test_default_targets_cover_public_host_and_bridge_health() -> None:
    """Infra-007.2/Infra-007.3: defaults cover host reachability and bridge."""
    watchdog = _load_watchdog()

    http_targets = watchdog.parse_http_targets("")
    assert [target.name for target in http_targets] == ["infra2-public-entrypoint"]
    assert http_targets[0].url == "https://cloud.zitian.party"

    ssh_targets = watchdog.parse_ssh_targets("")
    assert [target.name for target in ssh_targets] == [
        "infra2-ssh",
        "infra2-docker",
        "infra2-alert-bridge",
    ]
    assert ssh_targets[0].command == "echo infra2-ssh-ok"
    assert "docker info" in ssh_targets[1].command
    assert "docker exec platform-alerting" in ssh_targets[2].command
    assert "127.0.0.1:8080/health" in ssh_targets[2].command
    assert ssh_targets[2].expected_text == "healthy"


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
        ),
        watchdog.CheckResult(
            name="infra2-alert-bridge",
            ok=False,
            detail="ssh command did not contain expected text: secret-token",
        ),
    ]

    message = watchdog.format_failure_message(results, run_url="https://github/run/1")

    assert "[OUT-OF-BAND] Infra2 watchdog failed" in message
    assert "Route: GitHub Actions -> Feishu direct" in message
    assert "infra2-iac-runner" in message
    assert "infra2-alert-bridge" in message
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
