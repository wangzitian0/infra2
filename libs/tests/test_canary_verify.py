"""Unit tests for Canary Todo 4-pillar verification tool."""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from libs.observability import local_ledger, signoz_queries
from libs.tests.ambient_env import clear_telemetry_env
from tools.canary_verify import (
    CanaryRealityReport,
    PillarResult,
    _evaluate_freshness,
    _resolve_locked_sdk_version,
    _resolve_ssh_credentials,
    main,
    verify_canary,
)


FIXED_NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)

#: The /health document of the bridge since #1209: the `infra2_sdk.fastapi.init_service`
#: report. `test_alert_bridge_accepts_the_health_body_the_alerting_app_serves` proves that
#: the real app serves this shape.
BRIDGE_HEALTHY = json.dumps(
    {
        "status": "healthy",
        "checks": {
            "database": {
                "status": "present",
                "detail": "delivery config valid",
                "duration_ms": 0.03,
                "required": True,
            }
        },
        "reasons": [],
        "service": "platform-alerting",
    }
)


def _relative_ts(offset_seconds: float, now: datetime) -> str:
    """Return a timestamp string `offset_seconds` before the injected `now`."""
    target = now - timedelta(seconds=offset_seconds)
    return target.strftime("%Y-%m-%d %H:%M:%S")


DEFAULT_LEDGER_LINE = (
    '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}'
)
HEALTHY_STATUS_LINE = '{"ok": true, "failed": []}'


def _canned_stdout(
    *,
    traces: str,
    logs: str,
    bridge: str = BRIDGE_HEALTHY,
    ledger: str = DEFAULT_LEDGER_LINE,
    status: str = HEALTHY_STATUS_LINE,
) -> str:
    return (
        "=== STATUS ===\n"
        f"{status}\n"
        "=== SDK ===\n"
        f"{_resolve_locked_sdk_version()}\n"
        "=== TRACES ===\n"
        f"{traces}\n"
        "=== LOGS ===\n"
        f"{logs}\n"
        "=== LEDGER ===\n"
        f"{ledger}\n"
        "=== ALERT_BRIDGE ===\n"
        f"{bridge}\n"
    )


def _verify_with_canned_ssh(
    environment: str,
    stdout: str,
    health_body: dict[str, object] | None = None,
    **kwargs: object,
) -> tuple[CanaryRealityReport, list[str]]:
    """Run verify_canary with canned SSH output.

    Return the report and every remote command passed to `_ssh_cmd`.
    """
    commands: list[str] = []

    def fake_ssh(command: str, *_args: object, **_kwargs: object):
        commands.append(command)
        return 0, stdout, ""

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", side_effect=fake_ssh),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(
            {"ok": True} if health_body is None else health_body
        ).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp
        report = verify_canary(environment, now=FIXED_NOW, **kwargs)  # type: ignore[arg-type]
    return report, commands


def _remote_query(command: str, marker: str, next_marker: str) -> str:
    """Return the ClickHouse SQL the remote shell would receive for one section."""
    start_token = f'echo "=== {marker} ===" && '
    start = command.index(start_token) + len(start_token)
    end = command.index(f'echo "=== {next_marker} ==="')
    words = shlex.split(command[start:end])
    return words[words.index("--query") + 1]


def test_resolve_ssh_credentials_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read SSH credentials from custom environment variables."""
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_HOST", "1.2.3.4")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_USER", "admin")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "/tmp/test_key")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_PORT", "2222")

    host, user, key, port = _resolve_ssh_credentials()
    assert host == "1.2.3.4"
    assert user == "admin"
    assert key == "/tmp/test_key"
    assert port == 2222


def test_verify_canary_happy_path() -> None:
    """Verify happy path where all pillars return healthy status."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    fresh_traces = _relative_ts(60, now=fixed_now)
    fresh_logs = _relative_ts(120, now=fixed_now)

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        f"{locked_version}\n"
        "=== TRACES ===\n"
        f"500\t{fresh_traces}\n"
        "=== LOGS ===\n"
        f"100\t{fresh_logs}\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
        "=== ALERT_BRIDGE ===\n"
        f"{BRIDGE_HEALTHY}\n"
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is True
        assert report.installed_sdk == locked_version
        assert report.sdk.ok is True
        assert report.tracking.ok is True
        assert "fresh:" in report.tracking.summary
        assert report.logs.ok is True
        assert "fresh:" in report.logs.summary
        assert report.monitoring.ok is True
        assert report.alerts.ok is True


