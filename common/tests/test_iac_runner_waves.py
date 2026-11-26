"""Verify topological Wave 1 -> Wave 2 execution order in iac-runner sync_runner."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SYNC_RUNNER_PATH = ROOT / "bootstrap/06.iac_runner/sync_runner.py"


def _load_sync_runner(monkeypatch) -> ModuleType:
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location(
        "test_wave_sync_runner", SYNC_RUNNER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "test_wave_sync_runner", module)
    spec.loader.exec_module(module)
    return module


def test_partition_sync_waves_places_stateful_infra_before_apps(monkeypatch) -> None:
    sync_runner = _load_sync_runner(monkeypatch)
    waves = sync_runner.partition_sync_waves(
        [
            "finance_report/app",
            "truealpha/app",
            "platform/todo",
            "platform/postgres",
            "finance_report/postgres",
            "platform/prefect",
        ]
    )
    assert waves == [
        ["finance_report/postgres", "platform/postgres", "platform/prefect"],
        ["finance_report/app", "platform/todo", "truealpha/app"],
    ]


def test_sync_services_executes_wave1_before_starting_wave2(
    monkeypatch, tmp_path: Path
) -> None:
    """Regression: Wave 1 stateful foundation must finish before any Wave 2 app starts."""
    sync_runner = _load_sync_runner(monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    monkeypatch.setattr(sync_runner, "DEPLOYMENT_LOCK_FILE", tmp_path / "deploy.lock")
    monkeypatch.setattr(sync_runner, "WORKSPACE", tmp_path)
    monkeypatch.setattr(sync_runner, "REPO_NAME", "repo")
    monkeypatch.setattr(sync_runner, "update_repo", lambda ref=None: True)
    monkeypatch.setattr(sync_runner, "resolve_checkout_head", lambda _p: "abc1234")
    monkeypatch.setattr(
        sync_runner,
        "prepare_ref_env",
        lambda _p: SimpleNamespace(
            error="",
            python="python3",
            sdk_version="4.12.0",
            lock_sha256="a" * 64,
            reused=True,
            seconds=0.1,
            to_dict=lambda: {},
        ),
    )
    monkeypatch.setattr(
        sync_runner,
        "_service_task_map",
        lambda: {"finance_report/app": "fr.sync", "platform/postgres": "pg.sync"},
    )

    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _fake_invoke(task_name: str, _repo: Path, _env: str, _head: str, **_kw):
        with lock:
            events.append(("start", task_name))
        if task_name == "pg.sync":
            time.sleep(0.05)
        with lock:
            events.append(("end", task_name))
        return {"success": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(sync_runner, "run_invoke_task", _fake_invoke)
    res = sync_runner.sync_services(
        {"finance_report/app", "platform/postgres"},
        ref="main",
        deploy_env="staging",
    )
    assert res.success is True
    assert events == [
        ("start", "pg.sync"),
        ("end", "pg.sync"),
        ("start", "fr.sync"),
        ("end", "fr.sync"),
    ]
