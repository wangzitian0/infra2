"""Offline contract tests for the infra2 out-of-band watchdog."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ops-checks.yml"
WATCHDOG = ROOT / "tools/out_of_band_watchdog.py"
ALERTING_README = ROOT / "platform/12.alerting/README.md"
ALERTING_SSOT = ROOT / "docs/ssot/ops.observability.md"


#: #908: a configured report path; without it the watchdog pages its own config.
REPORT_ENV = {
    "INFRA2_REPORTS_FEISHU_APP_ID": "cli_report",
    "INFRA2_REPORTS_FEISHU_APP_SECRET": "report-secret",
    "INFRA2_REPORTS_FEISHU_CHAT_ID": "oc_report",
}


@pytest.fixture(autouse=True)
def _no_real_feishu(monkeypatch):
    """No test here may reach Feishu: a real send would post to a live chat."""
    import libs.alerting as alerting

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to deliver to Feishu for real")

    monkeypatch.setattr(alerting, "deliver_feishu_app_text", refuse)
    monkeypatch.setattr(alerting, "deliver_feishu_text", refuse)


def _page_blocks(message: str) -> list[dict[str, str]]:
    """A page's item blocks (#905): ``{label: value}`` in message order."""
    from libs.alerting import FIELD_SEPARATOR, PAGER_FIELDS

    blocks: list[dict[str, str]] = []
    for line in message.splitlines():
        label, separator, value = line.partition(FIELD_SEPARATOR)
        if re.fullmatch(r"— \d+/\d+ —", line):
            blocks.append({})
        elif blocks and separator and label in PAGER_FIELDS:
            blocks[-1][label] = value
    return blocks


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
    # #908: the Worker's /health always answered ok; /status freshness replaced it.
    assert [target.name for target in http_targets] == ["infra2-public-entrypoint"]
    assert http_targets[0].url == "https://cloud.zitian.party"

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

    bound = watchdog.worker_status_max_age_seconds()
    assert results == [
        watchdog.CheckResult(
            "cloudflare-worker-status",
            True,
            f"worker last run fresh: age=1800s (max {bound}s)",
        )
    ]
    assert captured == {
        "authorization": "Bearer status-token",
        "timeout": 3,
        "url": watchdog.DEFAULT_WORKER_STATUS_URL,
    }


def _worker_status_response(monkeypatch, watchdog, last_run: dict) -> list:
    """Serve one /status body built from `last_run`; return the request log."""
    served: list = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(
                {"ok": last_run.get("ok", True), "lastRun": last_run}
            ).encode()

    def fake_urlopen(request, **_kwargs):
        served.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(watchdog, "urlopen", fake_urlopen)
    return served


#: A fresh run that found failures and paged them itself.
DELIVERED_FAILING_RUN = {
    "ok": False,
    "ageSeconds": 890,
    "failureCount": 2,
    "routeTargetCount": 8,
    "heartbeatTargetCount": 2,
    "deliveryError": "",
}
#: The same run, but its pages never arrived.
UNDELIVERED_RUN = {**DELIVERED_FAILING_RUN, "deliveryError": "feishu delivery failed"}


