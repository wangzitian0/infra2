"""One GitHub issue per red ops-checks watchdog check (truealpha#876 W4).

Canonical implementation lives in ``infra2_sdk.observability`` (#1270).
"""

from __future__ import annotations

from infra2_sdk.observability import (
    CheckVerdict,
    GitHubIssues,
    IssueApi,
    ListingFailed,
    Trail,
    WriteFailed,
    check_name,
    issue_trail_mode,
    load_trail,
    reconcile_issue_trail,
    record_verdicts,
    scrub,
    title_for,
)

from common.observability.scheduler_peer_liveness import BOUND_CAP_ENV

TITLE_PREFIX = "ops-checks watchdog is red: "
ISSUE_LABELS = ("incident",)
VERDICTS_ENV = "INFRA2_WATCHDOG_VERDICTS_PATH"
SSH_OVERRIDE_ENV = "INFRA2_WATCHDOG_SSH_TARGETS_OVERRIDDEN"
DRY_RUN_ENV = "WATCHDOG_DRY_RUN"

WATCHDOG_SOURCE = "out-of-band-watchdog"
RUNNER_HEALTH_SOURCE = "iac-runner-health"
EXPECTED_SOURCES = (WATCHDOG_SOURCE, RUNNER_HEALTH_SOURCE)

FULL = "full"
OPEN_ONLY = "open-only"
OFF = "off"

API_BASE = "https://api.github.com"
API_TIMEOUT_SECONDS = 20.0
MAX_DETAIL_CHARS = 1000

reconcile = reconcile_issue_trail
reconcile_watchdog_issues = reconcile_issue_trail
GitHubIssueClient = IssueApi

__all__ = [
    "API_BASE",
    "API_TIMEOUT_SECONDS",
    "BOUND_CAP_ENV",
    "DRY_RUN_ENV",
    "EXPECTED_SOURCES",
    "FULL",
    "GitHubIssueClient",
    "GitHubIssues",
    "ISSUE_LABELS",
    "IssueApi",
    "ListingFailed",
    "MAX_DETAIL_CHARS",
    "OFF",
    "OPEN_ONLY",
    "RUNNER_HEALTH_SOURCE",
    "SSH_OVERRIDE_ENV",
    "TITLE_PREFIX",
    "Trail",
    "VERDICTS_ENV",
    "WATCHDOG_SOURCE",
    "CheckVerdict",
    "WriteFailed",
    "check_name",
    "issue_trail_mode",
    "load_trail",
    "reconcile",
    "reconcile_issue_trail",
    "reconcile_watchdog_issues",
    "record_verdicts",
    "scrub",
    "title_for",
]
