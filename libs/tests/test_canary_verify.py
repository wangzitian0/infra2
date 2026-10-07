"""Unit tests for Canary Todo 4-pillar verification tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.canary_verify import (
    CanaryRealityReport,
    PillarResult,
    _resolve_ssh_credentials,
    main,
    verify_canary,
)


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
    """Verify happy path where all four pillars return healthy status."""
    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        "3.0.2\n"
        "=== TRACES ===\n"
        "500\t2026-10-07 10:00:00\n"
        "=== LOGS ===\n"
        "100\t2026-10-07 10:00:00\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-07", "runs": 10, "signal": {"ok": 10, "fail": 0}}\n'
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

        report = verify_canary("staging")

        assert report.all_ok is True
        assert report.installed_sdk == "3.0.2"
        assert report.tracking.ok is True
        assert report.logs.ok is True
        assert report.monitoring.ok is True
        assert report.alerts.ok is True


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
    fake_ssh_stdout = (
        "=== STATUS ===\n"
        '{"ok": true, "failed": []}\n'
        "=== SDK ===\n"
        "3.0.2\n"
        "=== TRACES ===\n"
        "500\t2026-10-07 10:00:00\n"
        "=== LOGS ===\n"
        "100\t2026-10-07 10:00:00\n"
        "=== LEDGER ===\n"
        '{"day": "2026-10-07", "runs": 10, "signal": {"ok": 8, "fail": 2}}\n'
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

        report = verify_canary("staging")

        assert report.all_ok is False
        assert report.monitoring.ok is False


def test_main_cli_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI exit code and output formatting."""
    fake_report = CanaryRealityReport(
        environment="staging",
        all_ok=True,
        tracking=PillarResult(ok=True, summary="traces ok"),
        logs=PillarResult(ok=True, summary="logs ok"),
        monitoring=PillarResult(ok=True, summary="monitoring ok"),
        alerts=PillarResult(ok=True, summary="alerts ok"),
        installed_sdk="3.0.2",
    )

    with patch("tools.canary_verify.verify_canary", return_value=fake_report):
        code = main(["--env", "staging"])
        assert code == 0
        captured = capsys.readouterr()
        assert "Overall Status: PASS" in captured.out
        assert "Installed SDK:  3.0.2" in captured.out

    with patch("tools.canary_verify.verify_canary", return_value=fake_report):
        code_json = main(["--env", "staging", "--json"])
        assert code_json == 0
        captured_json = capsys.readouterr()
        data = json.loads(captured_json.out)
        assert data["all_ok"] is True
        assert data["installed_sdk"] == "3.0.2"
