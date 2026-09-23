"""CLAUDE.md must be committed, and must carry AGENTS.md from *any* directory.

Two defects shaped this file, and the second one is why it no longer asks for
an import.

**First defect:** CLAUDE.md was not committed at all. It lived as an untracked
symlink in one working tree, listed in `.git/info/exclude`, so every fresh
clone, CI job and worktree started with no project instructions. The guard
written for it concluded "not a symlink, a real file with an `@AGENTS.md`
import" -- but the thing that had failed was *untracked*, not *symlink*. A
committed symlink (mode 120000) is cloned like any other object, which the four
sibling repositories have been demonstrating daily.

**Second defect (#856):** `@AGENTS.md` resolves only when that CLAUDE.md is in
the current working directory. When the ancestor walk finds it instead, the
import is not followed, so a session started in `libs/`, `tools/`, `docs/ssot/`
or any worktree subdirectory ran with no merge gate, no SSOT-first rule and no
red lines -- silently. Measured with a discriminative probe ("does your loaded
instruction text contain this string", file reads forbidden), twice per cell:

    carrier                              repo root   subdirectory
    CLAUDE.md = one line @AGENTS.md      YES         NO
    CLAUDE.md -> AGENTS.md (symlink)     YES         YES
    CLAUDE.md inlining the whole text    YES         YES
    AGENTS.md alone, ancestor CLAUDE.md  NO          NO

The last row is why "just delete CLAUDE.md" is not the answer here: an ancestor
CLAUDE.md suppresses native AGENTS.md reading, and this workspace has one.

**What the old guard was protecting, and how it is protected now.** It forbade
symlinks because git checks one out as a one-line text file on Windows without
`core.symlinks`, yielding a CLAUDE.md whose whole content is a path -- and CI
runs on Linux, so it would fail only where nobody looks. That risk is real and
unchanged. What changed is the comparison: it is a platform-specific risk on a
platform this repository is not developed on, weighed against a measured
every-platform failure in the majority of sessions. So the symlink is taken and
the Windows failure is made loud instead of traded away -- see
`test_a_broken_symlink_checkout_fails_loudly`, which is exactly the shape such a
checkout produces.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET = "AGENTS.md"
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


def _committed_mode() -> str | None:
    out = _git("ls-files", "-s", "CLAUDE.md").stdout.split()
    return out[0] if out else None


def test_claude_md_is_committed_not_a_local_convenience() -> None:
    # HEAD, not the index: "is committed" is the claim, and `git ls-files`
    # would pass on a bare `git add` in a tree whose HEAD has no such file.
    assert _git("cat-file", "-e", "HEAD:CLAUDE.md").returncode == 0, (
        "CLAUDE.md is not committed. An untracked carrier works only in the tree "
        "that has it; every fresh clone, CI job and worktree then starts with no "
        "project instructions at all."
    )


def test_claude_md_is_a_committed_symlink_to_agents_md() -> None:
    """Mode 120000 is the only carrier measured to work from a subdirectory."""
    mode = _committed_mode()
    assert mode == "120000", (
        f"CLAUDE.md is committed with mode {mode or '(absent)'}, not 120000 "
        f"(symlink). A one-line `@{TARGET}` import is resolved only when "
        f"CLAUDE.md sits in the working directory, so every session started in a "
        f"subdirectory would silently run with no constitution (#856)."
    )
    blob = _git("cat-file", "-p", "HEAD:CLAUDE.md").stdout.strip()
    assert blob == TARGET, (
        f"CLAUDE.md is a symlink to {blob!r}, not {TARGET!r}. The carrier has to "
        f"point at the constitution, not at something next to it."
    )


def test_claude_md_reads_back_as_the_constitution_itself() -> None:
    """The effect, not the shape: opening CLAUDE.md must yield AGENTS.md's bytes.

    This is what every host actually does, and it is the assertion that survives
    a change of carrier -- inline the text, symlink it, or find a third way, and
    this still says whether the rules arrive.
    """
    carrier = (ROOT / "CLAUDE.md").read_bytes()
    constitution = (ROOT / TARGET).read_bytes()
    assert carrier == constitution, (
        "CLAUDE.md does not read back as AGENTS.md. Whatever the carrier, a host "
        "that opens CLAUDE.md has to receive the constitution; if it does not, "
        "the rules are simply absent and nothing says so."
    )


def test_a_broken_symlink_checkout_fails_loudly() -> None:
    """Windows without `core.symlinks` writes the target path as file content.

    That is the one failure mode the symlink carrier adds, and it is silent by
    nature: such a CLAUDE.md is a perfectly valid file that loads one word of
    rules. This is what turns it into a red test instead. It is deliberately
    written to fire on the *content shape*, so it works on any platform -- there
    is no need for a Windows runner to exist before the guard does.
    """
    path = ROOT / "CLAUDE.md"
    if path.is_symlink():
        return  # intact checkout; the assertion below has nothing to catch
    body = path.read_text(encoding="utf-8").strip()
    assert body != TARGET, (
        "CLAUDE.md is a regular file whose entire content is the single word "
        f"{TARGET!r}. That is what a symlink looks like after a git checkout on "
        "a platform without symlink support: the rules are not loaded, and "
        "nothing else would have said so. Enable core.symlinks and re-checkout."
    )


def test_agents_md_names_no_guard_that_does_not_exist() -> None:
    # The original defect was a sentence naming `ws-agents-lint`, which existed
    # nowhere. Pinning that one spelling would be a spelling check: any rename
    # defeats it, and the replacement sentence in AGENTS.md names this very
    # file, recreating the same claim. So every backticked repository path in
    # AGENTS.md must resolve.
    agents = (ROOT / TARGET).read_text(encoding="utf-8")
    missing = sorted(
        {p for p in BACKTICKED_PATH_RE.findall(agents) if not (ROOT / p).exists()}
    )
    assert not missing, (
        "AGENTS.md names paths that do not exist: "
        + ", ".join(missing)
        + ". A guard that is only a name reads as covered and is not."
    )
