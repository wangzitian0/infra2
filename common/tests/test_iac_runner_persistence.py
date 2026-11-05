"""#1439: the iac_runner keeps its deploy records on the /workspace volume.

Before this change the runner held `_in_flight_deploys` and `_recent_deploys` in memory
only. A worker or container restart erased them, and the client poll then failed with
"the runner has no record of this deployment" (run 41967457003, 2026-11-05). These tests
restart the module (a fresh import over the same state directory) and read the records
back through the public handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_SERVER = ROOT / "bootstrap/06.iac_runner/webhook_server.py"
DEPLOY_SHA = "a" * 40
STATE_FILE_NAME = "deploy_state.json"
SECRET = "test-webhook-secret"
_nonce_counter = [0]
_module_counter = [0]


class _FakeResult:
    def __init__(self, services, success=True):
        self.success = success
        self._services = list(services or [])

    def to_public_dict(self):
        return {
            "succeeded": len(self._services) if self.success else 0,
            "failed": 0 if self.success else len(self._services),
            "results": [],
        }


def _install_sync_runner(monkeypatch, *, success=True):
    stub = types.ModuleType("sync_runner")
    calls: list[tuple] = []

    def sync_services_by_version(env, ref, triggered_by, services=None, **kwargs):
        calls.append((env, ref, tuple(services or ())))
        return _FakeResult(services, success=success)

    stub.sync_services_by_version = sync_services_by_version
    monkeypatch.setitem(sys.modules, "sync_runner", stub)
    return calls


def _load_runner(monkeypatch, state_dir: Path):
    """Import webhook_server afresh over `state_dir`: the process-restart stand-in."""
    _module_counter[0] += 1
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("IAC_RUNNER_STATE_DIR", str(state_dir))
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    name = f"webhook_server_persistence_{_module_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, WEBHOOK_SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sign(ws, payload: dict | None = None) -> None:
    _nonce_counter[0] += 1
    timestamp = str(int(time.time()))
    nonce = f"persist-nonce-{_nonce_counter[0]:06d}"
    signature = hmac.new(
        SECRET.encode(), f"{timestamp}.{nonce}.".encode() + b"", hashlib.sha256
    ).hexdigest()
    ws.request.headers = {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-IAC-Timestamp": timestamp,
        "X-IAC-Nonce": nonce,
    }
    ws.request.data = b""
    ws.request.json = payload


COORD = {
    "env": "staging",
    "ref": DEPLOY_SHA,
    "triggered_by": "ci",
    "services": ["platform/redis"],
}


def _request_deploy(ws):
    _sign(ws, {**COORD, "wait": False})
    return ws.version_deploy()


def _poll(ws, deployment_id=None):
    payload = dict(COORD)
    if deployment_id:
        payload["deployment_id"] = deployment_id
    _sign(ws, payload)
    return ws.deployment_status()


class _NeverRuns:
    """A deploy thread that dies with the process: its target never finishes."""

    daemon = False

    def __init__(self, target, args):
        pass

    def start(self):
        pass


class _RunsNow:
    daemon = False

    def __init__(self, target, args):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_a_restart_between_request_and_poll_reads_back_interrupted_then_retries(
    monkeypatch, tmp_path
):
    """Run 41967457003: the runner restarted after it accepted the deploy. The poll got
    `not_found` and the client could do nothing. The record must read back as
    `interrupted`, and a second request for the same coordinates must run and complete."""
    calls = _install_sync_runner(monkeypatch)
    first = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _NeverRuns)
    accepted, code = _request_deploy(first)
    assert code == 202 and accepted["status"] == "in_progress"
    deployment_id = accepted["deployment_id"]

    # The process dies here. A new process loads the same state directory.
    second = _load_runner(monkeypatch, tmp_path)
    body, code = _poll(second, deployment_id)

    assert code == 200, body
    assert body["status"] == "interrupted"
    assert body["deployment_id"] == deployment_id
    assert "restarted" in body["error"]
    assert second._in_flight_deploys == set(), "a dead thread must not stay in flight"
    assert calls == [], "the restart must not re-run the deploy by itself"

    # The retry: the same coordinates run again; interrupted is never served as a result.
    monkeypatch.setattr(second.threading, "Thread", _RunsNow)
    retry, code = _request_deploy(second)
    assert code == 202 and retry["deployment_id"] == deployment_id
    assert retry.get("cached") is not True
    assert calls == [("staging", DEPLOY_SHA, ("platform/redis",))]
    done, code = _poll(second, deployment_id)
    assert (code, done["status"]) == (200, "completed")


def test_a_completed_deploy_and_its_result_survive_a_restart(monkeypatch, tmp_path):
    calls = _install_sync_runner(monkeypatch)
    first = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _RunsNow)
    accepted, _ = _request_deploy(first)

    second = _load_runner(monkeypatch, tmp_path)
    body, code = _poll(second, accepted["deployment_id"])

    assert (code, body["status"]) == (200, "completed")
    assert body["result"]["succeeded"] == 1
    # A remembered success still collapses a duplicate request after the restart.
    again, code = _request_deploy(second)
    assert code == 200 and again.get("cached") is True
    assert len(calls) == 1


def test_a_record_older_than_its_age_limit_is_dropped_on_load(monkeypatch, tmp_path):
    _install_sync_runner(monkeypatch)
    first = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _RunsNow)
    accepted, _ = _request_deploy(first)

    # A restart a minute later restores the record ...
    young = _load_runner(monkeypatch, tmp_path)
    body, code = _poll(young, accepted["deployment_id"])
    assert (code, body["status"]) == (200, "completed")

    # ... a restart two hours later does not: the record is older than its age limit.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 2 * 3600)
    old = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(time, "time", real_time)
    assert old._recent_deploys == {}, "the load itself must drop the expired record"
    body, code = _poll(old, accepted["deployment_id"])
    assert code == 404 and body["status"] == "not_found"


def test_the_record_count_is_bounded_and_the_newest_records_stay(monkeypatch, tmp_path):
    _install_sync_runner(monkeypatch)
    first = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first, "MAX_PERSISTED_DEPLOYS", 3, raising=False)
    monkeypatch.setattr(first.threading, "Thread", _RunsNow)
    ids = []
    for index in range(6):
        payload = {**COORD, "services": [f"platform/svc{index}"], "wait": False}
        _sign(first, payload)
        accepted, _ = first.version_deploy()
        ids.append((f"platform/svc{index}", accepted["deployment_id"]))
        # Records need distinct ages so the order of expiry is defined.
        time.sleep(0.01)

    second = _load_runner(monkeypatch, tmp_path)
    present = []
    for service, deployment_id in ids:
        _sign(second, {**COORD, "services": [service], "deployment_id": deployment_id})
        _body, code = second.deployment_status()
        present.append(code == 200)

    assert present == [False, False, False, True, True, True]


def test_a_corrupt_state_file_starts_empty_and_is_kept_aside(monkeypatch, tmp_path):
    _install_sync_runner(monkeypatch)
    first = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _RunsNow)
    _request_deploy(first)
    state_file = tmp_path / STATE_FILE_NAME
    assert state_file.exists(), "the first process must have persisted its record"
    state_file.write_text('{"version": 1, "recent": [', encoding="utf-8")

    second = _load_runner(monkeypatch, tmp_path)  # must not raise

    assert second._recent_deploys == {} and second._in_flight_deploys == set()
    kept = [p.name for p in tmp_path.iterdir() if "corrupt" in p.name]
    assert kept, "the unreadable file must be kept for diagnosis"


def test_a_failed_replace_leaves_the_previous_state_file_intact(monkeypatch, tmp_path):
    """Atomic write: if the swap fails, the old complete file stays and no temp file does."""
    _install_sync_runner(monkeypatch)
    ws = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(ws.threading, "Thread", _RunsNow)
    _request_deploy(ws)
    state_file = tmp_path / STATE_FILE_NAME
    before = state_file.read_text(encoding="utf-8")
    json.loads(before)

    def broken_replace(_src, _dst):
        raise OSError("disk went away between write and swap")

    monkeypatch.setattr(ws.os, "replace", broken_replace)
    payload = {**COORD, "services": ["platform/other"], "wait": False}
    _sign(ws, payload)
    body, code = ws.version_deploy()  # the deploy itself must not fail on a state error

    assert code == 202, body
    assert state_file.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
    assert ws._deploy_state_health()["last_error"]


def test_concurrent_writers_do_not_corrupt_or_lose_records(monkeypatch, tmp_path):
    """Four gunicorn threads finish deploys at the same moment. Every record must reach
    the file, and a reader must never see a torn file."""
    _install_sync_runner(monkeypatch)
    ws = _load_runner(monkeypatch, tmp_path)
    state_file = tmp_path / STATE_FILE_NAME
    real_replace = os.replace

    first_call = threading.Lock()
    seen_first = [False]

    def slow_replace(src, dst):
        # The first writer to reach the swap stalls. Writers that run unlocked then swap
        # in newer snapshots, and the stalled one swaps in its older snapshot last.
        with first_call:
            stall = not seen_first[0]
            seen_first[0] = True
        if stall:
            time.sleep(0.15)
        return real_replace(src, dst)

    monkeypatch.setattr(ws.os, "replace", slow_replace)
    writers = 16
    keys = [
        ws._deployment_key("staging", DEPLOY_SHA, [f"platform/svc{n}"])
        for n in range(writers)
    ]
    for key in keys:
        with ws._deploy_state_lock:
            ws._begin_deploy_locked(key)

    barrier = threading.Barrier(writers + 1)
    errors: list[BaseException] = []
    stop = threading.Event()
    torn: list[str] = []

    def writer(n: int) -> None:
        try:
            barrier.wait()
            ws._run_deployment("staging", DEPLOY_SHA, "ci", [f"platform/svc{n}"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        barrier.wait()
        while not stop.is_set():
            try:
                text = state_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            try:
                json.loads(text)
            except json.JSONDecodeError:
                torn.append(text[:60])

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(writers)]
    watcher = threading.Thread(target=reader)
    for thread in threads:
        thread.start()
    watcher.start()
    for thread in threads:
        thread.join(timeout=30)
    stop.set()
    watcher.join(timeout=5)

    assert errors == []
    assert torn == []
    data = json.loads(state_file.read_text(encoding="utf-8"))
    stored = {item["response"]["deployment_id"] for item in data["recent"]}
    assert stored == {ws._deployment_id(key) for key in keys}
    assert data["in_flight"] == []
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_health_reports_whether_deploy_records_persist(monkeypatch, tmp_path):
    """A silent loss of persistence is a GREEN-WHILE-EMPTY defect: /health must say so."""
    _install_sync_runner(monkeypatch)
    ws = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(ws.threading, "Thread", _RunsNow)
    _request_deploy(ws)
    health = ws._deploy_state_health()
    assert health["persistent"] is True and health["last_error"] == ""
    assert Path(health["path"]) == tmp_path / STATE_FILE_NAME

    def disk_full(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(ws.os, "replace", disk_full)
    _sign(ws, {**COORD, "services": ["platform/again"], "wait": False})
    ws.version_deploy()
    broken = ws._deploy_state_health()
    assert broken["persistent"] is False and "disk full" in broken["last_error"]


@pytest.mark.parametrize("damage", ["version", "recent"])
def test_a_state_file_in_an_unknown_shape_is_ignored_not_trusted(
    monkeypatch, tmp_path, damage
):
    _install_sync_runner(monkeypatch)
    first = _load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _RunsNow)
    accepted, _ = _request_deploy(first)
    state_file = tmp_path / STATE_FILE_NAME
    good = json.loads(state_file.read_text(encoding="utf-8"))

    # Control: the undamaged file restores the record.
    control = _load_runner(monkeypatch, tmp_path)
    body, code = _poll(control, accepted["deployment_id"])
    assert (code, body["status"]) == (200, "completed")

    if damage == "version":
        good["version"] = 99
    else:
        good["recent"] = "not-a-list"
    state_file.write_text(json.dumps(good), encoding="utf-8")
    damaged = _load_runner(monkeypatch, tmp_path)  # must not raise

    assert damaged._recent_deploys == {} and damaged._in_flight_deploys == set()
    body, code = _poll(damaged, accepted["deployment_id"])
    assert code == 404 and body["status"] == "not_found"
