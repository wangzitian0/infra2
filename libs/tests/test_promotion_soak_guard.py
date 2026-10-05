"""#970: the production promotion guard finds the staging soak by commit and stays fail-closed.

2026-10-05 (v1.2.10): a promotion dispatched right after the soak finished was refused,
and paged, because GitHub's branch-filtered run listing answered ``[]`` for a run that
existed (the same query returned it a minute later; the same flake reproduced live
during this fix). The listing is eventually consistent, so an EMPTY answer is retried a
bounded number of times; every other way of not finding a green soak still exits 1.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

import tools.promotion_soak_guard as guard
from tools.promotion_soak_guard import (
    EMPTY,
    ERROR,
    GREEN,
    NOT_GREEN,
    GhClient,
    LookupFailed,
    judge_soak,
    look_up_soak,
    main,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "reconcile-iac-inputs.yml"
TAG = "v1.2.10"
SHA = "ecf9b9abac9b564f40723326ef6066fcc775f692"
GUARD_STEP = "Production promotion guard (soak, identity, change set)"


def run(conclusion="success", *, event="push", branch=TAG, sha=SHA, n=1, status=None):
    """One element of the GitHub ``workflow_runs`` array, with the fields the guard reads."""
    return {
        "id": n,
        "event": event,
        "head_branch": branch,
        "head_sha": sha,
        "status": status or ("completed" if conclusion else "in_progress"),
        "conclusion": conclusion,
        "html_url": f"https://github.example/runs/{n}",
        "created_at": f"2026-10-05T07:3{n}:00Z",
    }


class FakeClient:
    """Scripted listings: one answer per call, in order; the last answer repeats."""

    def __init__(self, by_commit=(), by_branch=(), sha=SHA):
        self._by_commit = list(by_commit)
        self._by_branch = list(by_branch)
        self.sha = sha
        self.commit_calls = 0
        self.branch_calls = 0

    @staticmethod
    def _answer(script, calls):
        answer = script[min(calls, len(script) - 1)] if script else []
        if isinstance(answer, Exception):
            raise answer
        return answer

    def commit_of(self, tag):
        if isinstance(self.sha, Exception):
            raise self.sha
        return self.sha

    def runs_by_commit(self, sha):
        self.commit_calls += 1
        return self._answer(self._by_commit, self.commit_calls - 1)

    def runs_by_branch(self, tag):
        self.branch_calls += 1
        return self._answer(self._by_branch, self.branch_calls - 1)


def lookup(client, *, attempts=3, delay=15.0):
    sleeps: list[float] = []
    verdict = look_up_soak(
        TAG,
        client,
        attempts=attempts,
        delay_seconds=delay,
        sleep=sleeps.append,
        log=lambda _line: None,
    )
    return verdict, sleeps


# --- the pure decision --------------------------------------------------------------


def test_a_successful_push_run_of_the_tag_is_green() -> None:
    verdict = judge_soak([run("success", n=2)], tag=TAG, sha=SHA)
    assert verdict.state == GREEN
    assert verdict.url == "https://github.example/runs/2"
    assert verdict.created_at == "2026-10-05T07:32:00Z"


def test_no_runs_is_empty_not_green() -> None:
    assert judge_soak([], tag=TAG, sha=SHA).state == EMPTY


@pytest.mark.parametrize(
    "bad", ["failure", "cancelled", "timed_out", "skipped", "startup_failure"]
)
def test_any_concluded_run_that_is_not_success_blocks_wherever_it_is_listed(
    bad,
) -> None:
    first = judge_soak([run(bad, n=1), run("success", n=2)], tag=TAG, sha=SHA)
    last = judge_soak([run("success", n=2), run(bad, n=1)], tag=TAG, sha=SHA)
    alone = judge_soak([run(bad, n=1)], tag=TAG, sha=SHA)
    assert (first.state, last.state, alone.state) == (NOT_GREEN,) * 3


def test_a_run_still_in_progress_is_not_a_green_soak() -> None:
    verdict = judge_soak([run(None, status="in_progress")], tag=TAG, sha=SHA)
    assert verdict.state == NOT_GREEN
    assert verdict.seen[0]["status"] == "in_progress"


def test_a_run_in_progress_does_not_unmake_a_concluded_success() -> None:
    runs = [run(None, status="in_progress", n=1), run("success", n=2)]
    assert judge_soak(runs, tag=TAG, sha=SHA).state == GREEN


def test_runs_that_are_not_push_runs_of_this_tag_and_commit_are_ignored() -> None:
    """A dispatch run may be a dry run or a promotion itself; a run of another tag, or of
    this tag before it was moved, ran different code. None is a soak for this promotion."""
    ignored = [
        run("success", event="workflow_dispatch", n=1),
        run("success", event="schedule", n=2),
        run("success", branch="v1.2.9", n=3),
        run("success", sha="0" * 40, n=4),
        {"event": "push", "conclusion": "success", "html_url": "x"},  # no identity
    ]
    assert judge_soak(ignored, tag=TAG, sha=SHA).state == EMPTY


def test_a_wrong_event_success_does_not_hide_a_push_failure() -> None:
    runs = [run("success", event="workflow_dispatch", n=1), run("failure", n=2)]
    verdict = judge_soak(runs, tag=TAG, sha=SHA)
    assert verdict.state == NOT_GREEN
    assert [seen["conclusion"] for seen in verdict.seen] == ["failure"]


def test_the_same_run_from_both_listings_is_one_run() -> None:
    verdict = judge_soak([run("success", n=2), run("success", n=2)], tag=TAG, sha=SHA)
    assert verdict.state == GREEN and len(verdict.seen) == 1


# --- the lookup: retry only while nothing was found ---------------------------------


def test_empty_then_success_passes_after_one_wait() -> None:
    """The 2026-10-05 shape: the first listing is empty, the next one has the soak."""
    client = FakeClient(by_commit=[[], [run("success")]], by_branch=[[], []])
    verdict, sleeps = lookup(client)
    assert verdict.state == GREEN
    assert sleeps == [15.0]
    assert client.commit_calls == 2


def test_empty_every_attempt_fails_after_the_bounded_retries() -> None:
    client = FakeClient(by_commit=[[]], by_branch=[[]])
    verdict, sleeps = lookup(client, attempts=3, delay=15.0)
    assert verdict.state == EMPTY
    assert sleeps == [15.0, 15.0]  # no wait after the last attempt
    assert (client.commit_calls, client.branch_calls) == (3, 3)


def test_either_listing_may_supply_the_run() -> None:
    by_branch_only = lookup(FakeClient(by_commit=[[]], by_branch=[[run("success")]]))
    by_commit_only = lookup(FakeClient(by_commit=[[run("success")]], by_branch=[[]]))
    assert by_branch_only[0].state == GREEN
    assert by_commit_only[0].state == GREEN


def test_a_failure_listed_first_fails_at_once_without_retrying() -> None:
    client = FakeClient(by_commit=[[run("failure", n=1), run("success", n=2)]])
    verdict, sleeps = lookup(client)
    assert verdict.state == NOT_GREEN
    assert sleeps == [] and client.commit_calls == 1  # a verdict is not retried


def test_a_failure_found_by_only_one_listing_still_blocks() -> None:
    client = FakeClient(
        by_commit=[[run("success", n=2)]], by_branch=[[run("failure", n=1)]]
    )
    assert lookup(client)[0].state == NOT_GREEN


def test_an_api_error_on_every_attempt_fails() -> None:
    boom = LookupFailed("gh api ...: HTTP 502")
    client = FakeClient(by_commit=[boom], by_branch=[boom])
    verdict, sleeps = lookup(client)
    assert verdict.state == ERROR
    assert "HTTP 502" in verdict.detail
    assert len(sleeps) == 2


def test_an_api_error_on_one_listing_and_an_empty_other_never_passes() -> None:
    client = FakeClient(by_commit=[LookupFailed("HTTP 502")], by_branch=[[]])
    assert lookup(client)[0].state == ERROR


def test_a_transient_api_error_followed_by_the_soak_passes() -> None:
    boom = LookupFailed("HTTP 502")
    client = FakeClient(by_commit=[boom, [run("success")]], by_branch=[boom, []])
    verdict, sleeps = lookup(client)
    assert verdict.state == GREEN and sleeps == [15.0]


def test_an_unresolvable_tag_fails_without_asking_github() -> None:
    client = FakeClient(sha=LookupFailed("git rev-parse: tag resolves to no commit"))
    verdict, sleeps = lookup(client)
    assert verdict.state == ERROR and sleeps == []
    assert client.commit_calls == 0


# --- the CLI contract: exit 0 only on a green soak, the same error text otherwise ---


def _main(client, *extra):
    return main([TAG, "--repo", "o/r", *extra], client=client, sleep=lambda _s: None)


def test_main_exits_zero_and_names_the_soak(capsys) -> None:
    assert _main(FakeClient(by_commit=[[run("success", n=2)]])) == 0
    out = capsys.readouterr().out
    assert "staging soak of v1.2.10: https://github.example/runs/2" in out
    assert "::error::" not in out


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(by_commit=[[]], by_branch=[[]]),
        FakeClient(by_commit=[[run("failure")]]),
        FakeClient(by_commit=[[run("success", event="workflow_dispatch")]]),
        FakeClient(by_commit=[LookupFailed("HTTP 500")], by_branch=[LookupFailed("x")]),
        FakeClient(sha=LookupFailed("no such tag")),
    ],
    ids=["empty", "failure", "wrong-event", "api-error", "no-commit"],
)
def test_main_exits_one_with_the_explicit_error(client, capsys) -> None:
    assert _main(client) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    # retry notes are plain log lines; the one annotation is the final refusal
    assert [line.startswith("::error::") for line in lines].count(True) == 1
    out = lines[-1]
    assert out.startswith("::error::")
    assert "'v1.2.10' has no green staging soak" in out
    assert (
        "production is never promoted from a tag whose staging reconcile did not pass"
        in out
    )


def test_the_empty_refusal_says_it_retried() -> None:
    verdict, _ = lookup(FakeClient())
    message = guard.refusal_message(TAG, verdict, attempts=3, delay_seconds=15.0)
    assert "empty on all 3 attempts, 15s apart" in message
    assert SHA[:12] in message


def test_main_refuses_to_run_without_a_repository_or_with_no_attempts(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert main([TAG], sleep=lambda _s: None) == 1
    assert "no repository" in capsys.readouterr().out
    assert _main(FakeClient(by_commit=[[run("success")]]), "--attempts", "0") == 1


# --- the thin gh/git client ---------------------------------------------------------


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_gh_client_queries_by_commit_and_by_branch_for_push_runs_only() -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return _completed(json.dumps({"workflow_runs": [run("success")]}))

    client = GhClient("o/r", run=fake_run)
    assert client.runs_by_commit(SHA)[0]["conclusion"] == "success"
    assert client.runs_by_branch(TAG)[0]["conclusion"] == "success"

    commit_path, branch_path = calls[0][2], calls[1][2]
    assert calls[0][:2] == ["gh", "api"]
    prefix = "repos/o/r/actions/workflows/reconcile-iac-inputs.yml/runs?"
    assert commit_path.startswith(prefix) and branch_path.startswith(prefix)
    assert "event=push" in commit_path and f"head_sha={SHA}" in commit_path
    assert "event=push" in branch_path and f"branch={TAG}" in branch_path


def test_gh_client_resolves_an_annotated_tag_to_its_commit() -> None:
    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append(argv)
        return _completed(SHA + "\n")

    assert GhClient("o/r", run=fake_run).commit_of(TAG) == SHA
    assert seen == [
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{TAG}^{{commit}}"]
    ]


@pytest.mark.parametrize(
    "result",
    [
        _completed("", returncode=1, stderr="HTTP 403"),
        _completed("not json"),
        _completed(json.dumps({"message": "Bad credentials"})),
        _completed(json.dumps({"workflow_runs": "nope"})),
        _completed(json.dumps({"workflow_runs": ["nope"]})),
    ],
    ids=["exit-1", "not-json", "no-key", "not-a-list", "not-runs"],
)
def test_gh_client_turns_every_unreadable_answer_into_a_lookup_failure(result) -> None:
    client = GhClient("o/r", run=lambda argv, **_kw: result)
    with pytest.raises(LookupFailed):
        client.runs_by_commit(SHA)


@pytest.mark.parametrize(
    "exc", [FileNotFoundError("gh"), subprocess.TimeoutExpired("gh", 60)]
)
def test_gh_client_turns_a_missing_or_hung_gh_into_a_lookup_failure(exc) -> None:
    def fake_run(argv, **_kwargs):
        raise exc

    with pytest.raises(LookupFailed):
        GhClient("o/r", run=fake_run).runs_by_branch(TAG)


def test_gh_client_with_no_tag_commit_fails_closed() -> None:
    with pytest.raises(LookupFailed):
        GhClient("o/r", run=lambda argv, **_kw: _completed("\n")).commit_of(TAG)


# --- the workflow step delegates to the tool, keeping the rest of its contract -------


def test_the_workflow_guard_step_calls_the_tool_and_keeps_the_gate_inputs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    steps = workflow["jobs"]["reconcile"]["steps"]
    step = next(s for s in steps if s.get("name") == GUARD_STEP)
    script = step["run"]

    assert 'python -m tools.promotion_soak_guard "$after"' in script
    assert "gh run list" not in script  # the single eventually-consistent query is gone
    assert "--branch" not in script
    # the identity and change-set checks stay in the step, before and after the soak
    assert "merge-base --is-ancestor" in script
    assert script.index("merge-base --is-ancestor") < script.index(
        "promotion_soak_guard"
    )
    assert script.index("promotion_soak_guard") < script.index("git diff --stat")
    # the tool reads the repository and token from the step environment
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["GH_REPO"] == "${{ github.repository }}"
    # merge-gate inputs: no push path filter was introduced, one job, same name
    assert "paths" not in workflow["on"]["push"]
    assert [job.get("name") for job in workflow["jobs"].values()] == [
        "reconcile iac-pinned inputs"
    ]