def test_worker_status_is_green_on_a_fresh_run_that_delivered_its_findings(
    monkeypatch,
) -> None:
    """#908: the Worker paged its own findings; the audit judges only its liveness."""
    watchdog = _load_watchdog()
    _worker_status_response(monkeypatch, watchdog, DELIVERED_FAILING_RUN)

    (result,) = watchdog.run_worker_status_check(
        {"INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "status-token"},
        timeout=3,
        retry_delay_seconds=0,
        max_age_seconds=7200,
    )

    assert result.ok is True
    assert result.attempt_count == 1
    assert result.detail == "worker last run fresh: age=890s (max 7200s)"
    # ...and what the run found is carried to the report as information
    assert "delivered by the Worker itself: ok=False failures=2" in result.note
    assert "routes=8 heartbeats=2" in result.note


def test_worker_status_pages_when_its_last_run_could_not_deliver(monkeypatch) -> None:
    """Liveness is "runs AND delivers": undelivered pages are a blind Worker."""
    watchdog = _load_watchdog()
    served = _worker_status_response(monkeypatch, watchdog, UNDELIVERED_RUN)

    (result,) = watchdog.run_worker_status_check(
        {"INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "status-token"},
        timeout=3,
        retry_delay_seconds=0,
        max_age_seconds=7200,
    )

    assert (result.ok, result.failure_domain) == (False, "cloudflare-worker-health")
    assert result.detail == (
        "worker runs but could not deliver its alerts: age=890s failures=2 "
        "delivery_error=feishu delivery failed"
    )
    assert len(served) == 2  # retried once before turning red


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (7201, "worker last run is stale: age=7201s > max 7200s"),
        (-301, "worker last run is 301s in the future (clock skew?)"),
        (None, "worker has no recorded run: ageSeconds=None"),
    ],
)
def test_worker_status_is_red_when_the_last_run_is_not_fresh(
    monkeypatch, age, expected
) -> None:
    watchdog = _load_watchdog()
    served = _worker_status_response(
        monkeypatch, watchdog, {"ok": True, "ageSeconds": age}
    )

    (result,) = watchdog.run_worker_status_check(
        {"INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "status-token"},
        timeout=3,
        retry_delay_seconds=0,
        max_age_seconds=7200,
    )

    assert (result.ok, result.detail) == (False, expected)
    assert result.failure_domain == "cloudflare-worker-health"
    assert len(served) == 2  # retried once before turning red


def test_worker_status_is_green_at_exactly_the_bound(monkeypatch) -> None:
    watchdog = _load_watchdog()
    _worker_status_response(monkeypatch, watchdog, {"ok": True, "ageSeconds": 7200})

    (result,) = watchdog.run_worker_status_check(
        {"INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "t"},
        timeout=3,
        retry_delay_seconds=0,
        max_age_seconds=7200,
    )

    assert result.ok is True
    assert result.note == ""  # a clean run has nothing to report


def test_worker_status_bound_is_the_workers_own_configured_max_age(tmp_path) -> None:
    """One value: the audit reads the Worker's WATCHDOG_STATUS_MAX_AGE_SECONDS."""
    import tomllib

    watchdog = _load_watchdog()
    wrangler = tomllib.loads(watchdog.WORKER_WRANGLER.read_text(encoding="utf-8"))
    configured = int(wrangler["vars"]["WATCHDOG_STATUS_MAX_AGE_SECONDS"])

    assert watchdog.worker_status_max_age_seconds() == configured
    edited = tmp_path / "wrangler.toml"
    edited.write_text('[vars]\nWATCHDOG_STATUS_MAX_AGE_SECONDS = "600"\n')
    assert watchdog.worker_status_max_age_seconds(edited) == 600
    edited.write_text('[vars]\nWATCHDOG_STATUS_MAX_AGE_SECONDS = "0"\n')
    with pytest.raises(ValueError, match="not a positive bound"):
        watchdog.worker_status_max_age_seconds(edited)


def test_an_unreadable_worker_bound_is_a_red_configuration_failure(
    monkeypatch,
) -> None:
    watchdog = _load_watchdog()

    def unreadable():
        raise KeyError("vars")

    monkeypatch.setattr(watchdog, "worker_status_max_age_seconds", unreadable)
    (result,) = watchdog.run_worker_status_check(
        {"INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "t"}, timeout=3
    )

    assert (result.ok, result.failure_domain) == (False, "configuration")
    assert "WATCHDOG_STATUS_MAX_AGE_SECONDS" in result.detail


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
    lines = message.splitlines()

    assert lines[:3] == [
        "🟠 [P1 告警] GitHub 日级带外审计 · 2 项",
        "来源：GitHub Actions 日级带外审计 → 飞书直发(不经 bridge)",
        "运行：https://github/run/1",
    ]
    iac, bridge = _page_blocks(message)
    assert iac["对象"] == "infra2-iac-runner"
    assert iac["影响"].startswith("[host-diagnostics] ")
    assert bridge["对象"] == "infra2-alert-bridge"
    assert bridge["影响"].startswith("[alert-bridge] ")
    assert bridge["下一步"] == watchdog._suggested_action_for_failure(
        "infra2-alert-bridge", "alert-bridge"
    )
    assert bridge["Runbook"] == watchdog._runbook_url_for_failure("alert-bridge")
    assert "secret-token" not in message


def test_host_failures_link_to_concrete_p0_runbooks() -> None:
    watchdog = _load_watchdog()
    assert watchdog._runbook_url_for_failure("host-reachability").endswith(
        "docs/runbooks/infra022-p0.md#watchdog-silent"
    )
    assert watchdog._runbook_url_for_failure("docker-runtime").endswith(
        "docs/runbooks/infra022-p0.md#container-killed"
    )


def test_main_sends_feishu_only_when_a_paging_check_fails(monkeypatch) -> None:
    """Infra-007.2: successful checks stay quiet, paging failures send direct Feishu."""
    watchdog = _load_watchdog()
    sent_messages: list[str] = []
    reports: list[str] = []
    worker = [watchdog.CheckResult("cloudflare-worker-status", True, "fresh")]

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            watchdog.CheckResult("infra2-public-entrypoint", True, "HTTP 200")
        ],
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(watchdog, "run_backup_checks", lambda _config: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: list(worker),
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
    monkeypatch.setattr(
        watchdog, "deliver_infra2_report", lambda text, _env: reports.append(text)
    )
    env = {**REPORT_ENV, "WATCHDOG_DRY_RUN": "0"}

    assert watchdog.main(env) == 0
    assert sent_messages == []
    assert reports == []

    worker[:] = [
        watchdog.CheckResult(
            "cloudflare-worker-status", False, "stale", "cloudflare-worker-health"
        )
    ]

    assert (
        watchdog.main(
            {
                **env,
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "wangzitian0/infra2",
                "GITHUB_RUN_ID": "123",
            }
        )
        == 1
    )
    assert len(sent_messages) == 1
    (block,) = _page_blocks(sent_messages[0])
    assert block["对象"] == "cloudflare-worker-status"
    assert block["现象"] == "stale"


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
    monkeypatch.setattr(watchdog, "run_backup_checks", lambda _config: [])
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

    assert watchdog.main({**REPORT_ENV, "WATCHDOG_DRY_RUN": "0"}) == 0
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
    monkeypatch.setattr(watchdog, "run_backup_checks", lambda _config: [])
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
            **REPORT_ENV,
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
        watchdog, "run_http_checks", lambda _targets, _timeout, **_kwargs: []
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(watchdog, "run_backup_checks", lambda _config: [])
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
        lambda _env, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "truealpha-scheduler-liveness",
                False,
                "STALE: 20h old",
                "peer-scheduler-liveness",
            )
        ],
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

    assert watchdog.main({**REPORT_ENV, "WATCHDOG_DRY_RUN": "0"}) == 1
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
        watchdog, "run_http_checks", lambda _targets, _timeout, **_kwargs: []
    )
    monkeypatch.setattr(watchdog, "run_ssh_checks", lambda _config, _targets: [])
    monkeypatch.setattr(watchdog, "run_backup_checks", lambda _config: [])
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
        lambda _env, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "truealpha-scheduler-liveness",
                False,
                "STALE: 20h old",
                "peer-scheduler-liveness",
            )
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "deliver_out_of_band_alert",
        lambda _env, message: delivered.append(message),
    )
    monkeypatch.setattr(
        watchdog, "_emit_structured_log", lambda payload: emitted.append(dict(payload))
    )

    assert watchdog.main({**REPORT_ENV, "WATCHDOG_DRY_RUN": "0"}) == 1
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


NOW_TS = 1_790_000_000.0


def _dokploy_error(unit: str, *, recorded_hours_ago: float | None):
    watchdog = _load_watchdog()
    return watchdog.CheckResult(
        f"dokploy-status:truealpha/production/{unit}",
        False,
        "composeStatus=error",
        "dokploy-deploy-status",
        recorded_at=None
        if recorded_hours_ago is None
        else NOW_TS - recorded_hours_ago * 3600,
    )


