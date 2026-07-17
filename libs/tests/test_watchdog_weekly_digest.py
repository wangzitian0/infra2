"""Unit tests for out-of-band watchdog weekly digest."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "watchdog_weekly_digest.py"
WORKFLOW_PATH = ROOT / ".github/workflows/ops-checks.yml"


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


def test_fetch_recent_runs_filters_ops_checks_to_watchdog_jobs_and_paginates(monkeypatch) -> None:
    """Infra-012.8: digest only selects watchdog job runs from ops-checks pages."""
    digest = _load_module()
    now = datetime(2026, 6, 9, 0, 0, tzinfo=UTC)
    workflow_pages = {
        "1": [
            {
                "id": 1,
                "created_at": "2026-06-08T00:00:00Z",
                "conclusion": "success",
            },
            {
                "id": 2,
                "created_at": "2026-06-07T00:00:00Z",
                "conclusion": "success",
            },
        ],
        "2": [
            {
                "id": 3,
                "created_at": "2026-06-06T00:00:00Z",
                "conclusion": "failure",
            },
            {
                "id": 4,
                "created_at": "2026-05-20T00:00:00Z",
                "conclusion": "success",
            },
        ],
    }
    jobs_by_run = {
        "1": [
            {"name": "Run dynamic route canary", "conclusion": "success"},
            {"name": digest.WATCHDOG_JOB_NAME, "conclusion": "skipped"},
        ],
        "2": [{"name": digest.WATCHDOG_JOB_NAME, "conclusion": "success"}],
        "3": [{"name": digest.WATCHDOG_JOB_NAME, "conclusion": "failure"}],
    }
    requested_workflow_pages: list[str] = []
    requested_job_runs: list[str] = []

    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        page = query.get("page", ["1"])[0]
        if parsed.path.endswith("/actions/workflows/ops-checks.yml/runs"):
            requested_workflow_pages.append(page)
            return _Response({"workflow_runs": workflow_pages.get(page, [])})
        if "/actions/runs/" in parsed.path and parsed.path.endswith("/jobs"):
            run_id = parsed.path.split("/actions/runs/", 1)[1].split("/", 1)[0]
            requested_job_runs.append(run_id)
            return _Response({"jobs": jobs_by_run[run_id]})
        raise AssertionError(f"Unexpected URL: {request.full_url}")

    monkeypatch.setattr(digest, "urlopen", fake_urlopen)

    runs = digest.fetch_recent_runs(
        "wangzitian0/infra2",
        "token",
        per_page=2,
        now=now,
    )

    assert [run["id"] for run in runs] == [2, 3]
    assert requested_workflow_pages == ["1", "2"]
    assert requested_job_runs == ["1", "2", "3"]


def test_fetch_stale_open_issues_filters_prs_and_recent_and_paginates(monkeypatch) -> None:
    """#508: only open issues (not PRs) older than the threshold are returned."""
    digest = _load_module()
    now = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    issue_pages = {
        "1": [
            # stale, oldest first (API called with sort=updated&direction=asc)
            {
                "number": 438,
                "title": "Weekly ops review",
                "html_url": "https://github.com/wangzitian0/infra2/issues/438",
                "updated_at": "2026-06-25T00:00:00Z",
            },
            {
                # a PR — the issues API returns these too; must be excluded
                "number": 500,
                "title": "Not actually an issue",
                "html_url": "https://github.com/wangzitian0/infra2/pull/500",
                "updated_at": "2026-06-26T00:00:00Z",
                "pull_request": {"url": "https://api.github.com/..."},
            },
        ],
        "2": [
            {
                "number": 402,
                "title": "Unify Cloudflare ledger + GitHub watchdog recall reporting",
                "html_url": "https://github.com/wangzitian0/infra2/issues/402",
                "updated_at": "2026-06-19T00:00:00Z",
            },
            {
                # fresh — reached the cutoff, must stop paginating after this page
                "number": 999,
                "title": "Fresh issue",
                "html_url": "https://github.com/wangzitian0/infra2/issues/999",
                "updated_at": "2026-07-16T00:00:00Z",
            },
        ],
        "3": [
            {
                "number": 1,
                "title": "Should never be fetched",
                "html_url": "https://github.com/wangzitian0/infra2/issues/1",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        ],
    }
    requested_pages: list[str] = []

    class _Response:
        def __init__(self, payload) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        assert query.get("state") == ["open"]
        assert query.get("sort") == ["updated"]
        assert query.get("direction") == ["asc"]
        page = query.get("page", ["1"])[0]
        requested_pages.append(page)
        return _Response(issue_pages.get(page, []))

    monkeypatch.setattr(digest, "urlopen", fake_urlopen)

    stale = digest.fetch_stale_open_issues(
        "wangzitian0/infra2", "token", stale_days=14, per_page=2, now=now
    )

    assert [issue["number"] for issue in stale] == [438, 402]
    assert requested_pages == ["1", "2"]  # page 3 never fetched — stopped at the fresh issue


def test_summarize_stale_issues_shape() -> None:
    digest = _load_module()
    result = digest.summarize_stale_issues(
        [
            {
                "number": 438,
                "title": "  Weekly   ops review  ",
                "html_url": "https://github.com/wangzitian0/infra2/issues/438",
                "updated_at": "2026-06-25T00:00:00Z",
            }
        ]
    )
    assert result == [
        {
            "number": 438,
            "title": "Weekly ops review",
            "url": "https://github.com/wangzitian0/infra2/issues/438",
            "updated_at": "2026-06-25T00:00:00Z",
        }
    ]


def test_build_digest_message_includes_stale_issues_section() -> None:
    digest = _load_module()
    message = digest.build_digest_message(
        {
            "week_start_utc": "2026-07-10",
            "week_end_utc": "2026-07-17",
            "total_runs": 1,
            "success_count": 1,
            "failure_count": 0,
            "cancelled_count": 0,
            "other_count": 0,
            "success_rate_pct": 100.0,
            "stale_issues": [
                {
                    "number": 438,
                    "title": "Weekly ops review",
                    "url": "https://github.com/wangzitian0/infra2/issues/438",
                    "updated_at": "2026-06-25T00:00:00Z",
                }
            ],
        },
        repository="wangzitian0/infra2",
    )
    assert f"Stale open issues ({digest.STALE_ISSUE_DAYS}+ days untouched):" in message
    assert "#438 Weekly ops review (https://github.com/wangzitian0/infra2/issues/438)" in message


def test_build_digest_message_omits_stale_issues_section_when_empty() -> None:
    digest = _load_module()
    message = digest.build_digest_message(
        {
            "week_start_utc": "2026-07-10",
            "week_end_utc": "2026-07-17",
            "total_runs": 1,
            "success_count": 1,
            "failure_count": 0,
            "cancelled_count": 0,
            "other_count": 0,
            "success_rate_pct": 100.0,
            "stale_issues": [],
        },
        repository="wangzitian0/infra2",
    )
    assert "Stale open issues" not in message


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
            "log_audit": {
                "reviewed_run_count": 2,
                "structured_event_run_count": 2,
                "alertable_run_count": 1,
                "delivery_success_run_count": 1,
                "delivery_failure_run_count": 0,
                "fallback_issue_run_count": 0,
                "missing_alert_evidence_run_count": 0,
                "alert_recall_evidence_pct": 100.0,
                "failed_check_count": 2,
                "failure_domain_counts": {"alert-bridge": 1, "docker-runtime": 1},
                "log_fetch_error_count": 0,
            },
        },
        repository="wangzitian0/infra2",
    )
    assert "[WATCHDOG DIGEST]" in message
    assert "Runs: 7 | Success: 6 | Failure: 1" in message
    assert "Success rate: 85.71%" in message
    assert "Alert recall audit:" in message
    assert "Alertable runs: 1 | Delivery success: 1 | Delivery failure: 0" in message
    assert "Recall evidence: 100.0%" in message
    assert "Failure domains: alert-bridge=1, docker-runtime=1" in message
    assert "Runbook:" in message


