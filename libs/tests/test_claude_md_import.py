"""CLAUDE.md must be committed and must import AGENTS.md.

AGENTS.md called this pairing 承重件 and named `ws-agents-lint` as its guard.
That lint existed nowhere -- not in this repository, not on PATH -- and
CLAUDE.md itself was never committed: it lived only as an untracked symlink in
one developer's working tree, listed in .git/info/exclude, which is a
machine-local file that is never cloned.

So on any fresh clone, in CI, or in a git worktree, Claude Code found no
instructions at all. That does not break one rule; it unloads the rulebook.

Claude Code has read AGENTS.md directly since v2.1.277, but that does not make
this redundant: when a CLAUDE.md exists in the working directory or ANY ancestor
directory, only the CLAUDE.md files are read. This workspace has one at
~/zitian/CLAUDE.md, so AGENTS.md would stay unread there whatever this
repository does. An import is also what the documentation recommends over a
symlink, which git checks out as a one-line text file on Windows unless
core.symlinks is set, and which the Edit and Write tools refuse to write
through.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
IMPORT_LINE = "@AGENTS.md"


def _tracked(path: str) -> bool:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", path],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0


def test_claude_md_is_committed_not_a_local_convenience():
    assert _tracked("CLAUDE.md"), (
        "CLAUDE.md is not tracked. An untracked symlink works only in the tree "
        "that has it; every fresh clone, CI job and worktree then starts with no "
        "project instructions at all."
    )


def test_claude_md_imports_agents_md_rather_than_copying_it():
    body = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert IMPORT_LINE in body, (
        f"CLAUDE.md must contain {IMPORT_LINE!r} so AGENTS.md stays the single "
        "source. A copy drifts; an import cannot."
    )
    # A copy would be long. The import file has no reason to carry rules of its
    # own -- anything Claude-specific still belongs after the import line, so a
    # generous ceiling still catches a wholesale duplication of AGENTS.md.
    assert len(body.splitlines()) <= 20, (
        "CLAUDE.md has grown beyond an import plus a short Claude-specific note; "
        "rules belong in AGENTS.md."
    )


def test_agents_md_does_not_name_a_guard_that_does_not_exist():
    # The previous text named `ws-agents-lint`, which existed nowhere. A guard
    # that is only a name is worse than none: it reads as covered.
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "ws-agents-lint" not in agents, (
        "AGENTS.md names ws-agents-lint, which does not exist in this repository "
        "or on PATH."
    )