def test_verify_canary_stale_logs_rejected() -> None:
    """Falsify STALE-REPORTED-AS-FRESH: reject 3-day-old logs despite positive count."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    fresh_traces = _relative_ts(60, now=fixed_now)
    stale_logs = _relative_ts(3 * 86400, now=fixed_now)  # 3 days ago

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        f"{locked_version}\n"
        "=== TRACES ===\n"
        f"500\t{fresh_traces}\n"
        "=== LOGS ===\n"
        f"9711\t{stale_logs}\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
        "=== ALERT_BRIDGE ===\n"
        f"{BRIDGE_HEALTHY}\n"
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is False
        assert report.logs.ok is False
        assert "Stale log entries" in report.logs.summary
        assert "age:" in report.logs.summary


def test_verify_canary_stale_traces_rejected() -> None:
    """Falsify STALE-REPORTED-AS-FRESH: reject 3-day-old traces despite positive count."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    stale_traces = _relative_ts(3 * 86400, now=fixed_now)
    fresh_logs = _relative_ts(120, now=fixed_now)

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        f"{locked_version}\n"
        "=== TRACES ===\n"
        f"500\t{stale_traces}\n"
        "=== LOGS ===\n"
        f"100\t{fresh_logs}\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
        "=== ALERT_BRIDGE ===\n"
        f"{BRIDGE_HEALTHY}\n"
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is False
        assert report.tracking.ok is False
        assert "Stale spans" in report.tracking.summary


def test_verify_canary_zero_count_rejected() -> None:
    """Reject verification if log count is zero."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    fresh_traces = _relative_ts(60, now=fixed_now)

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        f"{locked_version}\n"
        "=== TRACES ===\n"
        f"500\t{fresh_traces}\n"
        "=== LOGS ===\n"
        "0\t\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
        "=== ALERT_BRIDGE ===\n"
        f"{BRIDGE_HEALTHY}\n"
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is False
        assert report.logs.ok is False
        assert "0 log entries recorded" in report.logs.summary


def test_verify_canary_malformed_timestamp_rejected() -> None:
    """Reject verification if timestamp cannot be parsed (fail-closed)."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    fresh_traces = _relative_ts(60, now=fixed_now)

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        f"{locked_version}\n"
        "=== TRACES ===\n"
        f"500\t{fresh_traces}\n"
        "=== LOGS ===\n"
        "100\tnot-a-valid-timestamp\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
        "=== ALERT_BRIDGE ===\n"
        f"{BRIDGE_HEALTHY}\n"
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is False
        assert report.logs.ok is False
        assert "Malformed timestamp" in report.logs.summary


