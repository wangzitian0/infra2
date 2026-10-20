"""Watchdog weekly and daily digest domain logic (#1311).

Canonical domain module for aggregating GitHub Actions watchdog runs, auditing
structured alert-recall logs, checking stale open issues, probing estate
application release/health parity via canonical registry & SDK routing
(``common.core.registry.domain_for_service`` and
``infra2_sdk.routing.resolve_service_url``), and rendering Feishu interactive
report cards via ``build_feishu_report_card``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import io
import json
import os
from typing import Any
from urllib.request import Request, urlopen
import zipfile

from infra2_sdk.routing import resolve_service_url

from common.alerting import (
    REPORT_TITLE_PREFIX,
    build_feishu_report_card,
    deliver_out_of_band_card,
    deliver_out_of_band_text,
)
from common.alerting.types import _one_line
from common.core.registry import domain_for_service

WORKFLOW_FILE = "ops-checks.yml"
WATCHDOG_JOB_NAME = "Check infra2 host and alert bridge"
DEFAULT_REPOSITORY = "wangzitian0/infra2"
DIGEST_WINDOW_DAYS = 7
DAILY_WINDOW_DAYS = 1
DEFAULT_INTERNAL_DOMAIN = "zitian.party"
JOURNEY_BOARD_URL = "https://wangzitian0.github.io/finance_report/board/"
OPENPANEL_URL = "https://openpanel.zitian.party"
SIGNOZ_URL = "https://signoz.zitian.party"
WATCHDOG_RUNBOOK_URL = "https://github.com/wangzitian0/infra2/blob/main/platform/12.alerting/README.md#out-of-band-watchdog"
STALE_ISSUE_DAYS = 14
DEFAULT_MAX_LOGS = 25
GITHUB_TIMEOUT_S = 15
LOG_TIMEOUT_S = 20
HEALTH_TIMEOUT_S = 5.0
MAX_FAILED_RUN_URLS = 5
MAX_FAILURE_DOMAINS_SHOWN = 5
MAX_STALE_ISSUES_SHOWN = 8
HEALTHY_STATUSES = frozenset({"ok", "healthy"})


def fetch_recent_runs(
    repository: str,
    token: str,
    *,
    workflow_file: str = WORKFLOW_FILE,
    watchdog_job_name: str = WATCHDOG_JOB_NAME,
    per_page: int = 100,
    now: datetime | None = None,
    window_days: int = DIGEST_WINDOW_DAYS,
    opener=None,
) -> list[dict[str, Any]]:
    """Fetch recent ops-checks runs that actually include the watchdog job."""
    active_opener = opener or urlopen
    owner, repo = repository.split("/", 1)
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=window_days)
    matching_runs: list[dict[str, Any]] = []
    page = 1

    while True:
        request = Request(
            (
                f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
                f"{workflow_file}/runs?per_page={per_page}&page={page}"
            ),
            headers=_github_headers(token),
            method="GET",
        )
        with active_opener(request, timeout=GITHUB_TIMEOUT_S) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        runs = payload.get("workflow_runs")
        page_runs = runs if isinstance(runs, list) else []
        if not page_runs:
            break

        reached_cutoff = False
        for run in page_runs:
            created = _run_created_at(run)
            if created is None:
                continue
            if created < cutoff:
                reached_cutoff = True
                continue
            run_id = run.get("id") or run.get("databaseId")
            if not run_id:
                continue
            if run_includes_job(
                repository,
                token,
                run_id,
                job_name=watchdog_job_name,
                opener=active_opener,
            ):
                matching_runs.append(run)

        if reached_cutoff or len(page_runs) < per_page:
            break
        page += 1

    return matching_runs


def fetch_run_jobs(
    repository: str,
    token: str,
    run_id: str | int,
    *,
    per_page: int = 100,
    opener=None,
) -> list[dict[str, Any]]:
    """Fetch jobs for one GitHub Actions workflow run."""
    active_opener = opener or urlopen
    owner, repo = repository.split("/", 1)
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        request = Request(
            (
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs/"
                f"{run_id}/jobs?per_page={per_page}&page={page}"
            ),
            headers=_github_headers(token),
            method="GET",
        )
        with active_opener(request, timeout=GITHUB_TIMEOUT_S) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        page_jobs = payload.get("jobs")
        if not isinstance(page_jobs, list) or not page_jobs:
            break
        jobs.extend(page_jobs)
        if len(page_jobs) < per_page:
            break
        page += 1

    return jobs


def run_includes_job(
    repository: str,
    token: str,
    run_id: str | int,
    *,
    job_name: str = WATCHDOG_JOB_NAME,
    opener=None,
) -> bool:
    """Return whether a workflow run contains the out-of-band watchdog job."""
    return any(
        str(job.get("name") or "") == job_name
        and str(job.get("conclusion") or "").lower() != "skipped"
        for job in fetch_run_jobs(repository, token, run_id, opener=opener)
    )


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "infra2-watchdog-weekly-digest/1.0",
    }


def _run_created_at(run: Mapping[str, Any]) -> datetime | None:
    created_at = run.get("created_at")
    if not isinstance(created_at, str):
        return None
    try:
        return _parse_iso8601(created_at)
    except ValueError:
        return None


def fetch_run_log(
    repository: str, token: str, run_id: str | int, *, opener=None
) -> str:
    """Fetch and decode the GitHub Actions log archive for one workflow run."""
    active_opener = opener or urlopen
    owner, repo = repository.split("/", 1)
    request = Request(
        f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
        headers=_github_headers(token),
        method="GET",
    )
    with active_opener(request, timeout=LOG_TIMEOUT_S) as response:  # noqa: S310
        return _decode_log_payload(response.read())


def fetch_recent_run_logs(
    repository: str,
    token: str,
    runs: list[dict[str, Any]],
    *,
    max_logs: int = DEFAULT_MAX_LOGS,
    log_fetcher=None,
) -> dict[str, str]:
    """Fetch logs for recent runs; encode fetch failures as review events."""
    active_fetcher = log_fetcher or fetch_run_log
    logs: dict[str, str] = {}
    for run in runs[: max(0, max_logs)]:
        run_id = run.get("id") or run.get("databaseId")
        if not run_id:
            continue
        key = str(run_id)
        try:
            logs[key] = active_fetcher(repository, token, key)
        except Exception as exc:  # noqa: BLE001 - digest must not fail closed on logs.
            logs[key] = json.dumps(
                {
                    "event": "watchdog.digest.log_fetch_failure",
                    "status": "fail",
                    "run_id": key,
                    "error": _one_line(str(exc)),
                },
                sort_keys=True,
            )
    return logs


def _decode_log_payload(payload: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            chunks: list[str] = []
            for name in sorted(archive.namelist()):
                if name.endswith("/"):
                    continue
                with archive.open(name) as handle:
                    chunks.append(handle.read().decode("utf-8", errors="replace"))
            return "\n".join(chunks)
    except zipfile.BadZipFile:
        return payload.decode("utf-8", errors="replace")


def _parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def recent_weekly_runs(
    runs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_days: int = DIGEST_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Return workflow runs created inside the digest window."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=window_days)
    recent: list[dict[str, Any]] = []
    for run in runs:
        created_at = run.get("created_at")
        if not isinstance(created_at, str):
            continue
        try:
            created = _parse_iso8601(created_at)
        except ValueError:
            continue
        if created >= cutoff:
            recent.append(run)
    return recent


def summarize_weekly_runs(
    runs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_days: int = DIGEST_WINDOW_DAYS,
) -> dict[str, Any]:
    """Aggregate workflow run health summary over ``window_days``."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=window_days)
    recent = recent_weekly_runs(runs, now=current, window_days=window_days)
    totals: dict[str, int] = {
        "success": 0,
        "failure": 0,
        "cancelled": 0,
        "timed_out": 0,
        "other": 0,
    }
    failed_urls: list[str] = []
    for run in recent:
        conclusion = str(run.get("conclusion") or "").lower()
        html_url = str(run.get("html_url") or "")
        if conclusion == "success":
            totals["success"] += 1
        elif conclusion == "timed_out":
            totals["failure"] += 1
            totals["timed_out"] += 1
            if html_url:
                failed_urls.append(html_url)
        elif conclusion == "failure":
            totals["failure"] += 1
            if html_url:
                failed_urls.append(html_url)
        elif conclusion == "cancelled":
            totals["cancelled"] += 1
        else:
            totals["other"] += 1

    total_count = len(recent)
    success_rate = (totals["success"] / total_count * 100.0) if total_count else 0.0
    failure_rate = (totals["failure"] / total_count * 100.0) if total_count else 0.0
    return {
        "total_runs": total_count,
        "success_count": totals["success"],
        "failure_count": totals["failure"],
        "cancelled_count": totals["cancelled"],
        "timed_out_count": totals["timed_out"],
        "other_count": totals["other"],
        "success_rate_pct": round(success_rate, 2),
        "failure_rate_pct": round(failure_rate, 2),
        "failed_run_urls": failed_urls[:MAX_FAILED_RUN_URLS],
        "window_days": window_days,
        "week_start_utc": cutoff.date().isoformat(),
        "week_end_utc": current.date().isoformat(),
    }


