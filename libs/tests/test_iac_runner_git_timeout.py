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
