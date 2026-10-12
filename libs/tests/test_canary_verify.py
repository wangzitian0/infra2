"""Unit tests for Canary Todo 4-pillar verification tool."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tools.canary_verify import (
    CanaryRealityReport,
    PillarResult,
    _evaluate_freshness,
    _resolve_locked_sdk_version,
    _resolve_ssh_credentials,
    main,
    verify_canary,
)


def _relative_ts(offset_seconds: float, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    target = base - timedelta(seconds=offset_seconds)
    return target.strftime("%Y-%m-%d %H:%M:%S")


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
        '{"status": "ok", "mode": "feishu_app"}\n'
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
        '{"status": "ok", "mode": "feishu_app"}\n'
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
        '{"status": "ok", "mode": "feishu_app"}\n'
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
        '{"status": "ok", "mode": "feishu_app"}\n'
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
        '{"status": "ok", "mode": "feishu_app"}\n'
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
        '{"status": "ok", "mode": "feishu_app"}\n'
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