def summarize_watchdog_log_events(logs_by_run: Mapping[str, str]) -> dict[str, Any]:
    """Review structured watchdog logs for alert recall evidence."""
    failure_domains: Counter[str] = Counter()
    reviewed_run_count = len(logs_by_run)
    structured_event_run_count = 0
    alertable_run_count = 0
    delivery_success_run_count = 0
    delivery_failure_run_count = 0
    fallback_issue_run_count = 0
    missing_alert_evidence_run_count = 0
    failed_check_count = 0
    log_fetch_error_count = 0

    for log_text in logs_by_run.values():
        events = list(_parse_watchdog_events(log_text))
        if events:
            structured_event_run_count += 1
        check_failures = [
            event
            for event in events
            if event.get("event") == "watchdog.check"
            and str(event.get("status") or "").lower() == "fail"
        ]
        failed_check_count += len(check_failures)
        for event in check_failures:
            domain = str(event.get("failure_domain") or "unknown")
            failure_domains[domain] += 1

        complete_failure = any(
            event.get("event") == "watchdog.run.complete"
            and str(event.get("status") or "").lower() == "fail"
            and _as_int(event.get("failure_count")) > 0
            for event in events
        )
        paged_failures = [
            event for event in check_failures if event.get("route") != "report"
        ]
        alertable = bool(paged_failures) or complete_failure
        if alertable:
            alertable_run_count += 1

        delivery_success = any(
            event.get("event") == "watchdog.delivery.success" for event in events
        )
        delivery_failures = [
            event
            for event in events
            if event.get("event") == "watchdog.delivery.failure"
        ]
        fallback_issue = any(
            str(event.get("fallback_issue_url") or "").strip()
            for event in delivery_failures
        )
        if delivery_success:
            delivery_success_run_count += 1
        if delivery_failures:
            delivery_failure_run_count += 1
        if fallback_issue:
            fallback_issue_run_count += 1
        if alertable and not delivery_success and not fallback_issue:
            missing_alert_evidence_run_count += 1
        if any(
            event.get("event") == "watchdog.digest.log_fetch_failure"
            for event in events
        ):
            log_fetch_error_count += 1

    recalled_runs = delivery_success_run_count + fallback_issue_run_count
    recall_pct = (
        round(recalled_runs / alertable_run_count * 100.0, 2)
        if alertable_run_count
        else 100.0
    )
    return {
        "reviewed_run_count": reviewed_run_count,
        "structured_event_run_count": structured_event_run_count,
        "alertable_run_count": alertable_run_count,
        "delivery_success_run_count": delivery_success_run_count,
        "delivery_failure_run_count": delivery_failure_run_count,
        "fallback_issue_run_count": fallback_issue_run_count,
        "missing_alert_evidence_run_count": missing_alert_evidence_run_count,
        "alert_recall_evidence_pct": recall_pct,
        "failed_check_count": failed_check_count,
        "failure_domain_counts": dict(sorted(failure_domains.items())),
        "log_fetch_error_count": log_fetch_error_count,
    }


