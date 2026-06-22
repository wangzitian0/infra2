"""Weekly digest for infra2 out-of-band watchdog job runs."""

from __future__ import annotations

from collections import Counter
import io
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import deliver_out_of_band_text  # noqa: E402

WORKFLOW_FILE = "ops-checks.yml"
WATCHDOG_JOB_NAME = "Check infra2 host and alert bridge"
DEFAULT_REPOSITORY = "wangzitian0/infra2"
DIGEST_WINDOW_DAYS = 7


def fetch_recent_runs(
    repository: str,
    token: str,
    *,
    workflow_file: str = WORKFLOW_FILE,
    watchdog_job_name: str = WATCHDOG_JOB_NAME,
    per_page: int = 100,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent ops-checks runs that actually include the watchdog job."""
    owner, repo = repository.split("/", 1)
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=DIGEST_WINDOW_DAYS)
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
        with urlopen(request, timeout=15) as response:  # noqa: S310
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
) -> list[dict[str, Any]]:
    """Fetch jobs for one GitHub Actions workflow run."""
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
        with urlopen(request, timeout=15) as response:  # noqa: S310
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
) -> bool:
    """Return whether a workflow run contains the out-of-band watchdog job."""
    return any(
        str(job.get("name") or "") == job_name
        and str(job.get("conclusion") or "").lower() != "skipped"
        for job in fetch_run_jobs(repository, token, run_id)
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


def fetch_run_log(repository: str, token: str, run_id: str | int) -> str:
    """Fetch and decode the GitHub Actions log archive for one workflow run."""
    owner, repo = repository.split("/", 1)
    request = Request(
        f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
        headers=_github_headers(token),
        method="GET",
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return _decode_log_payload(response.read())


def fetch_recent_run_logs(
    repository: str,
    token: str,
    runs: list[dict[str, Any]],
    *,
    max_logs: int = 25,
) -> dict[str, str]:
    """Fetch logs for recent runs; encode fetch failures as review events."""
    logs: dict[str, str] = {}
    for run in runs[: max(0, max_logs)]:
        run_id = run.get("id") or run.get("databaseId")
        if not run_id:
            continue
        key = str(run_id)
        try:
            logs[key] = fetch_run_log(repository, token, key)
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
    runs: list[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Return workflow runs created inside the 7-day digest window."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=DIGEST_WINDOW_DAYS)
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
    runs: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate 7-day workflow run health summary."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=DIGEST_WINDOW_DAYS)
    recent = recent_weekly_runs(runs, now=current)
    totals: dict[str, int] = {"success": 0, "failure": 0, "cancelled": 0, "other": 0}
    failed_urls: list[str] = []
    for run in recent:
        conclusion = str(run.get("conclusion") or "").lower()
        html_url = str(run.get("html_url") or "")
        if conclusion == "success":
            totals["success"] += 1
        elif conclusion in {"failure", "timed_out"}:
            totals["failure"] += 1
            if html_url:
                failed_urls.append(html_url)
        elif conclusion == "cancelled":
            totals["cancelled"] += 1
        else:
            totals["other"] += 1

    total_count = len(recent)
    success_rate = (totals["success"] / total_count * 100.0) if total_count else 0.0
    return {
        "total_runs": total_count,
        "success_count": totals["success"],
        "failure_count": totals["failure"],
        "cancelled_count": totals["cancelled"],
        "other_count": totals["other"],
        "success_rate_pct": round(success_rate, 2),
        "failed_run_urls": failed_urls[:5],
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
        alertable = bool(check_failures) or complete_failure
        if alertable:
            alertable_run_count += 1

        delivery_success = any(
            event.get("event") == "watchdog.delivery.success" for event in events
        )
        delivery_failures = [
            event for event in events if event.get("event") == "watchdog.delivery.failure"
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


def build_digest_message(summary: Mapping[str, Any], repository: str) -> str:
    """Build a compact weekly digest message."""
    lines = [
        "[WATCHDOG DIGEST] Infra2 out-of-band weekly summary",
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
                )[:5]
            )
            lines.append(f"Failure domains: {domain_text}")
        if audit.get("log_fetch_error_count"):
            lines.append(f"Log fetch errors: {audit['log_fetch_error_count']}")
    lines.append(
        "Runbook: https://github.com/wangzitian0/infra2/blob/main/platform/12.alerting/README.md#out-of-band-watchdog"
    )
    return "\n".join(lines)


def deliver_digest(env: Mapping[str, str], message: str) -> None:
    """Deliver digest via the shared out-of-band Feishu webhook/app path."""
    deliver_out_of_band_text(env, message)


def main(env: Mapping[str, str] | None = None) -> int:
    current_env = env or os.environ
    repository = (current_env.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY).strip()
    token = current_env.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required for weekly digest generation")
        return 1

    now = datetime.now(UTC)
    runs = fetch_recent_runs(repository, token, now=now)
    summary = summarize_weekly_runs(runs, now=now)
    if current_env.get("WATCHDOG_DIGEST_REVIEW_LOGS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }:
        max_logs = _as_int(current_env.get("WATCHDOG_DIGEST_LOG_LIMIT")) or 25
        logs = fetch_recent_run_logs(
            repository,
            token,
            recent_weekly_runs(runs, now=now),
            max_logs=max_logs,
        )
        summary["log_audit"] = summarize_watchdog_log_events(logs)
    message = build_digest_message(summary, repository)
    if current_env.get("WATCHDOG_DIGEST_DRY_RUN") == "1":
        print(message)
        return 0
    deliver_digest(current_env, message)
    print(message)
    return 0


def _one_line(value: str) -> str:
    return " ".join(value.split())


if __name__ == "__main__":
    raise SystemExit(main())
