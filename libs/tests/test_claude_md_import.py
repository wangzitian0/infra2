"""CLAUDE.md must be committed, be a real file, and import AGENTS.md for real.

AGENTS.md called this pairing 承重件 and named `ws-agents-lint` as its guard.
That lint existed nowhere, and CLAUDE.md itself was never committed: it lived
only as an untracked symlink in one working tree, listed in .git/info/exclude,
a machine-local file that is never cloned. On any fresh clone, in CI, or in a
worktree, Claude Code found no instructions at all.

The first version of this guard reproduced the defect it was written to fix,
which a blind audit demonstrated three ways. `"@AGENTS.md" in body` is a
substring test, and the memory docs define two constructs where that exact
substring imports nothing -- "Import parsing skips Markdown code spans and
fenced code blocks" and block-level HTML comments "are stripped before the
content is injected". A CLAUDE.md of ```@AGENTS.md```, or <!-- @AGENTS.md -->,
or See `@AGENTS.md`. loaded zero rules and passed all three assertions.

So the import is checked as a line, after fences, comments and code spans are
removed -- the same things the loader removes. And the file must be exactly
that line: a CLAUDE.md wins over AGENTS.md in precedence and is injected
verbatim, so anything else in it is a live rule that can contradict the
constitution. Nineteen lines of "ignore AGENTS.md rule n" fit under the old
line ceiling. Wanting Claude-specific content later is fine; changing this test
is then the deliberate decision, which is the point.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
IMPORT_LINE = "@AGENTS.md"
FENCE_RE = re.compile(r"^\s*(```|~~~)", re.M)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
CODE_SPAN_RE = re.compile(r"`+[^`\n]*`+")
# A backticked repository path -- a slash is required, so that a bare filename
# used as shorthand (AGENTS.md says `MANIFEST.yaml` meaning docs/ssot/…) is not
# mistaken for a broken reference. A guard is always named with its path.
BACKTICKED_PATH_RE = re.compile(
    r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:py|sh|yaml|yml|md|json))`"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True
    )


def _importable_lines(body: str) -> list[str]:
    """`body` with everything the loader ignores removed, split into lines."""
    body = HTML_COMMENT_RE.sub("", body)
    out, fenced = [], False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        out.append(CODE_SPAN_RE.sub("", line).strip())
    return out


def test_claude_md_is_committed_not_a_local_convenience():
    # HEAD, not the index: "is committed" is the claim, and `git ls-files`
    # would pass on a bare `git add` in a tree whose HEAD has no such file.
    assert _git("cat-file", "-e", "HEAD:CLAUDE.md").returncode == 0, (
        "CLAUDE.md is not committed. An untracked symlink works only in the tree "
        "that has it; every fresh clone, CI job and worktree then starts with no "
        "project instructions at all."
    )


def test_claude_md_is_a_regular_file_not_a_symlink():
    # Mode 120000 is a symlink. git checks one out as a one-line text file on
    # Windows unless core.symlinks is set, which yields a CLAUDE.md whose whole
    # content is a path -- importing nothing. CI runs on Linux, so a symlink
    # would pass every other assertion here and fail only where nobody looks.
    mode = _git("ls-files", "-s", "CLAUDE.md").stdout.split()[:1]
    assert mode == ["100644"], (
        f"CLAUDE.md is committed with mode {mode or ['(absent)']}, not 100644. "
        "Use an import, not a symlink."
    )


def test_claude_md_is_exactly_the_import():
    lines = [
        ln
        for ln in _importable_lines((ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
        if ln
    ]
    assert lines == [IMPORT_LINE], (
        f"CLAUDE.md must be exactly {IMPORT_LINE!r} once code fences, HTML "
        f"comments and code spans are removed -- the loader removes them too, so "
        f"an import inside any of them imports nothing. Found: {lines!r}"
    )


def test_agents_md_names_no_guard_that_does_not_exist():
    # The original defect was a sentence naming `ws-agents-lint`, which existed
    # nowhere. Pinning that one spelling would be a spelling check: any rename
    # defeats it, and the replacement sentence in AGENTS.md names this very
    # file, recreating the same claim. So every backticked repository path in
    # AGENTS.md must resolve.
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    missing = sorted(
        {p for p in BACKTICKED_PATH_RE.findall(agents) if not (ROOT / p).exists()}
    )
    assert not missing, (
        "AGENTS.md names paths that do not exist: "
        + ", ".join(missing)
        + ". A guard that is only a name reads as covered and is not."
    )
