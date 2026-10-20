"""Thin CLI wrapper for the out-of-band watchdog weekly/daily digest (#1311).

Domain logic lives in :mod:`common.observability.digest`. This module retains
hookable module-level bindings so existing unit tests and workflow invocations
operate identically.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
import sys
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alerting import (  # noqa: E402
    REPORT_TITLE_PREFIX,
    deliver_out_of_band_card,
    deliver_out_of_band_text,
)
from common.observability import digest as _digest  # noqa: E402
from common.observability.digest import (  # noqa: E402
    DAILY_WINDOW_DAYS,
    DEFAULT_INTERNAL_DOMAIN,
    DEFAULT_REPOSITORY,
    DIGEST_WINDOW_DAYS,
    JOURNEY_BOARD_URL,
    OPENPANEL_URL,
    SIGNOZ_URL,
    STALE_ISSUE_DAYS,
    WATCHDOG_JOB_NAME,
    WATCHDOG_RUNBOOK_URL,
    WORKFLOW_FILE,
    build_digest_card,
    build_digest_message,
    recent_weekly_runs,
    run_digest_cli,
    summarize_stale_issues,
    summarize_watchdog_log_events,
    summarize_weekly_runs,
)


def fetch_run_jobs(
    repository: str,
    token: str,
    run_id: str | int,
    *,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    return _digest.fetch_run_jobs(
        repository, token, run_id, per_page=per_page, opener=urlopen
    )


def run_includes_job(
    repository: str,
    token: str,
    run_id: str | int,
    *,
    job_name: str = WATCHDOG_JOB_NAME,
) -> bool:
    return _digest.run_includes_job(
        repository, token, run_id, job_name=job_name, opener=urlopen
    )


def fetch_recent_runs(
    repository: str,
    token: str,
    *,
    workflow_file: str = WORKFLOW_FILE,
    watchdog_job_name: str = WATCHDOG_JOB_NAME,
    per_page: int = 100,
    now: datetime | None = None,
    window_days: int = DIGEST_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    return _digest.fetch_recent_runs(
        repository,
        token,
        workflow_file=workflow_file,
        watchdog_job_name=watchdog_job_name,
        per_page=per_page,
        now=now,
        window_days=window_days,
        opener=urlopen,
    )


def fetch_run_log(repository: str, token: str, run_id: str | int) -> str:
    return _digest.fetch_run_log(repository, token, run_id, opener=urlopen)


def fetch_recent_run_logs(
    repository: str,
    token: str,
    runs: list[dict[str, Any]],
    *,
    max_logs: int = 25,
) -> dict[str, str]:
    return _digest.fetch_recent_run_logs(
        repository, token, runs, max_logs=max_logs, log_fetcher=fetch_run_log
    )


def fetch_stale_open_issues(
    repository: str,
    token: str,
    *,
    stale_days: int = STALE_ISSUE_DAYS,
    per_page: int = 100,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    return _digest.fetch_stale_open_issues(
        repository,
        token,
        stale_days=stale_days,
        per_page=per_page,
        now=now,
        opener=urlopen,
    )


def fetch_estate_app_health(
    internal_domain: str = DEFAULT_INTERNAL_DOMAIN,
    *,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    return _digest.fetch_estate_app_health(
        internal_domain, timeout=timeout, opener=urlopen
    )


def deliver_digest(
    env: Mapping[str, str],
    message: str,
    *,
    card: dict[str, Any] | None = None,
) -> None:
    _digest.deliver_digest(
        env,
        message,
        card=card,
        card_sender=deliver_out_of_band_card,
        text_sender=deliver_out_of_band_text,
    )


def main(
    env: Mapping[str, str] | None = None,
    argv: list[str] | None = None,
) -> int:
    return run_digest_cli(
        env,
        argv,
        runs_fetcher=fetch_recent_runs,
        logs_fetcher=fetch_recent_run_logs,
        health_fetcher=fetch_estate_app_health,
        stale_fetcher=fetch_stale_open_issues,
        deliverer=deliver_digest,
    )


__all__ = [
    "DAILY_WINDOW_DAYS",
    "DEFAULT_INTERNAL_DOMAIN",
    "DEFAULT_REPOSITORY",
    "DIGEST_WINDOW_DAYS",
    "JOURNEY_BOARD_URL",
    "OPENPANEL_URL",
    "REPORT_TITLE_PREFIX",
    "SIGNOZ_URL",
    "STALE_ISSUE_DAYS",
    "WATCHDOG_JOB_NAME",
    "WATCHDOG_RUNBOOK_URL",
    "WORKFLOW_FILE",
    "build_digest_card",
    "build_digest_message",
    "deliver_digest",
    "fetch_estate_app_health",
    "fetch_recent_run_logs",
    "fetch_recent_runs",
    "fetch_run_jobs",
    "fetch_run_log",
    "fetch_stale_open_issues",
    "main",
    "recent_weekly_runs",
    "run_includes_job",
    "summarize_stale_issues",
    "summarize_watchdog_log_events",
    "summarize_weekly_runs",
]


if __name__ == "__main__":
    raise SystemExit(main(argv=sys.argv[1:]))
