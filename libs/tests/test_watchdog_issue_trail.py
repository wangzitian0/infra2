"""truealpha#876 W4: the ops-checks watchdog leaves one GitHub issue per red check.

Opened (or commented on) by exact title while the check is red, closed by a green
run that is allowed to close; never duplicated on a failed listing; never closed
by a drill. Everything runs against a fake GitHub.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from libs import watchdog_issue_trail as trail_lib
from libs.watchdog_issue_trail import (
    FULL,
    OFF,
    OPEN_ONLY,
    RUNNER_HEALTH_SOURCE,
    WATCHDOG_SOURCE,
    CheckVerdict,
    GitHubIssues,
    ListingFailed,
    Trail,
    WriteFailed,
    issue_trail_mode,
    load_trail,
    reconcile,
    record_verdicts,
    title_for,
)
from tools import watchdog_issue_trail as trail_tool

RUN_URL = "https://github.com/wangzitian0/infra2/actions/runs/42"


class FakeIssues:
    def __init__(self, issues=(), *, list_error=None, fail_on=()) -> None:
        self.issues = [dict(issue) for issue in issues]
        self.list_error = list_error
        self.fail_on = set(fail_on)
        self.created: list[tuple[str, str, tuple[str, ...]]] = []
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def open_issues(self) -> list[dict]:
        if self.list_error:
            raise ListingFailed(self.list_error)
        return [issue for issue in self.issues if issue["number"] not in self.closed]

    def create(self, title, body, labels) -> int:
        if "create" in self.fail_on:
            raise WriteFailed("HTTP 403")
        number = 1000 + len(self.created)
        self.created.append((title, body, tuple(labels)))
        self.issues.append({"number": number, "title": title})
        return number

    def comment(self, number, body) -> None:
        if "comment" in self.fail_on:
            raise WriteFailed("HTTP 403")
        self.comments.append((number, body))

    def close(self, number) -> None:
        if "close" in self.fail_on:
            raise WriteFailed("HTTP 403")
        self.closed.append(number)


def _red(name, detail="broken"):
    return CheckVerdict(name, False, detail, "P1", "cloudflare-worker-health")


def _trail(failing=(), green=(), prefixes=()):
    return Trail(
        failing={verdict.name: verdict for verdict in failing},
        green=set(green),
        green_prefixes=set(prefixes),
    )


def _issue(number, name):
    return {"number": number, "title": title_for(name)}


# --- open / comment ----------------------------------------------------------------


def test_a_red_check_without_an_issue_opens_one_with_the_exact_title() -> None:
    api = FakeIssues()
    code = reconcile(
        api,
        _trail([_red("cloudflare-worker-status", "worker status unhealthy")]),
        mode=FULL,
        run_url=RUN_URL,
        context="scheduled run",
    )
    assert code == 0
    assert len(api.created) == 1
    title, body, labels = api.created[0]
    assert title == "ops-checks watchdog is red: cloudflare-worker-status"
    assert labels == ("incident",)
    assert (
        RUN_URL in body
        and "worker status unhealthy" in body
        and "scheduled run" in body
    )
    assert "Feishu" in body


def test_a_red_check_with_an_open_issue_comments_on_the_oldest_instead_of_filing() -> (
    None
):
    api = FakeIssues([_issue(12, "infra2-ssh"), _issue(7, "infra2-ssh")])
    assert reconcile(api, _trail([_red("infra2-ssh")]), mode=FULL, run_url=RUN_URL) == 0
    assert api.created == []
    assert [number for number, _ in api.comments] == [7]
    assert api.closed == []


def test_dedup_is_title_equality_not_containment() -> None:
    api = FakeIssues(
        [
            {"number": 3, "title": title_for("infra2-ssh") + " (flaky)"},
            {"number": 4, "title": "infra2-ssh"},
            {"number": 5, "title": title_for("infra2-ssh-extra")},
        ]
    )
    reconcile(
        api, _trail([_red("infra2-ssh")], green=["infra2-ssh-extra"]), mode=OPEN_ONLY
    )
    assert [title for title, _, _ in api.created] == [title_for("infra2-ssh")]
    assert api.comments == []


def test_a_failed_listing_never_falls_through_to_create(capsys) -> None:
    api = FakeIssues(list_error="HTTP 502")
    assert reconcile(api, _trail([_red("infra2-ssh")]), mode=FULL) == 0
    assert api.created == [] and api.comments == [] and api.closed == []
    assert "could not be listed" in capsys.readouterr().out


def test_a_failed_write_after_a_good_listing_is_exit_1() -> None:
    assert (
        reconcile(
            FakeIssues(fail_on={"create"}), _trail([_red("infra2-ssh")]), mode=FULL
        )
        == 1
    )
    commented = FakeIssues([_issue(1, "infra2-ssh")], fail_on={"comment"})
    assert reconcile(commented, _trail([_red("infra2-ssh")]), mode=FULL) == 1
    closing = FakeIssues([_issue(1, "infra2-ssh")], fail_on={"close"})
    assert reconcile(closing, _trail(green=["infra2-ssh"]), mode=FULL) == 1


# --- close -------------------------------------------------------------------------


def test_a_green_check_closes_every_open_issue_with_its_title() -> None:
    api = FakeIssues(
        [
            _issue(5, "cloudflare-worker-status"),
            _issue(9, "cloudflare-worker-status"),
            {"number": 10, "title": "unrelated"},
        ]
    )
    assert (
        reconcile(
            api,
            _trail(green=["cloudflare-worker-status"]),
            mode=FULL,
            run_url=RUN_URL,
            context="scheduled run",
        )
        == 0
    )
    assert api.closed == [5, 9]
    assert all(RUN_URL in body and "green again" in body for _, body in api.comments)
    assert api.created == []


def test_a_check_that_was_not_evaluated_is_left_open() -> None:
    api = FakeIssues([_issue(5, "infra2-alert-bridge")])
    assert reconcile(api, _trail(green=["infra2-ssh"]), mode=FULL) == 0
    assert api.closed == [] and api.comments == []


def test_a_drill_or_branch_run_never_closes() -> None:
    api = FakeIssues([_issue(5, "cloudflare-worker-status")])
    red = _red("truealpha-scheduler-liveness", "STALE: capped at 0h")
    assert (
        reconcile(
            api, _trail([red], green=["cloudflare-worker-status"]), mode=OPEN_ONLY
        )
        == 0
    )
    assert api.closed == []
    assert [title for title, _, _ in api.created] == [
        title_for("truealpha-scheduler-liveness")
    ]


def test_off_writes_nothing() -> None:
    api = FakeIssues(list_error="must not be listed")
    assert reconcile(api, _trail([_red("infra2-ssh")]), mode=OFF) == 0
    assert api.created == []
    assert reconcile(api, _trail(), mode="bogus") == 1


def test_the_dokploy_family_closes_absent_units_only_when_the_query_answered() -> None:
    unit_ok = "dokploy-status:finance-report/staging/backend"
    unit_red = "dokploy-status:finance-report/production/backend"
    issues = [_issue(1, unit_ok), _issue(2, unit_red)]
    answered = FakeIssues(issues)
    reconcile(
        answered,
        _trail(
            [_red(unit_red)],
            green=["infra2-dokploy-status"],
            prefixes=["dokploy-status:"],
        ),
        mode=FULL,
    )
    assert answered.closed == [1]
    assert [number for number, body in answered.comments if "is red" in body] == [2]
    unanswered = FakeIssues(issues)
    reconcile(unanswered, _trail([_red("infra2-dokploy-status")]), mode=FULL)
    assert unanswered.closed == []


def test_the_full_lifecycle_red_then_red_then_green() -> None:
    api = FakeIssues()
    red = _trail([_red("cloudflare-worker-status")])
    reconcile(api, red, mode=FULL)
    reconcile(api, red, mode=FULL)
    reconcile(api, _trail(green=["cloudflare-worker-status"]), mode=FULL)
    assert len(api.created) == 1
    assert [number for number, _ in api.comments] == [1000, 1000]
    assert api.closed == [1000]


# --- public-repo scrubbing -------------------------------------------------------


def test_bodies_carry_no_private_host_ip_or_secret() -> None:
    env = {
        "INFRA2_WATCHDOG_SSH_HOST": "vps.internal.example",
        "INFRA2_WATCHDOG_SSH_USER": "deployer",
        "INFRA2_WATCHDOG_WORKER_STATUS_TOKEN": "s3cr3t-value",
        "GITHUB_TOKEN": "fake-actions-token",
        "USER": "runner",
        "INFRA2_WATCHDOG_SSH_PORT": "22",
    }
    detail = (
        "ssh exited 255: deployer@vps.internal.example: connect to host 203.0.113.9 port 22 "
        "via 2001:db8:0:0:0:0:0:1 with s3cr3t-value fake-actions-token; iac-runner at 10:26:58"
    )
    api = FakeIssues()
    reconcile(api, _trail([_red("infra2-ssh", detail)]), mode=FULL, env=env)
    body = api.created[0][1]
    for leaked in (
        "vps.internal.example",
        "deployer",
        "203.0.113.9",
        "2001:db8",
        "s3cr3t-value",
        "fake-actions-token",
    ):
        assert leaked not in body
    # The runner's USER is not a secret, a port is too short, and a clock is not an IP.
    assert "iac-runner" in body and "port 22" in body and "10:26:58" in body


def test_a_name_with_a_private_value_is_scrubbed_in_the_title_and_still_closes() -> (
    None
):
    env = {"INFRA2_WATCHDOG_SSH_HOST": "vps.internal.example"}
    name = "diag-vps.internal.example"
    api = FakeIssues()
    reconcile(api, _trail([_red(name)]), mode=FULL, env=env)
    title, body, _ = api.created[0]
    assert title == title_for("diag-***")
    assert "vps.internal.example" not in body
    reconcile(api, _trail(green=[name]), mode=FULL, env=env)
    assert api.closed == [1000]


def test_a_long_detail_is_truncated() -> None:
    assert len(trail_lib.scrub("x" * 5000, {})) < 1100


# --- the verdict file ----------------------------------------------------------------


def test_recorded_verdicts_merge_and_a_red_wins(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.jsonl"
    record_verdicts(
        path,
        source=WATCHDOG_SOURCE,
        checks=[CheckVerdict("infra2-ssh", True), _red("cloudflare-worker-status")],
        green_prefixes=["dokploy-status:"],
    )
    record_verdicts(
        path,
        source=RUNNER_HEALTH_SOURCE,
        checks=[CheckVerdict(RUNNER_HEALTH_SOURCE, True)],
    )
    record_verdicts(
        path, source="retry", checks=[CheckVerdict("cloudflare-worker-status", True)]
    )
    trail = load_trail(path)
    assert set(trail.failing) == {"cloudflare-worker-status"}
    assert {"infra2-ssh", WATCHDOG_SOURCE, RUNNER_HEALTH_SOURCE} <= trail.green
    assert "cloudflare-worker-status" not in trail.green
    assert trail.is_green("dokploy-status:any/unit")


def test_a_step_that_recorded_nothing_is_red_and_its_next_record_closes_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "verdicts.jsonl"
    path.write_text(
        '{"source": "out-of-band-watchdog", "checks": [\n', encoding="utf-8"
    )  # torn
    record_verdicts(
        path,
        source=RUNNER_HEALTH_SOURCE,
        checks=[CheckVerdict(RUNNER_HEALTH_SOURCE, True)],
    )
    crashed = load_trail(path)
    assert set(crashed.failing) == {WATCHDOG_SOURCE}
    assert "recorded no verdict" in crashed.failing[WATCHDOG_SOURCE].detail
    api = FakeIssues()
    reconcile(api, crashed, mode=FULL)
    assert [title for title, _, _ in api.created] == [title_for(WATCHDOG_SOURCE)]

    record_verdicts(path, source=WATCHDOG_SOURCE, checks=[])
    reconcile(api, load_trail(path), mode=FULL)
    assert api.closed == [1000]


def test_a_missing_file_is_every_step_red(tmp_path: Path) -> None:
    assert set(load_trail(tmp_path / "absent.jsonl").failing) == {
        WATCHDOG_SOURCE,
        RUNNER_HEALTH_SOURCE,
    }


# --- mode ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "mode"),
    [
        ({"GITHUB_EVENT_NAME": "schedule", "GITHUB_REF": "refs/heads/main"}, FULL),
        (
            {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"},
            FULL,
        ),
        (
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/feature",
            },
            OPEN_ONLY,
        ),
        (
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "0",
            },
            OPEN_ONLY,
        ),
        (
            {
                "GITHUB_EVENT_NAME": "schedule",
                "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "1",
            },
            OPEN_ONLY,
        ),
        (
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "WATCHDOG_DRY_RUN": "1",
            },
            OFF,
        ),
        (
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "INFRA2_WATCHDOG_SSH_TARGETS_OVERRIDDEN": "1",
            },
            OFF,
        ),
        ({"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/heads/main"}, OFF),
        ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}, OFF),
        ({}, OFF),
    ],
)
def test_the_run_kind_decides_what_may_be_written(env, mode) -> None:
    assert issue_trail_mode(env) == mode


# --- the REST client -----------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_the_listing_skips_pull_requests_and_reads_every_page(monkeypatch) -> None:
    monkeypatch.setattr(trail_lib, "PER_PAGE", 2)
    pages = {
        1: [
            {"number": 1, "title": "a"},
            {"number": 2, "title": "pr", "pull_request": {}},
        ],
        2: [{"number": 3, "title": "c"}],
    }
    seen = []

    def opener(request, timeout):
        seen.append(
            (
                request.get_method(),
                request.full_url,
                request.get_header("Authorization"),
            )
        )
        page = int(request.full_url.rsplit("page=", 1)[1])
        return _Response(json.dumps(pages[page]).encode())

    issues = GitHubIssues("owner/repo", "tok", opener=opener).open_issues()
    assert issues == [{"number": 1, "title": "a"}, {"number": 3, "title": "c"}]
    assert seen[0] == (
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=open&per_page=2&page=1",
        "Bearer tok",
    )
    assert len(seen) == 2


@pytest.mark.parametrize(
    "answer",
    [
        HTTPError("u", 502, "Bad Gateway", {}, None),
        URLError("dns"),
        b"not json",
        b'{"message": "not a list"}',
        b'[{"number": "1", "title": "x"}]',
        b'["x"]',
    ],
)
def test_an_unreadable_listing_is_a_listing_failure(answer) -> None:
    def opener(_request, timeout):
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    with pytest.raises(ListingFailed):
        GitHubIssues("o/r", "t", opener=opener).open_issues()


def test_a_listing_longer_than_the_page_cap_is_not_trusted(monkeypatch) -> None:
    monkeypatch.setattr(trail_lib, "PER_PAGE", 1)
    monkeypatch.setattr(trail_lib, "MAX_PAGES", 2)
    opener = lambda _request, timeout: _Response(b'[{"number": 1, "title": "x"}]')  # noqa: E731
    with pytest.raises(ListingFailed, match="not read to the end"):
        GitHubIssues("o/r", "t", opener=opener).open_issues()


def test_writes_send_the_documented_payloads() -> None:
    sent = []

    def opener(request, timeout):
        sent.append((request.get_method(), request.full_url, json.loads(request.data)))
        return _Response(
            b'{"number": 77}' if request.full_url.endswith("/issues") else b"{}"
        )

    api = GitHubIssues("o/r", "t", opener=opener)
    assert api.create("T", "B", ["incident"]) == 77
    api.comment(77, "C")
    api.close(77)
    assert sent == [
        (
            "POST",
            "https://api.github.com/repos/o/r/issues",
            {"title": "T", "body": "B", "labels": ["incident"]},
        ),
        ("POST", "https://api.github.com/repos/o/r/issues/77/comments", {"body": "C"}),
        (
            "PATCH",
            "https://api.github.com/repos/o/r/issues/77",
            {"state": "closed", "state_reason": "completed"},
        ),
    ]


def test_a_failed_or_empty_write_is_a_write_failure() -> None:
    def refused(_request, timeout):
        raise HTTPError("u", 403, "Forbidden", {}, None)

    with pytest.raises(WriteFailed):
        GitHubIssues("o/r", "t", opener=refused).comment(1, "x")
    with pytest.raises(WriteFailed):
        GitHubIssues("o/r", "t", opener=lambda _r, timeout: _Response(b"{}")).create(
            "T", "B", []
        )


# --- the entry point ------------------------------------------------------------------


def _tool_env(tmp_path: Path, **extra) -> dict[str, str]:
    env = {
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "wangzitian0/infra2",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "42",
        "GITHUB_TOKEN": "tok",
        "INFRA2_WATCHDOG_VERDICTS_PATH": str(tmp_path / "verdicts.jsonl"),
    }
    env.update(extra)
    return env


def test_the_tool_reconciles_the_recorded_verdicts(tmp_path: Path) -> None:
    env = _tool_env(tmp_path)
    record_verdicts(
        env["INFRA2_WATCHDOG_VERDICTS_PATH"],
        source=WATCHDOG_SOURCE,
        checks=[_red("infra2-ssh")],
    )
    record_verdicts(
        env["INFRA2_WATCHDOG_VERDICTS_PATH"],
        source=RUNNER_HEALTH_SOURCE,
        checks=[CheckVerdict(RUNNER_HEALTH_SOURCE, True)],
    )
    api = FakeIssues([_issue(4, RUNNER_HEALTH_SOURCE)])
    built = []

    def factory(repository, token):
        built.append((repository, token))
        return api

    assert trail_tool.main(env, issues_factory=factory) == 0
    assert built == [("wangzitian0/infra2", "tok")]
    assert [title for title, _, _ in api.created] == [title_for("infra2-ssh")]
    assert RUN_URL in api.created[0][1]
    assert api.closed == [4]


def test_the_tool_drill_opens_but_never_closes(tmp_path: Path) -> None:
    env = _tool_env(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS="0",
    )
    record_verdicts(
        env["INFRA2_WATCHDOG_VERDICTS_PATH"],
        source=WATCHDOG_SOURCE,
        checks=[_red("truealpha-scheduler-liveness"), CheckVerdict("infra2-ssh", True)],
    )
    record_verdicts(
        env["INFRA2_WATCHDOG_VERDICTS_PATH"], source=RUNNER_HEALTH_SOURCE, checks=[]
    )
    api = FakeIssues([_issue(4, "infra2-ssh")])
    assert trail_tool.main(env, issues_factory=lambda _r, _t: api) == 0
    assert api.closed == []
    assert "drill" in api.created[0][1]


def test_the_tool_is_inert_when_off_and_loud_when_misconfigured(tmp_path: Path) -> None:
    def refuse(*_args):
        raise AssertionError("no API client may be built")

    assert (
        trail_tool.main(
            _tool_env(tmp_path, WATCHDOG_DRY_RUN="1"), issues_factory=refuse
        )
        == 0
    )
    assert (
        trail_tool.main(
            _tool_env(tmp_path, INFRA2_WATCHDOG_VERDICTS_PATH=""), issues_factory=refuse
        )
        == 1
    )
    assert (
        trail_tool.main(_tool_env(tmp_path, GITHUB_TOKEN=""), issues_factory=refuse)
        == 1
    )
    assert (
        trail_tool.main(
            _tool_env(tmp_path, GITHUB_REPOSITORY="infra2"), issues_factory=refuse
        )
        == 1
    )
