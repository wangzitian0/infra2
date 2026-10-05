"""Page a daily GitHub-plane job's conclusion only when its identity changes (#962).

Measured in the pager chat, 2026-09-26..10-05: 28 of ~40 messages were three
conditions (`infra2-backup-production` 10, the Vault self-refresh audit 9, the daily
facet reconcile 9), each paged again every day with the same conclusion. Their alert
steps paged "on any confirmed failure" and kept no memory across runs. The in-band
layer already stopped this (#903: only an identity change pages; a chronic failure is
a report); this is the same rule for the GitHub plane.

**Identity** = the job plus the stable key of each confirmed finding (a name, never a
reading, an age, a size, a timestamp or a run URL). A reading in the key would make
every day a new identity, which is the bug.

**Decision** (``decide``; ``mode`` is ``issue_trail_mode``):

====================================  =================================================
state read / what this run found      action
====================================  =================================================
off (dry run, SSH override, …)        page as before (dry run: deliver nothing)
state could not be read, findings     PAGE (fail loud); nothing is written
state marker unreadable, findings     PAGE; the marker is rewritten
no state, findings                    PAGE (first appearance); state recorded
same identity as the recorded one     REPORT ``[报告] 仍未恢复 · 第 N 天 · <job>`` to the
                                      reports chat, not the pager; nothing is written
different identity (added/removed)    PAGE with the whole new set; state replaced
no findings, run proved none, state   RESOLVED page, state closed (never in an
recorded                              open-only run: a drill or branch dispatch never
                                      closes anything)
no findings, run did not prove none   nothing (a transient lookup failure is not a
                                      recovery); the state is kept
no findings, but a recorded finding   nothing: a check that did not run (a skipped
belongs to a check not evaluated      section, an unreadable quota) has not recovered
open-only run (drill, branch          may page and report, but records nothing: a
dispatch)                             drill's identity must not become what the next
                                      scheduled run trusts
====================================  =================================================

The identity deliberately excludes readings (ages, sizes, counts, quota levels): a
worsening *within* one finding is not a new page. It is visible in the daily
``[报告] 仍未恢复`` message, which carries the current detail lines. A finding that
aggregates several units (a backup check over many artifacts) names each failing unit
in its keys, so a different unit failing, or the same unit failing worse in kind
(severity), is a new identity.

**State** is one open GitHub issue per job, titled exactly ``ops-checks paged: <job>``,
whose body ends in a ``<!-- infra2-page-dedup {json} -->`` marker holding the
identity's fingerprint, its (scrubbed) keys and when it began. It reuses the issue
trail's client, listing, scrubbing and run modes (``libs/observability/issue_trail.py``)
but not its issues: the trail's issues are opened by the job's *last* step whether or
not the page was delivered, so reading them as "already paged" would hide a page that
never arrived. Here state is written only after the page was delivered. Only issues
authored by the Actions bot or the repository owner count: the repository is public and
a stranger's same-title issue must not be able to silence the pager.

Failure never silences: a listing that fails, an unreadable marker, a missing token, a
report that cannot be delivered, or any exception in this module's own decision code
pages exactly as the jobs did before (``dedup_page`` guards its planning step). A page
that cannot be delivered raises and records nothing, so the next run pages again.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from libs.alerting import (
    DISPLAY_TIMEZONE,
    REPORT_TITLE_PREFIX,
    deliver_infra2_report,
    format_time,
    since_text,
)
from libs.observability.issue_trail import (
    DRY_RUN_ENV,
    ISSUE_LABELS,
    OFF,
    OPEN_ONLY,
    GitHubIssues,
    issue_trail_mode,
    scrub,
)

STATE_TITLE_PREFIX = "ops-checks paged: "
MARKER = "infra2-page-dedup"
STATE_VERSION = 1
#: Keys kept in the issue marker (the fingerprint covers all of them).
MAX_STORED_KEYS = 100
#: Keys listed in a report or RESOLVED message; the rest are counted.
MAX_LISTED_KEYS = 10
#: A key is shown (issue body, report, RESOLVED) at most this long; its fingerprint
#: always covers the whole key.
MAX_SHOWN_KEY_CHARS = 200
#: Detail lines shown in a report, each at most this long.
MAX_DETAIL_LINES = 15
MAX_DETAIL_CHARS = 300
#: Issues authored by these (plus the repository owner) count as page state.
TRUSTED_AUTHORS = ("github-actions[bot]",)

PAGE, REPORT, RESOLVED, NONE = "page", "report", "resolved", "none"
#: `unevaluated` for a run that observed nothing: every recorded finding is unproven.
ALL_UNEVALUATED = ("",)
FOUND, ABSENT, CORRUPT, UNREADABLE = "found", "none", "corrupt", "unreadable"

_BLANK = re.compile(r"\s+")
_MARKER = re.compile(r"<!-- " + re.escape(MARKER) + r" (\{.*?\}) -->", re.DOTALL)
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


# --- identity ----------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One confirmed finding: ``key`` is its identity, ``line`` only its display."""

    key: str
    line: str = ""


