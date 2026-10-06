"""libs/observability/scheduled_job_start: scheduled jobs that never started (#1101)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from libs.observability import scheduled_job_start as sjs

NOW = datetime(2026, 10, 7, 2, 17, tzinfo=UTC)

#: Run 37426882755 and its canary job as the GitHub API returned them (#1086).
NEVER_STARTED_RUN = {
    "id": 37426882755,
    "event": "schedule",
    "status": "completed",
    "conclusion": "failure",
    "created_at": "2026-10-06T06:59:09Z",
}
NEVER_STARTED_JOB = {
    "name": "deploy_v2 live canary",
    "status": "completed",
    "conclusion": "failure",
    "runner_name": None,
    "steps": [],
    "started_at": "2026-10-06T06:59:11Z",
    "completed_at": "2026-10-06T06:59:10Z",
}
STARTED_AND_FAILED_JOB = {
    "name": "Secrets reconcile + capacity",
    "status": "completed",
    "conclusion": "failure",
    "runner_name": "GitHub Actions 1000175311",
    "steps": [{"name": "Set up job", "conclusion": "success"}],
}
SKIPPED_JOB = {
    "name": "Audit deploy dependency graph",
    "status": "completed",
    "conclusion": "skipped",
    "runner_name": None,
    "steps": [],
}


class FakeGitHub:
    def __init__(self, runs, jobs_by_run, *, fail=False):
        self.runs = list(runs)
        self.jobs_by_run = jobs_by_run
        self.fail = fail
        self.paths: list[str] = []

    def __call__(self, path: str):
        self.paths.append(path)
        if self.fail:
            raise RuntimeError("GET answered HTTP 502")
        if "/actions/workflows/" in path:
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * sjs.RUNS_PAGE
            return {"workflow_runs": self.runs[start : start + sjs.RUNS_PAGE]}
        run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
        return {"jobs": self.jobs_by_run[run_id]}


def _run(run_id: int, conclusion: str) -> dict:
    return {
        "id": run_id,
        "event": "schedule",
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-10-06T07:00:00Z",
    }


def test_the_1086_canary_job_is_a_finding() -> None:
    gh = FakeGitHub(
        [NEVER_STARTED_RUN],
        {37426882755: [SKIPPED_JOB, NEVER_STARTED_JOB]},
    )

    verdict = sjs.evaluate(gh, now=NOW)

    assert verdict.status == sjs.NEVER_STARTED and not verdict.ok
    assert (
        "deploy_v2 live canary (run 37426882755, 2026-10-06T06:59:09Z)"
        in verdict.detail
    )
    assert "Audit deploy dependency graph" not in verdict.detail


def test_a_job_that_started_and_failed_is_not_a_finding() -> None:
    """It ran its own alert step; paging it again here would double the page."""
    gh = FakeGitHub([_run(1, "failure")], {1: [STARTED_AND_FAILED_JOB, SKIPPED_JOB]})

    verdict = sjs.evaluate(gh, now=NOW)

    assert verdict.ok, verdict.detail


def test_a_successful_run_is_not_read_job_by_job() -> None:
    gh = FakeGitHub(
        [_run(1, "success"), _run(2, "failure")], {2: [STARTED_AND_FAILED_JOB]}
    )

    assert sjs.evaluate(gh, now=NOW).ok
    assert not any("/actions/runs/1/" in path for path in gh.paths)
    assert any("/actions/runs/2/jobs" in path for path in gh.paths)


def test_an_unreadable_answer_is_unverifiable_never_a_pass() -> None:
    verdict = sjs.evaluate(FakeGitHub([], {}, fail=True), now=NOW)

    assert verdict.status == sjs.UNVERIFIABLE and not verdict.ok
    assert "HTTP 502" in verdict.detail


def test_an_answer_without_the_expected_list_is_unverifiable() -> None:
    verdict = sjs.evaluate(lambda _path: {"message": "Not Found"}, now=NOW)

    assert verdict.status == sjs.UNVERIFIABLE


def test_the_listing_asks_for_scheduled_runs_in_the_window_and_pages() -> None:
    runs = [_run(i, "success") for i in range(sjs.RUNS_PAGE)] + [NEVER_STARTED_RUN]
    gh = FakeGitHub(runs, {37426882755: [NEVER_STARTED_JOB]})

    verdict = sjs.evaluate(gh, now=NOW)

    assert verdict.status == sjs.NEVER_STARTED
    listing = [path for path in gh.paths if "/actions/workflows/" in path]
    assert len(listing) == 2 and listing[1].endswith("&page=2")
    since = (NOW - timedelta(hours=26)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert all(
        "event=schedule" in path and f"created=%3E%3D{since}" in path
        for path in listing
    )
    assert all("/actions/workflows/ops-checks.yml/runs" in path for path in listing)


def test_more_runs_than_the_page_limit_is_unverifiable() -> None:
    runs = [_run(i, "success") for i in range(sjs.RUNS_PAGE * sjs.MAX_PAGES)]

    verdict = sjs.evaluate(FakeGitHub(runs, {}), now=NOW)

    assert verdict.status == sjs.UNVERIFIABLE


def test_the_detail_names_a_few_jobs_and_counts_the_rest() -> None:
    runs = [dict(NEVER_STARTED_RUN, id=i) for i in range(1, 8)]
    gh = FakeGitHub(runs, {i: [NEVER_STARTED_JOB] for i in range(1, 8)})

    verdict = sjs.evaluate(gh, now=NOW)

    assert verdict.detail.startswith("7 scheduled job(s)")
    assert "(+2 more)" in verdict.detail


def test_units_are_the_job_names_without_run_ids_or_times() -> None:
    """Two days with the same job failing to start keep one identity; a new job is a
    new identity (page-dedup, #962)."""
    day_one = FakeGitHub([NEVER_STARTED_RUN], {37426882755: [NEVER_STARTED_JOB]})
    other_run = dict(NEVER_STARTED_RUN, id=99, created_at="2026-10-07T06:59:09Z")
    day_two = FakeGitHub(
        [other_run],
        {
            99: [
                NEVER_STARTED_JOB,
                dict(NEVER_STARTED_JOB, name="Secrets reconcile + capacity"),
            ]
        },
    )

    first = sjs.evaluate(day_one, now=NOW)
    second = sjs.evaluate(day_two, now=NOW)

    assert first.units == ("job:deploy_v2 live canary",)
    assert second.units == (
        "job:Secrets reconcile + capacity",
        "job:deploy_v2 live canary",
    )
    assert sjs.evaluate(FakeGitHub([], {}, fail=True), now=NOW).units == (
        "read:unverifiable",
    )
    assert sjs.evaluate(FakeGitHub([], {}), now=NOW).units == ()
