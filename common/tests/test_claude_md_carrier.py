"""AGENTS.md is the sole committed Repo-tier constitution; CLAUDE.md must stay absent.

Physical 2x2 probe measurement on `claude 2.1.303` (CLI) and `claude 2.1.299`
(Desktop) established that when both the workspace-root `CLAUDE.md` and the
repository `CLAUDE.md` are absent, Claude Code natively loads both workspace and
repository `AGENTS.md` files from the repository root and from deep
subdirectories. Removing `CLAUDE.md` eliminates the symlink carrier and its
Windows checkout failure mode while preserving full `AGENTS.md` injection across
all hosts.
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


def _committed_mode(path: str) -> str | None:
    out = _git("ls-files", "-s", path).stdout.split()
    return out[0] if out else None


def test_agents_md_is_committed_as_a_regular_file() -> None:
    """AGENTS.md must be committed in git as mode 100644 and exist as a non-empty regular file."""
    assert _git("cat-file", "-e", f"HEAD:{TARGET}").returncode == 0, (
        "AGENTS.md is not committed in HEAD. Every fresh clone, CI job, and "
        "worktree requires a committed AGENTS.md."
    )
    mode = _committed_mode(TARGET)
    assert mode == "100644", (
        f"AGENTS.md is committed with mode {mode or '(absent)'}, not 100644 "
        "(regular file)."
    )
    agents_path = ROOT / TARGET
    assert agents_path.is_file()
    assert not agents_path.is_symlink()
    assert agents_path.read_text(encoding="utf-8").strip(), "AGENTS.md is empty."


def test_claude_md_is_absent_so_native_agents_md_loads() -> None:
    """CLAUDE.md must not exist in git or on disk so Claude Code natively loads AGENTS.md.

    A 2x2 physical probe measurement on `claude 2.1.303` (CLI) and
    `claude 2.1.299` (Desktop) confirmed that when both the workspace-root
    `CLAUDE.md` and repository `CLAUDE.md` are absent, Claude Code natively
    loads both workspace and repository `AGENTS.md` from root and deep
    subdirectories. Any `CLAUDE.md` file in the directory walk suppresses
    native `AGENTS.md` loading.
    """
    assert _git("ls-files", "CLAUDE.md").stdout.strip() == "", (
        "CLAUDE.md is tracked in the git index. Remove it so native AGENTS.md "
        "loading remains active."
    )
    assert _git("cat-file", "-e", "HEAD:CLAUDE.md").returncode != 0, (
        "CLAUDE.md is still committed in HEAD. Commit its removal."
    )
    claude_path = ROOT / "CLAUDE.md"
    assert not claude_path.exists(), (
        "CLAUDE.md exists on disk. Delete it so it does not suppress native "
        "AGENTS.md loading."
    )
    assert not claude_path.is_symlink(), (
        "CLAUDE.md exists as a broken symlink on disk. Delete it."
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


def test_agents_md_points_at_the_function_that_actually_decides_provability() -> None:
    """AGENTS.md §6 stopped listing the provable files and now points at the code.

    It lives beside `test_agents_md_names_no_guard_that_does_not_exist` because
    it is the same job: a reference in AGENTS.md has to resolve. That one checks
    backticked *paths*, this one checks a *symbol*, and a rename defeats exactly
    one of them. It is deliberately not in test_pr_merge_gate.py -- that file is
    inside the gate's own self-governing closure, and a new guard over the
    prose does not belong to the case that closure exists for.

    The list was duplicated prose: it named `ci-gate-inventory.yaml` alone while
    `_direction_proof_for` had since added every `.github/workflows/` path by
    prefix. Two copies drift, and this one drifted *strict* -- which is not the
    safe direction it sounds like. Someone following the prose asks the owner
    once too often; someone following the code asks once too seldom, and which
    copy gets read first is luck (#855).

    A pointer only helps while it resolves. The existing guard checks that
    backticked *paths* in AGENTS.md exist; this checks the *symbol*, because a
    rename would leave the path valid and the sentence meaningless.
    """
    import tools.pr_merge_gate as gate

    name = "_direction_proof_for"
    assert hasattr(gate, name), (
        f"AGENTS.md §6 delegates the provable-file question to {name}(), which no "
        "longer exists in tools/pr_merge_gate.py. Either restore it or rewrite "
        "the rule -- a rule that points at nothing is worse than one that repeats "
        "itself, because it reads as delegated."
    )
    agents = (ROOT / TARGET).read_text(encoding="utf-8")
    # The backticked token, not a bare mention: prose that merely says the words
    # in passing is not a reference a reader can follow, and this test exists to
    # keep a followable one.
    assert f"`{name}()`" in agents, (
        f"AGENTS.md no longer names `{name}()` as a code reference. If the "
        "delegation moved, this test is the thing that was holding the prose and "
        "the code together."
    )
    # And the delegation has to be live: the function must still answer for the
    # two shapes the rule is about, or the prose is describing something else.
    # Reached through `name` so a rename cannot leave half the test pointing at
    # the old symbol and still passing.
    decide = getattr(gate, name)
    assert decide("docs/ssot/ci-gate-inventory.yaml") is not None
    assert decide(".github/workflows/infra-ci.yml") is not None
    assert decide("tools/pr_merge_gate.py") is None, (
        "the gate's own Python must stay unprovable; if it gained a proof, the "
        "defendant can now rewrite the law and show a certificate for it"
    )
