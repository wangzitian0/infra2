"""Weekly digest for infra2 out-of-band watchdog workflow runs."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import deliver_feishu_app_text, deliver_feishu_text  # noqa: E402

WORKFLOW_FILE = "out-of-band-watchdog.yml"
DEFAULT_REPOSITORY = "wangzitian0/infra2"


def fetch_recent_runs(
    repository: str,
    token: str,
    *,
    workflow_file: str = WORKFLOW_FILE,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Fetch workflow runs for the target workflow from GitHub API."""
    owner, repo = repository.split("/", 1)
    request = Request(
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs?per_page={per_page}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "infra2-watchdog-weekly-digest/1.0",
        },
        method="GET",
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    runs = payload.get("workflow_runs")
    return runs if isinstance(runs, list) else []


def _parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarize_weekly_runs(
    runs: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Aggregate 7-day workflow run health summary."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=7)
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
    lines.append(
        "Runbook: https://github.com/wangzitian0/infra2/blob/main/platform/12.alerting/README.md#out-of-band-watchdog"
    )
    return "\n".join(lines)


def deliver_digest(env: Mapping[str, str], message: str) -> None:
    """Deliver digest via existing Feishu webhook/app modes."""
    mode = (
        env.get("INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE")
        or env.get("ALERT_DELIVERY_MODE")
        or "feishu_webhook"
    ).strip()
    if mode == "feishu_app":
        deliver_feishu_app_text(
            app_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_ID")
            or env.get("FEISHU_APP_ID", ""),
            app_secret=env.get("INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET")
            or env.get("FEISHU_APP_SECRET", ""),
            chat_id=env.get("INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID")
            or env.get("FEISHU_CHAT_ID", ""),
            api_base=env.get("INFRA2_OUT_OF_BAND_FEISHU_API_BASE")
            or env.get("FEISHU_API_BASE", "https://open.feishu.cn"),
            text=message,
        )
        return
    webhook_url = (
        env.get("INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL")
        or env.get("FEISHU_WEBHOOK_URL")
        or ""
    ).strip()
    if not webhook_url:
        raise ValueError("Feishu webhook URL is required for weekly digest delivery")
    deliver_feishu_text(webhook_url, message)


def main(env: Mapping[str, str] | None = None) -> int:
    current_env = env or os.environ
    repository = (current_env.get("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY).strip()
    token = current_env.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required for weekly digest generation")
        return 1

    runs = fetch_recent_runs(repository, token)
    summary = summarize_weekly_runs(runs)
    message = build_digest_message(summary, repository)
    if current_env.get("WATCHDOG_DIGEST_DRY_RUN") == "1":
        print(message)
        return 0
    deliver_digest(current_env, message)
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
