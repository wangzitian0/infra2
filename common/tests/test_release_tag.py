"""The release tag tool (#1644 Q3): one tag for each free train, cut on ``origin/main``."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from common.deploy import release_tag

NOW = datetime(2026, 11, 27, 8, 17, tzinfo=timezone.utc)
MAIN = "a" * 40
OLD = "b" * 40
TAG_OBJECT = "d" * 40
TAGS = {"v3.2.9": "c" * 40, "v3.2.59": OLD}
DONE = "2026-11-27T07:00:00Z"


def _run(
    tag: str, status: str, started: str, *, event: str = "push", title: str = ""
) -> dict:
    return {
        "id": f"{event}-{tag}-{started}",
        "event": event,
        "head_branch": tag,
        "status": status,
        "run_started_at": started,
        "display_title": title or f"Reconcile IaC Inputs (staging {tag})",
        "html_url": f"https://example.test/{tag}",
    }


def test_a_tag_waits_while_a_tag_train_runs() -> None:
    """Release v3.2.60 was pushed 4 minutes after v3.2.59 and got the verdict of that train."""
    runs = [_run("v3.2.59", "in_progress", "2026-11-27T08:13:13Z")]
    decision = release_tag.decide(MAIN, TAGS, runs, NOW)
    assert decision.action == "wait"
    assert "the push run of v3.2.59 is in_progress" in decision.reason


def test_a_dispatch_run_holds_the_tag_and_a_production_run_does_not() -> None:
    """Release v3.2.61 was pushed 51 seconds after the start of a dispatch replay of v3.2.60."""
    train = _run("v3.2.59", "completed", DONE)
    replay = _run("main", "waiting", "2026-11-27T08:16:00Z", event="workflow_dispatch")
    decision = release_tag.decide(MAIN, TAGS, [train, replay], NOW)
    assert decision.action == "wait"
    assert "the workflow_dispatch run of main is waiting" in decision.reason
    promotion = replay | {"display_title": "Reconcile IaC Inputs (PROD v3.2.59)"}
    assert release_tag.decide(MAIN, TAGS, [train, promotion], NOW).action == "cut"


def test_a_tag_waits_30_minutes_after_the_start_of_the_newest_train() -> None:
    runs = [
        _run("v3.2.58", "completed", "2026-11-27T07:18:39Z"),
        _run("v3.2.59", "completed", "2026-11-27T08:13:13Z"),
    ]
    decision = release_tag.decide(MAIN, TAGS, runs, NOW)
    assert (decision.action, decision.tag) == ("wait", "")
    assert "the next tag is free at 2026-11-27T08:43Z" in decision.reason
    later = datetime(2026, 11, 27, 8, 43, 13, tzinfo=timezone.utc)
    assert release_tag.decide(MAIN, TAGS, runs, later) == release_tag.Decision(
        "cut", "origin/main is ahead of v3.2.59", tag="v3.2.60", sha=MAIN
    )


def test_a_tag_waits_when_the_train_of_the_newest_tag_is_not_listed() -> None:
    for runs in ([], [_run("v3.2.58", "completed", DONE)]):
        decision = release_tag.decide(MAIN, TAGS, runs, NOW)
        assert decision == release_tag.Decision(
            "wait", "the train of v3.2.59 is not in the run list"
        )


def test_no_tag_when_the_newest_tag_is_the_head_of_main() -> None:
    decision = release_tag.decide(OLD, TAGS, [], NOW)
    assert decision == release_tag.Decision(
        "none", "v3.2.59 is the head of origin/main"
    )


class _Git:
    """Answer ``git ls-remote`` and ``gh api``; record each other git command and its cwd."""

    def __init__(self, runs: list[dict], *, fail: str = "") -> None:
        self.runs, self.fail = runs, fail
        self.commands: list[list[str]] = []
        self.cwds: set[object] = set()

    def __call__(self, argv, **kwargs):
        out, code = "", 0
        if argv[0] == "git":
            self.cwds.add(kwargs.get("cwd"))
            assert kwargs.get("timeout")
        if argv[:2] == ["git", "ls-remote"]:
            # v3.2.59 is an annotated tag: its commit is on the peeled row.
            out = (
                f"{MAIN}\trefs/heads/main\n"
                f"{TAG_OBJECT}\trefs/tags/v3.2.59\n"
                f"{OLD}\trefs/tags/v3.2.59^{{}}\n"
                f"{MAIN}\trefs/tags/v4.14.2-canary\n"
            )
        elif argv[:2] == ["gh", "api"]:
            out = json.dumps({"workflow_runs": self.runs})
        else:
            self.commands.append(argv)
            code = 1 if argv[1] == self.fail else 0
        return subprocess.CompletedProcess(argv, code, out, "refused" if code else "")


def _main(git: _Git, tmp_path, *argv: str) -> int:
    return release_tag.main(list(argv), root=tmp_path, runner=git, now=lambda: NOW)


def test_push_tags_the_commit_of_origin_main_and_a_plain_run_changes_nothing(
    tmp_path, capsys
) -> None:
    git = _Git([_run("v3.2.59", "completed", DONE)])
    assert _main(git, tmp_path) == 0
    assert git.commands == []
    assert "release tag: cut v3.2.60 at aaaaaaaa" in capsys.readouterr().out

    assert _main(git, tmp_path, "--push") == 0
    assert git.commands == [
        ["git", "fetch", "--quiet", "origin", MAIN],
        ["git", "tag", "-a", "v3.2.60", "-m", "Release v3.2.60", MAIN],
        ["git", "push", "origin", "refs/tags/v3.2.60"],
    ]
    assert git.cwds == {tmp_path}
    assert "release tag: pushed v3.2.60 at aaaaaaaa" in capsys.readouterr().out


def test_the_peeled_commit_of_an_annotated_tag_gives_none(tmp_path, capsys) -> None:
    git = _Git([_run("v3.2.59", "completed", DONE)])
    main_sha, tags = release_tag.remote_refs(tmp_path, git)
    assert (main_sha, tags) == (MAIN, {"v3.2.59": OLD})
    assert release_tag.decide(OLD, tags, [], NOW).action == "none"


def test_a_failed_push_exits_4_and_deletes_the_tag_of_this_run(
    tmp_path, capsys
) -> None:
    git = _Git([_run("v3.2.59", "completed", DONE)], fail="push")
    assert _main(git, tmp_path, "--push") == 4
    assert git.commands[-1] == ["git", "tag", "-d", "v3.2.60"]
    out = capsys.readouterr().out
    assert "release tag: failed v3.2.60: git push failed: refused" in out
    assert "pushed" not in out


def test_wait_exits_1_and_pushes_nothing(tmp_path, capsys) -> None:
    git = _Git([_run("v3.2.59", "queued", "2026-11-27T08:13:13Z")])
    assert _main(git, tmp_path, "--push") == 1
    assert git.commands == []
    assert (
        "release tag: wait: the push run of v3.2.59 is queued"
        in capsys.readouterr().out
    )


def test_an_unreadable_run_list_exits_4(tmp_path, capsys) -> None:
    git = _Git([_run("v3.2.59", "completed", "")])
    assert _main(git, tmp_path, "--push") == 4
    assert git.commands == []
    assert "release tag: unreadable: a tag train" in capsys.readouterr().out
