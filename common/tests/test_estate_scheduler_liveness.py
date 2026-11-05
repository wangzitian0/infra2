"""#1432: the estate scheduler-liveness check that replaced truealpha's workflow.

Every red verdict is proven against a fake GitHub: discovery comes from the
workflow listing, the bound comes from each workflow's own crons (the real cron
parser runs), and an unreadable answer is red, never green.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import pytest

from common.observability import estate_scheduler_liveness as est

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
SIX_HOURLY = (
    "on:\n  schedule:\n    - cron: '41 */6 * * *'\n  workflow_dispatch:\n"  # bound 13 h
)
DAILY = "on:\n  schedule:\n    - cron: '17 2 * * *'\n"  # bound 49 h
NOT_SCHEDULED = "on:\n  push:\n  workflow_dispatch:\n"


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


def path_of(name: str) -> str:
    return est.WORKFLOW_DIR + name + ".yml"


def wf(
    name,
    text=SIX_HOURLY,
    *,
    state="active",
    age=timedelta(hours=1),
    unfiltered=None,
    changed_age=timedelta(days=3),
    wid=None,
):
    scheduled = [] if age is None else [_run(age)]
    return {
        "id": wid or sum(ord(c) * 31**i for i, c in enumerate(name)) % 10**9,
        "path": path_of(name),
        "state": state,
        "text": text,
        "scheduled": scheduled,
        "unfiltered": scheduled if unfiltered is None else unfiltered,
        "changed_age": changed_age,
    }


class FakeGitHub:
    """Answers the reads the check makes; an Exception value is raised."""

    def __init__(self, estate, overrides=None, listing_extra=()) -> None:
        self.calls: list[str] = []
        self.routes: dict[str, object] = {}
        for repo, workflows in estate.items():
            listing = [
                {"id": w["id"], "path": w["path"], "state": w["state"]}
                for w in workflows
            ]
            listing.extend(listing_extra)
            self.routes[
                f"/repos/{repo}/actions/workflows?per_page={est.LISTING_PAGE}"
            ] = {
                "total_count": len(listing),
                "workflows": listing,
            }
            self.routes[f"/repos/{repo}"] = {"default_branch": "main"}
            for w in workflows:
                base = f"/repos/{repo}/actions/workflows/{w['id']}"
                content = base64.b64encode(w["text"].encode()).decode()
                self.routes[f"/repos/{repo}/contents/{quote(w['path'])}?ref=main"] = {
                    "encoding": "base64",
                    "content": content,
                }
                self.routes[
                    f"{base}/runs?event=schedule&per_page={est.SCHEDULED_PAGE}"
                ] = {
                    "total_count": len(w["scheduled"]),
                    "workflow_runs": list(w["scheduled"]),
                }
                self.routes[f"{base}/runs?per_page={est.UNFILTERED_PAGE}"] = {
                    "workflow_runs": list(w["unfiltered"]),
                }
                self.routes[
                    f"/repos/{repo}/commits?path={quote(w['path'], safe='')}&sha=main&per_page=1"
                ] = [{"commit": {"committer": {"date": _iso(NOW - w["changed_age"])}}}]
        self.routes.update(overrides or {})

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError(f"unexpected read {path}")
        answer = self.routes[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


REPO_A, REPO_B = "o/a", "o/b"


def _verdicts(github, *, repositories=(REPO_A, REPO_B), **kwargs):
    return est.evaluate_workflows(github, now=NOW, repositories=repositories, **kwargs)


def _by_path(verdicts):
    return {(v.repository, v.path.rsplit("/", 1)[-1]): v for v in verdicts}


# --- discovery and the healthy case ---------------------------------------------------


def test_the_estate_names_every_repository_the_deleted_workflow_covered() -> None:
    assert set(est.ESTATE_REPOSITORIES) == {
        "wangzitian0/truealpha",
        "wangzitian0/infra2",
        "wangzitian0/finance_report",
        "wangzitian0/infra2-sdk",
    }


def test_a_healthy_estate_is_green_and_only_scheduled_workflows_are_checked() -> None:
    estate = {
        REPO_A: [wf("tick"), wf("ci", NOT_SCHEDULED, age=None)],
        REPO_B: [wf("nightly", DAILY, age=timedelta(hours=30))],
    }
    github = FakeGitHub(
        estate,
        listing_extra=[{"id": 7, "path": "dynamic/agents/copilot", "state": "active"}],
    )
    verdicts = _verdicts(github)
    assert [v.status for v in verdicts] == [est.OK, est.OK], verdicts
    assert set(_by_path(verdicts)) == {(REPO_A, "tick.yml"), (REPO_B, "nightly.yml")}
    aggregate = est.evaluate_estate_liveness(
        github, now=NOW, repositories=(REPO_A, REPO_B)
    )
    assert (
        aggregate.ok and "2 scheduled workflows in 2 repositories" in aggregate.detail
    )


def test_a_new_scheduled_workflow_is_covered_with_no_code_change() -> None:
    estate = {
        REPO_A: [wf("tick"), wf("brand-new", age=timedelta(days=5))],
        REPO_B: [wf("n")],
    }
    verdicts = _by_path(_verdicts(FakeGitHub(estate)))
    assert verdicts[(REPO_A, "brand-new.yml")].status == est.STALE
    assert verdicts[(REPO_A, "tick.yml")].status == est.OK


# --- the bound is measured from each workflow's own cron -------------------------------


@pytest.mark.parametrize(
    ("text", "age", "status"),
    [
        (SIX_HOURLY, timedelta(hours=12, minutes=59), est.OK),
        (SIX_HOURLY, timedelta(hours=13, minutes=1), est.STALE),
        (DAILY, timedelta(hours=48, minutes=59), est.OK),
        (DAILY, timedelta(hours=49, minutes=1), est.STALE),
    ],
)
def test_the_bound_follows_the_workflow_cron(text, age, status) -> None:
    github = FakeGitHub({REPO_A: [wf("w", text, age=age)]})
    (verdict,) = _verdicts(github, repositories=(REPO_A,))
    assert verdict.status == status, verdict


def test_the_largest_gap_of_several_crons_sets_the_bound() -> None:
    text = "on:\n  schedule:\n    - cron: '0 1,7 * * *'\n"  # gap 18 h, bound 37 h
    ok = FakeGitHub({REPO_A: [wf("w", text, age=timedelta(hours=36))]})
    assert _verdicts(ok, repositories=(REPO_A,))[0].status == est.OK
    stale = FakeGitHub({REPO_A: [wf("w", text, age=timedelta(hours=38))]})
    assert _verdicts(stale, repositories=(REPO_A,))[0].status == est.STALE


def test_a_stale_run_is_red_and_names_the_workflow() -> None:
    github = FakeGitHub(
        {REPO_A: [wf("tick", age=timedelta(days=2))], REPO_B: [wf("ok")]}
    )
    verdicts = _by_path(_verdicts(github))
    stale = verdicts[(REPO_A, "tick.yml")]
    assert stale.status == est.STALE
    assert "beyond the 13.0h bound" in stale.detail
    assert verdicts[(REPO_B, "ok.yml")].status == est.OK


def test_a_stale_filtered_listing_is_rescued_by_a_fresh_unfiltered_witness() -> None:
    # 2026-09-16: ?event=schedule answered twelve days old while the unfiltered
    # listing showed an hour-old scheduled run.
    w = wf(
        "tick", age=timedelta(days=12), unfiltered=[_run(timedelta(hours=1), run_id=2)]
    )
    (verdict,) = _verdicts(FakeGitHub({REPO_A: [w]}), repositories=(REPO_A,))
    assert verdict.status == est.OK and "1.0h old" in verdict.detail


def test_a_dispatch_is_not_a_tick() -> None:
    w = wf(
        "tick",
        age=timedelta(days=2),
        unfiltered=[_run(timedelta(minutes=5), event="workflow_dispatch", run_id=9)],
    )
    (verdict,) = _verdicts(FakeGitHub({REPO_A: [w]}), repositories=(REPO_A,))
    assert verdict.status == est.STALE


@pytest.mark.parametrize(
    "run",
    [
        _run(timedelta(hours=1), conclusion="startup_failure"),
        _run(timedelta(hours=1), status="queued", conclusion=None),
    ],
)
def test_a_run_that_started_no_job_is_not_a_tick(run) -> None:
    w = wf("tick", age=None)
    w["scheduled"] = w["unfiltered"] = [run]
    (verdict,) = _verdicts(FakeGitHub({REPO_A: [w]}), repositories=(REPO_A,))
    assert verdict.status == est.NEVER, verdict  # the file is 3 days old


def test_never_ran_is_green_only_while_the_file_is_younger_than_the_bound() -> None:
    young = wf("w", age=None, changed_age=timedelta(hours=4))
    old = wf("v", age=None, changed_age=timedelta(hours=14))
    verdicts = _verdicts(FakeGitHub({REPO_A: [young, old]}), repositories=(REPO_A,))
    assert (
        verdicts[0].status == est.OK
        and "waiting for its first tick" in verdicts[0].detail
    )
    assert verdicts[1].status == est.NEVER


# --- disabled and deleted -------------------------------------------------------------


@pytest.mark.parametrize(
    "state", ["disabled_inactivity", "disabled_manually", "deleted", None]
)
def test_a_scheduled_workflow_that_is_not_active_is_red(state) -> None:
    w = wf("tick", state=state, age=timedelta(minutes=10))
    (verdict,) = _verdicts(FakeGitHub({REPO_A: [w]}), repositories=(REPO_A,))
    assert verdict.status == est.DISABLED
    assert repr(state) in verdict.detail


def test_a_disabled_workflow_without_a_schedule_is_not_red() -> None:
    w = wf("manual", NOT_SCHEDULED, state="disabled_manually", age=None)
    github = FakeGitHub({REPO_A: [w, wf("tick")]})
    assert [v.path for v in _verdicts(github, repositories=(REPO_A,))] == [
        path_of("tick")
    ]


def _gone(w, repo=REPO_A, error="GET answered HTTP 404"):
    route = f"/repos/{repo}/contents/{quote(w['path'])}?ref=main"
    return {route: est.PeerReadError(error)}


def test_a_listed_workflow_whose_file_left_the_default_branch_after_ticking_is_red() -> (
    None
):
    w = wf("gone")
    github = FakeGitHub({REPO_A: [w, wf("tick")]}, overrides=_gone(w))
    verdicts = _by_path(_verdicts(github, repositories=(REPO_A,)))
    assert verdicts[(REPO_A, "gone.yml")].status == est.DISABLED
    assert "deleted from main" in verdicts[(REPO_A, "gone.yml")].detail
    assert verdicts[(REPO_A, "tick.yml")].status == est.OK


def test_a_deleted_listing_entry_is_red_even_when_its_file_is_gone() -> None:
    w = wf("tick", state="deleted", age=None)
    github = FakeGitHub({REPO_A: [w]}, overrides=_gone(w))
    (verdict,) = _verdicts(github, repositories=(REPO_A,))
    assert verdict.status == est.DISABLED and "'deleted'" in verdict.detail


def test_a_branch_only_workflow_that_never_ran_on_schedule_is_skipped() -> None:
    w = wf("probe", age=None)
    github = FakeGitHub({REPO_A: [w, wf("tick")]}, overrides=_gone(w))
    assert [v.path for v in _verdicts(github, repositories=(REPO_A,))] == [
        path_of("tick")
    ]


def test_a_contents_read_failure_other_than_not_found_is_red() -> None:
    w = wf("tick")
    github = FakeGitHub(
        {REPO_A: [w]}, overrides=_gone(w, error="GET answered HTTP 403")
    )
    (verdict,) = _verdicts(github, repositories=(REPO_A,))
    assert verdict.status == est.UNVERIFIABLE


def test_the_real_reader_reports_a_missing_file_in_the_form_the_check_reads() -> None:
    def opener(_request, timeout):
        raise HTTPError("https://api.github.com/x", 404, "Not Found", {}, None)

    with pytest.raises(est.PeerReadError, match="HTTP 404"):
        est.github_getter("t", opener=opener)("/x")


# --- unreadable is red ------------------------------------------------------------------


def test_a_failed_listing_is_red_for_that_repository_only() -> None:
    estate = {REPO_A: [wf("tick")], REPO_B: [wf("ok")]}
    route = f"/repos/{REPO_A}/actions/workflows?per_page={est.LISTING_PAGE}"
    github = FakeGitHub(
        estate, overrides={route: est.PeerReadError("GET answered HTTP 403")}
    )
    verdicts = _verdicts(github)
    assert [v.status for v in verdicts] == [est.UNVERIFIABLE, est.OK]
    assert verdicts[0].repository == REPO_A and "HTTP 403" in verdicts[0].detail


@pytest.mark.parametrize(
    "kind", ["repo", "contents", "scheduled", "unfiltered", "commits"]
)
def test_a_failed_read_is_red(kind) -> None:
    w = wf(
        "tick",
        age=timedelta(days=2)
        if kind == "unfiltered"
        else None
        if kind == "commits"
        else timedelta(hours=1),
    )
    github = FakeGitHub({REPO_A: [w]})
    base = f"/repos/{REPO_A}/actions/workflows/{w['id']}"
    route = {
        "repo": f"/repos/{REPO_A}",
        "contents": f"/repos/{REPO_A}/contents/{quote(w['path'])}?ref=main",
        "scheduled": f"{base}/runs?event=schedule&per_page={est.SCHEDULED_PAGE}",
        "unfiltered": f"{base}/runs?per_page={est.UNFILTERED_PAGE}",
        "commits": next(k for k in github.routes if "/commits?" in k),
    }[kind]
    github.routes[route] = est.PeerReadError("GET answered HTTP 403")
    (verdict,) = _verdicts(github, repositories=(REPO_A,))
    assert verdict.status == est.UNVERIFIABLE, verdict
    assert "HTTP 403" in verdict.detail


def test_a_truncated_listing_is_red() -> None:
    github = FakeGitHub({REPO_A: [wf("tick")]})
    route = f"/repos/{REPO_A}/actions/workflows?per_page={est.LISTING_PAGE}"
    github.routes[route]["total_count"] = 250
    verdicts = _verdicts(github, repositories=(REPO_A,))
    assert est.UNVERIFIABLE in {v.status for v in verdicts}


@pytest.mark.parametrize(
    "answer",
    [["not", "an", "object"], {"workflows": None}, {"total_count": 1}],
)
def test_an_unusable_listing_is_red(answer) -> None:
    route = f"/repos/{REPO_A}/actions/workflows?per_page={est.LISTING_PAGE}"
    github = FakeGitHub({REPO_A: [wf("tick")]}, overrides={route: answer})
    (verdict,) = _verdicts(github, repositories=(REPO_A,))
    assert verdict.status == est.UNVERIFIABLE


def test_an_estate_with_no_scheduled_workflow_is_red_not_green() -> None:
    github = FakeGitHub({REPO_A: [wf("ci", NOT_SCHEDULED, age=None)]})
    aggregate = est.evaluate_estate_liveness(github, now=NOW, repositories=(REPO_A,))
    assert (
        aggregate.status == est.UNVERIFIABLE
        and "no scheduled workflow" in aggregate.detail
    )


@pytest.mark.parametrize(
    ("crons", "gap"),
    [
        (["41 */6 * * *"], timedelta(hours=6)),
        (["17 2 * * *"], timedelta(days=1)),
        (["47 * * * *"], timedelta(hours=1)),
        (["0 1,7 * * *"], timedelta(hours=18)),
        (["0 0-12/6 * * *", "30 20 * * *"], timedelta(hours=8, minutes=30)),
        (["0 1 * * 1"], timedelta(days=7)),
        (["23 4 * * 0"], timedelta(days=7)),
        (["0 3 * * MON"], timedelta(days=7)),
        (["0 9 * * 1-5"], timedelta(days=3)),
        (["0 0 1 * *"], timedelta(days=31)),
    ],
)
def test_the_gap_is_measured_from_the_crons(crons, gap) -> None:
    assert est.largest_gap(crons) == gap


@pytest.mark.parametrize(
    "crons",
    [
        [],
        ["41 */6 * *"],
        ["61 * * * *"],
        ["x * * * *"],
        ["0 */0 * * *"],
        ["0 5-3 * * *"],
        ["0 0 1 1 *"],
    ],
)
def test_a_schedule_it_cannot_measure_is_rejected(crons) -> None:
    with pytest.raises(est.ScheduleError):
        est.largest_gap(crons)


def test_a_weekly_workflow_gets_a_weekly_bound() -> None:
    weekly = "on:\n  schedule:\n    - cron: '0 1 * * 1'\n"  # bound 7 d x 2 + 1 h
    ok = FakeGitHub({REPO_A: [wf("w", weekly, age=timedelta(days=14))]})
    assert _verdicts(ok, repositories=(REPO_A,))[0].status == est.OK
    stale = FakeGitHub({REPO_A: [wf("w", weekly, age=timedelta(days=15))]})
    assert _verdicts(stale, repositories=(REPO_A,))[0].status == est.STALE


def test_a_schedule_it_cannot_measure_is_red() -> None:
    w = wf("tick", "on:\n  schedule:\n    - cron: '0 0 1 1 *'\n")
    (verdict,) = _verdicts(FakeGitHub({REPO_A: [w]}), repositories=(REPO_A,))
    assert verdict.status == est.INVALID


# --- the aggregate and the drill cap ---------------------------------------------------------


def test_the_aggregate_lists_every_red_workflow_and_picks_a_non_retryable_status() -> (
    None
):
    estate = {
        REPO_A: [
            wf("tick", age=timedelta(days=2)),
            wf("off", state="disabled_inactivity"),
        ],
        REPO_B: [wf("ok")],
    }
    aggregate = est.evaluate_estate_liveness(
        FakeGitHub(estate), now=NOW, repositories=(REPO_A, REPO_B)
    )
    assert aggregate.status == est.DISABLED
    assert f"{REPO_A} {path_of('tick')} STALE" in aggregate.detail
    assert f"{REPO_A} {path_of('off')} DISABLED" in aggregate.detail
    assert "ok.yml" not in aggregate.detail


def test_the_aggregate_is_unverifiable_only_when_nothing_else_is_red() -> None:
    route = f"/repos/{REPO_B}/actions/workflows?per_page={est.LISTING_PAGE}"
    github = FakeGitHub(
        {REPO_A: [wf("tick")], REPO_B: [wf("ok")]},
        overrides={route: est.PeerReadError("HTTP 403")},
    )
    aggregate = est.evaluate_estate_liveness(
        github, now=NOW, repositories=(REPO_A, REPO_B)
    )
    assert aggregate.status == est.UNVERIFIABLE


def test_a_zero_cap_forces_red_and_a_cap_only_tightens() -> None:
    fresh = FakeGitHub({REPO_A: [wf("w", age=timedelta(minutes=20))]})
    assert (
        _verdicts(fresh, repositories=(REPO_A,), bound_cap=timedelta(0))[0].status
        == est.STALE
    )
    old = FakeGitHub({REPO_A: [wf("w", age=timedelta(hours=20))]})
    assert (
        _verdicts(old, repositories=(REPO_A,), bound_cap=timedelta(hours=100))[0].status
        == est.STALE
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
    assert est.parse_bound_cap_hours(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "abc", "inf", "nan", "1h"])
def test_a_bad_cap_is_rejected(raw) -> None:
    with pytest.raises(ValueError):
        est.parse_bound_cap_hours(raw)


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

    assert est.github_getter("t0ken", timeout=3, opener=opener)("/x") == {
        "state": "active"
    }
    assert est.github_getter("", opener=opener)("/y") == {"state": "active"}
    assert seen == [
        ("https://api.github.com/x", "Bearer t0ken", 3),
        ("https://api.github.com/y", None, est.API_TIMEOUT_SECONDS),
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

    with pytest.raises(est.PeerReadError):
        est.github_getter("t", opener=opener)("/x")


def test_the_reader_rejects_a_body_that_is_not_json() -> None:
    with pytest.raises(est.PeerReadError):
        est.github_getter("t", opener=lambda _r, timeout: _Response(b"<html>"))("/x")
