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

ISSUE_LABELS = ("incident",)
VERDICTS_ENV = "INFRA2_WATCHDOG_VERDICTS_PATH"
DRY_RUN_ENV = "WATCHDOG_DRY_RUN"

WATCHDOG_SOURCE = "out-of-band-watchdog"
RUNNER_HEALTH_SOURCE = "iac-runner-health"

FULL = "full"
OPEN_ONLY = "open-only"
OFF = "off"

reconcile = reconcile_issue_trail
reconcile_watchdog_issues = reconcile_issue_trail
GitHubIssueClient = IssueApi

__all__ = [
    "BOUND_CAP_ENV",
    "DRY_RUN_ENV",
    "FULL",
    "GitHubIssueClient",
    "GitHubIssues",
    "ISSUE_LABELS",
    "IssueApi",
    "ListingFailed",
    "OFF",
    "OPEN_ONLY",
    "RUNNER_HEALTH_SOURCE",
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