@dataclass(frozen=True)
class Identity:
    job: str
    #: Sorted, unique, whitespace-normalised keys: what the fingerprint covers.
    keys: tuple[str, ...]
    #: The same keys scrubbed for a public issue, in the same order.
    shown: tuple[str, ...]
    fingerprint: str


def _as_finding(item: Finding | tuple[str, str] | str) -> Finding:
    if isinstance(item, Finding):
        return item
    if isinstance(item, str):
        return Finding(item)
    key, line = item
    return Finding(key, line)


def _clean(text: str) -> str:
    return _BLANK.sub(" ", str(text)).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fingerprint(job: str, keys: Iterable[str]) -> str:
    return hashlib.sha256("\n".join([job, *keys]).encode("utf-8")).hexdigest()


def make_identity(
    job: str,
    findings: Iterable[Finding | tuple[str, str] | str],
    env: Mapping[str, str] | None = None,
) -> Identity:
    """The identity of a job's findings. A finding with no key falls back to its line,
    and then to a placeholder: findings that exist can never produce an empty
    identity, which would read as "all resolved"."""
    keys = sorted(
        {
            _clean(item.key) or _clean(item.line) or "unkeyed finding"
            for item in map(_as_finding, findings)
        }
    )
    shown = tuple(
        _clip(scrub(key, env or {}).replace("`", "'"), MAX_SHOWN_KEY_CHARS)
        for key in keys
    )
    return Identity(job, tuple(keys), shown, fingerprint(job, keys))


def day_number(since: float, now: float) -> int:
    """Which calendar day (owner's zone) of this identity ``now`` is; the day it
    was first paged is day 1. Calendar days, not 24 h blocks: a cron that starts a
    few minutes early must not read as the same day."""
    first = datetime.fromtimestamp(since, tz=DISPLAY_TIMEZONE).date()
    today = datetime.fromtimestamp(now, tz=DISPLAY_TIMEZONE).date()
    return max(1, (today - first).days + 1)


# --- recorded state --------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    fingerprint: str
    keys: tuple[str, ...]
    #: When this exact identity was first paged (epoch seconds).
    since: int
    #: When the job first went red; survives identity changes (RESOLVED duration).
    incident_since: int
    #: How many keys the identity had; more than ``keys`` holds when it was cut.
    total: int = 0


def state_title(job: str) -> str:
    return f"{STATE_TITLE_PREFIX}{job}"


def encode_marker(job: str, state: State, count: int) -> str:
    payload = {
        "v": STATE_VERSION,
        "job": job,
        "fingerprint": state.fingerprint,
        "keys": list(state.keys),
        "count": count,
        "since": state.since,
        "incident_since": state.incident_since,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # `-->` or a tag inside a key must not end the HTML comment.
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- {MARKER} {text} -->"


def parse_marker(body: str, job: str) -> State | None:
    """The state a body records, or None when the marker is absent, torn, from another
    job or does not have the recorded shape."""
    matches = _MARKER.findall(body or "")
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1])
        keys = payload["keys"]
        if (
            payload["v"] != STATE_VERSION
            or payload["job"] != job
            or not _FINGERPRINT.match(payload["fingerprint"])
            or not isinstance(keys, list)
            or not all(isinstance(key, str) for key in keys)
            or type(payload["since"]) is not int
            or type(payload["incident_since"]) is not int
        ):
            return None
        total = payload.get("count")
        return State(
            payload["fingerprint"],
            tuple(keys),
            payload["since"],
            payload["incident_since"],
            total if type(total) is int else len(keys),
        )
    except (ValueError, KeyError, TypeError):
        return None