CURRENT = "c" * 64
OLD = "0" * 64


def _container(
    name, *, state="running", exit_code="0", health="healthy", config=CURRENT
):
    watchdog = _load_watchdog()
    return watchdog.UnitContainer(name, state, exit_code, health, config)


def test_a_dokploy_record_is_stale_only_with_per_unit_runtime_evidence() -> None:
    """#908 review: age and a host-wide sweep prove nothing about one unit."""
    watchdog = _load_watchdog()
    proven = replace(
        _dokploy_error("postgres", recorded_hours_ago=1),
        runtime_evidence="1 container(s) of this unit run healthy on the current config",
    )
    old = _dokploy_error("redis", recorded_hours_ago=80)
    at_bound = _dokploy_error("app", recorded_hours_ago=72)
    no_time = _dokploy_error("worker", recorded_hours_ago=None)
    sweep = watchdog.CheckResult("infra2-docker-health", True, "docker-health-ok")

    kept, stale = watchdog.split_stale_dokploy_records(
        [proven, old, at_bound, no_time, sweep], now_ts=NOW_TS
    )

    assert stale == [(proven, proven.runtime_evidence)]
    # an old record with no per-unit evidence is still a failure, labelled truthfully
    assert kept[0].name == old.name and not kept[0].ok
    assert kept[0].detail == (
        "composeStatus=error; old record: latest deployment is 80h old (over 72h), "
        "and no per-unit runtime evidence shows the unit recovered"
    )
    # a green host-wide sweep no longer stales anything
    assert kept[1:] == [at_bound, no_time, sweep]


@pytest.mark.parametrize(
    ("containers", "expected_hash", "proven", "why"),
    [
        (
            [_container("app"), _container("init", state="exited", config="")],
            CURRENT,
            True,
            "2 container(s) of this unit run healthy on the current config cccccccccccc",
        ),
        (
            # the reviewer's case: the pull failed, the old version runs healthy
            [_container("app", config=OLD)],
            CURRENT,
            False,
            "app runs config 000000000000, current is cccccccccccc",
        ),
        (
            [_container("app"), _container("db", health="unhealthy")],
            CURRENT,
            False,
            "db is running (health unhealthy, exit 0)",
        ),
        (
            [_container("app"), _container("migrate", state="exited", exit_code="1")],
            CURRENT,
            False,
            "migrate is exited (health healthy, exit 1)",
        ),
        (
            [_container("app", config="")],
            CURRENT,
            False,
            "no container carries IAC_CONFIG_HASH",
        ),
        ([], CURRENT, False, "no container of this unit on the host"),
        (
            [_container("app")],
            "",
            False,
            "Dokploy holds no IAC_CONFIG_HASH for this unit, so its current config "
            "cannot be matched",
        ),
        (
            [_container("init", state="exited", config=CURRENT)],
            CURRENT,
            False,
            "no container is running",
        ),
    ],
    ids=[
        "current-and-healthy",
        "old-version-still-running",
        "unhealthy",
        "one-shot-failed",
        "no-config-identity",
        "no-containers",
        "no-current-hash",
        "nothing-running",
    ],
)
def test_unit_runtime_verdict(containers, expected_hash, proven, why) -> None:
    watchdog = _load_watchdog()

    assert watchdog.unit_runtime_verdict(containers, expected_hash) == (proven, why)


def test_an_unreadable_container_line_is_no_evidence() -> None:
    """Skipping a line could drop the one unhealthy container and prove the unit."""
    watchdog = _load_watchdog()

    (app,) = watchdog.parse_unit_containers(f"/app|running|0|healthy|{CURRENT}\n")
    assert vars(app) == vars(_container("app"))
    with pytest.raises(ValueError, match="unreadable container line"):
        watchdog.parse_unit_containers(
            f"/app|running|0|healthy|{CURRENT}\n/db|running\n"
        )


