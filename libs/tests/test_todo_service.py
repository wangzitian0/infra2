"""Unit tests for platform/30.todo — Canary Infrastructure Validation Tool."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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
    assert meta.data_path == ""
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
        "_probe_postgres",
        lambda host, port, **kw: {
            "status": "pass",
            "latency_ms": 1.0,
            "detail": "mock postgres ok",
        },
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_redis",
        lambda host, port, **kw: {
            "status": "pass",
            "latency_ms": 0.5,
            "detail": "mock redis pong & set/get ok",
        },
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_http",
        lambda url, **kw: {
            "status": "pass",
            "latency_ms": 1.5,
            "detail": "mock http 200",
        },
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), todo_mod.TodoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # 1. /api/health
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=3
        ) as resp:
            assert resp.getcode() == 200
            data = json.loads(resp.read().decode())
            assert data["ok"] is True
            assert data["service"] == "platform/todo"
            assert data["domain"] == "todo.zitian.party"

        # 2. /api/canary/status
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/canary/status", timeout=3
        ) as resp:
            assert resp.getcode() == 200
            status_report = json.loads(resp.read().decode())
            assert status_report["ok"] is True
            assert "checks" in status_report
            checks = status_report["checks"]
            assert set(checks.keys()) == {
                "postgres",
                "redis",
                "minio",
                "signoz",
                "openpanel",
                "authentik",
            }
            for name, c in checks.items():
                assert c["status"] == "pass"

        # 3. /api/todos (GET)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/todos", timeout=3
        ) as resp:
            assert resp.getcode() == 200
            todos = json.loads(resp.read().decode())
            assert isinstance(todos, list)
            assert len(todos) >= 5

        # 4. /api/todos (POST valid item)
        post_payload = json.dumps(
            {"title": "Canary Automated Task", "capability": "redis"}
        ).encode()
        post_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/todos",
            data=post_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(post_req, timeout=3) as resp:
            assert resp.getcode() == 201
            created = json.loads(resp.read().decode())
            assert created["title"] == "Canary Automated Task"
            assert created["capability"] == "redis"
            assert created["completed"] is False
            created_id = created["id"]

        # 5. /api/todos (POST invalid JSON -> 400 Bad Request)
        bad_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/todos",
            data=b"invalid-json{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_req, timeout=3)
            assert False, "Expected HTTPError 400 on malformed JSON"
        except urllib.error.HTTPError as err:
            assert err.code == 400
            err_body = json.loads(err.read().decode())
            assert "error" in err_body

        # 5b. /api/todos (POST non-boolean completed -> 400 Bad Request)
        invalid_type_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/todos",
            data=json.dumps({"title": "bad", "completed": "false"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(invalid_type_req, timeout=3)
            assert False, "Expected HTTPError 400 on string completed field"
        except urllib.error.HTTPError as err:
            assert err.code == 400
            err_body = json.loads(err.read().decode())
            assert "completed" in err_body.get("error", "")

        # 6. /api/todos/{id}/toggle (PUT)
        toggle_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/todos/{created_id}/toggle",
            data=b"",
            method="PUT",
        )
        with urllib.request.urlopen(toggle_req, timeout=3) as resp:
            assert resp.getcode() == 200
            toggled = json.loads(resp.read().decode())
            assert toggled["id"] == created_id
            assert toggled["completed"] is True

        # 7. /api/todos/{id} (DELETE)
        del_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/todos/{created_id}",
            method="DELETE",
        )
        with urllib.request.urlopen(del_req, timeout=3) as resp:
            assert resp.getcode() == 200
            del_result = json.loads(resp.read().decode())
            assert del_result["ok"] is True
            assert del_result["deleted_id"] == created_id

        # 8. /api/todos/{id} (DELETE non-existent -> 404)
        del_404_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/todos/{created_id}",
            method="DELETE",
        )
        try:
            urllib.request.urlopen(del_404_req, timeout=3)
            assert False, "Expected HTTPError 404 on deleting deleted item"
        except urllib.error.HTTPError as err:
            assert err.code == 404

        # 9. Web UI (/)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            assert resp.getcode() == 200
            html = resp.read().decode()
            assert "todo.zitian.party" in html
            assert "Canary Todo List" in html
    finally:
        server.shutdown()
        server.server_close()


def test_todo_app_concurrency_nonblocking(monkeypatch) -> None:
    """AC-canary.concurrency: Verify ThreadingHTTPServer does not block healthchecks during slow probe."""
    spec = importlib.util.spec_from_file_location(
        "todo_app", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    probe_started = threading.Event()

    # Simulate slow probe: each of the 6 checks sleeps 0.2s (total 1.2s)
    def _slow_probe(*args, **kwargs):
        probe_started.set()
        time.sleep(0.2)
        return {"status": "pass", "latency_ms": 200.0, "detail": "slow mock"}

    monkeypatch.setattr(todo_mod, "_probe_postgres", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_redis", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_http", _slow_probe)

    server = ThreadingHTTPServer(("127.0.0.1", 0), todo_mod.TodoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    canary_result = {}

    def _fetch_canary():
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/canary/status")
            with urllib.request.urlopen(req, timeout=5) as resp:
                canary_result["code"] = resp.getcode()
                canary_result["data"] = json.loads(resp.read().decode())
        except Exception as exc:
            canary_result["error"] = exc

    try:
        # Start slow canary probe in background thread
        t_canary = threading.Thread(target=_fetch_canary)
        t_canary.start()

        # Wait deterministically until the server actually enters the slow probe
        assert probe_started.wait(timeout=2.0), "Slow probe never started executing"

        # Health probe must return quickly (< 0.25s) even while canary probe is sleeping
        health_start = time.perf_counter()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=1
        ) as resp:
            health_elapsed = time.perf_counter() - health_start
            assert resp.getcode() == 200
            assert health_elapsed < 0.25, (
                f"Health check was blocked by slow probe! elapsed={health_elapsed}s"
            )

        t_canary.join(timeout=5)
        assert not t_canary.is_alive(), "Background canary probe timed out or hung"
        assert canary_result.get("code") == 200, (
            f"Background canary probe failed: {canary_result}"
        )
        assert "checks" in canary_result.get("data", {})
    finally:
        server.shutdown()
        server.server_close()


def test_probe_redis_noauth_handshake() -> None:
    """Verify _probe_redis treats -NOAUTH challenge as physical proof of RESP engine responsiveness."""
    spec = importlib.util.spec_from_file_location(
        "todo_app", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        conn, _ = srv.accept()
        with conn:
            req = conn.recv(1024)
            if b"PING" in req:
                conn.sendall(b"-NOAUTH Authentication required.\r\n")

    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    try:
        res = todo_mod._probe_redis("127.0.0.1", port, timeout=1.0)
        assert res["status"] == "pass"
        assert "auth required" in res["detail"]
    finally:
        srv.close()


def test_probe_redis_authenticated_lifecycle() -> None:
    """Verify _probe_redis with password completes full AUTH -> PING -> SETEX -> GET cycle."""
    spec = importlib.util.spec_from_file_location(
        "todo_app", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        conn, _ = srv.accept()
        with conn:
            rf = conn.makefile("rb")
            # 1. Expect AUTH
            assert rf.readline() == b"*2\r\n"
            assert rf.readline() == b"$4\r\n"
            assert rf.readline() == b"AUTH\r\n"
            assert rf.readline() == b"$9\r\n"
            assert rf.readline() == b"secret123\r\n"
            conn.sendall(b"+OK\r\n")
            # 2. Expect PING
            assert rf.readline() == b"*1\r\n"
            assert rf.readline() == b"$4\r\n"
            assert rf.readline() == b"PING\r\n"
            conn.sendall(b"+PONG\r\n")
            # 3. Expect SETEX
            assert rf.readline() == b"*4\r\n"
            assert rf.readline() == b"$5\r\n"
            assert rf.readline() == b"SETEX\r\n"
            assert rf.readline() == b"$11\r\n"
            assert rf.readline() == b"canary:ping\r\n"
            assert rf.readline() == b"$2\r\n"
            assert rf.readline() == b"10\r\n"
            assert rf.readline() == b"$2\r\n"
            assert rf.readline() == b"ok\r\n"
            conn.sendall(b"+OK\r\n")
            # 4. Expect GET
            assert rf.readline() == b"*2\r\n"
            assert rf.readline() == b"$3\r\n"
            assert rf.readline() == b"GET\r\n"
            assert rf.readline() == b"$11\r\n"
            assert rf.readline() == b"canary:ping\r\n"
            conn.sendall(b"$2\r\nok\r\n")

    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    try:
        res = todo_mod._probe_redis("127.0.0.1", port, password="secret123", timeout=1.0)
        assert res["status"] == "pass"
        assert "PONG & SETEX/GET verified" in res["detail"]
    finally:
        srv.close()


def test_prepare_dirs_noop_on_empty_data_path(monkeypatch) -> None:
    """Verify _prepare_dirs returns True without calling ssh when data_path is empty."""
    import libs.deploy.deployer as d
    from unittest.mock import MagicMock

    cmds: list[str] = []
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setattr(d, "run_with_status", lambda c, cmd, label: cmds.append(cmd))

    class DummyStatelessDeployer(d.Deployer):
        service = "stateless-dummy"
        data_path = ""

        @classmethod
        def env(cls):
            return {"ENV": "production", "VPS_HOST": "vps"}

    result = DummyStatelessDeployer._prepare_dirs(MagicMock())
    assert result is True
    assert len(cmds) == 0


def test_todo_traefik_dual_router_auth_contract() -> None:
    """Verify compose.yaml specifies dual routers for Authentik ForwardAuth + public probe bypass."""
    import yaml

    compose_file = REPO_ROOT / "platform/30.todo/compose.yaml"
    assert compose_file.exists()
    content = yaml.safe_load(compose_file.read_text())

    labels = content["services"]["todo"]["labels"]
    labels_dict = {}
    for item in labels:
        if "=" in item:
            k, v = item.split("=", 1)
            labels_dict[k] = v

    # 1. Traefik enabled
    assert labels_dict.get("traefik.enable") == "true"

    # 2. Public bypass router: priority 100, matches health and canary status, no auth middleware
    public_rule = labels_dict.get(
        "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.rule"
    )
    assert public_rule is not None
    assert "/api/health" in public_rule
    assert "/api/canary/status" in public_rule
    assert (
        labels_dict.get(
            "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.priority"
        )
        == "100"
    )
    assert (
        "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.middlewares"
        not in labels_dict
    )

    # 3. Protected router: priority 10, protected by Authentik ForwardAuth middleware
    assert (
        labels_dict.get(
            "traefik.http.routers.platform-todo${ENV_DOMAIN_SUFFIX}.priority"
        )
        == "10"
    )
    assert (
        labels_dict.get(
            "traefik.http.routers.platform-todo${ENV_DOMAIN_SUFFIX}.middlewares"
        )
        == "todo-auth${ENV_DOMAIN_SUFFIX}@docker"
    )

    # 4. Middleware forwardauth points to authentik server
    mw_addr = labels_dict.get(
        "traefik.http.middlewares.todo-auth${ENV_DOMAIN_SUFFIX}.forwardauth.address"
    )
    assert mw_addr is not None
    assert "platform-authentik-server${ENV_SUFFIX}:9000" in mw_addr
    assert "/outpost.goauthentik.io/auth/traefik" in mw_addr


def test_todo_app_authentik_identity_and_me_endpoint() -> None:
    """Verify /api/auth/me and UI rendering under Authentik ForwardAuth headers."""
    spec = importlib.util.spec_from_file_location(
        "todo_app", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    server = ThreadingHTTPServer(("127.0.0.1", 0), todo_mod.TodoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        # Unauthenticated /api/auth/me
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/auth/me", timeout=3
        ) as resp:
            assert resp.getcode() == 200
            data = json.loads(resp.read().decode())
            assert data["authenticated"] is False
            assert data["username"] is None
            assert data["groups"] == []

        # Authenticated /api/auth/me with ForwardAuth headers
        auth_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/auth/me",
            headers={
                "X-authentik-username": "wangzitian0",
                "X-authentik-name": "Zitian Wang",
                "X-authentik-email": "wangzitian0@gmail.com",
                "X-authentik-groups": "admins,developers",
            },
        )
        with urllib.request.urlopen(auth_req, timeout=3) as resp:
            assert resp.getcode() == 200
            auth_data = json.loads(resp.read().decode())
            assert auth_data["authenticated"] is True
            assert auth_data["username"] == "wangzitian0"
            assert auth_data["name"] == "Zitian Wang"
            assert auth_data["email"] == "wangzitian0@gmail.com"
            assert auth_data["groups"] == ["admins", "developers"]

        # UI renders authenticated badge with name
        ui_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            headers={
                "X-authentik-username": "wangzitian0",
                "X-authentik-name": "Zitian Wang",
            },
        )
        with urllib.request.urlopen(ui_req, timeout=3) as resp:
            assert resp.getcode() == 200
            html = resp.read().decode()
            assert "SSO 认证通过: Zitian Wang" in html
    finally:
        server.shutdown()
        server.server_close()


def test_portal_config_template_contains_canary_todo() -> None:
    """Verify platform/21.portal/config.yml.tmpl includes Canary Todo card."""
    import yaml

    portal_tmpl = REPO_ROOT / "platform/21.portal/config.yml.tmpl"
    assert portal_tmpl.exists()
    content = yaml.safe_load(portal_tmpl.read_text())

    platform_services = None
    for group in content.get("services", []):
        if group.get("name") == "Platform Services":
            platform_services = group
            break
    assert platform_services is not None, "Platform Services section not found in Homer config"

    todo_item = None
    for item in platform_services.get("items", []):
        if item.get("name") == "Canary Todo":
            todo_item = item
            break
    assert todo_item is not None, "Canary Todo item not found in Homer config"
    assert todo_item["url"] == "https://todo{{ENV_DOMAIN_SUFFIX}}.{{INTERNAL_DOMAIN}}"
    assert todo_item["tag"] == "canary"