def encode_cleared_marker(job: str) -> str:
    """The marker of a state issue whose findings were resolved: lookups ignore it."""
    payload = {"v": STATE_VERSION, "job": job, "cleared": True}
    return f"<!-- {MARKER} {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"


def is_cleared(body: str, job: str) -> bool:
    """Was this state resolved? An issue that stayed open after RESOLVED went out
    (the close call failed) says so in its marker, and is not state any more."""
    matches = _MARKER.findall(body or "")
    if not matches:
        return False
    try:
        payload = json.loads(matches[-1])
        return payload["cleared"] is True and payload["job"] == job
    except (ValueError, KeyError, TypeError):
        return False


@dataclass(frozen=True)
class Lookup:
    kind: str
    #: Every trusted open state issue of the job, oldest first.
    numbers: tuple[int, ...] = ()
    state: State | None = None
    detail: str = ""


class StateApi(Protocol):
    def open_issues(self, *, with_body: bool = False) -> list[dict]: ...

    def create(self, title: str, body: str, labels: Iterable[str]) -> int: ...

    def comment(self, number: int, body: str) -> None: ...

    def update_body(self, number: int, body: str) -> None: ...

    def close(self, number: int) -> None: ...


def trusted_authors(env: Mapping[str, str]) -> frozenset[str]:
    owner = (env.get("GITHUB_REPOSITORY") or "").partition("/")[0].strip()
    return frozenset(
        name.lower() for name in (*TRUSTED_AUTHORS, owner) if name and name.strip()
    )