def test_verify_canary_sdk_mismatch() -> None:
    """Report failure when installed SDK does not match the locked release."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    fresh_traces = _relative_ts(60, now=fixed_now)
    fresh_logs = _relative_ts(120, now=fixed_now)

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        "0.0.1-mismatch\n"
        "=== TRACES ===\n"
        f"500\t{fresh_traces}\n"
        "=== LOGS ===\n"
        f"100\t{fresh_logs}\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
        "=== ALERT_BRIDGE ===\n"
        f"{BRIDGE_HEALTHY}\n"
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is False
        assert report.sdk.ok is False
        assert "0.0.1-mismatch" in report.sdk.summary
        assert locked_version in report.sdk.summary


def test_verify_canary_ssh_failure() -> None:
    """Report failure when SSH execution returns non-zero code."""
    with (
        patch("urllib.request.urlopen") as mock_url,
        patch(
            "tools.canary_verify._ssh_cmd", return_value=(1, "", "Connection refused")
        ),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("production")

        assert report.all_ok is False
        assert "Connection refused" in report.tracking.summary


def test_verify_canary_ledger_failure() -> None:
    """Report failure when ledger records failures."""
    locked_version = _resolve_locked_sdk_version()
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    fresh_traces = _relative_ts(60, now=fixed_now)
    fresh_logs = _relative_ts(120, now=fixed_now)

    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        f"{locked_version}\n"
        "=== TRACES ===\n"
        f"500\t{fresh_traces}\n"
        "=== LOGS ===\n"
        f"100\t{fresh_logs}\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-12", "runs": 10, "signal": {"ok": 8, "fail": 2}}\n'
        "=== ALERT_BRIDGE ===\n"
        '{"status": "ok"}\n'
    )

    with (
        patch("urllib.request.urlopen") as mock_url,
        patch("tools.canary_verify._ssh_cmd", return_value=(0, fake_ssh_stdout, "")),
    ):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        report = verify_canary("staging", now=fixed_now)

        assert report.all_ok is False
        assert report.monitoring.ok is False


@pytest.mark.parametrize(
    ("offset_seconds", "expected_ok", "expected_keyword"),
    [
        (3599, True, "fresh"),
        (3600, True, "fresh"),
        (3601, False, "Stale"),
        (-5, True, "fresh"),  # clock skew within 60s
        (-65, False, "Clock skew"),  # clock skew beyond 60s
    ],
)
def test_evaluate_freshness_boundaries(
    offset_seconds: float, expected_ok: bool, expected_keyword: str
) -> None:
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    ts_str = _relative_ts(offset_seconds, now=fixed_now)

    ok, summary, _details = _evaluate_freshness(
        count=10,
        latest_str=ts_str,
        pillar_name="spans",
        max_age_seconds=3600,
        now=fixed_now,
    )
    assert ok is expected_ok
    assert expected_keyword in summary


def test_evaluate_freshness_empty_and_zero_count() -> None:
    fixed_now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)

    # Count zero
    ok, summary, _ = _evaluate_freshness(
        0, "2026-10-12 11:59:00", "spans", now=fixed_now
    )
    assert ok is False
    assert "0 spans recorded" in summary

    # Count negative
    ok, summary, _ = _evaluate_freshness(
        -1, "2026-10-12 11:59:00", "spans", now=fixed_now
    )
    assert ok is False
    assert "0 spans recorded" in summary

    # Empty timestamp
    ok, summary, _ = _evaluate_freshness(10, "", "spans", now=fixed_now)
    assert ok is False
    assert "Missing timestamp" in summary

    # None timestamp
    ok, summary, _ = _evaluate_freshness(10, None, "spans", now=fixed_now)  # type: ignore[arg-type]
    assert ok is False
    assert "Missing timestamp" in summary


def test_main_cli_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI exit code and output formatting."""
    locked_version = _resolve_locked_sdk_version()
    fake_report_pass = CanaryRealityReport(
        environment="staging",
        all_ok=True,
        tracking=PillarResult(ok=True, summary="traces ok"),
        logs=PillarResult(ok=True, summary="logs ok"),
        monitoring=PillarResult(ok=True, summary="monitoring ok"),
        alerts=PillarResult(ok=True, summary="alerts ok"),
        installed_sdk=locked_version,
        expected_sdk=locked_version,
        sdk=PillarResult(ok=True, summary=f"matches locked release {locked_version}"),
    )
    fake_report_fail = CanaryRealityReport(
        environment="staging",
        all_ok=False,
        tracking=PillarResult(ok=False, summary="Stale traces"),
        logs=PillarResult(ok=True, summary="logs ok"),
        monitoring=PillarResult(ok=True, summary="monitoring ok"),
        alerts=PillarResult(ok=True, summary="alerts ok"),
        installed_sdk=locked_version,
        expected_sdk=locked_version,
        sdk=PillarResult(ok=True, summary=f"matches locked release {locked_version}"),
    )

    with patch("tools.canary_verify.verify_canary", return_value=fake_report_pass):
        code = main(["--env", "staging"])
        assert code == 0
        captured = capsys.readouterr()
        assert "Overall Status: PASS" in captured.out
        assert f"Installed SDK:  {locked_version}" in captured.out
        assert "1. SDK:         [OK]" in captured.out

    with patch("tools.canary_verify.verify_canary", return_value=fake_report_pass):
        code_json = main(["--env", "staging", "--json"])
        assert code_json == 0
        captured_json = capsys.readouterr()
        data = json.loads(captured_json.out)
        assert data["all_ok"] is True
        assert data["installed_sdk"] == locked_version
        assert data["sdk"]["ok"] is True

    with patch("tools.canary_verify.verify_canary", return_value=fake_report_fail):
        code_fail = main(["--env", "staging"])
        assert code_fail == 1


