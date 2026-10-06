"""Scheduled Ops Checks jobs that GitHub failed before they started (#1101).

A scheduled job can end ``failure`` without ever starting: no runner, no step.
GitHub does this when a job waits on the concurrency group its own run holds
(#1086: the deploy_v2 canary on 2026-10-06 06:59 UTC). Nothing reports it:

- the job's own ``Alert on failure`` step never runs, so the page-dedup trail
  opens nothing;
- truealpha's scheduler-liveness counts the run as a tick, because the run
  itself completed.

This module lists the scheduled runs of ``ops-checks.yml`` created in the last
``WINDOW`` and reports every job that failed with no runner and no step. A job
that started and failed is not a finding: it alerts through its own step. A
failed read is UNVERIFIABLE, never a pass. The out-of-band watchdog runs it
daily, so the window covers one day plus the delay GitHub allows scheduled runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

REPOSITORY = "wangzitian0/infra2"
WORKFLOW = "ops-checks.yml"
#: One daily watchdog run to the next, plus the delay GitHub allows scheduled runs.
WINDOW = timedelta(hours=26)
RUNS_PAGE = 100
#: About 35 scheduled runs a day: one page today. More than this is an API fault.
MAX_PAGES = 5
#: A finding names this many jobs; the count says how many there are in all.
DETAIL_LIMIT = 5

OK = "OK"
NEVER_STARTED = "NEVER_STARTED"
UNVERIFIABLE = "UNVERIFIABLE"

Get = Callable[[str], object]


@dataclass(frozen=True)
class StartVerdict:
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == OK


class StartReadError(RuntimeError):
    """The GitHub API answer could not be read."""


def never_started(job: dict) -> bool:
    """A job GitHub failed before a runner took it: it ran no step at all."""
    return (
        job.get("conclusion") == "failure"
        and not job.get("runner_name")
        and not job.get("steps")
    )


def _list(payload: object, key: str, path: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise StartReadError(f"GET {path}: no {key!r} list in the answer")
    return [item for item in payload[key] if isinstance(item, dict)]


def scheduled_runs(
    get: Get, *, now: datetime, repository: str, workflow: str, window: timedelta
) -> list[dict]:
    since = (now - window).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        path = (
            f"/repos/{repository}/actions/workflows/{quote(workflow)}/runs"
            f"?event=schedule&created=%3E%3D{since}&per_page={RUNS_PAGE}&page={page}"
        )
        batch = _list(get(path), "workflow_runs", path)
        runs.extend(batch)
        if len(batch) < RUNS_PAGE:
            return runs
    raise StartReadError(
        f"more than {MAX_PAGES * RUNS_PAGE} scheduled runs since {since}"
    )


def evaluate(
    get: Get,
    *,
    now: datetime,
    repository: str = REPOSITORY,
    workflow: str = WORKFLOW,
    window: timedelta = WINDOW,
) -> StartVerdict:
    """The verdict over the scheduled runs created in the last ``window``."""
    hours = int(window.total_seconds() // 3600)
    try:
        runs = scheduled_runs(
            get, now=now, repository=repository, workflow=workflow, window=window
        )
        found: list[str] = []
        for run in runs:
            # A run with a never-started job concludes failure; others need no read.
            if run.get("status") != "completed" or run.get("conclusion") != "failure":
                continue
            path = f"/repos/{repository}/actions/runs/{run.get('id')}/jobs?per_page=100"
            for job in _list(get(path), "jobs", path):
                if never_started(job):
                    found.append(
                        f"{job.get('name')} (run {run.get('id')}, {run.get('created_at')})"
                    )
    except Exception as exc:  # noqa: BLE001 - an unreadable answer is red, never a pass.
        return StartVerdict(UNVERIFIABLE, f"{type(exc).__name__}: {exc}")
    if found:
        shown = "; ".join(found[:DETAIL_LIMIT])
        more = (
            f" (+{len(found) - DETAIL_LIMIT} more)" if len(found) > DETAIL_LIMIT else ""
        )
        return StartVerdict(
            NEVER_STARTED,
            f"{len(found)} scheduled job(s) of {workflow} failed before starting "
            f"in the last {hours} h, so their own alert never ran: {shown}{more}",
        )
    return StartVerdict(
        OK,
        f"{len(runs)} scheduled run(s) of {workflow} in the last {hours} h; every failed job started",
    )