def find_state(api: StateApi, job: str, trusted: frozenset[str]) -> Lookup:
    """Read the job's state issue. Anything but a clean read is UNREADABLE: the caller
    pages, because nothing may be decided from a listing it could not trust."""
    try:
        issues = api.open_issues(with_body=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable state must page, not crash.
        return Lookup(UNREADABLE, detail=f"{type(exc).__name__}: {exc}")
    title = state_title(job)
    named = sorted(
        (issue for issue in issues if issue.get("title") == title),
        key=lambda issue: issue["number"],
    )
    mine = [issue for issue in named if str(issue.get("author", "")).lower() in trusted]
    for issue in named:
        if issue not in mine:
            print(
                f"::warning::page dedup: ignoring #{issue['number']} ({title!r}): "
                f"authored by {issue.get('author') or 'an unknown author'}, not the "
                "Actions bot or the repository owner"
            )
    # A resolved state that could not be closed is no longer state: a relapse of the
    # same identity is a new incident and must page, not read as "still unresolved".
    mine = [
        issue for issue in mine if not is_cleared(str(issue.get("body") or ""), job)
    ]
    if not mine:
        return Lookup(ABSENT)
    numbers = tuple(issue["number"] for issue in mine)
    state = parse_marker(str(mine[0].get("body") or ""), job)
    if state is None:
        return Lookup(CORRUPT, numbers, detail=f"#{numbers[0]} has no readable marker")
    return Lookup(FOUND, numbers, state)


# --- the decision ------------------------------------------------------------------------

_STATE_NOTE = "注:跨运行去重状态读取失败,本次按新故障呼出(宁可多呼,不静默)。"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    day: int = 0
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: Record the identity once the page was delivered.
    write: bool = False
    #: A line appended to the page: the dedup state itself was unusable.
    note: str = ""


def _unproven(lookup: Lookup, unevaluated: Sequence[str]) -> str:
    """Which recorded finding belongs to a check this run did not evaluate, or ''.

    ``unevaluated`` are key prefixes: a skipped section, a quota that could not be
    read, a check that answered "could not tell". Its recorded findings have not
    recovered, they are unknown; ``""`` (all) when nothing was observed.
    """
    if not unevaluated:
        return ""
    state = lookup.state
    if state is None:
        return "the recorded findings are unknown (unreadable state)"
    if state.total > len(state.keys):
        return "the recorded findings were cut short"
    hit = next(
        (key for key in state.keys if any(key.startswith(p) for p in unevaluated)),
        None,
    )
    return "" if hit is None else f"`{hit}` was not evaluated in this run"


def decide(
    identity: Identity,
    lookup: Lookup,
    *,
    mode: str,
    now: float,
    observed_clean: bool = False,
    unevaluated: Sequence[str] = (),
) -> Decision:
    """The whole decision table, pure. ``observed_clean`` says that a run with no
    findings actually proved there are none (a clean job, or a complete report);
    without it an empty finding list is only a transient lookup failure.
    ``unevaluated`` (key prefixes) narrows that: a recorded finding whose check did
    not run in this run is not proven gone."""
    if mode == OFF:
        if identity.keys:
            return Decision(PAGE, "dedup is off for this run: paging as before")
        return Decision(NONE, "dedup is off for this run and nothing is red")
    #: A drill or a branch dispatch may page and report, but what it records is what
    #: the next scheduled run would trust: it records nothing.
    write = mode != OPEN_ONLY
    if not identity.keys:
        if not observed_clean:
            return Decision(
                NONE,
                "no confirmed finding, but the run did not prove there are none: "
                "recorded state kept",
            )
        if lookup.kind in (FOUND, CORRUPT):
            if mode == OPEN_ONLY:
                return Decision(
                    NONE, "open-only run (drill or branch dispatch) never resolves"
                )
            unproven = _unproven(lookup, unevaluated)
            if unproven:
                return Decision(
                    NONE, f"cannot prove the paged findings are gone: {unproven}"
                )
            removed = lookup.state.keys if lookup.state else ()
            return Decision(RESOLVED, "every paged finding is gone", removed=removed)
        return Decision(NONE, "nothing is red and no page is outstanding")
    if lookup.kind == UNREADABLE:
        return Decision(
            PAGE,
            f"state could not be read ({lookup.detail}): paging, nothing recorded",
            note=_STATE_NOTE,
        )
    if lookup.kind == ABSENT:
        return Decision(PAGE, "first appearance", write=write)
    if lookup.kind == CORRUPT:
        return Decision(
            PAGE,
            f"recorded state is unreadable ({lookup.detail}): paging",
            write=write,
            note=_STATE_NOTE,
        )
    state = lookup.state
    assert state is not None  # FOUND always carries its state
    if state.fingerprint == identity.fingerprint:
        return Decision(
            REPORT,
            "same identity as the last page",
            day=day_number(state.since, now),
        )
    recorded, current = set(state.keys), set(identity.shown)
    added = tuple(key for key in identity.shown if key not in recorded)
    removed = tuple(key for key in state.keys if key not in current)
    return Decision(
        PAGE,
        f"identity changed (+{len(added)} -{len(removed)})",
        added=added,
        removed=removed,
        write=write,
    )


# --- messages ----------------------------------------------------------------------------


def run_url_from_env(env: Mapping[str, str]) -> str:
    parts = [
        env.get(key, "")
        for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    ]
    return f"{parts[0]}/{parts[1]}/actions/runs/{parts[2]}" if all(parts) else ""


def _key_lines(keys: Sequence[str]) -> list[str]:
    lines = [f"• {key}" for key in keys[:MAX_LISTED_KEYS]]
    if len(keys) > MAX_LISTED_KEYS:
        lines.append(f"…及另外 {len(keys) - MAX_LISTED_KEYS} 项")
    return lines


def detail_lines(detail: str, findings: Sequence[Finding]) -> list[str]:
    """The current detail of the findings, one clipped line each: the explicit
    ``detail`` text, else the findings' own display lines. Unique, in order."""
    raw = detail.splitlines() if detail.strip() else [f.line for f in findings]
    seen: dict[str, None] = {}
    for line in raw:
        text = _clip(_clean(line), MAX_DETAIL_CHARS)
        if text:
            seen.setdefault(text)
    lines = list(seen)
    if len(lines) > MAX_DETAIL_LINES:
        hidden = len(lines) - MAX_DETAIL_LINES
        lines = [*lines[:MAX_DETAIL_LINES], f"…及另外 {hidden} 行"]
    return lines


def report_text(
    job: str,
    identity: Identity,
    state: State,
    day: int,
    run_url: str,
    detail: Sequence[str] = (),
) -> str:
    """The compact "still unresolved" report for the reports chat.

    The identity excludes readings, so a finding that got worse (older, bigger,
    more of them) does not page. The current ``detail`` lines are here so that it is
    still visible to whoever reads the daily report."""
    lines = [
        f"{REPORT_TITLE_PREFIX}仍未恢复 · 第 {day} 天 · {job}",
        f"{len(identity.keys)} 项结论与上次呼出相同,不再呼人;首次呼出 "
        f"{format_time(state.since)}。",
        *_key_lines(identity.shown),
    ]
    if detail:
        lines += ["今日详情(读数可能已变化):", *detail]
    if run_url:
        lines.append(f"运行:{run_url}")
    return "\n".join(lines)


def resolved_text(
    job: str, label: str, state: State | None, *, now: float, run_url: str
) -> str:
    """The RESOLVED page: what recovered and how long it was broken (§3.1)."""
    keys = state.keys if state else ()
    lines = [f"✅ [已恢复] {label} · 全部已恢复"]
    if keys:
        lines.append(f"已恢复 {len(keys)} 项:")
        lines += _key_lines(keys)
    else:
        lines.append(f"{job}:此前呼出的结论已消失。")
    if state:
        lines.append(f"开始于:{since_text(state.incident_since, now=now, end=now)}")
    if run_url:
        lines.append(f"运行:{run_url}")
    return "\n".join(lines)


def _page_with_note(message: str, decision: Decision) -> str:
    return f"{message}\n{decision.note}" if decision.note else message


# --- recording ------------------------------------------------------------------------------


def state_body(
    job: str, label: str, state: State, identity: Identity, run_url: str
) -> str:
    lines = [
        f"**`{job}` has an outstanding page** ({label}): "
        f"{len(identity.keys)} confirmed finding(s), first paged "
        f"{datetime.fromtimestamp(state.since, tz=DISPLAY_TIMEZONE):%Y-%m-%d %H:%M} (UTC+8).",
        "",
        "Findings (names only, never readings):",
        *(f"- `{key}`" for key in identity.shown[:MAX_STORED_KEYS]),
    ]
    if len(identity.shown) > MAX_STORED_KEYS:
        lines.append(f"- ...and {len(identity.shown) - MAX_STORED_KEYS} more")
    lines += [
        "",
        f"- Paged by: {run_url or '(no run URL)'}",
        "",
        "Page-dedup state (`libs/observability/page_dedup.py`, #962): the job pages the "
        "pager chat only when this set changes (a finding appears or disappears, or "
        "everything resolves) and reports `仍未恢复` to the reports chat while it stays "
        "the same. One open issue per job; the next clean scheduled run closes it. "
        "Do not edit the marker below.",
        "",
        encode_marker(job, state, len(identity.keys)),
    ]
    return "\n".join(lines)


def _record(
    api: StateApi,
    job: str,
    label: str,
    identity: Identity,
    lookup: Lookup,
    decision: Decision,
    *,
    now: float,
    run_url: str,
) -> None:
    """Record the identity that was just paged. Raises on a failed write."""
    incident = now
    if lookup.kind == FOUND and lookup.state is not None:
        incident = lookup.state.incident_since
    state = State(
        identity.fingerprint,
        identity.shown[:MAX_STORED_KEYS],
        int(now),
        int(incident),
        len(identity.keys),
    )
    body = state_body(job, label, state, identity, run_url)
    if lookup.numbers:
        number = lookup.numbers[0]
        api.update_body(number, body)
        api.comment(number, _change_comment(decision, identity, run_url))
        print(f"page dedup: updated #{number} ({state_title(job)})")
    else:
        number = api.create(state_title(job), body, ISSUE_LABELS)
        print(f"page dedup: opened #{number} ({state_title(job)})")


def _change_comment(decision: Decision, identity: Identity, run_url: str) -> str:
    lines = [
        f"Paged again: {decision.reason}; now {len(identity.keys)} finding(s) "
        f"({run_url or 'no run URL'}).",
    ]
    lines += [f"- added: `{key}`" for key in decision.added[:MAX_LISTED_KEYS]]
    lines += [f"- removed: `{key}`" for key in decision.removed[:MAX_LISTED_KEYS]]
    return "\n".join(lines)


def _close(
    api: StateApi, job: str, lookup: Lookup, *, now: float, run_url: str
) -> None:
    """Clear and close every trusted state issue of the job (a duplicate left open
    would read as a live page tomorrow).

    The cleared marker is written first: it is what makes a state issue stop being
    state, so even when the comment or the close call then fails, a relapse of the same
    identity is a new incident that pages, not "still unresolved". If the marker
    itself cannot be written nothing is cleared and the next run resolves again.
    """
    state = lookup.state
    duration = (
        f" after {since_text(state.incident_since, now=now, end=now)}" if state else ""
    )
    body = (
        f"**`{job}` is clear**{duration}: every paged finding is gone in "
        f"{run_url or 'the latest run'}. Closed by the page-dedup state "
        "(`libs/observability/page_dedup.py`, #962). A later red run opens a new issue."
    )
    cleared = f"{body}\n\n{encode_cleared_marker(job)}"
    failures: list[str] = []
    for number in lookup.numbers:
        api.update_body(number, cleared)
        for step, call in (
            ("comment", lambda n=number: api.comment(n, body)),
            ("close", lambda n=number: api.close(n)),
        ):
            try:
                call()
            except Exception as exc:  # noqa: BLE001 - cleared already; keep going.
                failures.append(f"#{number} {step}: {type(exc).__name__}: {exc}")
        print(f"page dedup: cleared #{number} ({state_title(job)})")
    if failures:
        raise RuntimeError("; ".join(failures))


# --- the entry points ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What was done. ``action`` is what was delivered, not what was planned: a
    report that could not be delivered and fell back to a page says ``page``."""

    action: str
    reason: str
    #: 1 when the state could not be written after a good delivery: the
    #: page/report/resolution itself happened, but the next run will repeat it.
    exit_code: int = 0
    day: int = 0


def _api_from_env(env: Mapping[str, str]) -> StateApi | None:
    repository = (env.get("GITHUB_REPOSITORY") or "").strip()
    token = (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    if "/" not in repository or not token:
        return None
    return GitHubIssues(repository, token)


def dedup_page(
    env: Mapping[str, str],
    *,
    job: str,
    findings: Iterable[Finding | tuple[str, str] | str],
    page_message: str,
    deliver_page: Callable[[Mapping[str, str], str], None],
    deliver_report: Callable[[str, Mapping[str, str]], bool] | None = None,
    label: str = "",
    run_url: str | None = None,
    observed_clean: bool = False,
    unevaluated: Sequence[str] = (),
    detail: str = "",
    mode: str | None = None,
    now: float | None = None,
    api: StateApi | None = None,
) -> Outcome:
    """Deliver a job's confirmed findings per the decision table above.

    ``deliver_page(env, text)`` is the job's pager (``deliver_out_of_band_alert``);
    ``deliver_report(text, env) -> bool`` the reports chat (``deliver_infra2_report``:
    False = not configured). ``observed_clean`` is for the call that has no findings:
    True only when the run proved there are none; ``unevaluated`` are the key
    prefixes of checks that did not run, whose recorded findings are then unproven
    (``""`` = nothing was observed). ``detail`` is the current detail text a report
    shows (default: the findings' own lines), since identity excludes readings.
    """
    findings = list(findings)
    now = time.time() if now is None else now
    run_url = run_url_from_env(env) if run_url is None else run_url
    label = label or job
    if env.get(DRY_RUN_ENV) == "1":
        print(f"page dedup [{job}]: dry run, nothing delivered, nothing written")
        if findings:
            print(page_message)
        return Outcome(NONE, "dry run")

    identity: Identity | None = None
    lookup = Lookup(ABSENT)
    try:
        run_mode = issue_trail_mode(env) if mode is None else mode
        identity = make_identity(job, findings, env)
        if run_mode != OFF and (identity.keys or observed_clean):
            api = api or _api_from_env(env)
            lookup = (
                find_state(api, job, trusted_authors(env))
                if api is not None
                else Lookup(UNREADABLE, detail="no GITHUB_TOKEN or GITHUB_REPOSITORY")
            )
        decision = decide(
            identity,
            lookup,
            mode=run_mode,
            now=now,
            observed_clean=observed_clean,
            unevaluated=unevaluated,
        )
    except Exception as exc:  # noqa: BLE001 - a bug here must page, never silence.
        print(
            f"::error::page dedup [{job}]: planning failed: {type(exc).__name__}: {exc}"
        )
        identity = None
        decision = (
            Decision(
                PAGE, f"page dedup failed ({type(exc).__name__}): paging as before"
            )
            if findings
            else Decision(NONE, "page dedup failed and nothing is red")
        )
    print(f"page dedup [{job}]: {decision.action} ({decision.reason})")

    if decision.action == NONE:
        return Outcome(NONE, decision.reason)

    if decision.action == REPORT:
        assert identity is not None and lookup.state is not None
        text = report_text(
            job,
            identity,
            lookup.state,
            decision.day,
            run_url,
            detail_lines(detail, [_as_finding(item) for item in findings]),
        )
        try:
            delivered = (deliver_report or deliver_infra2_report)(text, env)
            failure = "" if delivered else "the reports chat is not configured"
        except Exception as exc:  # noqa: BLE001 - an unreadable report must page.
            failure = f"{type(exc).__name__}: {exc}"
        if not failure:
            return Outcome(REPORT, decision.reason, day=decision.day)
        print(
            f"::warning::page dedup [{job}]: report not delivered ({failure}): paging"
        )
        deliver_page(
            env,
            f"{page_message}\n注:仍未恢复(第 {decision.day} 天),报告群不可用"
            f"({failure}),回落到告警群。",
        )
        return Outcome(PAGE, f"report undeliverable ({failure})", day=decision.day)

    if decision.action == PAGE:
        deliver_page(env, _page_with_note(page_message, decision))
        code = 0
        if decision.write and identity is not None and lookup.kind != UNREADABLE:
            try:
                assert api is not None
                _record(
                    api,
                    job,
                    label,
                    identity,
                    lookup,
                    decision,
                    now=now,
                    run_url=run_url,
                )
            except Exception as exc:  # noqa: BLE001 - the page went out; say loudly.
                code = 1
                print(
                    f"::error::page dedup [{job}]: the page was delivered but its state "
                    f"was not recorded ({type(exc).__name__}: {exc}); the next run pages again"
                )
        return Outcome(PAGE, decision.reason, exit_code=code)

    # RESOLVED
    assert api is not None
    deliver_page(
        env,
        resolved_text(job, label, lookup.state, now=now, run_url=run_url),
    )
    code = 0
    try:
        _close(api, job, lookup, now=now, run_url=run_url)
    except Exception as exc:  # noqa: BLE001 - the page went out; say loudly.
        code = 1
        print(
            f"::error::page dedup [{job}]: RESOLVED was delivered but clearing or "
            f"closing the state issue failed ({type(exc).__name__}: {exc})"
        )
    return Outcome(RESOLVED, decision.reason, exit_code=code)


def resolve_page_state(
    env: Mapping[str, str],
    *,
    job: str,
    deliver_page: Callable[[Mapping[str, str], str], None],
    label: str = "",
    run_url: str | None = None,
    unevaluated: Sequence[str] = (),
    mode: str | None = None,
    now: float | None = None,
    api: StateApi | None = None,
) -> Outcome:
    """A run that proved the job is clean: resolve what was paged, if anything,
    except findings whose check this run did not evaluate (``unevaluated`` prefixes)."""
    return dedup_page(
        env,
        job=job,
        findings=(),
        page_message="",
        deliver_page=deliver_page,
        label=label,
        run_url=run_url,
        observed_clean=True,
        unevaluated=unevaluated,
        mode=mode,
        now=now,
        api=api,
    )


__all__ = [
    "ABSENT",
    "CORRUPT",
    "FOUND",
    "NONE",
    "PAGE",
    "REPORT",
    "RESOLVED",
    "UNREADABLE",
    "Decision",
    "Finding",
    "Identity",
    "Lookup",
    "Outcome",
    "State",
    "StateApi",
    "ALL_UNEVALUATED",
    "dedup_page",
    "day_number",
    "decide",
    "find_state",
    "make_identity",
    "parse_marker",
    "resolve_page_state",
]