def test_the_unit_runtime_command_runs_under_sh_against_a_fake_docker(
    tmp_path,
) -> None:
    """The real host command, with a `docker` on PATH that records its arguments."""
    watchdog = _load_watchdog()
    calls = tmp_path / "calls"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'case "$1" in\n'
        '  ps) if [ "$4" = "label=com.docker.compose.project=fr-app-x1" ]; then '
        'printf "id1\\nid2\\n"; fi ;;\n'
        f'  inspect) printf "/app|running|0|healthy|{CURRENT}\\n'
        f'/db|running|0|none|{CURRENT}\\n" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}

    def run(project):
        completed = subprocess.run(
            ["sh", "-c", watchdog.unit_runtime_command(project)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout

    code, out = run("fr-app-x1")
    assert code == 0
    assert watchdog.unit_runtime_verdict(
        watchdog.parse_unit_containers(out), CURRENT
    ) == (
        True,
        "2 container(s) of this unit run healthy on the current config cccccccccccc",
    )
    ps_call, inspect_call = calls.read_text().splitlines()
    assert ps_call == "ps -aq --filter label=com.docker.compose.project=fr-app-x1"
    assert inspect_call == f"inspect --format {watchdog.UNIT_RUNTIME_FORMAT} id1 id2"
    # a unit with no containers prints nothing and still exits 0: no evidence
    assert run("other") == (0, "")


def _evidence_client(*, env_text: str, created_at: str = "2026-09-24T00:00:00Z"):
    class FakeClient:
        def list_projects(self):
            return _dokploy_projects_fixture()

        def get_latest_deployment(self, compose_id):
            return {
                "deploymentId": f"deploy-{compose_id}",
                "status": "error",
                "errorMessage": "image pull failed",
                "createdAt": created_at,
            }

        def get_compose(self, compose_id):
            return {"appName": f"app-{compose_id}", "env": env_text}

    return FakeClient()


def test_dokploy_status_check_gathers_per_unit_runtime_evidence() -> None:
    watchdog = _load_watchdog()
    asked: list[str] = []

    def unit_runtime(project):
        asked.append(project)
        config = CURRENT if project == "app-compose-prod-backend" else OLD
        return 0, f"/backend|running|0|healthy|{config}\n", ""

    results = watchdog.run_dokploy_status_check(
        {"DOKPLOY_API_KEY": "secret"},
        client_factory=lambda *, host: _evidence_client(
            env_text=f"A=1\nIAC_CONFIG_HASH={CURRENT}\n"
        ),
        unit_runtime=unit_runtime,
    )

    assert asked == ["app-compose-prod-backend", "app-compose-staging-backend"]
    prod, staging = results
    assert prod.runtime_evidence.endswith("healthy on the current config cccccccccccc")
    assert "runtime:" not in prod.detail
    assert staging.runtime_evidence == ""
    assert staging.detail.endswith(
        "runtime: backend runs config 000000000000, current is cccccccccccc"
    )
    expected = datetime(2026, 9, 24, tzinfo=UTC).timestamp()
    assert [result.recorded_at for result in results] == [expected, expected]


@pytest.mark.parametrize(
    ("unit_runtime", "why"),
    [
        (None, "runtime: no host access for per-unit runtime evidence"),
        (
            lambda _project: (255, "", "ssh: connect to host vps port 22: refused"),
            "runtime: could not list the unit's containers (exit 255): "
            "ssh: connect to host vps port 22: refused",
        ),
        (
            lambda _project: (0, "garbage\n", ""),
            "runtime: runtime evidence unavailable: ValueError: unreadable "
            "container line: garbage",
        ),
    ],
    ids=["no-ssh", "host-unreachable", "unreadable-output"],
)
def test_no_per_unit_evidence_keeps_the_dokploy_error_a_failure(
    unit_runtime, why
) -> None:
    watchdog = _load_watchdog()

    results = watchdog.run_dokploy_status_check(
        {"DOKPLOY_API_KEY": "secret"},
        client_factory=lambda *, host: _evidence_client(
            env_text=f"IAC_CONFIG_HASH={CURRENT}"
        ),
        unit_runtime=unit_runtime,
    )

    assert [
        (result.runtime_evidence, result.detail.endswith(why)) for result in results
    ] == [
        ("", True),
        ("", True),
    ]


@pytest.mark.parametrize(
    ("status", "domain", "detail"),
    [
        (401, "configuration", "Dokploy rejected DOKPLOY_API_KEY: HTTP 401"),
        (403, "configuration", "Dokploy rejected DOKPLOY_API_KEY: HTTP 403"),
        (
            500,
            "dokploy-control-plane",
            "dokploy status query raised HTTPStatusError: HTTP 500",
        ),
    ],
)
def test_a_rejected_dokploy_key_is_a_configuration_failure(
    status, domain, detail
) -> None:
    """A watchdog that Dokploy refuses is blind: that pages, a 5xx is reported."""
    import httpx

    watchdog = _load_watchdog()
    request = httpx.Request("GET", "https://dokploy.invalid/api/project.all")

    class RejectingClient:
        def list_projects(self):
            raise httpx.HTTPStatusError(
                f"HTTP {status}",
                request=request,
                response=httpx.Response(status, request=request),
            )

    (result,) = watchdog.run_dokploy_status_check(
        {"DOKPLOY_API_KEY": "secret"}, client_factory=lambda *, host: RejectingClient()
    )

    assert (result.name, result.ok, result.failure_domain, result.detail) == (
        "infra2-dokploy-status",
        False,
        domain,
        detail,
    )


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

    assert message.startswith("🔴 [P0 告警] GitHub 日级带外审计 · 3 项\n")
    # the most severe first, each block with its own level
    assert [
        (block["级别"], block["影响"].split("]")[0], block["对象"])
        for block in _page_blocks(message)
    ] == [
        ("P0", "[host-reachability", "infra2-ssh"),
        ("P1", "[dokploy-control-plane", "infra2-worker-status"),
        ("P2", "[dokploy-deploy-status", "dokploy-status:app/staging/backend"),
    ]


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
    monkeypatch.setattr(watchdog, "run_backup_checks", lambda _config: [])
    monkeypatch.setattr(
        watchdog,
        "run_worker_status_check",
        lambda _env, _timeout, **_kwargs: [
            watchdog.CheckResult(
                "cloudflare-worker-status",
                False,
                "worker last run is stale: token=abc",
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
    monkeypatch.setattr(watchdog, "deliver_infra2_report", lambda _text, _env: True)


def test_main_records_the_paging_verdicts_for_the_issue_trail(
    monkeypatch, tmp_path
) -> None:
    """truealpha#876 W4 / #908: paging verdicts reach the trail, green included."""
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
            {
                **REPORT_ENV,
                "WATCHDOG_DRY_RUN": "0",
                "INFRA2_WATCHDOG_VERDICTS_PATH": str(path),
            }
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
    # a report-only check (green here) never reaches the trail
    assert trail.green == {"out-of-band-watchdog"}


def test_a_configuration_failure_on_a_report_only_check_pages_as_the_watchdog(
    monkeypatch, tmp_path
) -> None:
    """#908: a blind watchdog pages, under its own name, not the report-only check's.

    The next run that records cleanly closes that issue, which a report-only
    check's own name never would (it is never recorded again).
    """
    from libs.watchdog_issue_trail import load_trail

    watchdog = _load_watchdog()
    missing_key = watchdog.CheckResult(
        watchdog.DOKPLOY_STATUS_CHECK,
        False,
        "DOKPLOY_API_KEY is missing",
        "configuration",
    )
    _patch_checks(monkeypatch, watchdog, dokploy=[missing_key])
    pages: list[str] = []
    monkeypatch.setattr(
        watchdog, "deliver_out_of_band_alert", lambda _env, text: pages.append(text)
    )
    path = tmp_path / "verdicts.jsonl"

    watchdog.main(
        {
            **REPORT_ENV,
            "WATCHDOG_DRY_RUN": "0",
            "INFRA2_WATCHDOG_VERDICTS_PATH": str(path),
        }
    )

    assert {
        (block["对象"], block["现象"], block["级别"])
        for block in _page_blocks(pages[0])
    } >= {("infra2-dokploy-status", "DOKPLOY_API_KEY is missing", "P0")}
    trail = load_trail(path, expected_sources=["out-of-band-watchdog"])
    assert set(trail.failing) == {"cloudflare-worker-status", "out-of-band-watchdog"}
    own = trail.failing["out-of-band-watchdog"]
    assert own.detail == "infra2-dokploy-status: DOKPLOY_API_KEY is missing"
    assert (own.severity, own.failure_domain) == ("P0", "configuration")


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


# --- #908: the GitHub layer pages only its own failure classes ----------------------

#: ops.observability.md §1.1: Worker liveness, data protection, the peer scheduler.
PAGING = {
    "cloudflare-worker-status",
    "infra2-backup-production",
    "infra2-restore-rehearsal",
    "truealpha-scheduler-liveness",
}
_FAILURE_LINE = re.compile(r"^- \[P\d\] (?:\[[^\]]+\] )?(\S+): ", re.MULTILINE)
_PAGE_OBJECT = re.compile(r"^对象：(\S+)$", re.MULTILINE)


def _failure_names(message: str) -> set[str]:
    """The checks a page or report lists as failures: a page's 对象 lines (#905), a
    report's `- [P1] [domain] name: ...` lines."""
    return set(_FAILURE_LINE.findall(message)) | set(_PAGE_OBJECT.findall(message))


#: The GitHub checks another layer pages (#908): they only report.
REPORT_ONLY = {
    "infra2-public-entrypoint",
    "infra2-ssh",
    "infra2-docker",
    "infra2-docker-health",
    "infra2-alert-bridge",
    "infra2-backup-staging",
    "infra2-dokploy-status",
}


def test_the_report_only_set_is_read_from_the_signal_registry(tmp_path) -> None:
    """What is not `type: report` pages -- including names the registry never saw."""
    watchdog = _load_watchdog()

    assert watchdog.load_report_only_checks() == REPORT_ONLY
    inventory = yaml.safe_load(watchdog.SIGNAL_REGISTRY.read_text(encoding="utf-8"))
    for signal in inventory["signals"]:
        if signal["signal"] == "cloudflare-worker-status":
            signal.update(type="report", tier="day")
    edited = tmp_path / "watchdog-signals.yaml"
    edited.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    assert watchdog.load_report_only_checks(edited) == REPORT_ONLY | {
        "cloudflare-worker-status"
    }
    for signal in inventory["signals"]:
        if signal.get("primary_owner") == "github":
            signal.update(type="report", tier="day")
    edited.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    with pytest.raises(ValueError, match="no paging GitHub signal"):
        watchdog.load_report_only_checks(edited)


def test_every_check_pages_unless_the_registry_says_report() -> None:
    watchdog = _load_watchdog()
    report_only = watchdog.load_report_only_checks()

    def pages(name, domain="x"):
        return watchdog.pages(
            watchdog.CheckResult(name, False, "", domain), report_only
        )

    assert [pages(name) for name in sorted(PAGING)] == [True] * len(PAGING)
    assert [pages(name) for name in sorted(REPORT_ONLY)] == [False] * len(REPORT_ONLY)
    assert pages("dokploy-status:truealpha/production/app") is False
    assert pages("fr-preview-leak") is True  # a vars-only target the audit never saw
    assert pages("infra2-ssh", "configuration") is True  # a blind watchdog pages


def _run_day(
    monkeypatch,
    tmp_path,
    *,
    worker_last_run: dict,
    failing: set[str] = frozenset(),
    dokploy=(),
    env_extra: dict | None = None,
    report_error: Exception | None = None,
    registry_error: Exception | None = None,
    extra_ssh=(),
    dokploy_check=None,
):
    """One main() run with every check faked: `failing` names the red ones.

    The Worker's /status is served to the real run_worker_status_check, so the
    freshness judgment under test is the production one.
    """
    watchdog = _load_watchdog()
    _worker_status_response(monkeypatch, watchdog, worker_last_run)
    if registry_error is not None:

        def unreadable(*_args):
            raise registry_error

        monkeypatch.setattr(watchdog, "load_report_only_checks", unreadable)

    def result(name: str, domain: str):
        red = name in failing
        return watchdog.CheckResult(name, not red, "broken" if red else "ok", domain)

    monkeypatch.setattr(
        watchdog,
        "run_http_checks",
        lambda _targets, _timeout, **_kwargs: [
            result("infra2-public-entrypoint", "host-reachability")
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "run_ssh_checks",
        lambda _config, _targets: [
            result("infra2-ssh", "host-reachability"),
            result("infra2-docker", "docker-runtime"),
            result("infra2-docker-health", "docker-runtime"),
            result("infra2-alert-bridge", "alert-bridge"),
            *(result(name, "host-diagnostics") for name in extra_ssh),
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "run_backup_checks",
        lambda _config: [
            result("infra2-backup-production", "backup"),
            result("infra2-backup-staging", "backup"),
            result("infra2-restore-rehearsal", "restore-rehearsal"),
        ],
    )
    monkeypatch.setattr(
        watchdog,
        "run_dokploy_status_check",
        dokploy_check or (lambda _env, **_kwargs: list(dokploy)),
    )
    monkeypatch.setattr(
        watchdog,
        "run_peer_scheduler_liveness_check",
        lambda _env, _timeout, **_kwargs: [
            result("truealpha-scheduler-liveness", "peer-scheduler-liveness")
        ],
    )
    pages: list[str] = []
    reports: list[str] = []
    events: list[dict] = []

    def deliver_report(text, env):
        if report_error is not None:
            raise report_error
        assert env["INFRA2_REPORTS_FEISHU_CHAT_ID"] == "oc_report"
        reports.append(text)
        return True

    monkeypatch.setattr(
        watchdog, "deliver_out_of_band_alert", lambda _env, text: pages.append(text)
    )
    monkeypatch.setattr(watchdog, "deliver_infra2_report", deliver_report)
    monkeypatch.setattr(watchdog, "_emit_structured_log", events.append)
    verdicts = tmp_path / "verdicts.jsonl"
    env = {
        **REPORT_ENV,
        "WATCHDOG_DRY_RUN": "0",
        "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "status-token",
        "INFRA2_WATCHDOG_RETRY_DELAY_SECONDS": "0",
        "INFRA2_WATCHDOG_VERDICTS_PATH": str(verdicts),
        **(env_extra or {}),
    }
    code = watchdog.main(
        {key: value for key, value in env.items() if value is not None}
    )
    from libs.watchdog_issue_trail import load_trail

    trail = load_trail(verdicts, expected_sources=["out-of-band-watchdog"])
    return code, pages, reports, trail, events


FRESH_CLEAN_RUN = {"ok": True, "ageSeconds": 600, "failureCount": 0}


def test_main_pages_only_the_owned_classes_and_reports_the_rest(
    monkeypatch, tmp_path
) -> None:
    """#908 deliverables 1, 2, 3, 5 in one run.

    Everything is red except the rehearsal and the peer; the Worker's last run is
    fresh but found failures. Only the production backup pages; every
    report-only failure lands in the one report; the trail sees paging checks only.
    """
    now = datetime.now(UTC).timestamp()
    code, pages, reports, trail, events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=DELIVERED_FAILING_RUN,
        failing={
            "infra2-public-entrypoint",
            "infra2-ssh",
            "infra2-docker-health",
            "infra2-alert-bridge",
            "infra2-backup-production",
            "infra2-backup-staging",
        },
        dokploy=[
            replace(
                _dokploy_error("postgres", recorded_hours_ago=None),
                recorded_at=now - 1 * 3600,
            ),
            replace(
                _dokploy_error("redis", recorded_hours_ago=None),
                recorded_at=now - 100 * 3600,
                runtime_evidence="1 container(s) of this unit run healthy on the "
                "current config cccccccccccc",
            ),
            replace(
                _dokploy_error("app", recorded_hours_ago=None),
                recorded_at=now - 100 * 3600,
            ),
        ],
    )

    assert code == 1
    assert len(pages) == 1
    assert _failure_names(pages[0]) == {"infra2-backup-production"}
    assert len(reports) == 1
    assert _failure_names(reports[0]) == {
        "infra2-public-entrypoint",
        "infra2-ssh",
        "infra2-docker-health",
        "infra2-alert-bridge",
        "infra2-backup-staging",
        "dokploy-status:truealpha/production/postgres",
        "dokploy-status:truealpha/production/app",
    }
    failures, stale = reports[0].split("Stale Dokploy records (1)", 1)
    assert "dokploy-status:truealpha/production/redis" in stale
    assert "run healthy on the current config cccccccccccc" in stale
    # an old record without per-unit evidence is a failure, labelled with its age
    assert (
        "production/app: composeStatus=error; old record: latest deployment is 100h old"
        in failures
    )
    info = reports[0].split("Information:", 1)[1]
    assert "delivered by the Worker itself: ok=False failures=2" in info
    assert set(trail.failing) == {"infra2-backup-production"}
    assert trail.complete
    assert trail.green == {
        "out-of-band-watchdog",
        "cloudflare-worker-status",
        "infra2-restore-rehearsal",
        "truealpha-scheduler-liveness",
    }
    routes = {
        event["name"]: event["route"]
        for event in events
        if event.get("event") == "watchdog.check" and event["status"] == "fail"
    }
    assert routes == {
        "infra2-backup-production": "page",
        **{name: "report" for name in _failure_names(reports[0])},
    }
    complete = [e for e in events if e.get("event") == "watchdog.run.complete"]
    assert [(e["failure_count"], e["report_failure_count"]) for e in complete] == [
        (1, 7)
    ]


@pytest.mark.parametrize(
    ("last_run", "paged", "code", "reported_note"),
    [
        (DELIVERED_FAILING_RUN, set(), 0, [True]),
        (UNDELIVERED_RUN, {"cloudflare-worker-status"}, 1, []),
        ({**FRESH_CLEAN_RUN, "ageSeconds": 7201}, {"cloudflare-worker-status"}, 1, []),
    ],
    ids=["fresh-and-delivered", "fresh-but-undelivered", "stale"],
)
def test_main_judges_the_worker_on_running_and_delivering(
    monkeypatch, tmp_path, last_run, paged, code, reported_note
) -> None:
    """#908 deliverable 3 (+ review): the Worker's delivered findings are
    information; a Worker that is stale or cannot deliver pages and opens the
    trail issue."""
    got_code, pages, reports, trail, _events = _run_day(
        monkeypatch, tmp_path, worker_last_run=last_run
    )

    assert got_code == code
    assert set().union(*map(_failure_names, pages)) == paged
    assert set(trail.failing) == paged
    assert ("cloudflare-worker-status" in trail.green) is (not paged)
    assert ["ok=False failures=2" in report for report in reports] == reported_note


def test_main_sends_nothing_on_a_green_day(monkeypatch, tmp_path) -> None:
    code, pages, reports, trail, _events = _run_day(
        monkeypatch, tmp_path, worker_last_run=FRESH_CLEAN_RUN
    )

    assert (code, pages, reports) == (0, [], [])
    assert trail.failing == {}
    assert trail.green == PAGING | {"out-of-band-watchdog"}


def test_main_reports_a_report_only_day_without_paging(monkeypatch, tmp_path) -> None:
    code, pages, reports, trail, _events = _run_day(
        monkeypatch, tmp_path, worker_last_run=FRESH_CLEAN_RUN, failing={"infra2-ssh"}
    )

    assert (code, pages) == (0, [])
    assert [_failure_names(report) for report in reports] == [{"infra2-ssh"}]
    assert trail.failing == {}


def test_main_pages_when_the_report_path_is_not_configured(
    monkeypatch, tmp_path
) -> None:
    """A report nobody can receive is GREEN-WHILE-EMPTY; the pager says so."""
    code, pages, reports, trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        failing={"infra2-ssh"},
        env_extra={"INFRA2_REPORTS_FEISHU_CHAT_ID": None},
    )

    assert code == 1 and reports == []
    assert _failure_names(pages[0]) == {"watchdog-report-delivery"}
    assert "INFRA2_REPORTS_FEISHU_CHAT_ID missing" in pages[0]
    assert "undelivered: 1 report-only failure(s) (infra2-ssh)" in pages[0]
    assert set(trail.failing) == {"out-of-band-watchdog"}
    assert trail.failing["out-of-band-watchdog"].severity == "P0"


def test_main_pages_when_the_report_cannot_be_delivered(monkeypatch, tmp_path) -> None:
    code, pages, reports, trail, events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        failing={"infra2-docker-health"},
        report_error=RuntimeError("Feishu OpenAPI request failed"),
    )

    assert code == 1 and reports == []
    assert _failure_names(pages[0]) == {"watchdog-report-delivery"}
    assert "report delivery failed: Feishu OpenAPI request failed" in pages[0]
    assert set(trail.failing) == {"out-of-band-watchdog"}
    assert trail.failing["out-of-band-watchdog"].severity == "P1"
    assert [e["event"] for e in events if "report.delivery" in e.get("event", "")] == [
        "watchdog.report.delivery.failure"
    ]


def test_main_pages_every_failure_when_the_registry_is_unreadable(
    monkeypatch, tmp_path
) -> None:
    """Unknown routing fails loud: everything pages, the trail records only itself."""
    code, pages, _reports, trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        failing={"infra2-ssh"},
        registry_error=FileNotFoundError("watchdog-signals.yaml"),
    )

    assert code == 1
    assert _failure_names(pages[0]) == {"infra2-ssh", "watchdog-signal-registry"}
    assert set(trail.failing) == {"out-of-band-watchdog"}
    assert trail.green == set()
    # it could not tell which checks page, so the trail must not retire any issue
    assert trail.complete is False


def test_a_target_added_only_through_repository_variables_pages(
    monkeypatch, tmp_path
) -> None:
    """#908 review: `vars.INFRA2_WATCHDOG_*_TARGETS` adds checks the registry and the
    audit never see; unknown is not report-only, so it pages and is tracked."""
    code, pages, reports, trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        failing={"fr-preview-leak"},
        extra_ssh=["fr-preview-leak"],
    )

    assert (code, reports) == (1, [])
    assert _failure_names(pages[0]) == {"fr-preview-leak"}
    assert set(trail.failing) == {"fr-preview-leak"}


def test_a_rejected_dokploy_key_pages_from_main(monkeypatch, tmp_path) -> None:
    """The real Dokploy check inside main(): a 401 is a blind watchdog, and pages."""
    import httpx

    watchdog = _load_watchdog()
    real_check = watchdog.run_dokploy_status_check
    request = httpx.Request("GET", "https://dokploy.invalid/api/project.all")

    class RejectingClient:
        def list_projects(self):
            raise httpx.HTTPStatusError(
                "HTTP 401",
                request=request,
                response=httpx.Response(401, request=request),
            )

    code, pages, _reports, trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        env_extra={"DOKPLOY_API_KEY": "revoked"},
        dokploy_check=lambda env, **kwargs: real_check(
            env, client_factory=lambda *, host: RejectingClient(), **kwargs
        ),
    )

    assert code == 1
    assert _failure_names(pages[0]) == {"infra2-dokploy-status"}
    assert "Dokploy rejected DOKPLOY_API_KEY: HTTP 401" in pages[0]
    assert set(trail.failing) == {"out-of-band-watchdog"}


def test_a_failed_pull_under_a_green_host_sweep_is_a_failure_not_stale(
    monkeypatch, tmp_path
) -> None:
    """The reviewer's case end to end: a 300 s-old `image pull failed`, the old
    version still running healthy, infra2-docker-health green. Real Dokploy check,
    real verdict; only the Dokploy API and the host are faked."""
    watchdog = _load_watchdog()
    real_check = watchdog.run_dokploy_status_check
    recent = datetime.fromtimestamp(datetime.now(UTC).timestamp() - 300, UTC)

    def old_version_running(_project):
        return 0, f"/backend|running|0|healthy|{OLD}\n", ""

    code, pages, reports, _trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        dokploy_check=lambda env, **_kwargs: real_check(
            env,
            client_factory=lambda *, host: _evidence_client(
                env_text=f"IAC_CONFIG_HASH={CURRENT}", created_at=recent.isoformat()
            ),
            unit_runtime=old_version_running,
        ),
        env_extra={"DOKPLOY_API_KEY": "secret"},
    )

    assert (code, pages) == (0, [])
    assert "Stale Dokploy records" not in reports[0]
    assert _failure_names(reports[0]) == {
        "dokploy-status:finance-report/production/backend",
        "dokploy-status:finance-report/staging/backend",
    }
    assert "latest_deployment_errorMessage=image pull failed" in reports[0]
    assert (
        "runtime: backend runs config 000000000000, current is cccccccccccc"
        in (reports[0])
    )


