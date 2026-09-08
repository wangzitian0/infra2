"""infra2#629 / #635: a git timeout inside the iac-runner is retried once and then reported
as a failed step, never raised out of the worker."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNC_RUNNER = ROOT / "bootstrap/06.iac_runner/sync_runner.py"


def _load(monkeypatch):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location(
        "sync_runner_git_timeout", SYNC_RUNNER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_runner_git_timeout"] = module
    spec.loader.exec_module(module)
    return module


def test_a_timed_out_fetch_is_retried_once_and_then_succeeds(monkeypatch, tmp_path):
    sync_runner = _load(monkeypatch)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs["timeout"]))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)
    assert sync_runner.run_git_command(
        ["fetch", "--tags", "--prune", "origin"], tmp_path, "fetch"
    )
    assert [c[0] for c in calls] == [
        ("git", "fetch", "--tags", "--prune", "origin")
    ] * 2
    assert {c[1] for c in calls} == {sync_runner.GIT_COMMAND_TIMEOUT_SECONDS}


def test_a_persistent_timeout_is_a_failed_step_not_an_exception(
    monkeypatch, tmp_path, caplog
):
    sync_runner = _load(monkeypatch)

    def always_hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sync_runner.subprocess, "run", always_hangs)
    with caplog.at_level("ERROR"):
        assert (
            sync_runner.run_git_command(["fetch", "origin"], tmp_path, "fetch") is False
        )
    assert "git_command_timeout" in caplog.text


def test_a_non_zero_exit_is_not_retried(monkeypatch, tmp_path):
    sync_runner = _load(monkeypatch)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)
    assert (
        sync_runner.run_git_command(["reset", "--hard", "HEAD"], tmp_path, "reset")
        is False
    )
    assert len(calls) == 1


def test_the_workspace_fetch_and_clone_never_recurse_into_submodules(
    monkeypatch, tmp_path
) -> None:
    """infra2 carries repos/truealpha and repos/finance_report as submodules; the runner
    deploys infra2's OWN tree and reads neither. Git's on-demand recursion still walks them
    when a fetched commit moves a pointer, and on 2026-09-08 that failed a healthy fetch
    (`Could not access submodule 'repos/finance_report'`) and aborted the sync, blocking
    truealpha's v0.0.49 staging release."""
    sync_runner = _load(monkeypatch)
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)
    sync_runner.run_git_command(
        ["fetch", "--no-recurse-submodules", "--tags", "--prune", "origin"],
        tmp_path,
        "fetch",
    )
    assert seen[0] == [
        "git",
        "fetch",
        "--no-recurse-submodules",
        "--tags",
        "--prune",
        "origin",
    ]

    # Assert on the parsed argument lists, not on formatting: ruff may wrap either call.
    import ast

    tree = ast.parse((ROOT / "bootstrap/06.iac_runner/sync_runner.py").read_text())
    argv_lists = [
        [element.value for element in node.elts if isinstance(element, ast.Constant)]
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
    ]
    clone = next(argv for argv in argv_lists if "clone" in argv)
    assert "--no-recurse-submodules" in clone, clone
    fetch = next(argv for argv in argv_lists if "fetch" in argv and "origin" in argv)
    assert "--no-recurse-submodules" in fetch, fetch