def _parse_watchdog_events(log_text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    events: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        start = line.find("{")
        while start >= 0:
            candidate = line[start:].strip()
            try:
                parsed, _end = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                start = line.find("{", start + 1)
                continue
            if isinstance(parsed, dict) and str(parsed.get("event") or "").startswith(
                "watchdog."
            ):
                events.append(parsed)
            break
    return events


def _as_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def fetch_stale_open_issues(
    repository: str,
    token: str,
    *,
    stale_days: int = STALE_ISSUE_DAYS,
    per_page: int = 100,
    now: datetime | None = None,
    opener=None,
) -> list[dict[str, Any]]:
    """Open issues (not PRs) untouched for ``stale_days``+, oldest-updated first."""
    active_opener = opener or urlopen
    owner, repo = repository.split("/", 1)
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=stale_days)
    stale: list[dict[str, Any]] = []
    page = 1

    while True:
        request = Request(
            (
                f"https://api.github.com/repos/{owner}/{repo}/issues"
                f"?state=open&sort=updated&direction=asc&per_page={per_page}&page={page}"
            ),
            headers=_github_headers(token),
            method="GET",
        )
        with active_opener(request, timeout=GITHUB_TIMEOUT_S) as response:  # noqa: S310
            page_issues = json.loads(response.read().decode("utf-8"))
        if not isinstance(page_issues, list) or not page_issues:
            break

        reached_fresh = False
        for issue in page_issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            updated = (
                _parse_iso8601(issue["updated_at"]) if issue.get("updated_at") else None
            )
            if updated is None:
                continue
            if updated >= cutoff:
                reached_fresh = True
                continue
            stale.append(issue)

        if reached_fresh or len(page_issues) < per_page:
            break
        page += 1

    return stale