# --- Environment scoping (issue #1205) -------------------------------------


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_remote_queries_filter_on_environment_and_window(environment: str) -> None:
    """Trace and log SQL must carry the environment predicate and the time window."""
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
    )
    _report, commands = _verify_with_canned_ssh(environment, stdout)
    assert len(commands) == 1

    traces_sql = _remote_query(commands[0], "TRACES", "LOGS")
    logs_sql = _remote_query(commands[0], "LOGS", "LEDGER")
    predicate = f"resources_string['deployment.environment.name'] = '{environment}'"

    assert predicate in traces_sql
    assert predicate in logs_sql
    assert "'platform-todo'" in traces_sql
    assert "'platform-todo'" in logs_sql
    assert "INTERVAL 3600 SECOND" in traces_sql
    assert "INTERVAL 3600 SECOND" in logs_sql
    assert traces_sql.startswith("SELECT count(), ")
    assert logs_sql.startswith("SELECT count(), ")


def test_remote_queries_differ_only_in_environment_value() -> None:
    """Staging and production must not share one query (the #1205 defect)."""
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
    )
    _r1, staging_cmds = _verify_with_canned_ssh("staging", stdout)
    _r2, production_cmds = _verify_with_canned_ssh("production", stdout)

    for marker, next_marker in (("TRACES", "LOGS"), ("LOGS", "LEDGER")):
        staging_sql = _remote_query(staging_cmds[0], marker, next_marker)
        production_sql = _remote_query(production_cmds[0], marker, next_marker)
        assert staging_sql != production_sql
        assert "'staging'" in staging_sql
        assert "'production'" not in staging_sql
        assert staging_sql.replace("'staging'", "'production'") == production_sql


def test_query_window_follows_max_age_seconds() -> None:
    """The SQL window and the freshness limit must use one value."""
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
    )
    _report, commands = _verify_with_canned_ssh(
        "production", stdout, max_age_seconds=7200
    )
    for marker, next_marker in (("TRACES", "LOGS"), ("LOGS", "LEDGER")):
        sql = _remote_query(commands[0], marker, next_marker)
        assert "INTERVAL 7200 SECOND" in sql
        assert "INTERVAL 3600 SECOND" not in sql


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_fresh_scoped_output_passes_and_prints_latest_timestamp(
    environment: str,
) -> None:
    fresh_traces = _relative_ts(60, now=FIXED_NOW)
    fresh_logs = _relative_ts(120, now=FIXED_NOW)
    stdout = _canned_stdout(traces=f"1238\t{fresh_traces}", logs=f"48\t{fresh_logs}")

    report, _commands = _verify_with_canned_ssh(environment, stdout)

    assert report.all_ok is True
    assert report.tracking.ok is True
    assert report.logs.ok is True
    assert f"latest: {fresh_traces}" in report.tracking.summary
    assert f"latest: {fresh_logs}" in report.logs.summary
    assert report.tracking.details["count"] == 1238
    assert report.logs.details["count"] == 48


