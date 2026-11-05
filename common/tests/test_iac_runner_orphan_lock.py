"""#1439 review: a gunicorn worker-only restart must not allow a second concurrent sync.

The sync runs `invoke` through `subprocess.run`. The arbiter kills a worker by its pid
only, so the invoke child keeps running. The deployment lock was opened close-on-exec and
died with the worker, so the restarted worker reported the deploy `interrupted`, the train
retried it, and a second sync started beside the first.

These tests use real processes: a "worker" that holds the deployment lock and runs an
invoke child, which the test kills with SIGKILL like the arbiter does.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC_RUNNER = ROOT / "bootstrap/06.iac_runner/sync_runner.py"

# The worker: runs the real sync_services, whose invoke child is `CHILD_CODE` instead of
# invoke. sync_services takes the deployment lock, so what is tested is its own choice of
# options for that lock.
WORKER = textwrap.dedent(
    """
    import importlib.util, os, sys
    from pathlib import Path

    os.environ["GIT_REPO_URL"] = "https://github.com/wangzitian0/infra2"
    spec = importlib.util.spec_from_file_location("sync_runner", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_runner"] = module
    spec.loader.exec_module(module)
    module.DEPLOYMENT_LOCK_FILE = Path(sys.argv[2])
    module.WORKSPACE = Path(sys.argv[3])
    module.INVOKE_BOOTSTRAP = os.environ["TEST_CHILD_CODE"]
    module.resolve_vault_token = lambda env: None
    module.update_repo = lambda ref=None: True
    module.resolve_checkout_head = lambda path: "a" * 40
    module._service_task_map = lambda: {"platform/redis": "redis.sync"}
    # The real sync_services: it takes the deployment lock and runs the invoke child.
    module.sync_services({"platform/redis"}, ref="a" * 40, deploy_env="staging")
    """
)

# The invoke child: records its pid, forks a grandchild with a plain fork+exec (the way a
# pty-based `c.run` does) and sleeps. The grandchild must not keep the lock alive.
CHILD_CODE = textwrap.dedent(
    """
    import os, time
    open(os.environ["TEST_CHILD_PID"], "w").write(str(os.getpid()))
    pid = os.fork()
    if pid == 0:
        os.execvp("sleep", ["sleep", "120"])
    open(os.environ["TEST_GRANDCHILD_PID"], "w").write(str(pid))
    time.sleep(120)
    """
)


def _lock_is_held(lock_path: Path) -> bool:
    with open(lock_path, "w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _wait_for(predicate, what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_the_deployment_lock_outlives_a_killed_worker_while_its_invoke_child_runs(
    tmp_path,
):
    lock = tmp_path / "deployment.lock"
    (tmp_path / "infra2").mkdir()  # the repo checkout: the child's working directory
    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    env = {
        **os.environ,
        "TEST_CHILD_CODE": CHILD_CODE,
        "TEST_CHILD_PID": str(child_pid_file),
        "TEST_GRANDCHILD_PID": str(grandchild_pid_file),
    }
    worker = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(SYNC_RUNNER), str(lock), str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child = grandchild = None
    try:

        def ready() -> bool:
            assert worker.poll() is None, f"worker exited early: {worker.stderr.read()}"
            return child_pid_file.exists() and grandchild_pid_file.exists()

        _wait_for(ready, "the invoke child and its grandchild")
        child = int(child_pid_file.read_text())
        grandchild = int(grandchild_pid_file.read_text())
        assert _lock_is_held(lock), "the worker must hold the lock while it syncs"

        # The arbiter kills the worker by pid. Its children keep running.
        worker.kill()
        worker.wait(timeout=10)
        assert _alive(child), "the invoke child must survive the worker"
        assert _lock_is_held(lock), (
            "the deployment lock must stay held while the orphaned invoke child runs; "
            "if it is free, a retry starts a second sync beside the first"
        )

        # The orphan ends: the OS releases the lock. The grandchild, forked and exec'd
        # by the child, must not keep it alive (that would block every later sync).
        os.kill(child, signal.SIGKILL)
        _wait_for(lambda: not _lock_is_held(lock), "the lock to be released")
        assert _alive(grandchild), "the grandchild must outlive the child in this test"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        for pid in (child, grandchild):
            if pid:
                _reap(pid)


def test_a_new_sync_waits_for_the_lock_and_gives_up_with_a_clear_error(
    monkeypatch, tmp_path
):
    """A hung orphan must not block every later sync for ever: the wait has a limit."""
    import importlib.util

    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location("sync_runner_lock_wait", SYNC_RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_runner_lock_wait"] = module
    spec.loader.exec_module(module)

    lock = tmp_path / "deployment.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time; f = open(sys.argv[1], 'w'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(module.LockTimeout, match="still holds the deployment lock"):
            with module.file_lock(lock, "deployment", wait_seconds=0.4):
                pytest.fail("the lock is held by another process")
        assert 0.3 <= time.monotonic() - started < 5
    finally:
        holder.kill()
        holder.wait(timeout=10)

    # Free again: the same call now succeeds at once.
    with module.file_lock(lock, "deployment", wait_seconds=0.4):
        pass


def test_sync_services_reports_a_held_lock_as_a_failed_sync_not_a_crash(
    monkeypatch, tmp_path
):
    import importlib.util

    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location("sync_runner_lock_sync", SYNC_RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_runner_lock_sync"] = module
    spec.loader.exec_module(module)

    lock = tmp_path / "deployment.lock"
    monkeypatch.setattr(module, "DEPLOYMENT_LOCK_FILE", lock)
    monkeypatch.setattr(module, "DEPLOYMENT_LOCK_WAIT_SECONDS", 0.3)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time; f = open(sys.argv[1], 'w'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        result = module.sync_services({"platform/redis"}, ref="a" * 40)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert result.success is False
    assert "still holds the deployment lock" in (result.error or "")