@pytest.mark.parametrize(
    ("env_extra", "report_error", "lost"),
    [
        ({"INFRA2_REPORTS_FEISHU_CHAT_ID": None}, None, "missing"),
        ({}, RuntimeError("Feishu OpenAPI request failed"), "request failed"),
    ],
    ids=["unconfigured", "delivery-failed"],
)
def test_a_notes_only_report_that_is_lost_says_what_was_lost(
    monkeypatch, tmp_path, env_extra, report_error, lost
) -> None:
    """No failures, one Worker note: the page must not claim "0 failures" undelivered."""
    code, pages, _reports, _trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=DELIVERED_FAILING_RUN,
        env_extra=env_extra,
        report_error=report_error,
    )

    assert code == 1
    assert lost in pages[0]
    assert "; undelivered: 1 information line(s)" in pages[0]
    assert "report-only failure(s)" not in pages[0]


def test_a_dry_run_prints_a_page_holding_only_the_paging_classes(
    monkeypatch, tmp_path, capsys
) -> None:
    """#908 acceptance: the dry run shows what would page and what would be reported."""
    code, pages, reports, _trail, _events = _run_day(
        monkeypatch,
        tmp_path,
        worker_last_run=FRESH_CLEAN_RUN,
        failing={"infra2-ssh", "infra2-restore-rehearsal"},
        env_extra={"WATCHDOG_DRY_RUN": "1"},
    )

    assert (code, pages, reports) == (1, [], [])
    out = capsys.readouterr().out
    page_start = out.index("告警] GitHub 日级带外审计")
    page = out[page_start:]
    report = out[out.index("[报告] Infra2 GitHub watchdog") : page_start]
    assert _failure_names(page) == {"infra2-restore-rehearsal"}
    assert _failure_names(report) == {"infra2-ssh"}