@pytest.mark.parametrize("pillar", ["tracking", "logs"])
def test_empty_window_rejected_with_epoch_timestamp(pillar: str) -> None:
    """ClickHouse returns epoch for max() over zero rows; zero count must fail."""
    fresh = _relative_ts(60, now=FIXED_NOW)
    empty = "0\t1970-01-01 00:00:00"
    stdout = _canned_stdout(
        traces=empty if pillar == "tracking" else f"500\t{fresh}",
        logs=empty if pillar == "logs" else f"100\t{fresh}",
    )

    report, _commands = _verify_with_canned_ssh("staging", stdout)

    assert report.all_ok is False
    result = getattr(report, pillar)
    assert result.ok is False
    assert " recorded in the last 3600s" in result.summary
    assert result.summary.startswith("0 ")


def test_alert_bridge_summary_reports_the_real_status() -> None:
    fresh_traces = _relative_ts(60, now=FIXED_NOW)
    fresh_logs = _relative_ts(120, now=FIXED_NOW)
    stdout = _canned_stdout(
        traces=f"500\t{fresh_traces}",
        logs=f"100\t{fresh_logs}",
    )

    report, _commands = _verify_with_canned_ssh("production", stdout)

    assert report.alerts.ok is True
    assert report.alerts.summary == "Bridge healthy: status=healthy"
    assert report.alerts.details["status"] == "healthy"
    assert "debounce" not in report.alerts.summary
    assert "firing" not in report.alerts.summary


@pytest.mark.parametrize(
    ("environment", "bridge", "todo"),
    [
        ("production", "platform-alerting", "platform-todo"),
        ("staging", "platform-alerting-staging", "platform-todo-staging"),
    ],
)
def test_the_ssh_command_asks_the_bridge_of_the_same_environment(
    environment: str, bridge: str, todo: str
) -> None:
    """The container suffix is `-staging` outside production (compose `container_name:
    platform-alerting${ENV_SUFFIX}`). A staging run once asked the production bridge."""
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
    )

    _report, commands = _verify_with_canned_ssh(environment, stdout)

    (command,) = commands
    bridge_part = command.split('echo "=== ALERT_BRIDGE ===" && ')[1]
    assert bridge_part.startswith(f"docker exec {bridge} python3 -c "), bridge_part
    status_part = command.split('echo "=== STATUS ===" && ')[1]
    assert status_part.startswith(f"docker exec {todo} python3 -c "), status_part


