"""Tests for common/core/ref_worktree.py."""

from __future__ import annotations

import subprocess
import sys

from common.core.ref_worktree import run_at_ref


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_runs_in_the_tree_of_the_ref_and_removes_it(tmp_path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("old")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "one")
    (tmp_path / "f.txt").write_text("new")
    code = "import os; print(open('f.txt').read(), os.getcwd())"
    proc = run_at_ref("main", [sys.executable, "-c", code], root=tmp_path, timeout=30)
    text, cwd = proc.stdout.split()
    assert text == "old"
    assert not subprocess.run(["ls", cwd], capture_output=True).returncode == 0
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert cwd not in listing


def test_no_other_module_makes_its_own_worktree() -> None:
    """The runner has one source: config_drift and replay call `run_at_ref` (#1653)."""
    from common.core.ref_worktree import __file__ as runner
    from pathlib import Path

    root = Path(runner).resolve().parents[2]
    for name in ("common/deploy/config_drift.py", "common/gate/replay.py"):
        text = (root / name).read_text()
        assert "run_at_ref(" in text, name
        assert '"worktree"' not in text, name