def test_the_report_secrets_reach_only_the_run_watchdog_step() -> None:
    """#908: without them report-only failures page as a config failure; with them
    in the job env every other step (inline scripts included) would hold them."""
    from libs.alerting import INFRA2_REPORTS_ENV

    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["watchdog"]
    holders = {
        step.get("name"): sorted(set(INFRA2_REPORTS_ENV) & set(step.get("env") or {}))
        for step in job["steps"]
    }
    run = next(step for step in job["steps"] if step.get("name") == "Run watchdog")

    assert set(INFRA2_REPORTS_ENV).isdisjoint(job["env"])
    assert {name: found for name, found in holders.items() if found} == {
        "Run watchdog": sorted(INFRA2_REPORTS_ENV)
    }
    assert {name: run["env"][name] for name in INFRA2_REPORTS_ENV} == {
        name: "${{ secrets.%s }}" % name for name in INFRA2_REPORTS_ENV
    }


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


# ---- the pager layout (#905) ---------------------------------------------------------


def test_a_github_page_shows_every_field_of_the_shared_layout() -> None:
    """#905: the page carries 级别 · 环境 · 对象 · 现象 · 开始于 · 影响 · 下一步 ·
    Runbook for every failure, and no fixed scope line that describes nothing."""
    from libs.alerting import PAGER_FIELDS

    watchdog = _load_watchdog()
    results = [
        watchdog.CheckResult(
            "infra2-backup-staging",
            False,
            "latest manifest is 9 days old",
            "backup",
            severity="P2",
        ),
        watchdog.CheckResult(
            "cloudflare-worker-status",
            False,
            "worker last ran 9000s ago (max 7200s); token=abc123",
            "cloudflare-worker-health",
            severity="P1",
        ),
        watchdog.CheckResult(
            "infra2-backup-production",
            False,
            "postgres dump missing from the manifest",
            "backup",
            severity="P1",
        ),
    ]

    message = watchdog.format_failure_message(
        results, run_url="https://github.example/run/9", now=1_790_236_800
    )
    worker, production, staging = _page_blocks(message)

    assert "Scope" not in message and "abc123" not in message
    assert worker == {
        "级别": "P1",
        "环境": "global",
        "对象": "cloudflare-worker-status",
        "现象": "worker last ran 9000s ago (max 7200s); token=***",
        "开始于": "2026-09-24 08:00 UTC 检出(日级审计;实际开始时间未知)",
        "影响": "[cloudflare-worker-health] "
        + watchdog._IMPACT_BY_DOMAIN["cloudflare-worker-health"],
        "下一步": watchdog._suggested_action_for_failure(
            "cloudflare-worker-status", "cloudflare-worker-health"
        ),
        "Runbook": watchdog._runbook_url_for_failure("cloudflare-worker-health"),
    }
    assert list(worker) == list(PAGER_FIELDS[:8])
    assert (production["环境"], production["级别"]) == ("production", "P1")
    assert (staging["环境"], staging["级别"]) == ("staging", "P2")
    assert production["Runbook"].endswith("ops.recovery.md#sop-004-备份-freshness-验证")


