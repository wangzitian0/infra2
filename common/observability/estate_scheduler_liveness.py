"""Estate scheduler liveness (#1432, successor of truealpha#876 W5).

truealpha's ``scheduler-liveness`` workflow was deleted in truealpha#1589 when the
SRE workflows moved to infra2 (#1416). This module replaces it and its peer check.

For each estate repository it DISCOVERS the scheduled workflows through the
GitHub API (the workflow listing, then each file on the default branch and its
``schedule:`` crons). The bound of each workflow comes from its own crons: two
times the largest gap plus an hour. No workflow name or period is written here.

A scheduled workflow is red when:

- its state is not ``active`` (``disabled_*``): DISABLED. The live listing does not
  return ``deleted`` workflows (truealpha ``scheduler-liveness.yml``, id 359536403,
  has state ``deleted`` and is absent from it), so a workflow deleted from the
  default branch is NOT detected. A state ``deleted`` entry would be DISABLED;
- a listed workflow whose file is missing on the default branch ran on schedule
  before: DISABLED. Without a scheduled run it is branch-only and skipped;
- its newest scheduled run that started a job is older than the bound: STALE.
  Scheduled runs that exist but none started a job are STALE too, once one is a
  ``startup_failure`` or has queued longer than ``QUEUED_RUN_ALLOWANCE`` (1 hour).
  A run seen in both run listings counts once (by run id);
- it never ran on schedule and its file changed longer ago than the bound: NEVER;
- the YAML is invalid, ``schedule`` is named with no cron (``on: schedule``,
  ``on: [push, schedule]``, ``schedule: []``, an entry without ``cron``), or a cron
  cannot be measured: INVALID;
- an API read failed or a listing is truncated: UNVERIFIABLE. A read error is
  never a pass.

The schedule is read from the parsed YAML (``on.schedule[*].cron``), so flow style,
quoted keys and the boolean ``on`` key are handled. The estate with no scheduled
workflow is UNVERIFIABLE, not green. Workflows outside ``.github/workflows/`` are
skipped. The watchdog runs daily, so detection lags a stop by up to bound + 24 h.
Nothing automated watches the scheduler of ``ops-checks.yml`` itself.

Canonical cron and bound primitives live in ``infra2_sdk`` (#1270).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import yaml

from infra2_sdk.ci import ScheduleError, largest_gap
from infra2_sdk.observability import (
    DELAY_ALLOWANCE,
    MISSED_TICKS_TOLERATED,
    PeerReadError,
    bound_for,
    github_getter,
    newest_tick,
    parse_bound_cap_hours,
)

ESTATE_REPOSITORIES = (
    "wangzitian0/truealpha",
    "wangzitian0/infra2",
    "wangzitian0/finance_report",
    "wangzitian0/infra2-sdk",
)
WORKFLOW_DIR = ".github/workflows/"
BOUND_CAP_ENV = "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS"
LISTING_PAGE = 100
SCHEDULED_PAGE = 5
UNFILTERED_PAGE = 50
API_TIMEOUT_SECONDS = 20.0
MAX_DETAIL_ENTRIES = 12
#: A scheduled run that has not started a job is red only after this age, or at once
#: on ``startup_failure``. The first run of a new workflow waits in the queue first.
QUEUED_RUN_ALLOWANCE = timedelta(hours=1)
#: A timestamp this far ahead of the local clock is clock skew, not a young run. The
#: Worker check (``tools/out_of_band_watchdog.py``) reads this one value too.
CLOCK_SKEW_TOLERANCE_SECONDS = 300

OK = "OK"
STALE = "STALE"
DISABLED = "DISABLED"
NEVER = "NEVER"
INVALID = "INVALID"
UNVERIFIABLE = "UNVERIFIABLE"
#: Aggregate status order: a retryable read failure ranks last.
_STATUS_ORDER = (DISABLED, STALE, NEVER, INVALID, UNVERIFIABLE)
LISTING = "(workflow listing)"

Get = Callable[[str], object]


@dataclass(frozen=True)
class WorkflowVerdict:
    repository: str
    path: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == OK


def schedule_crons(text: str) -> list[str]:
    """Return ``on.schedule[*].cron`` of a workflow file; ``[]`` when it has no schedule.

    Raises ``ScheduleError`` for invalid YAML or a ``schedule`` entry with no cron.
    PyYAML reads the key ``on`` as boolean ``True``; both spellings are accepted.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScheduleError(f"the file is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ScheduleError("the file is not a YAML mapping")
    triggers = document.get("on", document.get(True))
    if triggers == "schedule" or (
        isinstance(triggers, list) and "schedule" in triggers
    ):
        raise ScheduleError("on names schedule without a cron: GitHub never runs it")
    if not isinstance(triggers, dict) or "schedule" not in triggers:
        return []
    entries = triggers["schedule"]
    if not isinstance(entries, list) or not entries:
        raise ScheduleError("on.schedule has no cron: it is not a non-empty list")
    crons = []
    for entry in entries:
        cron = entry.get("cron") if isinstance(entry, dict) else None
        if not isinstance(cron, str) or not cron.strip():
            raise ScheduleError(f"a schedule entry has no cron: {entry!r}")
        crons.append(cron.strip())
    return crons


@dataclass(frozen=True)
class EstateVerdict:
    """The estate verdict. ``units`` names each red workflow as ``repo:path:status``.

    Units hold no age or timestamp, so a reading change keeps the page identity and
    a second red workflow changes it (#962).
    """

    status: str
    detail: str
    units: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == OK


def _object(get: Get, path: str) -> dict:
    body = get(path)
    if not isinstance(body, dict):
        raise PeerReadError(f"GET {path} returned {type(body).__name__}, not an object")
    return body


def _hours(span: timedelta) -> str:
    return f"{span.total_seconds() / 3600:.1f}h"


def _timestamp(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise PeerReadError(f"{what} has no timestamp ({value!r})")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PeerReadError(f"{what} has an unreadable timestamp ({value!r})") from exc
    if parsed.tzinfo is None:
        raise PeerReadError(f"{what} has a timestamp without a zone ({value!r})")
    return parsed.astimezone(UTC)


def _runs(get: Get, path: str) -> list[dict]:
    runs = _object(get, path).get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise PeerReadError(f"GET {path} has no usable workflow_runs list")
    return runs


#: Conclusions of a completed run that did not execute its scheduled work (#1484).
_NOT_A_TICK_CONCLUSIONS = frozenset({"cancelled", "skipped"})


def _tick(runs: list[dict], base: str, now: datetime) -> datetime | None:
    eligible = [
        run for run in runs if run.get("conclusion") not in _NOT_A_TICK_CONCLUSIONS
    ]
    try:
        tick = newest_tick(eligible)
    except ValueError as exc:  # an unreadable created_at
        raise PeerReadError(f"{base} runs: {exc}") from exc
    if tick is not None and tick - now > timedelta(
        seconds=CLOCK_SKEW_TOLERANCE_SECONDS
    ):
        raise PeerReadError(
            f"{base} newest tick {tick.isoformat()} is ahead of the clock "
            "(clock skew?): its age cannot be trusted"
        )
    return tick


def _distinct(runs: list[dict]) -> list[dict]:
    """Drop repeats of one run id; the two run listings overlap."""
    unique: dict[object, dict] = {}
    for run in runs:
        key = run.get("id")
        unique.setdefault(id(run) if key is None else key, run)
    return list(unique.values())


def _never_started(run: dict, now: datetime) -> bool:
    """Return True for a run that failed to start or has queued past the allowance."""
    if (
        run.get("conclusion") == "startup_failure"
        or run.get("conclusion") in _NOT_A_TICK_CONCLUSIONS
    ):
        return True
    created = _timestamp(run.get("created_at"), f"run {run.get('id')}")
    if created - now > timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS):
        raise PeerReadError(
            f"run {run.get('id')} created_at {run.get('created_at')} is ahead of the "
            "clock (clock skew?): its age cannot be trusted"
        )
    return now - created > QUEUED_RUN_ALLOWANCE


def _file_text(get: Get, repository: str, path: str, branch: str) -> str:
    body = _object(
        get, f"/repos/{repository}/contents/{quote(path)}?ref={quote(branch, safe='')}"
    )
    if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        raise PeerReadError(f"{path} came back without base64 content")
    try:
        return base64.b64decode(body["content"]).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise PeerReadError(f"{path} content does not decode: {exc}") from exc


def _last_changed(get: Get, repository: str, path: str, branch: str) -> datetime:
    commits = get(
        f"/repos/{repository}/commits?path={quote(path, safe='')}"
        f"&sha={quote(branch, safe='')}&per_page=1"
    )
    if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
        raise PeerReadError(f"no commit on {branch} touches {path}")
    committer = (commits[0].get("commit") or {}).get("committer") or {}
    return _timestamp(committer.get("date"), f"the last commit to {path}")


def _list_workflows(get: Get, repository: str) -> list[dict]:
    path = f"/repos/{repository}/actions/workflows?per_page={LISTING_PAGE}"
    body = _object(get, path)
    workflows = body.get("workflows")
    total = body.get("total_count")
    if not isinstance(workflows, list) or not all(
        isinstance(w, dict) for w in workflows
    ):
        raise PeerReadError(f"GET {path} has no usable workflows list")
    if not isinstance(total, int) or total > len(workflows):
        raise PeerReadError(
            f"GET {path} lists {len(workflows)} of {total!r} workflows: truncated"
        )
    return [w for w in workflows if str(w.get("path", "")).startswith(WORKFLOW_DIR)]


def _evaluate_workflow(
    get: Get,
    repository: str,
    workflow: dict,
    branch: str,
    *,
    now: datetime,
    bound_cap: timedelta | None,
) -> WorkflowVerdict | None:
    """Return the verdict of one workflow, or ``None`` when it has no schedule."""
    path = str(workflow["path"])
    where = f"{repository} {path}"
    state = workflow.get("state")
    workflow_id = workflow.get("id")
    if state == "deleted":
        return WorkflowVerdict(repository, path, DISABLED, f"{where} state='deleted'")
    try:
        text = _file_text(get, repository, path, branch)
    except PeerReadError as exc:
        if "HTTP 404" not in str(exc):
            raise
        # Schedules fire only from the default branch. A listed workflow whose file
        # is not there is branch-only, unless it already ran on schedule: then its
        # file was deleted while it was in use.
        base = f"/repos/{repository}/actions/workflows/{workflow_id}"
        ran = _object(get, f"{base}/runs?event=schedule&per_page={SCHEDULED_PAGE}")
        count = ran.get("total_count")
        if not isinstance(count, int) or isinstance(count, bool):
            raise PeerReadError(f"GET {base}/runs has no integer total_count")
        if count == 0:
            return None
        return WorkflowVerdict(
            repository,
            path,
            DISABLED,
            f"{where} is deleted from {branch} but ran on schedule {count} times",
        )
    try:
        crons = schedule_crons(text)
    except ScheduleError as exc:
        return WorkflowVerdict(repository, path, INVALID, f"{where}: {exc}")
    if not crons:
        return None
    if state != "active":
        return WorkflowVerdict(
            repository,
            path,
            DISABLED,
            f"{where} state={state!r}, not 'active': its scheduler does not run",
        )
    try:
        bound = bound_for(largest_gap(crons))
    except ScheduleError as exc:
        return WorkflowVerdict(repository, path, INVALID, f"{where}: {exc}")
    if bound_cap is not None:
        bound = min(bound, bound_cap)
    base = f"/repos/{repository}/actions/workflows/{workflow_id}"
    scheduled = _runs(get, f"{base}/runs?event=schedule&per_page={SCHEDULED_PAGE}")
    newest = _tick(scheduled, base, now)
    if newest is None or now - newest > bound:
        unfiltered = _runs(get, f"{base}/runs?per_page={UNFILTERED_PAGE}")
        scheduled = _distinct(
            scheduled + [run for run in unfiltered if run.get("event") == "schedule"]
        )
        witness = _tick(unfiltered, base, now)
        if witness is not None and (newest is None or witness > newest):
            newest = witness
    if newest is not None:
        age = now - newest
        seen = f"{where}: newest scheduled run {newest.isoformat()}"
        if age > bound:
            return WorkflowVerdict(
                repository,
                path,
                STALE,
                f"{seen} is {_hours(age)} old, beyond the {_hours(bound)} bound: "
                "its scheduler stopped ticking",
            )
        return WorkflowVerdict(
            repository, path, OK, f"{seen} is {_hours(age)} old (bound {_hours(bound)})"
        )
    if scheduled:
        total = f"{len(scheduled)} scheduled run{'' if len(scheduled) == 1 else 's'}"
        if any([_never_started(run, now) for run in scheduled]):  # reads every run
            return WorkflowVerdict(
                repository,
                path,
                STALE,
                f"{where} has {total} and none started a job: "
                "its scheduler fires but the workflow does not run",
            )
        return WorkflowVerdict(
            repository,
            path,
            OK,
            f"{where} has {total} queued for under {_hours(QUEUED_RUN_ALLOWANCE)}, "
            "waiting for the first job to start",
        )
    since = now - _last_changed(get, repository, path, branch)
    never = f"{where} never ran on schedule; its file changed {_hours(since)} ago"
    if since > bound:
        return WorkflowVerdict(
            repository, path, NEVER, f"{never}, beyond the {_hours(bound)} bound"
        )
    return WorkflowVerdict(
        repository,
        path,
        OK,
        f"{never}, waiting for its first tick (bound {_hours(bound)})",
    )


def evaluate_workflows(
    get: Get,
    *,
    now: datetime,
    bound_cap: timedelta | None = None,
    repositories: Iterable[str] | None = None,
) -> list[WorkflowVerdict]:
    """Return one verdict per scheduled workflow, plus one per unreadable repository."""
    verdicts: list[WorkflowVerdict] = []
    for repository in ESTATE_REPOSITORIES if repositories is None else repositories:
        try:
            workflows = _list_workflows(get, repository)
            branch = _object(get, f"/repos/{repository}").get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise PeerReadError(f"{repository} has no default_branch")
        except PeerReadError as exc:
            verdicts.append(
                WorkflowVerdict(
                    repository,
                    LISTING,
                    UNVERIFIABLE,
                    f"cannot list {repository}: {exc}",
                )
            )
            continue
        for workflow in workflows:
            try:
                verdict = _evaluate_workflow(
                    get, repository, workflow, branch, now=now, bound_cap=bound_cap
                )
            except PeerReadError as exc:
                verdict = WorkflowVerdict(
                    repository,
                    str(workflow.get("path")),
                    UNVERIFIABLE,
                    f"cannot verify {repository} {workflow.get('path')}: {exc}",
                )
            if verdict is not None:
                verdicts.append(verdict)
    return verdicts


def evaluate_estate_liveness(
    get: Get,
    *,
    now: datetime,
    bound_cap: timedelta | None = None,
    repositories: Iterable[str] | None = None,
) -> EstateVerdict:
    """Fold the per-workflow verdicts into one watchdog verdict."""
    repositories = tuple(ESTATE_REPOSITORIES if repositories is None else repositories)
    verdicts = evaluate_workflows(
        get, now=now, bound_cap=bound_cap, repositories=repositories
    )
    red = [v for v in verdicts if not v.ok]
    if red:
        status = next(s for s in _STATUS_ORDER if any(v.status == s for v in red))
        lines = [f"{v.repository} {v.path} {v.status}: {v.detail}" for v in red]
        shown = lines[:MAX_DETAIL_ENTRIES]
        if len(lines) > len(shown):
            shown.append(f"+{len(lines) - len(shown)} more")
        units = tuple(sorted(f"{v.repository}:{v.path}:{v.status}" for v in red))
        return EstateVerdict(
            status, f"{len(red)} of {len(verdicts)} red; " + "; ".join(shown), units
        )
    if not verdicts:
        return EstateVerdict(
            UNVERIFIABLE,
            "no scheduled workflow found in the estate: the listing cannot be trusted",
            (f"{LISTING}:{UNVERIFIABLE}",),
        )
    return EstateVerdict(
        OK,
        f"{len(verdicts)} scheduled workflows in {len(repositories)} repositories "
        "ticked within their measured bounds",
    )


__all__ = [
    "API_TIMEOUT_SECONDS",
    "BOUND_CAP_ENV",
    "CLOCK_SKEW_TOLERANCE_SECONDS",
    "DELAY_ALLOWANCE",
    "DISABLED",
    "ESTATE_REPOSITORIES",
    "Get",
    "INVALID",
    "LISTING",
    "LISTING_PAGE",
    "MISSED_TICKS_TOLERATED",
    "NEVER",
    "OK",
    "PeerReadError",
    "EstateVerdict",
    "SCHEDULED_PAGE",
    "STALE",
    "ScheduleError",
    "UNFILTERED_PAGE",
    "UNVERIFIABLE",
    "WorkflowVerdict",
    "bound_for",
    "schedule_crons",
    "evaluate_estate_liveness",
    "evaluate_workflows",
    "github_getter",
    "largest_gap",
    "newest_tick",
    "parse_bound_cap_hours",
]