def summarize_stale_issues(issues: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "number": issue.get("number"),
            "title": _one_line(str(issue.get("title") or "")),
            "url": issue.get("html_url"),
            "updated_at": issue.get("updated_at"),
        }
        for issue in issues
    ]


def fetch_estate_app_health(
    internal_domain: str = DEFAULT_INTERNAL_DOMAIN,
    *,
    timeout: float = HEALTH_TIMEOUT_S,
    opener=None,
) -> list[dict[str, Any]]:
    """Probe Production vs. Staging /api/health endpoints via canonical SDK routing."""
    active_opener = opener or urlopen
    specs = (
        ("finance_report", "finance_report/app", "report"),
        ("truealpha", "truealpha/app", "truealpha"),
    )
    results: list[dict[str, Any]] = []
    for app_id, service_id, slug in specs:
        custom_domain = domain_for_service(service_id)
        service_domain = custom_domain or internal_domain
        envs: dict[str, dict[str, str]] = {}
        for env_name in ("production", "staging"):
            url = (
                f"https://{custom_domain}/api/health"
                if env_name == "production" and custom_domain
                else resolve_service_url(
                    slug,
                    env_name,
                    base_domain=service_domain,
                    path="/api/health",
                )
            )
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "infra2-watchdog-weekly-digest/1.0",
                },
                method="GET",
            )
            try:
                with active_opener(request, timeout=timeout) as response:  # noqa: S310
                    payload = json.loads(response.read().decode("utf-8"))
                status = str(payload.get("status") or "unknown")
                version = str(
                    payload.get("version")
                    or payload.get("app_version")
                    or payload.get("git_sha")
                    or "unknown"
                )
            except Exception as exc:  # noqa: BLE001 - digest probes are best-effort
                status = f"unreachable ({type(exc).__name__})"
                version = "unknown"
            envs[env_name] = {"status": status, "version": version, "url": url}
        prod_v = envs["production"]["version"]
        stg_v = envs["staging"]["version"]
        prod_ok = envs["production"]["status"].strip().lower() in HEALTHY_STATUSES
        stg_ok = envs["staging"]["status"].strip().lower() in HEALTHY_STATUSES
        parity = (
            "in_sync"
            if prod_v != "unknown" and prod_v == stg_v and prod_ok and stg_ok
            else ("ahead_or_drift" if stg_v != prod_v else "unknown")
        )
        results.append(
            {
                "app": app_id,
                "production": envs["production"],
                "staging": envs["staging"],
                "parity": parity,
            }
        )
    return results


