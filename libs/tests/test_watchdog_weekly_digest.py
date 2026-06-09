"""Unit tests for out-of-band watchdog weekly digest."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "watchdog_weekly_digest.py"
WORKFLOW_PATH = ROOT / ".github/workflows/watchdog-weekly-digest.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("watchdog_weekly_digest", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Failed to load watchdog_weekly_digest module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarize_weekly_runs_filters_old_runs() -> None:
    """Infra-012.8: digest only counts runs in last 7 days."""
    digest = _load_module()
    now = datetime(2026, 6, 9, 0, 0, tzinfo=UTC)
    runs = [
        {
            "created_at": "2026-06-08T00:00:00Z",
            "conclusion": "success",
            "html_url": "https://example/success",
        },
        {
            "created_at": "2026-06-07T00:00:00Z",
            "conclusion": "failure",
            "html_url": "https://example/failure",
        },
        {
            "created_at": "2026-05-20T00:00:00Z",
            "conclusion": "failure",
            "html_url": "https://example/old",
        },
    ]
    summary = digest.summarize_weekly_runs(runs, now=now)
    assert summary["total_runs"] == 2
    assert summary["success_count"] == 1
    assert summary["failure_count"] == 1
    assert summary["success_rate_pct"] == 50.0
    assert summary["failed_run_urls"] == ["https://example/failure"]


def test_build_digest_message_contains_runbook_and_counts() -> None:
    """Infra-012.8: digest message must include counts and actionable runbook."""
    digest = _load_module()
    message = digest.build_digest_message(
        {
            "week_start_utc": "2026-06-02",
            "week_end_utc": "2026-06-09",
            "total_runs": 7,
            "success_count": 6,
            "failure_count": 1,
            "cancelled_count": 0,
            "other_count": 0,
            "success_rate_pct": 85.71,
            "failed_run_urls": ["https://github.com/wangzitian0/infra2/actions/runs/1"],
        },
        repository="wangzitian0/infra2",
    )
    assert "[WATCHDOG DIGEST]" in message
    assert "Runs: 7 | Success: 6 | Failure: 1" in message
    assert "Success rate: 85.71%" in message
    assert "Failure rate: 14.29%" in message
    assert "Runbook:" in message


def test_weekly_digest_workflow_schedule_and_dispatch_contract() -> None:
    """Infra-012.8: weekly digest workflow keeps fixed weekly schedule + manual dry-run."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    assert workflow["on"]["schedule"] == [{"cron": "0 1 * * 1"}]
    assert "workflow_dispatch" in workflow["on"]
    assert "dry_run" in workflow["on"]["workflow_dispatch"]["inputs"]
