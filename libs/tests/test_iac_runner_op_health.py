"""#284: the iac-runner /health op check must be FUNCTIONAL, not presence-only.

A deleted/invalid 1Password service account leaves OP_SERVICE_ACCOUNT_TOKEN SET but op
broken — the original silent-green outage. The old check (`bool(env var)`) reported healthy
through it. These lock in that the check actually runs op and fails closed.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_SERVER = ROOT / "bootstrap/06.iac_runner/webhook_server.py"


def _stub_flask(monkeypatch):
    """webhook_server imports flask (a runtime-only dep, not in the test env). Stub the
    bits the module touches at import: Flask(), app.config, and @app.route."""
    flask = types.ModuleType("flask")

    class _App:
        config: dict = {}

        def route(self, *_a, **_k):
            return lambda fn: fn

    flask.Flask = lambda *_a, **_k: _App()
    flask.request = types.SimpleNamespace()
    flask.jsonify = lambda *a, **k: (a, k)
    monkeypatch.setitem(sys.modules, "flask", flask)


def _load(monkeypatch, name: str):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    _stub_flask(monkeypatch)
    spec = importlib.util.spec_from_file_location(name, WEBHOOK_SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_op_health_runs_a_real_op_call_and_passes_on_success(monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sa-token")
    ws = _load(monkeypatch, "ws_op_ok")
    ws._op_health_cache["at"] = float("-inf")  # reliably stale under any clock

    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"{}", b"")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    assert ws.op_service_account_works() is True
    # a REAL op invocation (authenticates the SA token), not a bare env-var check
    assert calls and calls[0][:2] == ["op", "whoami"]


def test_op_health_fails_closed_when_op_errors(monkeypatch):
    """Deleted/invalid SA: token present, op returns non-zero -> health False (NOT green)."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sa-token")
    ws = _load(monkeypatch, "ws_op_broken")
    ws._op_health_cache["at"] = float("-inf")
    monkeypatch.setattr(
        ws.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 1, b"", b"no account"),
    )
    assert ws.op_service_account_works() is False


def test_op_health_false_when_token_absent_without_calling_op(monkeypatch):
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    ws = _load(monkeypatch, "ws_op_absent")
    called = []
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **k: called.append(1))
    assert ws.op_service_account_works() is False
    assert called == []  # short-circuit on absent token, no op call


def test_op_health_throttles_the_op_call(monkeypatch):
    """Token validity is slow-changing — don't call the 1Password API on every /health hit."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sa-token")
    ws = _load(monkeypatch, "ws_op_throttle")
    ws._op_health_cache.update({"ok": False, "at": float("-inf")})
    n = []

    def fake_run(cmd, **_k):
        n.append(1)
        return subprocess.CompletedProcess(cmd, 0, b"{}", b"")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    assert ws.op_service_account_works() is True  # runs op
    assert ws.op_service_account_works() is True  # cached within TTL
    assert len(n) == 1  # op called only once (throttled)


def test_op_health_first_call_after_init_runs_op_not_default_cache(monkeypatch):
    """The default cache must be stale so the FIRST /health after process start runs the real
    op check, not serve the default ok=False — otherwise a fresh container whose monotonic
    clock is < TTL would report a false 503 for up to the TTL window (Copilot CR #421)."""
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sa-token")
    ws = _load(monkeypatch, "ws_op_firstcall")  # fresh module => real initializer
    assert ws._op_health_cache["at"] == float("-inf")  # always-stale sentinel
    calls = []
    monkeypatch.setattr(
        ws.subprocess,
        "run",
        lambda cmd, **_k: calls.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, b"{}", b""),
    )
    assert ws.op_service_account_works() is True
    assert calls  # op actually ran on the first call (not the default cached False)