def build_digest_message(summary: Mapping[str, Any], repository: str) -> str:
    """Build a compact weekly or daily digest message."""
    cadence = "daily" if summary.get("window_days") == 1 else "weekly"
    lines = [
        f"{REPORT_TITLE_PREFIX}[WATCHDOG DIGEST] Infra2 out-of-band {cadence} summary",
        f"Repository: {repository}",
        f"Window (UTC): {summary['week_start_utc']} -> {summary['week_end_utc']}",
        (
            f"Runs: {summary['total_runs']} | Success: {summary['success_count']} | "
            f"Failure: {summary['failure_count']} | Cancelled: {summary['cancelled_count']} | "
            f"Other: {summary['other_count']}"
        ),
        f"Success rate: {summary['success_rate_pct']}%",
    ]
    failed_urls = summary.get("failed_run_urls", [])
    if isinstance(failed_urls, list) and failed_urls:
        lines.append("Recent failed runs:")
        for url in failed_urls:
            lines.append(f"- {url}")
    audit = summary.get("log_audit")
    if isinstance(audit, Mapping):
        lines.append("Alert recall audit:")
        lines.append(
            f"Reviewed runs: {audit['reviewed_run_count']} | "
            f"Structured-event runs: {audit['structured_event_run_count']} | "
            f"Failed checks: {audit['failed_check_count']}"
        )
        lines.append(
            f"Alertable runs: {audit['alertable_run_count']} | "
            f"Delivery success: {audit['delivery_success_run_count']} | "
            f"Delivery failure: {audit['delivery_failure_run_count']} | "
            f"Fallback issues: {audit['fallback_issue_run_count']} | "
            f"Missing evidence: {audit['missing_alert_evidence_run_count']} | "
            f"Recall evidence: {audit['alert_recall_evidence_pct']}%"
        )
        domains = audit.get("failure_domain_counts")
        if isinstance(domains, Mapping) and domains:
            domain_text = ", ".join(
                f"{name}={count}"
                for name, count in sorted(
                    domains.items(), key=lambda item: (-int(item[1]), str(item[0]))
                )[:MAX_FAILURE_DOMAINS_SHOWN]
            )
            lines.append(f"Failure domains: {domain_text}")
        if audit.get("log_fetch_error_count"):
            lines.append(f"Log fetch errors: {audit['log_fetch_error_count']}")
    estate_apps = summary.get("estate_apps")
    if isinstance(estate_apps, list) and estate_apps:
        lines.append("Estate app release parity:")
        for app_entry in estate_apps:
            if not isinstance(app_entry, Mapping):
                continue
            prod = app_entry.get("production") or {}
            stg = app_entry.get("staging") or {}
            lines.append(
                f"- {app_entry.get('app')}: prod={prod.get('version')} ({prod.get('status')}) | "
                f"staging={stg.get('version')} ({stg.get('status')}) [{app_entry.get('parity')}]"
            )
    stale_issues = summary.get("stale_issues")
    if isinstance(stale_issues, list) and stale_issues:
        lines.append(f"Stale open issues ({STALE_ISSUE_DAYS}+ days untouched):")
        for issue in stale_issues[:MAX_STALE_ISSUES_SHOWN]:
            lines.append(f"- #{issue['number']} {issue['title']} ({issue['url']})")
        if len(stale_issues) > MAX_STALE_ISSUES_SHOWN:
            lines.append(f"  ...and {len(stale_issues) - MAX_STALE_ISSUES_SHOWN} more")
    lines.append(f"Runbook: {WATCHDOG_RUNBOOK_URL}")
    return "\n".join(lines)


