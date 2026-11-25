"""The runner runs each deploy task under the uv.lock of the checked-out ref (#1822).

Staging v3.1.174 failed in the runner: the task ran with the image interpreter, so it
imported the image SDK, and that SDK rejected `x-infra2.vault.env` of the ref.
`prepare_ref_env` builds a venv from the ref lock (`uv sync --locked`), keyed by the
lock sha256, and `run_invoke_task` starts the task with that venv interpreter.

### Mutation evidence
- `run_invoke_task` with `sys.executable` instead of `python`:
  `test_invoke_task_runs_with_the_ref_env_interpreter` fails.
- Removing the venv reuse (sync on every call): `test_same_lock_reuses_the_venv` fails.
- A key that is not the lock hash (one shared venv):
  `test_a_different_lock_gets_its_own_venv` fails.
- Falling back to the image interpreter when the sync fails:
  `test_sync_failure_fails_the_deploy_with_no_fallback` fails.
- Removing the subprocess timeout handling: `test_sync_timeout_is_a_clear_error` fails.
- Keeping a venv without the marker: `test_partial_venv_is_rebuilt` fails.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC_RUNNER = ROOT / "bootstrap/06.iac_runner/sync_runner.py"


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    name = "sync_runner_ref_env_under_test"
    spec = importlib.util.spec_from_file_location(name, SYNC_RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REF_ENV_ROOT", tmp_path / ".ref-envs")
    monkeypatch.setattr(module, "UV_CACHE_DIR", tmp_path / ".uv-cache")
    return module


class FakeUv:
    """`uv sync` writes `bin/python`; the probe of that interpreter prints the SDK."""

    def __init__(self, sdk: str = "8.7.0", sync_rc: int = 0, timeout: bool = False):
        self.sdk, self.sync_rc, self.timeout = sdk, sync_rc, timeout
        self.syncs: list[dict] = []

    def __call__(self, argv, **kwargs):
        if argv[0] == "uv":
            self.syncs.append({"argv": argv, **kwargs})
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if self.sync_rc == 0:
                venv = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
                (venv / "bin").mkdir(parents=True, exist_ok=True)
                (venv / "bin" / "python").write_text("#!fake\n")
            return subprocess.CompletedProcess(argv, self.sync_rc, "", "lock mismatch")
        return subprocess.CompletedProcess(argv, 0, f"{self.sdk}\n", "")


def _checkout(tmp_path: Path, lock: str = "version = 1\n") -> Path:
    repo = tmp_path / "workspace" / "infra2"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "uv.lock").write_text(lock)
    return repo


def test_sync_builds_the_ref_lock_venv(runner, monkeypatch, tmp_path) -> None:
    fake = FakeUv()
    monkeypatch.setattr(runner.subprocess, "run", fake)
    env = runner.prepare_ref_env(_checkout(tmp_path))

    assert env.error == ""
    assert env.sdk_version == "8.7.0"
    assert env.reused is False
    assert env.python.endswith(f"/.ref-envs/{env.lock_sha256[:16]}/bin/python")
    sync = fake.syncs[0]
    assert sync["argv"][:3] == ["uv", "sync", "--locked"]
    assert sync["timeout"] == runner.REF_ENV_SYNC_TIMEOUT
    assert sync["env"]["UV_PYTHON_DOWNLOADS"] == "never"


def test_same_lock_reuses_the_venv(runner, monkeypatch, tmp_path) -> None:
    fake = FakeUv()
    monkeypatch.setattr(runner.subprocess, "run", fake)
    repo = _checkout(tmp_path)
    first = runner.prepare_ref_env(repo)
    second = runner.prepare_ref_env(repo)

    assert len(fake.syncs) == 1
    assert second.reused is True
    assert second.python == first.python


def test_a_different_lock_gets_its_own_venv(runner, monkeypatch, tmp_path) -> None:
    fake = FakeUv()
    monkeypatch.setattr(runner.subprocess, "run", fake)
    first = runner.prepare_ref_env(_checkout(tmp_path, "version = 1\n# a\n"))
    second = runner.prepare_ref_env(_checkout(tmp_path, "version = 1\n# b\n"))

    assert len(fake.syncs) == 2
    assert first.python != second.python


def test_sync_failure_fails_the_deploy_with_no_fallback(
    runner, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(runner.subprocess, "run", FakeUv(sync_rc=2))
    repo = _checkout(tmp_path)
    env = runner.prepare_ref_env(repo)
    assert env.python == ""
    assert env.error.startswith("uv sync --locked failed (exit 2)")

    @contextmanager
    def unlocked(_path, _description):
        yield

    ran: list[str] = []
    monkeypatch.setattr(runner, "WORKSPACE", repo.parent)
    monkeypatch.setattr(runner, "file_lock", unlocked)
    monkeypatch.setattr(runner, "update_repo", lambda ref=None: True)
    monkeypatch.setattr(runner, "resolve_checkout_head", lambda _p: None)
    monkeypatch.setattr(
        runner, "run_invoke_task", lambda task, *_a, **_k: ran.append(task)
    )
    result = runner.sync_services({"platform/redis"}, ref="main")

    assert ran == []
    assert result.success is False
    assert result.error.startswith("ref venv (uv sync --locked) failed")
    assert result.to_public_dict()["ref_env"]["error"] == env.error


def test_sync_timeout_is_a_clear_error(runner, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runner.subprocess, "run", FakeUv(timeout=True))
    env = runner.prepare_ref_env(_checkout(tmp_path))
    assert env.error == (
        f"uv sync --locked timed out after {runner.REF_ENV_SYNC_TIMEOUT}s"
    )
    assert env.python == ""


def test_partial_venv_is_rebuilt(runner, monkeypatch, tmp_path) -> None:
    fake = FakeUv()
    monkeypatch.setattr(runner.subprocess, "run", fake)
    repo = _checkout(tmp_path)
    env = runner.prepare_ref_env(repo)
    venv = Path(env.python).parents[1]
    (venv / runner.REF_ENV_MARKER).unlink()
    (venv / "stale-file").write_text("x")

    again = runner.prepare_ref_env(repo)

    assert len(fake.syncs) == 2
    assert again.reused is False
    assert not (venv / "stale-file").exists()


def test_old_venvs_are_pruned(runner, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runner.subprocess, "run", FakeUv())
    for n in range(5):
        runner.prepare_ref_env(_checkout(tmp_path, f"version = 1\n# {n}\n"))
    assert len(list(runner.REF_ENV_ROOT.iterdir())) == runner.REF_ENV_KEEP


def test_invoke_task_runs_with_the_ref_env_interpreter(
    runner, monkeypatch, tmp_path
) -> None:
    seen: list[list[str]] = []

    def record(argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", record)
    monkeypatch.setattr(runner, "resolve_vault_token", lambda _env: None)
    runner.run_invoke_task(
        "redis.sync", tmp_path, "staging", python="/ref-envs/abc/bin/python"
    )
    assert seen[0][0] == "/ref-envs/abc/bin/python"
    assert seen[0][-1] == "redis.sync"
