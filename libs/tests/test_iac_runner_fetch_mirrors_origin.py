"""infra2#666-adjacent: the runner's workspace is a mirror of origin, so a tag that was
re-cut upstream must not wedge it.

A real git fixture rather than an argv assertion: what matters is that `update_repo`
survives a moved tag, not which flags spell that.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNC_RUNNER = ROOT / "bootstrap/06.iac_runner/sync_runner.py"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _origin_with_one_commit(path: Path) -> str:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "service.py").write_text("first\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "first", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _load(monkeypatch, origin: Path, workspace: Path):
    monkeypatch.setenv("GIT_REPO_URL", str(origin))
    spec = importlib.util.spec_from_file_location(
        "sync_runner_fetch_mirror", SYNC_RUNNER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_runner_fetch_mirror"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WORKSPACE", workspace)
    monkeypatch.setattr(module, "WORKSPACE_LOCK_FILE", workspace / "workspace.lock")
    return module


def test_a_tag_re_cut_upstream_does_not_wedge_the_workspace(monkeypatch, tmp_path):
    origin = tmp_path / "infra2"
    first = _origin_with_one_commit(origin)
    _git("tag", "-a", "v1.1.77", "-m", "first attempt", first, cwd=origin)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sync_runner = _load(monkeypatch, origin, workspace)

    assert sync_runner.update_repo("v1.1.77")
    checkout = workspace / "infra2"
    assert _git("rev-parse", "HEAD", cwd=checkout) == first

    # The release is aborted and the tag re-cut on a later commit — exactly what happened
    # to v1.1.77 on 2026-09-09.
    (origin / "service.py").write_text("second\n")
    _git("commit", "-qam", "second", cwd=origin)
    second = _git("rev-parse", "HEAD", cwd=origin)
    _git("tag", "-d", "v1.1.77", cwd=origin)
    _git("tag", "-a", "v1.1.77", "-m", "re-cut", second, cwd=origin)

    assert sync_runner.update_repo("v1.1.77"), (
        "a re-cut tag must not abort the fetch: before --force/--prune-tags this failed "
        "with 'would clobber existing tag' and every later deploy aborted with "
        "'Failed to update repo'"
    )
    assert _git("rev-parse", "HEAD", cwd=checkout) == second


def test_a_tag_deleted_upstream_is_pruned_from_the_workspace(monkeypatch, tmp_path):
    origin = tmp_path / "infra2"
    first = _origin_with_one_commit(origin)
    _git("tag", "-a", "v1.1.15", "-m", "withdrawn", first, cwd=origin)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sync_runner = _load(monkeypatch, origin, workspace)
    assert sync_runner.update_repo("main")

    _git("tag", "-d", "v1.1.15", cwd=origin)
    assert sync_runner.update_repo("main")

    checkout = workspace / "infra2"
    assert "v1.1.15" not in _git("tag", "--list", cwd=checkout).split()