def build_digest_card(summary: Mapping[str, Any], repository: str) -> dict[str, Any]:
    """Build a Feishu interactive card for the weekly or daily watchdog digest."""
    cadence = "daily" if summary.get("window_days") == 1 else "weekly"
    failure_count = _as_int(summary.get("failure_count"))
    audit = (
        summary.get("log_audit")
        if isinstance(summary.get("log_audit"), Mapping)
        else {}
    )
    missing_recall = _as_int(audit.get("missing_alert_evidence_run_count"))
    recall_pct = audit.get("alert_recall_evidence_pct", 100.0)
    stale_issues = (
        summary.get("stale_issues")
        if isinstance(summary.get("stale_issues"), list)
        else []
    )
    is_healthy = failure_count == 0 and missing_recall == 0
    template = "green" if is_healthy else ("orange" if missing_recall == 0 else "red")
    icon = "🟢" if is_healthy else ("🟡" if missing_recall == 0 else "🔴")
    header_title = (
        f"{icon} {REPORT_TITLE_PREFIX}[WATCHDOG DIGEST] "
        f"Infra2 out-of-band {cadence} summary"
    )
    kpi_fields = [
        (
            "Watchdog Runs",
            f"{summary['success_count']}/{summary['total_runs']} ({summary['success_rate_pct']}%)",
        ),
        (
            "Alert Recall Evidence",
            f"{recall_pct}% (missing: {missing_recall})",
        ),
        (
            "Window (UTC)",
            f"{summary['week_start_utc']} → {summary['week_end_utc']}",
        ),
        (
            "Repository & Stale Issues",
            f"`{repository}` · {len(stale_issues)} stale",
        ),
    ]
    sections: list[str] = []
    hr_after: set[int] = set()

    estate_apps = summary.get("estate_apps")
    if isinstance(estate_apps, list) and estate_apps:
        app_lines = ["**Estate App Release & Health Parity**"]
        for app_entry in estate_apps:
            if not isinstance(app_entry, Mapping):
                continue
            prod = app_entry.get("production") or {}
            stg = app_entry.get("staging") or {}
            parity = app_entry.get("parity", "unknown")
            status_icon = "✅" if parity == "in_sync" else "🔄"
            app_lines.append(
                f"{status_icon} **{app_entry.get('app')}**: Prod `{prod.get('version')}` "
                f"({prod.get('status')}) · Staging `{stg.get('version')}` "
                f"({stg.get('status')})"
            )
        sections.append("\n".join(app_lines))
        hr_after.add(len(sections) - 1)

    if audit:
        domains = audit.get("failure_domain_counts")
        domain_text = (
            ", ".join(
                f"`{name}`={count}"
                for name, count in sorted(
                    domains.items(), key=lambda item: (-int(item[1]), str(item[0]))
                )[:MAX_FAILURE_DOMAINS_SHOWN]
            )
            if isinstance(domains, Mapping) and domains
            else "none"
        )
        audit_md = (
            "**Alert Recall Audit**\n"
            f"• Reviewed runs: **{audit.get('reviewed_run_count', 0)}** · "
            f"Alertable: **{audit.get('alertable_run_count', 0)}** · "
            f"Delivered: **{audit.get('delivery_success_run_count', 0)}** · "
            f"Fallback issues: **{audit.get('fallback_issue_run_count', 0)}**\n"
            f"• Failure domains: {domain_text}"
        )
        sections.append(audit_md)

    failed_urls = summary.get("failed_run_urls", [])
    if isinstance(failed_urls, list) and failed_urls:
        fail_lines = ["**Recent Failed Runs**"] + [
            f"• [{url.rsplit('/', 1)[-1]}]({url})"
            for url in failed_urls[:MAX_FAILED_RUN_URLS]
        ]
        sections.append("\n".join(fail_lines))

    if stale_issues:
        issue_lines = [
            f"**Stale Open Issues ({STALE_ISSUE_DAYS}+ days untouched)**"
        ] + [
            f"• [#{issue['number']}]({issue['url']}) {issue['title']}"
            for issue in stale_issues[:MAX_STALE_ISSUES_SHOWN]
        ]
        if len(stale_issues) > MAX_STALE_ISSUES_SHOWN:
            issue_lines.append(
                f"• …and {len(stale_issues) - MAX_STALE_ISSUES_SHOWN} more"
            )
        sections.append("\n".join(issue_lines))

    actions = [
        ("Core Journey 看板", JOURNEY_BOARD_URL, "primary"),
        ("OpenPanel 漏斗", OPENPANEL_URL, "default"),
        ("SigNoz 监控", SIGNOZ_URL, "default"),
        ("Watchdog Runbook", WATCHDOG_RUNBOOK_URL, "default"),
    ]
    return build_feishu_report_card(
        title=header_title,
        template=template,
        kpi_fields=kpi_fields,
        sections=sections,
        actions=actions,
        hr_after_sections=hr_after,
    )


def deliver_digest(
    env: Mapping[str, str],
    message: str,
    *,
    card: dict[str, Any] | None = None,
    card_sender=None,
    text_sender=None,
) -> None:
    """Deliver digest via the shared out-of-band Feishu webhook/app path."""
    active_card_sender = card_sender or deliver_out_of_band_card
    active_text_sender = text_sender or deliver_out_of_band_text
    fmt = env.get("WATCHDOG_DIGEST_FORMAT", "card").strip().lower()
    if card is not None and fmt != "text":
        active_card_sender(env, card, fallback_text=message)
        return
    active_text_sender(env, message)


