"""Infra2 Observability Watchdog Issue Trail SSOT."""

from __future__ import annotations


from libs.watchdog_issue_trail import (
    CheckVerdict,
    GitHubIssues,
    IssueApi,
    Trail,
    load_trail,
    reconcile,
    record_verdicts,
)

# O-04 Canonical aliases
reconcile_watchdog_issues = reconcile
GitHubIssueClient = IssueApi

__all__ = [
    "CheckVerdict",
    "GitHubIssueClient",
    "GitHubIssues",
    "IssueApi",
    "Trail",
    "load_trail",
    "reconcile",
    "reconcile_watchdog_issues",
    "record_verdicts",
]
