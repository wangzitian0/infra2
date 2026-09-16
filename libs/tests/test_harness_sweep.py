"""libs/harness_sweep + `tools.harness sweep`: one state per watched item, read-only."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from libs import harness_sweep as sweep
from libs.harness_sweep import (
    ACTION,
    DONE,
    STALL,
    UNKNOWN,
    WAITING,
    CommandResult,
    Env,
    PrFacts,
    Status,
)
from tools import harness

NOW = 1_800_000_000.0
MIN = 60.0


def _facts(**overrides) -> PrFacts:
    base = dict(
        state="OPEN",
        draft=False,
        head="14756032fbc5707ad65852aa5535e6b7647e5b02",
        merge_state="CLEAN",
        review_decision="",
        head_at=NOW - 30 * MIN,
        updated_at=NOW - 5 * MIN,
        checks=(
            ("ci-required", "pass", NOW - 29 * MIN, NOW - 20 * MIN),
            ("e2e", "skipping", None, None),
        ),
        unresolved=0,
        threads_total=2,
    )
    base.update(overrides)
    return PrFacts(**base)


def _pr(facts: PrFacts, gate: int | None = 1, **kwargs) -> Status:
    return sweep.classify_pr("truealpha#901", facts, gate, now=NOW, **kwargs)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# --- plumbing


def test_run_command_reports_a_missing_binary_as_127_and_passes_output_through():
    missing = sweep.run_command(["/nonexistent/binary-for-sweep-test"])
    assert missing.rc == 127 and "FileNotFoundError" in missing.err
    echoed = sweep.run_command([sys.executable, "-c", "print('hi')"])
    assert (echoed.rc, echoed.out) == (0, "hi\n")


def test_pid_alive_distinguishes_live_and_finished_processes(monkeypatch):
    assert sweep.pid_alive(os.getpid())
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert not sweep.pid_alive(done.pid)

    def denied(pid, signal):
        raise PermissionError

    monkeypatch.setattr(sweep.os, "kill", denied)
    assert sweep.pid_alive(1)  # someone else's live process


# --- classify_pr


def test_the_2026_09_16_unresolved_thread_is_action_not_waiting():
    # merge_ready printed "1 unresolved review thread(s)"; the grep waiter kept waiting.
    status = _pr(_facts(unresolved=1))
    assert status.state == ACTION
    assert "1 unresolved review thread" in status.detail


def test_an_unresolved_thread_wins_over_pending_checks():
    pending = (("ci", "pending", NOW - MIN, None),)
    assert _pr(_facts(unresolved=1, checks=pending)).state == ACTION


def test_gate_prose_is_never_read_only_its_exit_code():
    env = Env(run=lambda argv, cwd: CommandResult(1, "#901 clear to merge\n", ""))
    assert sweep.run_gate(env, {"argv": ["gate", "{number}"]}, 901) == 1
    assert "READY" not in _pr(_facts(), gate=1).detail


def test_gate_exit_0_is_ready_and_exit_2_is_the_owner():
    ready, owner = _pr(_facts(), 0), _pr(_facts(), 2)
    assert (ready.state, ready.detail[:5]) == (ACTION, "READY")
    assert (owner.state, owner.detail[:5]) == (ACTION, "OWNER")


def test_a_crashed_or_missing_gate_is_unknown_not_a_verdict():
    assert _pr(_facts(), 127).state == UNKNOWN


def test_gate_exit_1_after_settling_with_nothing_pending_needs_a_reader():
    status = _pr(_facts(head_at=NOW - 30 * MIN), gate=1)
    assert status.state == ACTION and "read the gate output" in status.detail


def test_settling_waits_then_ends():
    assert _pr(_facts(head_at=NOW - 5 * MIN), gate=1).state == WAITING
    assert _pr(_facts(head_at=NOW - 13 * MIN), gate=1).state == ACTION
    assert _pr(_facts(head_at=NOW - 5 * MIN), settle_minutes=3).state == ACTION


def test_pending_checks_wait_until_nothing_finished_for_the_stall_window():
    running = (
        ("a", "pass", NOW - 20 * MIN, NOW - 2 * MIN),
        ("b", "pending", NOW - 20 * MIN, None),
    )
    assert _pr(_facts(checks=running, head_at=NOW - 20 * MIN)).state == WAITING
    hung = (
        ("a", "pass", NOW - 60 * MIN, NOW - 45 * MIN),
        ("b", "pending", NOW - 60 * MIN, None),
    )
    status = _pr(_facts(checks=hung, head_at=NOW - 60 * MIN))
    assert status.state == STALL and "none finished for 45m" in status.detail


def test_red_or_cancelled_checks_are_action():
    assert _pr(_facts(checks=(("ci", "fail", None, NOW),))).state == ACTION
    assert _pr(_facts(checks=(("ci", "cancel", None, NOW),))).state == ACTION
    many = tuple((f"c{i}", "fail", None, NOW) for i in range(6))
    assert "…" in _pr(_facts(checks=many)).detail


def test_unrecognised_buckets_and_merge_states_are_unknown():
    assert _pr(_facts(checks=(("ci", "exploded", None, None),))).state == UNKNOWN
    assert _pr(_facts(merge_state="QUANTUM")).state == UNKNOWN


def test_terminal_pr_states():
    assert _pr(_facts(state="MERGED"), gate=None).state == DONE
    # A gate exits 1 on a closed PR exactly as on "not yet"; the sweep does not wait.
    assert _pr(_facts(state="CLOSED"), gate=1).state == ACTION
    assert "unknown state" in _pr(_facts(state=""), gate=None).detail


def test_conflicts_behind_changes_requested_and_thread_overflow_are_action():
    assert _pr(_facts(merge_state="DIRTY")).state == ACTION
    assert _pr(_facts(merge_state="BEHIND")).state == ACTION
    assert _pr(_facts(review_decision="CHANGES_REQUESTED")).state == ACTION
    overflow = _pr(_facts(threads_total=150))
    assert overflow.state == ACTION and "only 100 read" in overflow.detail


def test_no_checks_waits_then_stalls():
    assert _pr(_facts(checks=(), head_at=NOW - 2 * MIN)).state == WAITING
    assert _pr(_facts(checks=(), head_at=NOW - 40 * MIN)).state == STALL


def test_a_draft_waits_on_author_activity():
    assert _pr(_facts(draft=True, updated_at=NOW - 5 * MIN)).state == WAITING
    assert _pr(_facts(draft=True, updated_at=NOW - 50 * MIN)).state == STALL


def test_mergeability_still_computing_waits():
    status = _pr(_facts(merge_state="UNKNOWN", head_at=NOW - 20 * MIN), gate=None)
    assert status.state == WAITING and "computing" in status.detail


def test_without_a_gate_the_facts_are_reported():
    clean = _pr(_facts(), gate=None)
    assert clean.state == ACTION and "mergeable by facts" in clean.detail
    blocked = _pr(_facts(merge_state="BLOCKED"), gate=None)
    assert blocked.state == ACTION and "BLOCKED" in blocked.detail


def test_the_fingerprint_ignores_age():
    pending = (("b", "pending", NOW - MIN, None),)
    first = _pr(_facts(checks=pending, head_at=NOW - 2 * MIN))
    later = sweep.classify_pr(
        "truealpha#901",
        _facts(checks=pending, head_at=NOW - 2 * MIN),
        1,
        now=NOW + 60,
    )
    assert first.fingerprint == later.fingerprint


# --- probing PRs


class FakeRun:
    """Maps an argv prefix to a result and records every call."""

    def __init__(self, table):
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        for prefix, result in self.table:
            if list(argv[: len(prefix)]) == prefix:
                return result
        return CommandResult(99, "", f"unexpected call {argv}")


def _view(**overrides) -> CommandResult:
    body = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "abc1234def",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "updatedAt": "2027-01-15T08:00:00Z",
        "commits": [{"committedDate": "2027-01-15T07:00:00Z"}],
    }
    body.update(overrides)
    return CommandResult(0, json.dumps(body), "")


def _threads(*resolved: bool) -> CommandResult:
    nodes = [{"isResolved": r} for r in resolved]
    doc = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {"totalCount": len(nodes), "nodes": nodes}
                }
            }
        }
    }
    return CommandResult(0, json.dumps(doc), "")


def test_no_checks_reported_is_zero_checks_not_an_error():
    run = FakeRun(
        [
            (["gh", "pr", "view"], _view()),
            (
                ["gh", "pr", "checks"],
                CommandResult(1, "", "no checks reported on the 'x' branch"),
            ),
            (["gh", "api", "graphql"], _threads(True, False)),
        ]
    )
    facts = sweep.collect_pr(Env(run=run), "wangzitian0/truealpha", 901)
    assert (facts.checks, facts.unresolved, facts.threads_total) == ((), 1, 2)
    assert facts.head_at == sweep.epoch("2027-01-15T07:00:00Z")


def test_checks_json_is_read_on_gh_pending_exit_8_and_other_failures_raise():
    body = json.dumps([{"name": "ci", "bucket": "pending", "startedAt": None}])
    pending = FakeRun([(["gh", "pr", "checks"], CommandResult(8, body, ""))])
    assert sweep._read_checks(pending, "o/r", 1) == [
        {"name": "ci", "bucket": "pending", "startedAt": None}
    ]
    empty = FakeRun([(["gh", "pr", "checks"], CommandResult(0, "", ""))])
    assert sweep._read_checks(empty, "o/r", 1) == []
    broken = FakeRun([(["gh", "pr", "checks"], CommandResult(1, "", "HTTP 502"))])
    with pytest.raises(sweep.ProbeError, match="HTTP 502"):
        sweep._read_checks(broken, "o/r", 1)


def test_a_gh_failure_becomes_a_visible_unknown_under_the_same_key():
    run = FakeRun([(["gh", "pr", "view"], CommandResult(1, "", "HTTP 502"))])
    [status] = sweep.sweep(
        Env(run=run), [{"kind": "pr", "repo": "o/r", "number": 1}], {}
    )
    assert (status.key, status.state) == ("r#1", UNKNOWN)
    assert "HTTP 502" in status.detail


def test_a_terminal_pr_skips_the_gate():
    run = FakeRun(
        [
            (["gh", "pr", "view"], _view(state="MERGED")),
            (["gh", "pr", "checks"], CommandResult(0, "[]", "")),
            (["gh", "api", "graphql"], _threads(True)),
        ]
    )
    item = {"kind": "pr", "repo": "o/r", "number": 1, "gate": {"argv": ["gate"]}}
    [status] = sweep.sweep(Env(run=run), [item], {})
    assert status.state == DONE
    assert not any(call[0] == "gate" for call in run.calls)


def test_an_open_pr_runs_the_gate_in_its_cwd_and_uses_only_the_exit_code():
    seen = []

    def run(argv, cwd):
        if argv[0] == "gate":
            seen.append((list(argv), cwd))
            return CommandResult(0, "not yet, keep waiting", "")
        return FakeRun(
            [
                (["gh", "pr", "view"], _view()),
                (
                    ["gh", "pr", "checks"],
                    CommandResult(0, '[{"name":"ci","bucket":"pass"}]', ""),
                ),
                (["gh", "api", "graphql"], _threads(True)),
            ]
        )(argv, cwd)

    item = {
        "kind": "pr",
        "repo": "o/r",
        "number": 7,
        "gate": {"argv": ["gate", "{number}", "--policy", "either"], "cwd": "/w"},
    }
    [status] = sweep.sweep(Env(run=run, now=lambda: NOW), [item], {})
    assert status.state == ACTION and status.detail.startswith("READY")
    assert seen == [(["gate", "7", "--policy", "either"], "/w")]


@pytest.mark.parametrize(
    "argv",
    [
        ["pr_merge_gate", "{number}", "--merge"],
        ["pr_merge_gate", "{number}", "--request-review"],
        ["gh", "pr", "merge", "--auto"],
        ["gh", "pr", "merge", "--admin"],
        # argparse expands unambiguous prefixes: `--mer` would merge.
        ["pr_merge_gate", "{number}", "--mer"],
        ["pr_merge_gate", "{number}", "--request=1"],
        # `--` ends the wrapper's options; the gate still receives --merge.
        [
            "uv",
            "run",
            "--",
            "python",
            "-m",
            "tools.pr_merge_gate",
            "{number}",
            "--merge",
        ],
    ],
)
def test_gate_commands_carrying_a_mutating_flag_are_refused(argv):
    run = FakeRun([])
    with pytest.raises(sweep.ProbeError, match="read-only"):
        sweep.run_gate(Env(run=run), {"argv": argv}, 1)
    assert run.calls == []


def test_ordinary_gate_flags_are_allowed():
    assert (
        sweep.mutating_flags(["gate", "--repo", "o/r", "--policy", "x", "--", "-m"])
        == []
    )


def test_a_pr_by_head_waits_for_its_pr_then_stalls():
    run = FakeRun([(["gh", "pr", "list"], CommandResult(0, "[]", ""))])
    clock = [NOW]
    env = Env(run=run, now=lambda: clock[0])
    memory: dict = {}
    item = {
        "kind": "pr",
        "repo": "wangzitian0/infra2",
        "head": "ops/x",
        "stall_minutes": 10,
    }
    first = sweep.sweep(env, [item], memory)[0]
    assert (first.key, first.state) == ("infra2@ops/x", WAITING)
    clock[0] += 11 * MIN
    assert sweep.sweep(env, [item], memory)[0].state == STALL


def test_a_pr_by_head_prefers_the_open_pr_and_names_it():
    rows = [
        {"number": 5, "state": "CLOSED", "updatedAt": "2027-02-01T00:00:00Z"},
        {"number": 7, "state": "OPEN", "updatedAt": "2027-01-01T00:00:00Z"},
    ]
    run = FakeRun(
        [
            (["gh", "pr", "list"], CommandResult(0, json.dumps(rows), "")),
            (["gh", "pr", "view"], _view(state="MERGED")),
            (["gh", "pr", "checks"], CommandResult(0, "[]", "")),
            (["gh", "api", "graphql"], _threads()),
        ]
    )
    assert sweep.resolve_pr_number(Env(run=run), "o/r", "b") == 7
    item = {"kind": "pr", "repo": "o/r", "head": "b"}
    [status] = sweep.sweep(Env(run=run), [item], {})
    assert (status.key, status.state) == ("r@b", DONE)
    assert status.detail.startswith("#7 merged")
    assert ["gh", "pr", "view", "7"] == run.calls[-3][:4]


# --- workflow runs


def _run(status, conclusion="", created=NOW - 5 * MIN, sha="aaa"):
    return {
        "databaseId": 1,
        "status": status,
        "conclusion": conclusion,
        "headSha": sha,
        "createdAt": _iso(created),
    }


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_run("in_progress"), WAITING),
        (_run("queued", created=NOW - 50 * MIN), STALL),
        (_run("completed", "success"), DONE),
        (_run("completed", "failure"), ACTION),
        (_run("completed", "skipped"), ACTION),
        (_run("completed"), ACTION),
        (_run("teleporting"), UNKNOWN),
        (None, UNKNOWN),
    ],
)
def test_workflow_run_states(run, expected):
    assert sweep.classify_run("w", run, now=NOW, stall_minutes=45).state == expected


def test_a_branch_head_without_a_run_waits_then_stalls():
    old = _run("completed", "success", sha="old")
    fresh = sweep.classify_run(
        "w", old, now=NOW, stall_minutes=45, branch_head="new", branch_head_at=NOW - MIN
    )
    assert fresh.state == WAITING
    late = sweep.classify_run(
        "w",
        None,
        now=NOW,
        stall_minutes=45,
        branch_head="new",
        branch_head_at=NOW - 11 * MIN,
    )
    assert late.state == STALL


def test_probe_workflow_passes_filters_and_reads_the_branch_head():
    runs = json.dumps([_run("completed", "success", sha="abc")])
    commit = json.dumps(
        {"sha": "abc", "commit": {"committer": {"date": "2027-01-15T07:00:00Z"}}}
    )
    run = FakeRun(
        [
            (["gh", "run", "list"], CommandResult(0, runs, "")),
            (["gh", "api", "repos/o/r/commits/main"], CommandResult(0, commit, "")),
        ]
    )
    item = {
        "kind": "workflow",
        "repo": "o/r",
        "workflow": "CI",
        "branch": "main",
        "event": "push",
        "expect_branch_head": True,
    }
    [status] = sweep.sweep(Env(run=run, now=lambda: NOW), [item], {})
    assert (status.key, status.state) == ("r:CI@main", DONE)
    listing = run.calls[1]
    assert listing[listing.index("--branch") + 1] == "main"
    assert listing[listing.index("--event") + 1] == "push"
    # The run of the head commit is asked for, not whichever run is newest.
    assert listing[listing.index("--commit") + 1] == "abc"
    empty = FakeRun([(["gh", "run", "list"], CommandResult(0, "[]", ""))])
    no_branch = {"kind": "workflow", "repo": "o/r", "workflow": "CI"}
    assert sweep.sweep(Env(run=empty), [no_branch], {})[0].state == UNKNOWN


# --- release logs

RELEASE_DONE = """== tagging ==
  v0.0.75 pushed
