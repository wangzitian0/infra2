"""Tests for IaC Runner deploy result semantics."""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IAC_RUNNER = ROOT / "bootstrap/06.iac_runner"


def _load_module(name: str, path: Path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_sync_services_returns_structured_failure_result(monkeypatch) -> None:
    """#182: sync result reports real failed service tasks."""
    sync_runner = _load_module("sync_runner_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch)

    @contextmanager
    def unlocked(_path, _description):
        yield

    monkeypatch.setattr(sync_runner, "file_lock", unlocked)
    monkeypatch.setattr(sync_runner, "update_repo", lambda ref=None: True)

    def fake_run(task_name, _repo_path, deploy_env):
        return {
            "task": task_name,
            "success": task_name != "redis.sync",
            "stdout": f"{deploy_env}:{task_name}",
            "stderr": "boom" if task_name == "redis.sync" else "",
        }

    monkeypatch.setattr(sync_runner, "run_invoke_task", fake_run)

    result = sync_runner.sync_services(
        {"platform/postgres", "platform/redis", "bootstrap/vault"},
        ref="main",
        deploy_env="staging",
    )

    assert result.success is False
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.skipped == 1
    payload = result.to_dict()
    assert payload["results"][0]["service"] == "bootstrap/vault"
    assert any(item["stderr"] == "boom" for item in payload["results"])


def test_deploy_wait_returns_500_when_sync_fails(monkeypatch) -> None:
    """#182: /deploy wait=true exposes failed sync to GitHub Actions."""
    sync_runner = _load_module("sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch)
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(
        headers={},
        data=b"",
        json={"env": "staging", "ref": "main", "wait": True, "triggered_by": "ci"},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module("webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch)

    failed = sync_runner.SyncResult(
        env="staging",
        ref="main",
        requested_services=["platform/redis"],
        results=[
            sync_runner.ServiceSyncResult(
                service="platform/redis",
                task="redis.sync",
                success=False,
                stderr="boom",
            )
        ],
    )
    monkeypatch.setattr(sync_runner, "sync_services_by_version", lambda *_args: failed)

    response = webhook_server.version_deploy()

    body, status_code = response
    assert status_code == 500
    assert body["status"] == "failed"
    assert body["result"]["failed"] == 1
