"""Unit tests for platform/30.todo — Canary Infrastructure Validation Tool."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path


from libs import service_registry as reg
from libs.deploy.deployer import discover_services

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_todo_service_registered_in_service_registry() -> None:
    attrs = reg.service_attrs()
    assert "platform/todo" in attrs

    meta = attrs["platform/todo"]
    assert meta.service == "todo"
    assert meta.subdomain is None
    assert meta.service_port == 8000
    assert meta.service_name == "todo"
    assert meta.compose_path == "platform/30.todo/compose.yaml"
    assert meta.data_path == "/data/platform/todo"
    assert meta.prod_only is False


def test_todo_service_discovered_by_deployer() -> None:
    discovered = discover_services()
    assert "platform/todo" in discovered
    assert discovered["platform/todo"] == "todo.sync"


def test_todo_app_endpoints_contract(monkeypatch) -> None:
    """Verify Todo app HTTP endpoints respond per the platform contract."""
    spec = importlib.util.spec_from_file_location(
        "todo_app", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    # Mock probes to run deterministically in unit test
    monkeypatch.setattr(
        todo_mod,
        "_probe_tcp",
        lambda host, port, **kw: {"status": "pass", "latency_ms": 1.0, "detail": "mock ok"},
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_redis",
        lambda host, port, **kw: {"status": "pass", "latency_ms": 0.5, "detail": "mock pong"},
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_http",
        lambda url, **kw: {"status": "pass", "latency_ms": 1.5, "detail": "mock http 200"},
    )

    server = HTTPServer(("127.0.0.1", 0), todo_mod.TodoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # 1. /api/health
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            assert resp.getcode() == 200
            data = json.loads(resp.read().decode())
            assert data["ok"] is True
            assert data["service"] == "platform/todo"
            assert data["domain"] == "todo.zitian.party"

        # 2. /api/canary/status
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/canary/status", timeout=3) as resp:
            assert resp.getcode() == 200
            status_report = json.loads(resp.read().decode())
            assert status_report["ok"] is True
            assert "checks" in status_report
            checks = status_report["checks"]
            assert set(checks.keys()) == {"postgres", "redis", "minio", "signoz", "openpanel", "authentik"}
            for name, c in checks.items():
                assert c["status"] == "pass"

        # 3. /api/todos (GET & POST)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/todos", timeout=3) as resp:
            assert resp.getcode() == 200
            todos = json.loads(resp.read().decode())
            assert isinstance(todos, list)
            assert len(todos) >= 5

        # 4. Web UI (/)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            assert resp.getcode() == 200
            html = resp.read().decode()
            assert "todo.zitian.party" in html
            assert "Canary Todo List" in html
    finally:
        server.shutdown()
        server.server_close()