== staging surface walk ==
  walk run 1 green
staging verified; promote with:
  tools/cut_release.sh v0.0.75 --prod
== done ==
"""


def _log(text, mtime=NOW - MIN, alive=None, stall=25):
    return sweep.classify_release_log(
        "rel", text, mtime, now=NOW, stall_minutes=stall, alive=alive
    )


def test_a_release_log_done_line_is_done():
    status = _log(RELEASE_DONE)
    assert status.state == DONE and "promote" in status.detail
    assert "surface walk" in status.detail


def test_a_release_failure_line_is_action():
    status = _log("== staging ==\ncut_release: staging deploy run 9: failure\n")
    assert status.state == ACTION and "run 9" in status.detail


def test_the_appended_exit_marker_wins():
    assert _log("== tagging ==\nexit=0\n").state == DONE
    failed = _log("== tagging ==\ncut_release: boom\nexit=1\n")
    assert failed.state == ACTION and "boom" in failed.detail
    assert "== tagging ==" in _log("== tagging ==\nexit=3\n").detail


def test_a_running_release_waits_then_stalls_or_dies():
    mid = "== waiting for tag ci-required ==\n"
    assert _log(mid, mtime=NOW - 19 * MIN, alive=True).state == WAITING
    assert _log(mid, mtime=NOW - 30 * MIN).state == STALL
    died = _log(mid, alive=False)
    assert died.state == STALL and "DIED" in died.detail


def test_a_missing_release_log_is_unknown():
    assert _log(None, mtime=None).state == UNKNOWN


def test_probe_release_log_reads_the_log_and_the_process(tmp_path):
    log = tmp_path / "release-v1.log"
    log.write_text("== tagging ==\n")
    by_pid = {"kind": "release_log", "path": str(log), "pid": 4242}
    env = Env(now=lambda: log.stat().st_mtime + 30, alive=lambda pid: pid != 4242)
    status = sweep.probe_one(env, by_pid, {})
    assert (status.key, status.state) == ("release:release-v1.log", STALL)
    run = FakeRun([(["pgrep"], CommandResult(0, "123\n", ""))])
    by_match = {"kind": "release_log", "path": str(log), "process_match": "cut_release"}
    env = Env(run=run, now=lambda: log.stat().st_mtime + 30)
    status = sweep.probe_one(env, by_match, {})
    assert status.state == WAITING and "process alive" in status.detail
    assert run.calls == [["pgrep", "-f", "cut_release"]]


# --- agents and worktrees


def test_an_agent_transcript_is_only_stat_ed(tmp_path):
    out = tmp_path / "a.output"
    out.write_text("transcript")
    out.chmod(0)  # unreadable: any open() would raise, stat() still works
    try:
        item = {"kind": "agent", "name": "x", "output": str(out)}
        mtime = out.stat().st_mtime
        active = sweep.probe_one(Env(now=lambda: mtime + 60), item, {})
        assert (active.key, active.state) == ("agent:x", WAITING)
        quiet = sweep.probe_one(Env(now=lambda: mtime + 30 * MIN), item, {})
        assert quiet.state == STALL and "SendMessage" in quiet.detail
    finally:
        out.chmod(stat.S_IRUSR | stat.S_IWUSR)
    missing = {"kind": "agent", "name": "x", "output": str(tmp_path / "gone")}
    assert sweep.probe_one(Env(), missing, {}).state == UNKNOWN


def _git(path: Path, *args: str) -> None:
    moment = "@1700000000 +0000"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
        "GIT_AUTHOR_DATE": moment,
        "GIT_COMMITTER_DATE": moment,
    }
    subprocess.run(["git", "-C", str(path), *args], check=True, env=env)


def test_worktree_activity_counts_commits_and_edited_files(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "work")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "x")
    committed = 1_700_000_000
    item = {"kind": "worktree", "path": str(tmp_path), "label": "wt"}
    fresh = sweep.probe_one(Env(now=lambda: committed + 5 * MIN), item, {})
    assert fresh.state == WAITING
    assert "work @" in fresh.detail and "no upstream" in fresh.detail
    later = Env(now=lambda: committed + 60 * MIN)
    assert sweep.probe_one(later, item, {}).state == STALL
    edited = tmp_path / "f.txt"
    edited.write_text("x")
    os.utime(edited, (committed + 58 * MIN, committed + 58 * MIN))
    status = sweep.probe_one(later, item, {})
    assert (status.state, "dirty=1" in status.detail) == (WAITING, True)


def test_a_vanished_or_broken_worktree_is_reported(tmp_path):
    gone = {"kind": "worktree", "path": str(tmp_path / "gone")}
    status = sweep.probe_one(Env(), gone, {})
    assert (status.key, status.state) == ("worktree:gone", ACTION)
    failing = Env(run=lambda argv, cwd: CommandResult(128, "", "not a git repository"))
    broken = sweep.probe_one(failing, {"kind": "worktree", "path": str(tmp_path)}, {})
    assert broken.state == UNKNOWN and "not a git repository" in broken.detail


def test_porcelain_paths_follow_renames_and_quotes():
    out = ' M a.py\nR  old.py -> new.py\n?? "sp ace.md"\n\n'
    assert sweep._porcelain_paths(out) == ["a.py", "new.py", "sp ace.md"]


# --- sweep, keys, exit codes


def test_unknown_kinds_are_unknown():
    [status] = sweep.sweep(Env(), [{"kind": "telepathy"}], {})
    assert status.state == UNKNOWN and "telepathy" in status.detail


def test_item_keys_are_stable_and_labels_win():
    assert sweep.item_key({"kind": "pr", "repo": "o/r", "number": 3}) == "r#3"
    assert (
        sweep.item_key({"kind": "pr", "repo": "o/r", "number": 3, "label": "L"}) == "L"
    )
    assert (
        sweep.item_key({"kind": "workflow", "repo": "o/r", "workflow": "CI"}) == "r:CI"
    )


def _status(key, state, fingerprint=("x",)):
    return Status(key, state, f"{key} is {state}", fingerprint)


def test_exit_code_precedence():
    code = sweep.exit_code
    assert (
        code([_status("a", ACTION), _status("b", STALL)], watching=False)
        == sweep.EXIT_STALL
    )
    assert (
        code([_status("a", STALL), _status("b", UNKNOWN)], watching=False)
        == sweep.EXIT_UNKNOWN
    )
    assert (
        code([_status("a", WAITING), _status("b", DONE)], watching=False)
        == sweep.EXIT_OK
    )
    assert (
        code([_status("a", WAITING), _status("b", DONE)], watching=True)
        == sweep.EXIT_PROGRESS
    )
    assert code([_status("a", DONE)], watching=True) == sweep.EXIT_OK
    assert code([_status("a", ACTION)], watching=True) == sweep.EXIT_ACTION
    assert set(sweep.EXIT_CODES) == set(range(6))


# --- watch


def _scripted(monkeypatch, frames):
    clock = [NOW]
    frames = iter(frames)
    monkeypatch.setattr(sweep, "sweep", lambda env, items, memory: next(frames))

    def sleep(seconds):
        clock[0] += seconds

    return Env(now=lambda: clock[0]), sleep


def _watch(env, sleep, lines, **overrides):
    options = dict(interval=60, heartbeat=240, max_minutes=0, unknown_tolerance=1)
    options.update(overrides)
    return sweep.watch(env, [{}], emit=lines.append, sleep=sleep, **options)


def test_a_quiet_watch_heartbeats_then_exits_on_the_first_action(monkeypatch):
    waiting = [_status("pr", WAITING)]
    env, sleep = _scripted(
        monkeypatch, [waiting] * 7 + [[_status("pr", ACTION, ("y",))]]
    )
    lines: list[str] = []
    assert _watch(env, sleep, lines) == sweep.EXIT_ACTION
    assert len(lines) == 4  # initial state, one heartbeat at 4 min, transition, exit
    assert "heartbeat: 1 waiting, 0 unknown" in lines[1]
    assert "WAITING->ACTION" in lines[2]
    assert "exit 1 (an item needs action)" in lines[3]


def test_unknown_is_tolerated_once_then_ends_the_watch(monkeypatch):
    unknown = [_status("pr", UNKNOWN)]
    env, sleep = _scripted(monkeypatch, [unknown, unknown])
    assert _watch(env, sleep, []) == sweep.EXIT_UNKNOWN


def test_a_recovered_unknown_resets_the_tolerance(monkeypatch):
    frames = [
        [_status("pr", UNKNOWN)],
        [_status("pr", WAITING)],
        [_status("pr", UNKNOWN)],
        [_status("pr", DONE)],
    ]
    env, sleep = _scripted(monkeypatch, frames)
    lines: list[str] = []
    assert _watch(env, sleep, lines) == sweep.EXIT_OK
    assert any("UNKNOWN->WAITING" in line for line in lines)


def test_done_while_others_wait_is_progress(monkeypatch):
    env, sleep = _scripted(monkeypatch, [[_status("a", WAITING), _status("b", DONE)]])
    lines: list[str] = []
    assert _watch(env, sleep, lines) == sweep.EXIT_PROGRESS
    assert "1 waiting, 1 need a look" in lines[-1]


def test_the_watch_budget_ends_the_watch(monkeypatch):
    env, sleep = _scripted(monkeypatch, [[_status("a", WAITING)]] * 10)
    lines: list[str] = []
    assert _watch(env, sleep, lines, max_minutes=3) == sweep.EXIT_BUDGET
    assert "re-arm" in lines[-1]


# --- watch list


def test_load_items_accepts_an_object_or_a_list(tmp_path):
    wrapped = tmp_path / "a.json"
    wrapped.write_text(json.dumps({"items": [{"kind": "pr"}]}))
    bare = tmp_path / "b.json"
    bare.write_text(json.dumps([{"kind": "agent"}]))
    assert sweep.load_items(wrapped) == [{"kind": "pr"}]
    assert sweep.load_items(bare) == [{"kind": "agent"}]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot read"),
        ("{not json", "cannot read"),
        ('{"items": []}', "lists no items"),
        ('{"other": 1}', "lists no items"),
        ('{"items": [1]}', "not objects"),
    ],
)
def test_load_items_rejects_unusable_lists(tmp_path, content, message):
    path = tmp_path / "watch.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(sweep.SweepConfigError, match=message):
        sweep.load_items(path)


# --- CLI


def _agent_list(tmp_path: Path) -> Path:
    output = tmp_path / "agent.output"
    output.write_text("")
    watch = tmp_path / "watch.json"
    item = {"kind": "agent", "name": "a1", "output": str(output)}
    watch.write_text(json.dumps({"items": [item]}))
    return watch


def test_cli_one_shot_prints_one_line_per_item_and_a_verdict(tmp_path, capsys):
    assert harness.main(["sweep", str(_agent_list(tmp_path))]) == sweep.EXIT_OK
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("WAITING agent:a1: active")
    assert out[-1].startswith("sweep: exit 0")


def test_cli_watch_honours_its_budget(tmp_path, capsys):
    watch = _agent_list(tmp_path)
    code = harness.main(
        ["sweep", str(watch), "--watch", "--interval", "0", "--max-minutes", "0.00001"]
    )
    assert code == sweep.EXIT_BUDGET
    assert "exit 5" in capsys.readouterr().out.splitlines()[-1]


def test_cli_errors_exit_4_never_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as usage:
        harness.main(["sweep", "--bogus"])
    assert usage.value.code == sweep.EXIT_UNKNOWN
    assert harness.main(["sweep", str(tmp_path / "missing.json")]) == sweep.EXIT_UNKNOWN
    assert "cannot read watch list" in capsys.readouterr().err


def test_cli_help_lists_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as shown:
        harness.main(["sweep", "--help"])
    assert shown.value.code == 0
    out = capsys.readouterr().out
    assert "exit codes:" in out and "5  the watch budget" in out
    with pytest.raises(SystemExit):
        harness.main(["--help"])
    assert "sweep" in capsys.readouterr().out