def test_summarize_watchdog_log_events_counts_alert_recall_evidence() -> None:
    """Infra-012.8: weekly digest reviews watchdog logs for alert recall evidence."""
    digest = _load_module()
    logs = {
        "run-1": "\n".join(
            [
                'prefix {"event":"watchdog.check","name":"infra2-alert-bridge","status":"fail","failure_domain":"alert-bridge"}',
                '{"event":"watchdog.delivery.success","status":"ok","failure_count":1}',
                '{"event":"watchdog.run.complete","status":"fail","failure_count":1}',
            ]
        ),
        "run-2": "\n".join(
            [
                '{"event":"watchdog.check","name":"infra2-docker","status":"fail","failure_domain":"docker-runtime"}',
                '{"event":"watchdog.delivery.failure","status":"fail","fallback_issue_url":"https://github/issues/1"}',
            ]
        ),
    }

    audit = digest.summarize_watchdog_log_events(logs)

    assert audit["reviewed_run_count"] == 2
    assert audit["structured_event_run_count"] == 2
    assert audit["alertable_run_count"] == 2
    assert audit["delivery_success_run_count"] == 1
    assert audit["delivery_failure_run_count"] == 1
    assert audit["fallback_issue_run_count"] == 1
    assert audit["missing_alert_evidence_run_count"] == 0
    assert audit["alert_recall_evidence_pct"] == 100.0
    assert audit["failed_check_count"] == 2
    assert audit["failure_domain_counts"] == {
        "alert-bridge": 1,
        "docker-runtime": 1,
    }


def test_summarize_watchdog_log_events_flags_missing_alert_evidence() -> None:
    """Infra-012.8: a failed run without delivery/fallback evidence is a recall gap."""
    digest = _load_module()

    audit = digest.summarize_watchdog_log_events(
        {
            "run-1": '{"event":"watchdog.check","status":"fail","failure_domain":"host-reachability"}',
        }
    )

    assert audit["alertable_run_count"] == 1
    assert audit["missing_alert_evidence_run_count"] == 1
    assert audit["alert_recall_evidence_pct"] == 0.0


def test_weekly_digest_workflow_schedule_and_dispatch_contract() -> None:
    """Infra-012.8: weekly digest workflow keeps fixed weekly schedule + manual dry-run."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    assert {"cron": "0 1 * * 1"} in workflow["on"]["schedule"]
    assert "workflow_dispatch" in workflow["on"]
    assert "watchdog-weekly-digest" in workflow["on"]["workflow_dispatch"]["inputs"]["task"]["options"]
    assert "dry_run" in workflow["on"]["workflow_dispatch"]["inputs"]
