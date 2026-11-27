"""The release tag tool (#1644 Q3): one tag for each free train, cut on ``origin/main``."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from common.deploy import release_tag

NOW = datetime(2026, 11, 27, 8, 17, tzinfo=timezone.utc)
MAIN = "a" * 40
OLD = "b" * 40
TAGS = {"v3.2.9": "c" * 40, "v3.2.59": OLD}


def _run(tag: str, status: str, started: str) -> dict:
    return {
        "head_branch": tag,
        "status": status,
        "run_started_at": started,
        "html_url": f"https://example.test/{tag}",
    }


def test_a_tag_waits_while_a_tag_train_runs() -> None:
    """Release v3.2.60 was pushed 4 minutes after v3.2.59 and got the verdict of that train."""
    runs = [_run("v3.2.59", "in_progress", "2026-11-27T08:13:13Z")]
    decision = release_tag.decide(MAIN, TAGS, runs, NOW)
    assert decision.action == "wait"
    assert "the train of v3.2.59 is in_progress" in decision.reason


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


def test_no_tag_when_the_newest_tag_is_the_head_of_main() -> None:
    decision = release_tag.decide(OLD, TAGS, [], NOW)
    assert decision == release_tag.Decision(
        "none", "v3.2.59 is the head of origin/main"
    )


class _Git:
    """Answer ``git ls-remote`` and ``gh api``; record each other command."""

    def __init__(self, runs: list[dict]) -> None:
        self.runs, self.commands = runs, []

    def __call__(self, argv, **_kwargs):
        out = ""
        if argv[:2] == ["git", "ls-remote"]:
            out = f"{MAIN}\trefs/heads/main\n{OLD}\trefs/tags/v3.2.59\n"
        elif argv[:2] == ["gh", "api"]:
            assert "event=push" in argv[2]
            out = json.dumps({"workflow_runs": self.runs})
        else:
            self.commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_push_tags_the_commit_of_origin_main_and_a_plain_run_changes_nothing(
    tmp_path, capsys
) -> None:
    git = _Git([_run("v3.2.59", "completed", "2026-11-27T07:00:00Z")])
    assert release_tag.main([], root=tmp_path, runner=git, now=lambda: NOW) == 0
    assert git.commands == []
    assert "release tag: cut v3.2.60 at aaaaaaaa" in capsys.readouterr().out

    assert release_tag.main(["--push"], root=tmp_path, runner=git, now=lambda: NOW) == 0
    assert git.commands == [
        ["git", "fetch", "--quiet", "origin", MAIN],
        ["git", "tag", "-a", "v3.2.60", "-m", "Release v3.2.60", MAIN],
        ["git", "push", "origin", "refs/tags/v3.2.60"],
    ]
    assert "release tag: pushed v3.2.60 at aaaaaaaa" in capsys.readouterr().out


def test_wait_exits_1_and_pushes_nothing(tmp_path, capsys) -> None:
    git = _Git([_run("v3.2.59", "queued", "2026-11-27T08:13:13Z")])
    assert release_tag.main(["--push"], root=tmp_path, runner=git, now=lambda: NOW) == 1
    assert git.commands == []
    assert (
        "release tag: wait: the train of v3.2.59 is queued" in capsys.readouterr().out
    )
