"""Peer liveness for truealpha's ``scheduler-liveness`` workflow (truealpha#876).

Canonical implementation lives in ``infra2_sdk.observability`` (#1270).
Includes a stdlib fallback for bare-interpreter CI jobs (``ops-checks.yml``).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PEER_REPOSITORY = "wangzitian0/truealpha"
PEER_WORKFLOW = "scheduler-liveness.yml"
BOUND_CAP_ENV = "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS"
SCHEDULED_PAGE = 5
UNFILTERED_PAGE = 50
STARTED = ("in_progress", "completed")
STARTUP_FAILURE = "startup_failure"
MINUTES_PER_DAY = 24 * 60
API_BASE = "https://api.github.com"
API_TIMEOUT_SECONDS = 20.0

OK = "OK"
STALE = "STALE"
DISABLED = "DISABLED"
NEVER = "NEVER"
INVALID = "INVALID"
UNVERIFIABLE = "UNVERIFIABLE"

Get = Callable[[str], object]

try:
    from infra2_sdk.observability import (
        DELAY_ALLOWANCE,
        MISSED_TICKS_TOLERATED,
        PeerReadError,
        PeerVerdict,
        ScheduleError,
        bound_for,
        crons_in,
        evaluate_peer_liveness,
        github_getter,
        largest_gap,
        newest_tick,
        parse_bound_cap_hours,
    )
except ImportError:  # pragma: no cover - bare interpreter without infra2-sdk installed
    MISSED_TICKS_TOLERATED = 2
    DELAY_ALLOWANCE = timedelta(hours=1)

    class PeerReadError(RuntimeError):  # type: ignore[no-redef]
        """A read the verdict depends on did not answer."""

    class ScheduleError(ValueError):  # type: ignore[no-redef]
        """The workflow's schedule is not one this module can measure."""

    @dataclass(frozen=True)
    class PeerVerdict:  # type: ignore[no-redef]
        status: str
        detail: str

        @property
        def ok(self) -> bool:
            return self.status == OK

    def github_getter(  # type: ignore[misc]
        token: str = "", *, timeout: float = API_TIMEOUT_SECONDS, opener=urlopen
    ) -> Get:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "infra2-scheduler-peer-liveness/1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def get(path: str) -> object:
            request = Request(f"{API_BASE}{path}", headers=headers, method="GET")
            try:
                with opener(request, timeout=timeout) as response:  # noqa: S310
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raise PeerReadError(f"GET {path} answered HTTP {exc.code}") from exc
            except (OSError, URLError, ValueError) as exc:
                raise PeerReadError(f"GET {path} failed: {exc}") from exc

        return get

    _CRON_LINE = re.compile(
        r"""^[ \t]*-?[ \t]*cron:[ \t]*(?P<quote>["']?)(?P<expr>[^"'#\n]+?)(?P=quote)[ \t]*(?:#.*)?$""",
        re.MULTILINE,
    )
    _ELEMENT = re.compile(r"^(?P<span>\*|\d+(?:-\d+)?)(?:/(?P<step>\d+))?$")

    def crons_in(workflow_text: str) -> list[str]:  # type: ignore[misc]
        return [match.group("expr").strip() for match in _CRON_LINE.finditer(workflow_text)]

    def _field(text: str, name: str, low: int, high: int) -> set[int]:
        values: set[int] = set()
        for element in text.split(","):
            match = _ELEMENT.match(element)
            if not match:
                raise ScheduleError(f"{name} {element!r} is not a number, range or step")
            span, step_text = match.group("span"), match.group("step")
            step = int(step_text) if step_text is not None else 1
            if step < 1:
                raise ScheduleError(f"{name} step in {element!r} must be at least 1")
            if span == "*":
                first, last = low, high
            elif "-" in span:
                first, last = (int(part) for part in span.split("-", 1))
            else:
                first = int(span)
                last = high if step_text is not None else first
            if not low <= first <= last <= high:
                raise ScheduleError(f"{name} {element!r} is outside {low}-{high}")
            values.update(range(first, last + 1, step))
        return values

    def largest_gap(crons: Iterable[str]) -> timedelta:  # type: ignore[misc]
        fires: set[int] = set()
        listed = list(crons)
        if not listed:
            raise ScheduleError("the workflow has no cron")
        for expression in listed:
            parts = expression.split()
            if len(parts) != 5:
                raise ScheduleError(f"cron {expression!r} has {len(parts)} fields, not 5")
            minute, hour, day, month, weekday = parts
            if (day, month, weekday) != ("*", "*", "*"):
                raise ScheduleError(
                    f"cron {expression!r} is not daily-periodic; teach "
                    "libs/observability/scheduler_peer_liveness.py to measure it"
                )
            minutes = _field(minute, "minute", 0, 59)
            hours = _field(hour, "hour", 0, 23)
            fires.update(h * 60 + m for h in hours for m in minutes)
        ordered = sorted(fires)
        gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:], strict=False)]
        gaps.append(ordered[0] + MINUTES_PER_DAY - ordered[-1])
        return timedelta(minutes=max(gaps))

    def bound_for(gap: timedelta) -> timedelta:  # type: ignore[misc]
        return MISSED_TICKS_TOLERATED * gap + DELAY_ALLOWANCE

    def parse_bound_cap_hours(raw: str | None) -> timedelta | None:  # type: ignore[misc]
        text = (raw or "").strip()
        if not text:
            return None
        hours = float(text)
        if not math.isfinite(hours) or hours < 0:
            raise ValueError(f"bound cap {text!r} must be a finite number of hours >= 0")
        return timedelta(hours=hours)

    def _object(get: Get, path: str) -> dict:
        body = get(path)
        if not isinstance(body, dict):
            raise PeerReadError(f"GET {path} returned {type(body).__name__}, not an object")
        return body

    def _timestamp(value: object, what: str) -> datetime:
        if not isinstance(value, str):
            raise PeerReadError(f"{what} has no timestamp ({value!r})")
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PeerReadError(f"{what} has an unreadable timestamp {value!r}") from exc
        if moment.tzinfo is None:
            raise PeerReadError(f"{what} has a timestamp without a zone ({value!r})")
        return moment.astimezone(UTC)

    def _runs(get: Get, path: str) -> list[dict]:
        body = _object(get, path)
        runs = body.get("workflow_runs")
        if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
            raise PeerReadError(f"GET {path} has no usable workflow_runs list")
        return runs

    def newest_tick(runs: Iterable[dict]) -> datetime | None:  # type: ignore[misc]
        ticks = [
            _timestamp(run.get("created_at"), f"run {run.get('id')}")
            for run in runs
            if run.get("event") == "schedule"
            and run.get("status") in STARTED
            and run.get("conclusion") != STARTUP_FAILURE
        ]
        return max(ticks) if ticks else None

    def _file_text(get: Get, path: str, branch: str) -> str:
        body = _object(
            get,
            f"/repos/{PEER_REPOSITORY}/contents/{quote(path)}?ref={quote(branch, safe='')}",
        )
        if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
            raise PeerReadError(f"{path} came back without base64 content")
        try:
            return base64.b64decode(body["content"]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise PeerReadError(f"{path} content does not decode: {exc}") from exc

    def _last_changed(get: Get, path: str, branch: str) -> datetime:
        commits = get(
            f"/repos/{PEER_REPOSITORY}/commits?path={quote(path, safe='')}"
            f"&sha={quote(branch, safe='')}&per_page=1"
        )
        if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
            raise PeerReadError(f"no commit on {branch} touches {path}")
        committer = (commits[0].get("commit") or {}).get("committer") or {}
        return _timestamp(committer.get("date"), f"the last commit to {path}")

    def _hours(span: timedelta) -> str:
        return f"{span.total_seconds() / 3600:.1f}h"

    def evaluate_peer_liveness(  # type: ignore[misc]
        get: Get, *, now: datetime, bound_cap: timedelta | None = None
    ) -> PeerVerdict:
        workflow_path = f"/repos/{PEER_REPOSITORY}/actions/workflows/{PEER_WORKFLOW}"
        try:
            workflow = _object(get, workflow_path)
            state = workflow.get("state")
            if state != "active":
                return PeerVerdict(
                    DISABLED,
                    f"{PEER_REPOSITORY} {PEER_WORKFLOW} state={state!r}, not 'active': "
                    "its scheduler does not run",
                )
            path = workflow.get("path")
            if not isinstance(path, str) or not path:
                raise PeerReadError(f"GET {workflow_path} has no path")
            branch = _object(get, f"/repos/{PEER_REPOSITORY}").get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise PeerReadError(f"{PEER_REPOSITORY} has no default_branch")
            try:
                bound = bound_for(largest_gap(crons_in(_file_text(get, path, branch))))
            except ScheduleError as exc:
                return PeerVerdict(INVALID, f"{PEER_REPOSITORY} {path}: {exc}")
            if bound_cap is not None:
                bound = min(bound, bound_cap)

            newest = newest_tick(
                _runs(get, f"{workflow_path}/runs?event=schedule&per_page={SCHEDULED_PAGE}")
            )
            if newest is None or now - newest > bound:
                witness = newest_tick(
                    _runs(get, f"{workflow_path}/runs?per_page={UNFILTERED_PAGE}")
                )
                if witness is not None and (newest is None or witness > newest):
                    newest = witness
            if newest is not None:
                age = now - newest
                where = f"{PEER_REPOSITORY} {PEER_WORKFLOW}: newest scheduled run {newest.isoformat()}"
                if age > bound:
                    return PeerVerdict(
                        STALE,
                        f"{where} is {_hours(age)} old, beyond the {_hours(bound)} bound: "
                        "its scheduler stopped ticking",
                    )
                return PeerVerdict(
                    OK, f"{where} is {_hours(age)} old (bound {_hours(bound)})"
                )

            changed = _last_changed(get, path, branch)
            since = now - changed
            where = (
                f"{PEER_REPOSITORY} {PEER_WORKFLOW} never ran on schedule; "
                f"{path} changed {_hours(since)} ago"
            )
            if since > bound:
                return PeerVerdict(NEVER, f"{where}, beyond the {_hours(bound)} bound")
            return PeerVerdict(
                OK, f"{where}, waiting for its first tick (bound {_hours(bound)})"
            )
        except PeerReadError as exc:
            return PeerVerdict(
                UNVERIFIABLE, f"cannot verify {PEER_REPOSITORY} {PEER_WORKFLOW}: {exc}"
            )


evaluate = evaluate_peer_liveness

__all__ = [
    "API_BASE",
    "API_TIMEOUT_SECONDS",
    "BOUND_CAP_ENV",
    "DELAY_ALLOWANCE",
    "DISABLED",
    "Get",
    "INVALID",
    "MINUTES_PER_DAY",
    "MISSED_TICKS_TOLERATED",
    "NEVER",
    "OK",
    "PEER_REPOSITORY",
    "PEER_WORKFLOW",
    "PeerReadError",
    "PeerVerdict",
    "SCHEDULED_PAGE",
    "STALE",
    "STARTED",
    "STARTUP_FAILURE",
    "ScheduleError",
    "UNFILTERED_PAGE",
    "UNVERIFIABLE",
    "bound_for",
    "crons_in",
    "evaluate",
    "evaluate_peer_liveness",
    "github_getter",
    "largest_gap",
    "newest_tick",
    "parse_bound_cap_hours",
]