def test_alert_bridge_accepts_the_health_body_the_alerting_app_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the status from the document the real app serves, not from a canned copy."""
    import importlib.util
    from pathlib import Path

    from starlette.testclient import TestClient

    clear_telemetry_env(monkeypatch)
    path = Path(__file__).resolve().parents[2] / "platform/12.alerting/app.py"
    spec = importlib.util.spec_from_file_location("alerting_for_canary_verify", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("ALERT_DELIVERY_MODE", "feishu_app")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("FEISHU_CHAT_ID", "oc_pager")
    served = TestClient(module.fastapi_app).get("/health")
    assert served.status_code == 200

    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
        bridge=served.text,
    )
    report, _commands = _verify_with_canned_ssh("production", stdout)

    assert report.alerts.ok is True
    assert report.alerts.details["status"] == "healthy"


def test_alert_bridge_rejects_legacy_ok_status() -> None:
    """The bridge must report status=healthy; the legacy ok status is rejected."""
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
        bridge='{"status": "ok", "mode": "feishu_app"}',
    )

    report, _commands = _verify_with_canned_ssh("production", stdout)

    assert report.alerts.ok is False
    assert report.alerts.summary == "Bridge unhealthy: status=ok"


@pytest.mark.parametrize(
    ("bridge", "expected"),
    [
        ('{"status": "unhealthy", "reasons": ["database: bad"]}', "status=unhealthy"),
        ('{"status": "degraded", "mode": "log_only"}', "status=degraded"),
        ('{"mode": "log_only"}', "status=missing"),
        ("not-json", "not valid JSON"),
    ],
)
def test_alert_bridge_unhealthy_reports_what_it_read(
    bridge: str, expected: str
) -> None:
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
        bridge=bridge,
    )

    report, _commands = _verify_with_canned_ssh("production", stdout)

    assert report.alerts.ok is False
    assert report.all_ok is False
    assert expected in report.alerts.summary
    assert "Bridge healthy" not in report.alerts.summary


# --- Query builders (issue #1205) -------------------------------------------


def test_traces_query_exact_contract() -> None:
    assert signoz_queries.traces_query(
        service_name="platform-todo", environment="staging", window_seconds=3600
    ) == (
        "SELECT count(), toDateTime(max(timestamp), 'UTC') "
        "FROM signoz_traces.distributed_signoz_index_v3 "
        "WHERE serviceName = 'platform-todo' "
        "AND resources_string['deployment.environment.name'] = 'staging' "
        "AND timestamp >= now() - INTERVAL 3600 SECOND"
    )


def test_logs_query_exact_contract() -> None:
    assert signoz_queries.logs_query(
        service_name="platform-todo", environment="production", window_seconds=900
    ) == (
        "SELECT count(), toDateTime(intDiv(max(timestamp), 1000000000), 'UTC') "
        "FROM signoz_logs.distributed_logs_v2 "
        "WHERE resources_string['service.name'] = 'platform-todo' "
        "AND resources_string['deployment.environment.name'] = 'production' "
        "AND timestamp >= toUnixTimestamp64Nano(now64(3) - INTERVAL 900 SECOND)"
    )


@pytest.mark.parametrize("builder", ["traces_query", "logs_query"])
@pytest.mark.parametrize(
    "bad_value", ["x' OR 1=1 --", "a b", "", "prod;DROP", "a\\b", "x'"]
)
def test_query_builders_reject_unsafe_values(builder: str, bad_value: str) -> None:
    build = getattr(signoz_queries, builder)
    with pytest.raises(ValueError):
        build(service_name="platform-todo", environment=bad_value, window_seconds=60)
    with pytest.raises(ValueError):
        build(service_name=bad_value, environment="staging", window_seconds=60)


@pytest.mark.parametrize("builder", ["traces_query", "logs_query"])
@pytest.mark.parametrize("bad_window", [0, -1, True, 1.5, "60"])
def test_query_builders_reject_bad_window(builder: str, bad_window: object) -> None:
    build = getattr(signoz_queries, builder)
    with pytest.raises(ValueError):
        build(
            service_name="platform-todo",
            environment="staging",
            window_seconds=bad_window,
        )


def test_window_default_is_shared_by_function_and_cli() -> None:
    import inspect

    assert (
        inspect.signature(verify_canary).parameters["max_age_seconds"].default
        == signoz_queries.DEFAULT_WINDOW_SECONDS
    )
    pass_report = CanaryRealityReport(
        environment="staging",
        all_ok=True,
        tracking=PillarResult(ok=True, summary="t"),
        logs=PillarResult(ok=True, summary="l"),
        monitoring=PillarResult(ok=True, summary="m"),
        alerts=PillarResult(ok=True, summary="a"),
        installed_sdk="x",
    )
    with patch(
        "tools.canary_verify.verify_canary", return_value=pass_report
    ) as mock_verify:
        assert main(["--env", "staging"]) == 0
    assert (
        mock_verify.call_args.kwargs["max_age_seconds"]
        == signoz_queries.DEFAULT_WINDOW_SECONDS
    )


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_cli_rejects_non_positive_window(bad: str) -> None:
    with patch("tools.canary_verify.verify_canary") as mock_verify:
        with pytest.raises(SystemExit) as excinfo:
            main(["--env", "staging", "--max-age-seconds", bad])
    assert excinfo.value.code == 2
    mock_verify.assert_not_called()


# --- Monitoring pillar judges recent failures (issue #1223) -----------------

NOW_TS = int(FIXED_NOW.timestamp())
WINDOW = signoz_queries.DEFAULT_WINDOW_SECONDS


def _monitoring_report(
    signal: dict[str, object] | None,
    *,
    environment: str = "staging",
    status: str = HEALTHY_STATUS_LINE,
    health_body: dict[str, object] | None = None,
    **kwargs: object,
) -> CanaryRealityReport:
    """Run the verifier on a ledger line that holds `signal`, with fresh telemetry."""
    ledger = json.dumps({"day": "2026-10-12", "runs": 700, "signal": signal})
    stdout = _canned_stdout(
        traces=f"500\t{_relative_ts(60, now=FIXED_NOW)}",
        logs=f"100\t{_relative_ts(120, now=FIXED_NOW)}",
        ledger=ledger,
        status=status,
    )
    report, _commands = _verify_with_canned_ssh(
        environment, stdout, health_body=health_body, **kwargs
    )
    return report


def _signal(
    *,
    ok: int = 560,
    fail: int = 2,
    fail_age: object = None,
    ok_age: object = 60,
) -> dict[str, object]:
    """A ledger signal whose last failed / ok run is `*_age` seconds before now.

    `None` leaves the field out, as the writer did before #1223.
    """
    signal: dict[str, object] = {"ok": ok, "fail": fail}
    if isinstance(fail_age, int) and not isinstance(fail_age, bool):
        signal["lastFailAt"] = NOW_TS - fail_age
    elif fail_age is not None:
        signal["lastFailAt"] = fail_age
    if isinstance(ok_age, int) and not isinstance(ok_age, bool):
        signal["lastOkAt"] = NOW_TS - ok_age
    elif ok_age is not None:
        signal["lastOkAt"] = ok_age
    return signal


def test_failures_older_than_the_window_do_not_fail_the_pillar() -> None:
    """The #1223 case: a redeploy restart logged 2 failed runs hours ago."""
    report = _monitoring_report(_signal(fail=2, fail_age=2 * WINDOW, ok_age=60))

    assert report.monitoring.ok is True
    assert report.all_ok is True
    assert report.monitoring.summary.startswith("Ledger ok=560 fail=2")
    assert f"last fail {2 * WINDOW}s ago" in report.monitoring.summary
    assert "last ok 60s ago" in report.monitoring.summary
    assert report.monitoring.details["ledger"]["signal"]["fail"] == 2


