"""Cut the next infra2 release tag on ``origin/main``, at most one tag in 30 minutes (#1644 Q3).

A release tag starts the Staging train. On 2026-11-27 the repository got 23 release tags in
9.5 hours, one tag after each fix. Tag v3.2.60 was made 4 minutes after v3.2.59, waited
behind it, and was refused for the red Canary check of v3.2.59. This tool cuts no tag:

* when the newest release tag is the head of ``origin/main``;
* in the 30 minutes after the newest release tag was made.

The longest tag train of that day took 26 minutes (v3.2.62). A train that takes more than
30 minutes makes the next tag wait behind it in the concurrency group of the train
workflow. The pull requests that merge in the 30 minutes ride one tag.

The tool reads git only. It lists no workflow run: a run listing is eventually consistent,
and the code of the release train must not decide from it (#970, #1646,
``test_guard_lists_no_runs``). It does not see a dispatch replay; the concurrency group of
the train workflow puts a tag behind that run.

Without ``--push`` the tool prints the decision and changes nothing. Exit code 0: ``cut`` or
``none``. Exit code 1: ``wait``. Exit code 4: a source is not readable, or the tag or the
push failed.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.core.constants import REPO_ROOT
from common.deploy.release_markers import RELEASE_TAG_RE, version_key

MIN_INTERVAL = timedelta(minutes=30)
#: A tagger time later than the local clock by more than this is a wrong clock.
CLOCK_SKEW = timedelta(minutes=5)
ORIGIN = "origin"
EXIT_OK, EXIT_WAIT, EXIT_UNREADABLE = 0, 1, 4

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Decision:
    """``action`` is ``cut``, ``wait`` or ``none``; ``tag`` and ``sha`` are set for ``cut``."""

    action: str
    reason: str
    tag: str = ""
    sha: str = ""


@dataclass(frozen=True)
class Tag:
    """A release tag of ``origin``: its own object and the commit that it names."""

    obj: str
    commit: str


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _git(runner: Runner, root: Path, *argv: str, timeout: int = 60) -> str:
    """The stdout of one git command. ``RuntimeError`` on a non-zero exit."""
    done = runner(
        ["git", *argv], cwd=root, capture_output=True, text=True, timeout=timeout
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {argv[0]} failed: {(done.stderr or '').strip()}")
    return done.stdout or ""


def decide(
    main_sha: str,
    tags: Mapping[str, Tag],
    made_at: Callable[[str], datetime],
    now: datetime,
) -> Decision:
    """The decision from the head of ``origin/main`` and the release tags of ``origin``.

    ``made_at`` gives the time when a tag was made. It is called for the newest tag, and
    only when ``origin/main`` is ahead of it.
    """
    if not main_sha or not tags:
        raise ValueError("origin has no main branch or no release tag")
    newest = max(tags, key=version_key)
    if tags[newest].commit == main_sha:
        return Decision("none", f"{newest} is the head of origin/main")
    made = made_at(newest)
    free = made + MIN_INTERVAL
    if now < free:
        return Decision(
            "wait",
            f"{newest} was made at {_utc(made)}; the next tag is free at {_utc(free)}",
        )
    major, minor, patch = version_key(newest)
    return Decision(
        "cut",
        f"origin/main is ahead of {newest}",
        tag=f"v{major}.{minor}.{patch + 1}",
        sha=main_sha,
    )


def remote_refs(
    root: Path, runner: Runner = subprocess.run
) -> tuple[str, dict[str, Tag]]:
    """``(main commit, {release tag: Tag})`` of ``origin``, from one ``git ls-remote``.

    An annotated tag has a peeled row ``<tag>^{}``; that row holds its commit.
    """
    out = _git(runner, root, "ls-remote", ORIGIN, "refs/heads/main", "refs/tags/v*")
    refs = dict(reversed(line.split()) for line in out.splitlines() if line)
    tags = {
        name: Tag(obj=sha, commit=refs.get(f"refs/tags/{name}^{{}}", sha))
        for ref, sha in refs.items()
        if RELEASE_TAG_RE.fullmatch(name := ref.removeprefix("refs/tags/"))
    }
    return refs.get("refs/heads/main", ""), tags


def _header_time(text: str, key: str) -> datetime | None:
    """The time of the ``key`` line (``tagger`` or ``committer``) in the header of an object."""
    for line in text.splitlines():
        if not line:
            return None
        if line.startswith(f"{key} "):
            return datetime.fromtimestamp(int(line.split()[-2]), tz=timezone.utc)
    return None


def tag_time(
    name: str, tag: Tag, root: Path, runner: Runner, now: datetime
) -> datetime:
    """When the tag was made: the ``tagger`` time of an annotated tag.

    The ``committer`` time of the commit is used in two cases. A lightweight tag has no time
    of its own. A tagger time in the future is a wrong clock on the tagging machine; without
    this case the tool waits until that time. The commit time is earlier than the push, so
    the interval is then shorter than 30 minutes.
    """
    _git(runner, root, "fetch", "--quiet", "--no-tags", ORIGIN, f"refs/tags/{name}")
    made = _header_time(_git(runner, root, "cat-file", "-p", tag.obj), "tagger")
    if made is None or made > now + CLOCK_SKEW:
        made = _header_time(
            _git(runner, root, "cat-file", "-p", tag.commit), "committer"
        )
    if made is None:
        raise ValueError(f"the object of {name} holds no tagger or committer time")
    return made


def cut(decision: Decision, message: str, root: Path, runner: Runner) -> None:
    """Create the annotated tag on the commit of ``origin/main`` and push it.

    A failed push deletes the local tag of this run, so the next run can make it again.
    """
    _git(runner, root, "fetch", "--quiet", "--no-tags", ORIGIN, decision.sha)
    _git(runner, root, "tag", "-a", decision.tag, "-m", message, decision.sha)
    try:
        _git(runner, root, "push", ORIGIN, f"refs/tags/{decision.tag}", timeout=120)
    except (
        BaseException
    ):  # also an interrupt: a local tag left behind stops each later run
        runner(
            ["git", "tag", "-d", decision.tag],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        raise


def _terminate(signum: int, _frame: object) -> None:
    """SIGTERM as an exception, so ``cut`` deletes the tag of this run before the exit."""
    raise SystemExit(128 + signum)


def main(
    argv: list[str] | None = None,
    *,
    root: Path = REPO_ROOT,
    runner: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--push", action="store_true", help="create and push the tag")
    parser.add_argument("-m", "--message", default="", help="the tag message")
    args = parser.parse_args(argv)
    moment = now()
    try:
        main_sha, tags = remote_refs(root, runner)
        decision = decide(
            main_sha,
            tags,
            lambda name: tag_time(name, tags[name], root, runner, moment),
            moment,
        )
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"release tag: unreadable: {exc}")
        return EXIT_UNREADABLE
    if decision.action == "cut" and args.push:
        previous = signal.signal(signal.SIGTERM, _terminate)
        try:
            cut(decision, args.message or f"Release {decision.tag}", root, runner)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"release tag: failed {decision.tag}: {exc}")
            return EXIT_UNREADABLE
        finally:
            signal.signal(signal.SIGTERM, previous)
    done = "pushed" if decision.action == "cut" and args.push else decision.action
    target = f" {decision.tag} at {decision.sha[:8]}" if decision.tag else ""
    print(f"release tag: {done}{target}: {decision.reason}")
    return EXIT_WAIT if decision.action == "wait" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