@pytest.mark.parametrize(
    ("name", "domain", "environment"),
    [
        (
            "dokploy-status:finance_report/staging/backend",
            "dokploy-deploy-status",
            "staging",
        ),
        (
            "dokploy-status:platform/production/vault",
            "dokploy-deploy-status",
            "production",
        ),
        ("infra2-backup-staging", "backup", "staging"),
        ("infra2-restore-rehearsal", "restore-rehearsal", "production"),
        ("watchdog-signal-registry", "configuration", "global"),
        ("truealpha-scheduler-liveness", "peer-scheduler-liveness", "global"),
    ],
)
def test_a_github_check_names_its_environment(name, domain, environment) -> None:
    watchdog = _load_watchdog()
    result = watchdog.CheckResult(name, False, "x", domain)

    assert watchdog._check_environment(result) == environment


def test_every_runbook_the_github_watchdog_links_resolves() -> None:
    """Each infra2 runbook anchor the page uses is a heading that exists."""
    from libs.tests.test_pager_format import BLOB, _anchors

    watchdog = _load_watchdog()
    domains = [
        *watchdog._IMPACT_BY_DOMAIN,
        "http-target",
        "host-diagnostics",
    ]
    urls = {watchdog._runbook_url_for_failure(domain) for domain in domains}
    infra2 = sorted(url for url in urls if url.startswith(f"{BLOB}/"))
    missing = [
        url
        for url in infra2
        if url.split("#", 1)[1]
        not in _anchors(ROOT / url.removeprefix(f"{BLOB}/").split("#", 1)[0])
    ]

    assert len(infra2) >= 6
    assert missing == []


def test_the_github_report_is_titled_as_a_report() -> None:
    watchdog = _load_watchdog()
    report = watchdog.format_report_message(
        [
            watchdog.CheckResult(
                "infra2-ssh", False, "ssh exited 255", "host-reachability"
            )
        ],
        [],
        [],
        run_url="",
    )

    assert report.splitlines()[0] == "[报告] Infra2 GitHub watchdog 日级审计"