@pytest.mark.parametrize(
    ("fail_age", "expected_ok"),
    [
        (0, False),
        (-30, False),  # a failure time in the future is never old
        (600, False),
        (WINDOW - 1, False),
        (WINDOW, False),  # the window is inclusive, as for traces and logs
        (WINDOW + 1, True),
        (86400, True),
    ],
)
def test_last_failure_is_compared_with_the_window(
    fail_age: int, expected_ok: bool
) -> None:
    report = _monitoring_report(_signal(fail_age=fail_age, ok_age=30))

    assert report.monitoring.ok is expected_ok
    assert report.all_ok is expected_ok


@pytest.mark.parametrize(
    ("ok_age", "expected_ok"),
    [
        (-60, True),  # clock skew within tolerance
        (-61, False),  # the last ok run is in the future
        (0, True),
        (WINDOW, True),
        (WINDOW + 1, False),  # no ok run for a window: the writer stopped
        (86400, False),
    ],
)
def test_last_ok_run_must_be_recent_when_failures_are_old(
    ok_age: int, expected_ok: bool
) -> None:
    report = _monitoring_report(_signal(fail_age=2 * WINDOW, ok_age=ok_age))

    assert report.monitoring.ok is expected_ok


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param(_signal(fail=2, fail_age=None, ok_age=None), id="old-format"),
        pytest.param(
            _signal(fail=2, fail_age=None, ok_age=60), id="no-lastFailAt-with-lastOkAt"
        ),
        pytest.param(
            _signal(fail=2, fail_age=2 * WINDOW, ok_age=None), id="no-lastOkAt"
        ),
    ],
)
def test_missing_timestamps_with_failures_fail_closed(
    signal: dict[str, object],
) -> None:
    """A ledger written before #1223 has counters only: any failure stays red."""
    report = _monitoring_report(signal)

    assert report.monitoring.ok is False
    assert report.all_ok is False
    assert "fail=2" in report.monitoring.summary


