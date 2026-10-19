"""One GitHub issue per red ops-checks watchdog check (truealpha#876 W4).

Canonical implementation lives in ``infra2_sdk.observability`` (#1270).
"""

from __future__ import annotations

from urllib.error import HTTPError, URLError

from infra2_sdk.observability import (
    CheckVerdict,
    GitHubIssues as _SdkGitHubIssues,
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

PER_PAGE = 100
MAX_PAGES = 10
API_BASE = "https://api.github.com"
API_TIMEOUT_SECONDS = 20.0
MAX_DETAIL_CHARS = 1000


class GitHubIssues(_SdkGitHubIssues):
    def open_issues(self, *, with_body: bool = False) -> list[dict]:
        issues: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            path = f"/issues?state=open&per_page={PER_PAGE}&page={page}"
            try:
                items = self._call("GET", path)
            except (HTTPError, URLError, OSError, ValueError) as exc:
                raise ListingFailed(f"GET {path}: {exc}") from exc
            if not isinstance(items, list):
                raise ListingFailed(
                    f"GET {path} returned {type(items).__name__}, not a list"
                )
            for item in items:
                if not isinstance(item, dict):
                    raise ListingFailed(f"GET {path} returned a non-object issue")
                if "pull_request" in item:
                    continue
                number, title = item.get("number"), item.get("title")
                if not isinstance(number, int) or not isinstance(title, str):
                    raise ListingFailed(
                        f"GET {path} returned an issue without number/title"
                    )
                entry = {"number": number, "title": title}
                if with_body:
                    author = item.get("user")
                    entry["body"] = item.get("body") or ""
                    entry["author"] = (
                        str(author.get("login") or "")
                        if isinstance(author, dict)
                        else ""
                    )
                issues.append(entry)
            if len(items) < PER_PAGE:
                return issues
        raise ListingFailed(
            f"more than {MAX_PAGES * PER_PAGE} open issues; not read to the end"
        )


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
    "MAX_PAGES",
    "OFF",
    "OPEN_ONLY",
    "PER_PAGE",
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
