"""What judges a pull request is computed, and must never compute to less.

`SELF_GOVERNING_FILES` was a hand-written list of five paths. A hand-written
list is open: `tools/pr_merge_gate.py` imports `tools/omca_gate_policy.py` (the
blocking audit layer) and `libs/console.py`, and neither was on it. Adding an
import widened what judges a pull request without widening what protects it.

Replacing it with a closure fixes that by construction and introduces the
opposite risk — a computation can quietly return less than the list did, and
an empty or shrunken protected set is a silent loosening of exactly the rule
it implements. Hence the historical list is pinned here.
"""

from __future__ import annotations

import pathlib

from tools import pr_merge_gate as gate

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The five paths the hand-written list held, verbatim. This is a ratchet, not
# a description: the closure may grow past it, never behind it.
LIST_BEFORE_THE_CLOSURE = frozenset(
    {
        "tools/pr_merge_gate.py",
        "libs/tests/test_pr_merge_gate.py",
        "docs/ssot/ops.merge-gate.md",
        "docs/ssot/ci-gate-inventory.yaml",
        "AGENTS.md",
    }
)


def test_the_closure_still_covers_everything_the_list_did() -> None:
    lost = sorted(LIST_BEFORE_THE_CLOSURE - gate.self_governing_files())
    assert not lost, (
        f"the computed set no longer protects {lost}, which the hand-written "
        "list did — a change to those would now be judged by the version it "
        "introduces"
    )


def test_the_closure_found_what_the_list_had_missed() -> None:
    """The reason for computing it, asserted rather than described."""
    found = gate.self_governing_files()
    # pr_merge_gate imports omca_gate_policy, which can block a merge.
    assert "tools/omca_gate_policy.py" in found
    assert "libs/console.py" in found


def test_an_import_reaches_the_protected_set(tmp_path) -> None:
    """The property that makes the closure a ratchet: importing something new
    protects it, with nobody having to remember."""
    module = "libs/console.py"
    assert module in gate._repo_deps("tools/pr_merge_gate.py"), (
        "the dependency walk stopped finding a module this file imports; the "
        "closure is no longer following imports"
    )


def test_rule_texts_are_listed_because_no_code_reads_them() -> None:
    """A closure over code cannot reach a file no code opens.

    Both rule texts appear in `pr_merge_gate.py` only inside comments, and the
    walk deliberately does not follow prose — following it would sweep in
    every path ever mentioned in a docstring.
    """
    source = (ROOT / "tools/pr_merge_gate.py").read_text(encoding="utf-8")
    for rel in gate.RULE_TEXT_FILES:
        assert rel in gate.self_governing_files()
        assert (
            f'"{rel}"'
            not in source.replace(f'RULE_TEXT_FILES = ("AGENTS.md", "{rel}")', "")
            or rel == "AGENTS.md"
        ), f"{rel} is now read by code; compute it instead"


def test_the_set_is_never_empty() -> None:
    """An empty protected set would let every rewrite through — the one
    outcome worse than a wrong set."""
    found = gate.self_governing_files()
    assert found
    assert "tools/pr_merge_gate.py" in found


def test_every_member_exists() -> None:
    """A stale entry protects nothing and reads as though it does."""
    missing = sorted(f for f in gate.self_governing_files() if not (ROOT / f).is_file())
    assert not missing, f"protected paths that do not exist: {missing}"
