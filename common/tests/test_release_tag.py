"""The release tag tool (#1644 Q3): at most one release tag in 30 minutes, cut on ``origin/main``."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from common.deploy import release_tag
from common.deploy.release_tag import Tag

MAIN = "a" * 40
OLD = "b" * 40
TAG_OBJECT = "d" * 40
TAGS = {"v3.2.9": Tag("c" * 40, "c" * 40), "v3.2.59": Tag(TAG_OBJECT, OLD)}
MADE = datetime(2026, 11, 27, 8, 13, 13, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 27, 8, 17, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 27, 8, 43, 13, tzinfo=timezone.utc)
COMMITTED = datetime(2026, 11, 27, 7, 50, tzinfo=timezone.utc)


def _made(name: str) -> datetime:
    assert name == "v3.2.59"
    return MADE


def test_a_tag_waits_30_minutes_after_the_newest_tag() -> None:
    """Release v3.2.60 was made 4 minutes after v3.2.59 and got the verdict of that train."""
    decision = release_tag.decide(MAIN, TAGS, _made, NOW)
    assert decision == release_tag.Decision(
        "wait",
        "v3.2.59 was made at 2026-11-27T08:13Z; the next tag is free at 2026-11-27T08:43Z",
    )
    assert release_tag.decide(MAIN, TAGS, _made, LATER) == release_tag.Decision(
        "cut", "origin/main is ahead of v3.2.59", tag="v3.2.60", sha=MAIN
    )


def test_no_tag_and_no_time_read_when_the_newest_tag_is_the_head_of_main() -> None:
    def unread(_name: str) -> datetime:
        raise AssertionError("the tag time is not needed")

    decision = release_tag.decide(OLD, TAGS, unread, NOW)
    assert decision == release_tag.Decision(
        "none", "v3.2.59 is the head of origin/main"
    )


class _Git:
    """Answer the read commands of the tool; record each command that changes a ref."""

    def __init__(
        self, *, fail: str = "", annotated: bool = True, made: datetime | None = None
    ) -> None:
        self.fail, self.annotated, self.made = fail, annotated, made
        self.read: list[list[str]] = []
        self.commands: list[list[str]] = []
        self.cwds: set[object] = set()

    def __call__(self, argv, **kwargs):
        assert argv[0] == "git" and kwargs.get("timeout")
        self.cwds.add(kwargs.get("cwd"))
        out, code = "", 1 if argv[1] == self.fail else 0
        if argv[1] == "ls-remote":
            # v3.2.59 is an annotated tag: its commit is on the peeled row.
            out = (
                f"{MAIN}\trefs/heads/main\n"
                f"{TAG_OBJECT}\trefs/tags/v3.2.59\n"
                f"{OLD}\trefs/tags/v3.2.59^{{}}\n"
                f"{MAIN}\trefs/tags/v4.14.2-canary\n"
            )
        elif argv[1] == "cat-file":
            stamp = int((self.made or MADE).timestamp())
            if argv[-1] == TAG_OBJECT and self.annotated:
                out = (
                    f"object {OLD}\ntype commit\ntag v3.2.59\n"
                    f"tagger t t <t@example.com> {stamp} +0800\n\nRelease\n"
                )
            else:
                # A commit: the author time is another time and is not the answer.
                out = (
                    f"tree {'e' * 40}\nparent {'f' * 40}\n"
                    f"author a a <a@example.com> 1700000000 +0000\n"
                    f"committer c c <c@example.com> {int(COMMITTED.timestamp())} +0000\n"
                    f"gpgsig -----BEGIN\n\ncommitter x <x@example.com> 1 +0000\n"
                )
        if argv[1] in ("ls-remote", "cat-file") or (
            argv[1] == "fetch" and argv[-1].startswith("refs/tags/")
        ):
            self.read.append(argv)
        else:
            self.commands.append(argv)
        return subprocess.CompletedProcess(argv, code, out, "refused" if code else "")


def _main(git: _Git, tmp_path, *argv: str, now: datetime = LATER) -> int:
    return release_tag.main(list(argv), root=tmp_path, runner=git, now=lambda: now)


def test_the_tool_reads_origin_with_git_and_the_time_of_the_tag_object(
    tmp_path, capsys
) -> None:
    git = _Git()
    assert _main(git, tmp_path) == 0
    assert git.read == [
        ["git", "ls-remote", "origin", "refs/heads/main", "refs/tags/v*"],
        ["git", "fetch", "--quiet", "--no-tags", "origin", "refs/tags/v3.2.59"],
        ["git", "cat-file", "-p", TAG_OBJECT],
    ]
    assert git.commands == []
    assert git.cwds == {tmp_path}
    assert "release tag: cut v3.2.60 at aaaaaaaa" in capsys.readouterr().out
    assert release_tag.remote_refs(tmp_path, git) == (
        MAIN,
        {"v3.2.59": Tag(TAG_OBJECT, OLD)},
    )


def test_a_lightweight_tag_uses_the_committer_time_of_its_commit(tmp_path) -> None:
    git = _Git(annotated=False)
    light = Tag(OLD, OLD)
    assert release_tag.tag_time("v3.2.59", light, tmp_path, git, LATER) == COMMITTED


def test_a_tagger_time_in_the_future_uses_the_commit_time(tmp_path) -> None:
    """A wrong clock on the tagging machine must not hold each later tag for days."""
    ahead = datetime(2026, 11, 29, 8, 0, tzinfo=timezone.utc)
    tag = TAGS["v3.2.59"]
    late = _Git(made=ahead)
    assert release_tag.tag_time("v3.2.59", tag, tmp_path, late, LATER) == COMMITTED
    assert release_tag.tag_time("v3.2.59", tag, tmp_path, _Git(), LATER) == MADE


def test_push_tags_the_commit_of_origin_main(tmp_path, capsys) -> None:
    git = _Git()
    assert _main(git, tmp_path, "--push", "-m", "Release notes") == 0
    assert git.commands == [
        ["git", "fetch", "--quiet", "--no-tags", "origin", MAIN],
        ["git", "tag", "-a", "v3.2.60", "-m", "Release notes", MAIN],
        ["git", "push", "origin", "refs/tags/v3.2.60"],
    ]
    assert git.cwds == {tmp_path}
    assert "release tag: pushed v3.2.60 at aaaaaaaa" in capsys.readouterr().out


def test_wait_exits_1_and_pushes_nothing(tmp_path, capsys) -> None:
    git = _Git()
    assert _main(git, tmp_path, "--push", now=NOW) == 1
    assert git.commands == []
    assert "release tag: wait: v3.2.59 was made at" in capsys.readouterr().out


def test_a_failed_push_exits_4_and_deletes_the_tag_of_this_run(
    tmp_path, capsys
) -> None:
    git = _Git(fail="push")
    assert _main(git, tmp_path, "--push") == 4
    assert git.commands[-1] == ["git", "tag", "-d", "v3.2.60"]
    out = capsys.readouterr().out
    assert "release tag: failed v3.2.60: git push failed: refused" in out
    assert "pushed" not in out


def test_a_failed_tag_deletes_no_tag(tmp_path, capsys) -> None:
    """The name exists in this checkout: this run made no tag, so it deletes none."""
    git = _Git(fail="tag")
    assert _main(git, tmp_path, "--push") == 4
    assert [argv[1] for argv in git.commands] == ["fetch", "tag"]
    assert "release tag: failed v3.2.60: git tag failed" in capsys.readouterr().out


def test_an_interrupt_in_the_push_deletes_the_tag_of_this_run(tmp_path) -> None:
    class _Interrupted(_Git):
        def __call__(self, argv, **kwargs):
            if argv[1] == "push":
                self.commands.append(argv)
                raise KeyboardInterrupt
            return super().__call__(argv, **kwargs)

    git = _Interrupted()
    with pytest.raises(KeyboardInterrupt):
        _main(git, tmp_path, "--push")
    assert git.commands[-1] == ["git", "tag", "-d", "v3.2.60"]
    # SIGTERM takes the same path: the handler of main() raises SystemExit.
    with pytest.raises(SystemExit) as stopped:
        release_tag._terminate(15, None)
    assert stopped.value.code == 143


@pytest.mark.parametrize("source", ["ls-remote", "cat-file"])
def test_an_unreadable_source_exits_4_and_pushes_nothing(
    tmp_path, capsys, source
) -> None:
    git = _Git(fail=source)
    assert _main(git, tmp_path, "--push") == 4
    assert git.commands == []
    assert f"release tag: unreadable: git {source} failed" in capsys.readouterr().out