def _env_enabled(env: Mapping[str, str], name: str, *, default: str = "1") -> bool:
    return env.get(name, default).strip().lower() not in {"0", "false", "no"}


def run_digest_cli(
    env: Mapping[str, str] | None = None,
    argv: list[str] | None = None,
    *,
    runs_fetcher=None,
    logs_fetcher=None,
    health_fetcher=None,
    stale_fetcher=None,
    deliverer=None,
) -> int:
    """CLI runner for the weekly/daily watchdog digest."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Generate a 1-day daily digest instead of a 7-day weekly digest.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Custom window in days (overrides --daily and WATCHDOG_DIGEST_WINDOW_DAYS).",
    )
    args = parser.parse_args(argv if argv is not None else [])

    current_env = env or os.environ
    repository = (current_env.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY).strip()
    token = current_env.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required for weekly digest generation")
        return 1

    window_days = (
        args.days
        or (DAILY_WINDOW_DAYS if args.daily else None)
        or _as_int(current_env.get("WATCHDOG_DIGEST_WINDOW_DAYS"))
        or DIGEST_WINDOW_DAYS
    )
    active_runs_fetcher = runs_fetcher or fetch_recent_runs
    active_logs_fetcher = logs_fetcher or fetch_recent_run_logs
    active_health_fetcher = health_fetcher or fetch_estate_app_health
    active_stale_fetcher = stale_fetcher or fetch_stale_open_issues
    active_deliverer = deliverer or deliver_digest

    now = datetime.now(UTC)
    runs = active_runs_fetcher(repository, token, now=now, window_days=window_days)
    summary = summarize_weekly_runs(runs, now=now, window_days=window_days)
    if _env_enabled(current_env, "WATCHDOG_DIGEST_REVIEW_LOGS"):
        max_logs = (
            _as_int(current_env.get("WATCHDOG_DIGEST_LOG_LIMIT")) or DEFAULT_MAX_LOGS
        )
        logs = active_logs_fetcher(
            repository,
            token,
            recent_weekly_runs(runs, now=now, window_days=window_days),
            max_logs=max_logs,
        )
        summary["log_audit"] = summarize_watchdog_log_events(logs)
    if _env_enabled(current_env, "WATCHDOG_DIGEST_ESTATE_HEALTH"):
        domain = (current_env.get("INTERNAL_DOMAIN") or DEFAULT_INTERNAL_DOMAIN).strip()
        summary["estate_apps"] = active_health_fetcher(domain)
    if _env_enabled(current_env, "WATCHDOG_DIGEST_STALE_ISSUES"):
        stale_days = (
            _as_int(current_env.get("WATCHDOG_DIGEST_STALE_ISSUE_DAYS"))
            or STALE_ISSUE_DAYS
        )
        stale = active_stale_fetcher(repository, token, stale_days=stale_days, now=now)
        summary["stale_issues"] = summarize_stale_issues(stale)
    message = build_digest_message(summary, repository)
    card = build_digest_card(summary, repository)
    if current_env.get("WATCHDOG_DIGEST_DRY_RUN") == "1":
        print(message)
        return 0
    active_deliverer(current_env, message, card=card)
    print(message)
    return 0


def main(
    env: Mapping[str, str] | None = None,
    argv: list[str] | None = None,
) -> int:
    """CLI entrypoint for weekly/daily watchdog digest."""
    return run_digest_cli(env, argv)


__all__ = [
    "DAILY_WINDOW_DAYS",
    "DEFAULT_INTERNAL_DOMAIN",
    "DEFAULT_MAX_LOGS",
    "DEFAULT_REPOSITORY",
    "DIGEST_WINDOW_DAYS",
    "GITHUB_TIMEOUT_S",
    "HEALTHY_STATUSES",
    "HEALTH_TIMEOUT_S",
    "JOURNEY_BOARD_URL",
    "LOG_TIMEOUT_S",
    "MAX_FAILED_RUN_URLS",
    "MAX_FAILURE_DOMAINS_SHOWN",
    "MAX_STALE_ISSUES_SHOWN",
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
    "run_digest_cli",
    "run_includes_job",
    "summarize_stale_issues",
    "summarize_watchdog_log_events",
    "summarize_weekly_runs",
]
