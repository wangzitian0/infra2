"""Estate scheduler liveness (#1432, successor of truealpha#876 W5).

truealpha's ``scheduler-liveness`` workflow was deleted in truealpha#1589 when the
SRE workflows moved to infra2 (#1416). This module replaces it and its peer check.

For each estate repository it DISCOVERS the scheduled workflows through the
GitHub API (the workflow listing, then each file on the default branch and its
``schedule:`` crons). The bound of each workflow comes from its own crons: two
times the largest gap plus an hour. No workflow name or period is written here.

A scheduled workflow is red when:

- its state is not ``active`` (``disabled_*``, ``deleted``): DISABLED;
- its newest scheduled run that started a job is older than the bound: STALE;
- it never ran on schedule and its file changed longer ago than the bound: NEVER;
- a cron cannot be measured: INVALID;
- an API read failed, a listing is truncated, or the file is gone: UNVERIFIABLE.
  A read error is never a pass.

The estate has no scheduled workflow at all only when a read lied: that case is
UNVERIFIABLE, not green. Workflows outside ``.github/workflows/`` are skipped.
A listed workflow whose file is missing on the default branch is skipped when it
never ran on schedule (branch-only), and DISABLED when it did. A workflow whose
listing entry is gone cannot be discovered; see ``docs/ssot/ops.observability.md``.

Canonical cron and bound primitives live in ``infra2_sdk`` (#1270).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from infra2_sdk.ci import ScheduleError, crons_in
from infra2_sdk.observability import (
    DELAY_ALLOWANCE,
    MISSED_TICKS_TOLERATED,
    PeerReadError,
    PeerVerdict,
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


# --- cron gap -----------------------------------------------------------------
# infra2_sdk.ci.largest_gap reads only daily-periodic crons and rejects a
# day-of-week restriction (live: ``0 1 * * 1`` is INVALID). The estate has weekly
# crons, so the gap is measured here over a window of days.

#: A cron must fire at least twice in this window to have a measurable gap.
GAP_WINDOW_DAYS = 62
_NAMES = {
    "month": {
        n: i
        for i, n in enumerate(
            "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1
        )
    },
    "dow": {n: i for i, n in enumerate("SUN MON TUE WED THU FRI SAT".split())},
}
_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),
}


def _value(token: str, field: str) -> int:
    low, high = _RANGES[field]
    number = int(token) if token.isdigit() else _NAMES.get(field, {}).get(token.upper())
    if number is None or not low <= number <= high:
        raise ScheduleError(
            f"{field}: {token!r} is not a value in {low}-{high}; teach this reader"
        )
    return number


def _field_values(text: str, field: str) -> set[int]:
    low, high = _RANGES[field]
    values: set[int] = set()
    for element in text.split(","):
        span, _, step_text = element.partition("/")
        if step_text and not step_text.isdigit():
            raise ScheduleError(f"{field}: step {step_text!r} is not a number")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ScheduleError(f"{field}: step must be at least 1")
        if span == "*":
            first, last = low, (6 if field == "dow" else high)
        elif "-" in span:
            first, _, end = span.partition("-")
            first, last = _value(first, field), _value(end, field)
        else:
            first = _value(span, field)
            last = high if step_text else first
        if first > last:
            raise ScheduleError(f"{field}: range {span!r} runs backwards")
        values.update(range(first, last + 1, step))
    return values


def largest_gap(crons: list[str]) -> timedelta:
    """Return the largest gap between consecutive fire times of all *crons*."""
    if not crons:
        raise ScheduleError("the workflow has no cron to measure")
    start = datetime(
        2026, 1, 1, tzinfo=UTC
    )  # a fixed Thursday; the gap is calendar-independent enough
    fires: list[datetime] = []
    for cron in crons:
        parts = cron.split()
        if len(parts) != 5:
            raise ScheduleError(
                f"cron {cron!r} does not have 5 fields; teach this reader"
            )
        minutes, hours, doms, months, dows = (
            _field_values(part, field)
            for part, field in zip(parts, _RANGES, strict=True)
        )
        dows = {d % 7 for d in dows}
        dom_free, dow_free = parts[2].startswith("*"), parts[4].startswith("*")
        for offset in range(GAP_WINDOW_DAYS):
            day = start + timedelta(days=offset)
            weekday = (day.weekday() + 1) % 7
            by_dom, by_dow = day.day in doms, weekday in dows
            if day.month not in months:
                continue
            # Vixie rule: both restricted -> either matches; else both must match.
            if not (
                (by_dom or by_dow)
                if not dom_free and not dow_free
                else (by_dom and by_dow)
            ):
                continue
            fires.extend(day.replace(hour=h, minute=m) for h in hours for m in minutes)
    fires = sorted(set(fires))
    if len(fires) < 2:
        raise ScheduleError(
            f"crons {crons!r} fire fewer than twice in {GAP_WINDOW_DAYS} days; teach this reader"
        )
    return max(b - a for a, b in zip(fires, fires[1:], strict=False))


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


def _newest(get: Get, path: str) -> datetime | None:
    try:
        return newest_tick(_runs(get, path))
    except ValueError as exc:  # an unreadable created_at
        raise PeerReadError(f"GET {path}: {exc}") from exc


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
        if not ran.get("total_count"):
            return None
        return WorkflowVerdict(
            repository,
            path,
            DISABLED,
            f"{where} is deleted from {branch} but ran on schedule {ran['total_count']} times",
        )
    crons = crons_in(text)
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
    newest = _newest(get, f"{base}/runs?event=schedule&per_page={SCHEDULED_PAGE}")
    if newest is None or now - newest > bound:
        witness = _newest(get, f"{base}/runs?per_page={UNFILTERED_PAGE}")
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
) -> PeerVerdict:
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
        return PeerVerdict(
            status, f"{len(red)} of {len(verdicts)} red; " + "; ".join(shown)
        )
    if not verdicts:
        return PeerVerdict(
            UNVERIFIABLE,
            "no scheduled workflow found in the estate: the listing cannot be trusted",
        )
    return PeerVerdict(
        OK,
        f"{len(verdicts)} scheduled workflows in {len(repositories)} repositories "
        "ticked within their measured bounds",
    )


__all__ = [
    "API_TIMEOUT_SECONDS",
    "BOUND_CAP_ENV",
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
    "PeerVerdict",
    "SCHEDULED_PAGE",
    "STALE",
    "ScheduleError",
    "UNFILTERED_PAGE",
    "UNVERIFIABLE",
    "WorkflowVerdict",
    "bound_for",
    "crons_in",
    "evaluate_estate_liveness",
    "evaluate_workflows",
    "github_getter",
    "largest_gap",
    "newest_tick",
    "parse_bound_cap_hours",
]
