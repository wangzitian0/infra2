"""truealpha#876: the peer check that sees truealpha's scheduler-liveness die.

Every red verdict is proven against a fake GitHub: the bound is enforced, a
non-active workflow is red, an unreadable answer is red, and "never ran" is only
green while the workflow file is younger than the bound.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError

import pytest

from libs import scheduler_peer_liveness as peer

NOW = datetime(2026, 9, 17, 2, 30, tzinfo=UTC)
WORKFLOW = f"/repos/{peer.PEER_REPOSITORY}/actions/workflows/{peer.PEER_WORKFLOW}"
PATH = ".github/workflows/scheduler-liveness.yml"
WORKFLOW_TEXT = """\
name: scheduler-liveness
on:
  schedule:
    # Every 6 hours at an off-minute. Bound for itself: 13 h.
    - cron: "41 */6 * * *"
  workflow_dispatch:
"""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(
    age: timedelta,
    *,
    event="schedule",
    status="completed",
    conclusion="success",
    run_id=1,
):
    return {
        "id": run_id,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": _iso(NOW - age),
    }


class FakeGitHub:
    """Answers the six reads the peer check makes; an Exception value is raised."""

    def __init__(
        self,
        *,
        state="active",
        scheduled=(),
        unfiltered=None,
        changed_age=timedelta(days=3),
        workflow_text=WORKFLOW_TEXT,
        overrides=None,
    ) -> None:
        self.calls: list[str] = []
        content = base64.b64encode(workflow_text.encode()).decode()
        self.routes = {
            WORKFLOW: {"state": state, "path": PATH},
            f"/repos/{peer.PEER_REPOSITORY}": {"default_branch": "main"},
            f"/repos/{peer.PEER_REPOSITORY}/contents/{PATH}?ref=main": {
                "encoding": "base64",
                "content": content,
            },
            f"{WORKFLOW}/runs?event=schedule&per_page={peer.SCHEDULED_PAGE}": {
                "total_count": len(scheduled),
                "workflow_runs": list(scheduled),
            },
            f"{WORKFLOW}/runs?per_page={peer.UNFILTERED_PAGE}": {
                "workflow_runs": list(scheduled if unfiltered is None else unfiltered),
            },
            (
                f"/repos/{peer.PEER_REPOSITORY}/commits?path=.github%2Fworkflows%2F"
                "scheduler-liveness.yml&sha=main&per_page=1"
            ): [{"commit": {"committer": {"date": _iso(NOW - changed_age)}}}],
        }
        self.routes.update(overrides or {})

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError(f"unexpected read {path}")
        answer = self.routes[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _verdict(github: FakeGitHub, **kwargs) -> peer.PeerVerdict:
    return peer.evaluate(github, now=NOW, **kwargs)


# --- the bound ------------------------------------------------------------------


def test_the_bound_is_measured_from_the_workflow_crons() -> None:
    assert peer.bound_for(peer.largest_gap(peer.crons_in(WORKFLOW_TEXT))) == timedelta(
        hours=13
    )
    assert peer.largest_gap(["17 2 * * *"]) == timedelta(days=1)
    assert peer.largest_gap(["47 * * * *"]) == timedelta(hours=1)
    assert peer.largest_gap(["0 1,7 * * *"]) == timedelta(hours=18)
    assert peer.largest_gap(["0 0-12/6 * * *", "30 20 * * *"]) == timedelta(
        hours=8, minutes=30
    )
    assert peer.crons_in("on:\n  schedule:\n    - cron: '5 4 * * *'  # nightly\n") == [
        "5 4 * * *"
    ]


@pytest.mark.parametrize(
    "crons",
    [
        [],
        ["41 */6 * * 1"],
        ["41 */6 * *"],
        ["61 * * * *"],
        ["x * * * *"],
        ["0 */0 * * *"],
        ["0 5-3 * * *"],
    ],
)
def test_a_schedule_it_cannot_measure_is_rejected(crons) -> None:
    with pytest.raises(peer.ScheduleError):
        peer.largest_gap(crons)


def test_a_fresh_scheduled_tick_is_green() -> None:
    github = FakeGitHub(scheduled=[_run(timedelta(hours=5))])
    verdict = _verdict(github)
    assert verdict.ok, verdict
    assert "5.0h old (bound 13.0h)" in verdict.detail
    # One witness is enough when it is inside the bound.
    assert not any("runs?per_page" in call for call in github.calls)


@pytest.mark.parametrize(
    ("age", "status"),
    [
        (timedelta(hours=12, minutes=59), peer.OK),
        (timedelta(hours=13, minutes=1), peer.STALE),
    ],
)
def test_the_bound_is_enforced(age, status) -> None:
    verdict = _verdict(FakeGitHub(scheduled=[_run(age)]))
    assert verdict.status == status, verdict


def test_a_stale_filtered_listing_is_rescued_by_a_fresh_unfiltered_witness() -> None:
    # 2026-09-16: ?event=schedule answered twelve days old while the unfiltered
    # listing showed an hour-old scheduled run.
    github = FakeGitHub(
        scheduled=[_run(timedelta(days=12))],
        unfiltered=[
            _run(timedelta(minutes=30), event="workflow_dispatch", run_id=3),
            _run(timedelta(hours=1), run_id=2),
        ],
    )
    verdict = _verdict(github)
    assert verdict.ok, verdict
    assert "1.0h old" in verdict.detail


def test_both_witnesses_stale_is_red_and_a_dispatch_is_not_a_tick() -> None:
    github = FakeGitHub(
        scheduled=[_run(timedelta(days=2))],
        unfiltered=[_run(timedelta(minutes=5), event="workflow_dispatch", run_id=9)],
    )
    verdict = _verdict(github)
    assert verdict.status == peer.STALE
    assert "beyond the 13.0h bound" in verdict.detail


@pytest.mark.parametrize(
    "run",
    [
        _run(timedelta(hours=1), conclusion="startup_failure"),
        _run(timedelta(hours=1), status="queued", conclusion=None),
        _run(timedelta(hours=1), status="waiting", conclusion=None),
    ],
)
def test_a_run_that_started_no_job_is_not_a_tick(run) -> None:
    verdict = _verdict(FakeGitHub(scheduled=[run, _run(timedelta(days=1), run_id=2)]))
    assert verdict.status == peer.STALE, verdict


def test_the_newest_tick_is_the_maximum_not_the_first_listed() -> None:
    runs = [_run(timedelta(days=1), run_id=1), _run(timedelta(hours=2), run_id=2)]
    assert _verdict(FakeGitHub(scheduled=runs)).ok


# --- never ran --------------------------------------------------------------------


def test_never_ran_is_green_only_while_the_file_is_younger_than_the_bound() -> None:
    young = _verdict(FakeGitHub(changed_age=timedelta(hours=4)))
    assert young.ok, young
    assert "waiting for its first tick" in young.detail
    old = _verdict(FakeGitHub(changed_age=timedelta(hours=14)))
    assert old.status == peer.NEVER, old


# --- not active -----------------------------------------------------------------


@pytest.mark.parametrize("state", ["disabled_inactivity", "disabled_manually", None])
def test_a_workflow_that_is_not_active_is_red(state) -> None:
    github = FakeGitHub(state=state, scheduled=[_run(timedelta(minutes=10))])
    verdict = _verdict(github)
    assert verdict.status == peer.DISABLED
    assert repr(state) in verdict.detail


# --- unreadable is red ----------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        WORKFLOW,
        f"/repos/{peer.PEER_REPOSITORY}",
        f"/repos/{peer.PEER_REPOSITORY}/contents/{PATH}?ref=main",
        f"{WORKFLOW}/runs?event=schedule&per_page={peer.SCHEDULED_PAGE}",
    ],
)
def test_a_failed_read_is_red(route) -> None:
    github = FakeGitHub(
        scheduled=[_run(timedelta(hours=1))],
        overrides={route: peer.PeerReadError(f"GET {route} answered HTTP 403")},
    )
    verdict = _verdict(github)
    assert verdict.status == peer.UNVERIFIABLE
    assert "HTTP 403" in verdict.detail


def test_a_failed_second_witness_or_commit_read_is_red() -> None:
    stale = FakeGitHub(
        scheduled=[_run(timedelta(days=2))],
        overrides={
            f"{WORKFLOW}/runs?per_page={peer.UNFILTERED_PAGE}": peer.PeerReadError(
                "timeout"
            )
        },
    )
    assert _verdict(stale).status == peer.UNVERIFIABLE
    never = FakeGitHub(
        overrides={
            key: peer.PeerReadError("rate limited")
            for key in FakeGitHub().routes
            if "/commits?" in key
        }
    )
    assert _verdict(never).status == peer.UNVERIFIABLE


@pytest.mark.parametrize(
    ("route", "answer"),
    [
        (WORKFLOW, ["not", "an", "object"]),
        (WORKFLOW, {"state": "active"}),
        (f"/repos/{peer.PEER_REPOSITORY}", {}),
        (
            f"{WORKFLOW}/runs?event=schedule&per_page={peer.SCHEDULED_PAGE}",
            {"workflow_runs": None},
        ),
        (
            f"{WORKFLOW}/runs?event=schedule&per_page={peer.SCHEDULED_PAGE}",
            {
                "workflow_runs": [
                    {
                        "event": "schedule",
                        "status": "completed",
                        "created_at": "yesterday",
                    }
                ]
            },
        ),
        (
            f"/repos/{peer.PEER_REPOSITORY}/contents/{PATH}?ref=main",
            {"encoding": "none", "content": ""},
        ),
    ],
)
def test_an_unusable_answer_is_red(route, answer) -> None:
    verdict = _verdict(FakeGitHub(overrides={route: answer}))
    assert verdict.status == peer.UNVERIFIABLE, verdict


def test_a_schedule_it_cannot_measure_is_red() -> None:
    verdict = _verdict(
        FakeGitHub(workflow_text="on:\n  schedule:\n    - cron: '0 3 * * MON'\n")
    )
    assert verdict.status == peer.INVALID
    assert "teach" in verdict.detail


# --- the drill cap -----------------------------------------------------------------


def test_a_zero_cap_forces_red_and_a_cap_only_tightens() -> None:
    fresh = [_run(timedelta(minutes=20))]
    assert (
        _verdict(FakeGitHub(scheduled=fresh), bound_cap=timedelta(0)).status
        == peer.STALE
    )
    assert (
        _verdict(
            FakeGitHub(changed_age=timedelta(minutes=5)), bound_cap=timedelta(0)
        ).status
        == peer.NEVER
    )
    old = [_run(timedelta(hours=20))]
    assert (
        _verdict(FakeGitHub(scheduled=old), bound_cap=timedelta(hours=100)).status
        == peer.STALE
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        (None, None),
        ("  ", None),
        ("0", timedelta(0)),
        ("2.5", timedelta(hours=2.5)),
    ],
)
def test_the_cap_parses(raw, expected) -> None:
    assert peer.parse_bound_cap_hours(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "abc", "inf", "nan", "1h"])
def test_a_bad_cap_is_rejected(raw) -> None:
    with pytest.raises(ValueError):
        peer.parse_bound_cap_hours(raw)


# --- the real reader ----------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_the_reader_sends_the_token_only_when_there_is_one() -> None:
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        return _Response(json.dumps({"state": "active"}).encode())

    assert peer.github_getter("t0ken", timeout=3, opener=opener)("/x") == {
        "state": "active"
    }
    assert peer.github_getter("", opener=opener)("/y") == {"state": "active"}
    assert seen == [
        ("https://api.github.com/x", "Bearer t0ken", 3),
        ("https://api.github.com/y", None, peer.API_TIMEOUT_SECONDS),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("https://api.github.com/x", 401, "Unauthorized", {}, None),
        URLError("dns"),
        TimeoutError("slow"),
    ],
)
def test_the_reader_turns_every_transport_failure_into_a_read_error(failure) -> None:
    def opener(_request, timeout):
        raise failure

    with pytest.raises(peer.PeerReadError):
        peer.github_getter("t", opener=opener)("/x")


def test_the_reader_rejects_a_body_that_is_not_json() -> None:
    with pytest.raises(peer.PeerReadError):
        peer.github_getter("t", opener=lambda _r, timeout: _Response(b"<html>"))("/x")