@pytest.mark.parametrize(
    "bad",
    ["2026-10-12", True, False, -5, 1.5, None, [], {}],
    ids=["str", "true", "false", "negative", "float", "null", "list", "object"],
)
@pytest.mark.parametrize("field", ["lastFailAt", "lastOkAt"])
def test_malformed_timestamps_fail_closed(field: str, bad: object) -> None:
    signal = _signal(fail_age=2 * WINDOW, ok_age=60)
    signal[field] = bad

    report = _monitoring_report(signal)

    assert report.monitoring.ok is False


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param({"ok": 10, "fail": 0}, id="no-timestamps"),
        pytest.param(
            {"ok": 10, "fail": 0, "lastOkAt": NOW_TS - 60}, id="with-lastOkAt"
        ),
        pytest.param(
            {"ok": 10, "fail": 0, "lastOkAt": NOW_TS - 5 * WINDOW}, id="stale-ok"
        ),
    ],
)
def test_no_failures_still_pass(signal: dict[str, object]) -> None:
    report = _monitoring_report(signal)

    assert report.monitoring.ok is True
    assert report.all_ok is True


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param(None, id="signal-missing"),
        pytest.param({"ok": 0, "fail": 0}, id="no-runs"),
        pytest.param(
            {"ok": 0, "fail": 2, "lastFailAt": NOW_TS - 2 * WINDOW, "lastOkAt": NOW_TS},
            id="old-failures-but-no-ok-run",
        ),
        pytest.param({"ok": "10", "fail": 0}, id="counter-is-a-string"),
        pytest.param({"ok": True, "fail": 0}, id="counter-is-a-bool"),
    ],
)
def test_a_ledger_without_a_positive_signal_stays_red(
    signal: dict[str, object] | None,
) -> None:
    report = _monitoring_report(signal)

    assert report.monitoring.ok is False


def test_old_failures_do_not_hide_a_service_that_fails_now() -> None:
    """The ledger tolerates old failures only; the live checks keep their veto."""
    old_failures = _signal(fail_age=2 * WINDOW, ok_age=60)

    internal = _monitoring_report(
        old_failures, status='{"ok": false, "failed": ["s3"]}'
    )
    public = _monitoring_report(old_failures, health_body={"ok": False})

    assert internal.monitoring.ok is False
    assert "internal_status=fail" in internal.monitoring.summary
    assert public.monitoring.ok is False
    assert "public_health=fail" in public.monitoring.summary


def test_the_failure_window_follows_max_age_seconds() -> None:
    """One window scopes traces, logs and the ledger failure time."""
    signal = _signal(fail_age=5000, ok_age=60)

    assert _monitoring_report(signal).monitoring.ok is True
    assert _monitoring_report(signal, max_age_seconds=7200).monitoring.ok is False
    assert _monitoring_report(signal, max_age_seconds=4999).monitoring.ok is True


def test_the_pillar_reads_the_fields_the_writer_writes() -> None:
    """Drive the real writer, then the verifier: a renamed field on one side fails."""
    probe = {"g": [{"spec": {"name": "todo-canary-status"}, "ok": False}]}
    healthy = {"g": [{"spec": {"name": "todo-canary-status"}, "ok": True}]}

    def entry_after_restart_at(restart_age: int) -> dict[str, object]:
        ledger = local_ledger.new_ledger("staging")
        local_ledger.record_round(ledger, healthy, "staging", NOW_TS - 5 * WINDOW)
        local_ledger.record_round(ledger, probe, "staging", NOW_TS - restart_age)
        local_ledger.record_round(ledger, healthy, "staging", NOW_TS - restart_age + 60)
        local_ledger.record_round(ledger, healthy, "staging", NOW_TS - 30)
        day = ledger["days"][local_ledger.utc_day(NOW_TS)]
        return day["signals"]["staging:todo-canary-status"]

    old_restart = entry_after_restart_at(3 * WINDOW)
    recent_restart = entry_after_restart_at(WINDOW // 2)

    assert old_restart["fail"] == recent_restart["fail"] == 1
    assert _monitoring_report(old_restart).monitoring.ok is True
    assert _monitoring_report(recent_restart).monitoring.ok is False
