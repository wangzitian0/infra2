"""#1439: the iac_runner keeps its deploy records on the /workspace volume. They were in
memory, so a restart erased them (run 41967457003). Each test restarts the module (a fresh
import over the same state directory) and reads the records back through the handlers."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from common.tests.test_iac_runner_deploy_result import _load_module, _signed_headers

SERVER = Path(__file__).parents[2] / "bootstrap/06.iac_runner/webhook_server.py"
SHA = "a" * 40
STATE = "deploy_state.json"
COORD = {
    "env": "staging",
    "ref": SHA,
    "triggered_by": "ci",
    "services": ["platform/redis"],
}
_n = [0]
_SECRET_ALREADY_SET = types.SimpleNamespace(setenv=lambda *a, **k: None)
_RESULT = types.SimpleNamespace(
    success=True, to_public_dict=lambda: {"succeeded": 1, "failed": 0, "results": []}
)


def _thread(runs: bool):
    """A stand-in for threading.Thread: it runs the deploy at once, or dies with the process."""

    class Thread:
        def __init__(self, target, args):
            self.target, self.args, self.daemon = target, args, False

        def start(self):
            if runs:
                self.target(*self.args)

    return Thread


def _runner(monkeypatch, state_dir: Path):
    """Import webhook_server afresh over `state_dir`: the process-restart stand-in."""
    sync = types.ModuleType("sync_runner")
    sync.calls = []
    sync.sync_services_by_version = lambda *a, **k: sync.calls.append(a) or _RESULT
    flask = types.ModuleType("flask")
    flask.Flask = lambda _n: types.SimpleNamespace(route=lambda *a, **k: lambda f: f)
    flask.jsonify = lambda payload: payload
    flask.request = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "sync_runner", sync)
    monkeypatch.setitem(sys.modules, "flask", flask)
    monkeypatch.setenv("IAC_RUNNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    _n[0] += 1
    return _load_module(f"ws_persist_{_n[0]}", SERVER, monkeypatch)


def _call(ws, handler, payload):
    ws.request.headers = _signed_headers(_SECRET_ALREADY_SET)
    ws.request.data, ws.request.json = b"", payload
    return getattr(ws, handler)()


def _deploy(ws, **extra):
    return _call(ws, "version_deploy", {**COORD, "wait": False, **extra})


def _status(ws, deployment_id, **extra):
    return _call(
        ws, "deployment_status", {**COORD, "deployment_id": deployment_id, **extra}
    )


def test_a_restart_between_request_and_poll_reads_back_interrupted_then_retries(
    monkeypatch, tmp_path
):
    """The record reads back as `interrupted`, never `not_found`; a second request for the
    same coordinates then runs and completes."""
    first = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _thread(False))
    accepted, code = _deploy(first)
    assert code == 202

    second = _runner(monkeypatch, tmp_path)
    body, code = _status(second, accepted["deployment_id"])
    assert (code, body["status"]) == (200, "interrupted")
    assert second._in_flight_deploys == set() and sys.modules["sync_runner"].calls == []

    monkeypatch.setattr(second.threading, "Thread", _thread(True))
    retry, code = _deploy(second)
    assert code == 202 and retry["deployment_id"] == accepted["deployment_id"]
    assert len(sys.modules["sync_runner"].calls) == 1
    assert _status(second, accepted["deployment_id"])[0]["status"] == "completed"


def test_a_result_survives_a_restart_and_a_too_old_record_does_not(
    monkeypatch, tmp_path
):
    first = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(first.threading, "Thread", _thread(True))
    deployment_id = _deploy(first)[0]["deployment_id"]

    body, code = _status(_runner(monkeypatch, tmp_path), deployment_id)
    assert (code, body["status"]) == (200, "completed")

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 2 * 3600)
    late = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(time, "time", real_time)
    assert _status(late, deployment_id)[1] == 404


def test_only_the_newest_records_are_kept(monkeypatch, tmp_path):
    ws = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(ws, "MAX_PERSISTED_DEPLOYS", 3)
    with ws._deploy_state_lock:
        for n in range(6):
            key = ws._deployment_key("staging", SHA, [f"platform/s{n}"])
            ws._recent_deploys[key] = (time.monotonic() + n, {"n": n})
        ws._save_state_locked()

    saved = json.loads((tmp_path / STATE).read_text())["recent"]
    assert [item["response"]["n"] for item in saved] == [3, 4, 5]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\xff\xfe\x00garbage", id="not-utf8"),
        pytest.param(
            b"[" * 100000 + b"]" * 100000, id="nested-past-the-recursion-limit"
        ),
        pytest.param(
            b'{"version": 1, "recent": [1], "in_flight": []}', id="wrong-shape"
        ),
    ],
)
def test_the_loader_never_raises_and_keeps_the_bad_file_aside(
    monkeypatch, tmp_path, content
):
    """An exception at import keeps every gunicorn worker from booting."""
    (tmp_path / STATE).write_bytes(content)

    ws = _runner(monkeypatch, tmp_path)  # must not raise

    assert ws._recent_deploys == {} and ws._in_flight_deploys == set()
    assert (tmp_path / (STATE + ".corrupt")).read_bytes() == content


def test_a_failed_swap_leaves_the_previous_file_whole_and_the_deploy_goes_on(
    monkeypatch, tmp_path
):
    ws = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(ws.threading, "Thread", _thread(True))
    _deploy(ws)
    before = (tmp_path / STATE).read_text()

    monkeypatch.setattr(ws.os, "replace", lambda *a: 1 / 0)  # the swap fails
    body, code = _deploy(ws, services=["platform/other"])

    assert code == 202, body
    assert (tmp_path / STATE).read_text() == before
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_concurrent_writers_do_not_corrupt_or_lose_records(monkeypatch, tmp_path):
    """Four gunicorn threads finish at once. The writer that takes the first snapshot waits
    until every record is stored, then stalls at the swap: without the lock it would swap
    its stale snapshot in last and lose records."""
    ws = _runner(monkeypatch, tmp_path)
    count = 16
    keys = [
        ws._deployment_key("staging", SHA, [f"platform/s{n}"]) for n in range(count)
    ]
    ws._in_flight_deploys.update(keys)
    owner, real_replace, real_dump = [], os.replace, json.dump

    def dump(payload, handle):  # runs once the snapshot is taken
        first = not owner
        if first:
            owner.append(threading.get_ident())
        real_dump(payload, handle)
        deadline = time.monotonic() + 0.3
        while first and len(ws._recent_deploys) < count and time.monotonic() < deadline:
            time.sleep(0.005)

    def replace(src, dst):
        if owner and threading.get_ident() == owner[0]:
            time.sleep(0.15)
        return real_replace(src, dst)

    monkeypatch.setattr(ws.json, "dump", dump)
    monkeypatch.setattr(ws.os, "replace", replace)
    barrier = threading.Barrier(count)

    def finish(n):
        barrier.wait()
        ws._run_deployment("staging", SHA, "ci", [f"platform/s{n}"])

    threads = [threading.Thread(target=finish, args=(n,)) for n in range(count)]
    [thread.start() for thread in threads]
    [thread.join(timeout=30) for thread in threads]

    saved = json.loads((tmp_path / STATE).read_text())
    assert {r["response"]["deployment_id"] for r in saved["recent"]} == {
        ws._deployment_id(k) for k in keys
    }
    assert saved["in_flight"] == []
