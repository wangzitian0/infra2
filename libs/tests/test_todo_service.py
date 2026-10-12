"""Unit tests for platform/30.todo — Canary Infrastructure Validation Tool."""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

from starlette.testclient import TestClient


from libs.core import registry as reg
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
    monkeypatch.setattr(
        todo_mod,
        "_probe_s3",
        lambda **kw: {
            "status": "pass",
            "latency_ms": 2.5,
            "detail": "mock s3 put/get/delete ok",
        },
    )

    client = TestClient(todo_mod.fastapi_app, raise_server_exceptions=False)

    # 1. /api/health
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["service"] == "platform/todo"
    assert data["domain"] == "todo.zitian.party"

    # 2. /api/canary/status
    resp = client.get("/api/canary/status")
    assert resp.status_code == 200
    status_report = resp.json()
    assert status_report["ok"] is True
    assert "checks" in status_report
    checks = status_report["checks"]
    assert set(checks.keys()) == {
        "postgres",
        "redis",
        "s3",
        "minio",
        "signoz",
        "openpanel",
        "authentik",
    }
    for name, c in checks.items():
        assert c["status"] == "pass"

    # 3. /api/todos (GET)
    resp = client.get("/api/todos")
    assert resp.status_code == 200
    todos = resp.json()
    assert isinstance(todos, list)
    assert len(todos) >= 5

    # 4. /api/todos (POST valid item)
    resp = client.post(
        "/api/todos",
        json={"title": "Canary Automated Task", "capability": "redis"},
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["title"] == "Canary Automated Task"
    assert created["capability"] == "redis"
    assert created["completed"] is False
    created_id = created["id"]

    # 5. /api/todos (POST invalid JSON -> 400 Bad Request)
    resp = client.post(
        "/api/todos",
        content=b"invalid-json{",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    err_body = resp.json()
    assert "error" in err_body

    # 5b. /api/todos (POST non-boolean completed -> 400 Bad Request)
    resp = client.post(
        "/api/todos",
        json={"title": "bad", "completed": "false"},
    )
    assert resp.status_code == 400
    err_body = resp.json()
    assert "completed" in err_body.get("error", "")

    # 6. /api/todos/{id}/toggle (PUT)
    resp = client.put(f"/api/todos/{created_id}/toggle")
    assert resp.status_code == 200
    toggled = resp.json()
    assert toggled["id"] == created_id
    assert toggled["completed"] is True

    # 7. /api/todos/{id} (DELETE)
    resp = client.delete(f"/api/todos/{created_id}")
    assert resp.status_code == 200
    del_result = resp.json()
    assert del_result["ok"] is True
    assert del_result["deleted_id"] == created_id

    # 8. /api/todos/{id} (DELETE non-existent -> 404)
    resp = client.delete(f"/api/todos/{created_id}")
    assert resp.status_code == 404

    # 9. Web UI (/)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "todo.zitian.party" in html
    assert "Canary Todo List" in html


def test_todo_app_concurrency_nonblocking(monkeypatch) -> None:
    """AC-canary.concurrency: Verify ThreadingHTTPServer does not block healthchecks during slow probe."""
    spec = importlib.util.spec_from_file_location(
        "todo_app", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    probe_started = threading.Event()

    # Simulate slow probes: each of the 6 checks sleeps 0.2s
    def _slow_probe(*args, **kwargs):
        probe_started.set()
        time.sleep(0.2)
        return {"status": "pass", "latency_ms": 200.0, "detail": "slow mock"}

    monkeypatch.setattr(todo_mod, "_probe_postgres", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_redis", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_http", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_s3", _slow_probe)

    canary_result = {}

    def _fetch_canary():
        try:
            client = TestClient(todo_mod.fastapi_app)
            resp = client.get("/api/canary/status")
            canary_result["code"] = resp.status_code
            canary_result["data"] = resp.json()
        except Exception as exc:
            canary_result["error"] = exc

    # Start slow canary probe in background thread
    t_canary = threading.Thread(target=_fetch_canary)
    t_canary.start()

    # Wait deterministically until the server actually enters the slow probe
    assert probe_started.wait(timeout=2.0), "Slow probe never started executing"

    # Health probe must return quickly (< 0.25s) even while canary probe is sleeping
    client = TestClient(todo_mod.fastapi_app)
    health_start = time.perf_counter()
    resp = client.get("/api/health")
    health_elapsed = time.perf_counter() - health_start
    assert resp.status_code == 200
    assert health_elapsed < 0.25, (
        f"Health check was blocked by slow probe! elapsed={health_elapsed}s"
    )

    t_canary.join(timeout=5)
    assert not t_canary.is_alive(), "Background canary probe timed out or hung"
    assert canary_result.get("code") == 200, (
        f"Background canary probe failed: {canary_result}"
    )
    assert "checks" in canary_result.get("data", {})


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

    # 2. Public bypass router: priority 100, no auth middleware, and it serves ONLY the
    #    health path. Evaluate the rule instead of matching its text (#991).
    from libs.tests.traefik_rules import rule_matches

    public_rule = labels_dict.get(
        "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.rule"
    )
    assert public_rule is not None
    public_rule = public_rule.replace("${ENV_DOMAIN_SUFFIX}", "").replace(
        "${INTERNAL_DOMAIN}", "zitian.party"
    )
    host = "todo.zitian.party"
    assert rule_matches(public_rule, host=host, path="/api/health")
    for path in (
        "/api/canary/status",
        "/api/canary/status/",
        "/api/health/extra",
        "/api/todos",
        "/",
    ):
        assert not rule_matches(public_rule, host=host, path=path), path
    assert not rule_matches(public_rule, host="evil.example", path="/api/health")
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

    client = TestClient(todo_mod.fastapi_app)

    # Unauthenticated /api/auth/me
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False
    assert data["username"] is None
    assert data["groups"] == []

    # Authenticated /api/auth/me with ForwardAuth headers
    auth_headers = {
        "X-authentik-username": "wangzitian0",
        "X-authentik-name": "Zitian Wang",
        "X-authentik-email": "wangzitian0@gmail.com",
        "X-authentik-groups": "admins,developers",
    }
    resp = client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    auth_data = resp.json()
    assert auth_data["authenticated"] is True
    assert auth_data["username"] == "wangzitian0"
    assert auth_data["name"] == "Zitian Wang"
    assert auth_data["email"] == "wangzitian0@gmail.com"
    assert auth_data["groups"] == ["admins", "developers"]

    # UI renders authenticated badge with name
    ui_headers = {
        "X-authentik-username": "wangzitian0",
        "X-authentik-name": "Zitian Wang",
    }
    resp = client.get("/", headers=ui_headers)
    assert resp.status_code == 200
    html = resp.text
    assert "SSO 认证通过: Zitian Wang" in html

    # UI escapes HTML entities in user header to prevent XSS
    xss_headers = {
        "X-authentik-username": "attacker",
        "X-authentik-name": "<script>alert('xss')</script>",
    }
    resp = client.get("/", headers=xss_headers)
    assert resp.status_code == 200
    html_xss = resp.text
    assert "<script>alert('xss')</script>" not in html_xss
    assert "&lt;script&gt;" in html_xss

    # HTTP HEAD requests supported without 501 error and return no body
    resp_head = client.head("/api/health")
    assert resp_head.status_code == 200
    assert resp_head.headers.get("Content-Type") == "application/json"
    assert "HEAD" in (resp_head.headers.get("Access-Control-Allow-Methods") or "")
    assert resp_head.text == ""

    # HEAD request on HTML UI root returns headers but no body
    resp_ui_head = client.head("/")
    assert resp_ui_head.status_code == 200
    assert "text/html" in (resp_ui_head.headers.get("Content-Type") or "")
    assert "HEAD" in (resp_ui_head.headers.get("Access-Control-Allow-Methods") or "")
    assert resp_ui_head.text == ""

    # OPTIONS preflight request returns 204 with CORS headers
    resp_options = client.options("/api/todos")
    assert resp_options.status_code == 204
    assert resp_options.headers.get("Access-Control-Allow-Origin") == "*"
    assert "OPTIONS" in (resp_options.headers.get("Access-Control-Allow-Methods") or "")
    assert resp_options.headers.get("Content-Length") == "0"


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
    assert platform_services is not None, (
        "Platform Services section not found in Homer config"
    )

    todo_item = None
    for item in platform_services.get("items", []):
        if item.get("name") == "Canary Todo":
            todo_item = item
            break
    assert todo_item is not None, "Canary Todo item not found in Homer config"
    assert todo_item["url"] == "https://todo{{ENV_DOMAIN_SUFFIX}}.{{INTERNAL_DOMAIN}}"
    assert todo_item["tag"] == "canary"


def test_todo_fastapi_endpoints_contract() -> None:
    """Verify Canary Todo exposes FastAPI endpoints via infra2_sdk.fastapi."""
    spec = importlib.util.spec_from_file_location(
        "todo_app_fastapi", REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    client = TestClient(todo_mod.fastapi_app)

    # 1. Standard /livez probe from infra2_sdk.fastapi.init_service
    resp_livez = client.get("/livez")
    assert resp_livez.status_code == 200
    assert resp_livez.json() == {"status": "alive", "service": "platform-todo"}

    # 2. Standard /readyz probe from infra2_sdk.fastapi.init_service
    resp_readyz = client.get("/readyz")
    assert resp_readyz.status_code in (200, 503)

    # 3. Standard /health probe from infra2_sdk.fastapi.init_service
    resp_health = client.get("/health")
    assert resp_health.status_code in (200, 503)
    health_data = resp_health.json()
    assert "status" in health_data
    assert health_data["service"] == "platform-todo"

    # 4. Backwards-compatible /api/health
    resp_api_health = client.get("/api/health")
    assert resp_api_health.status_code == 200
    assert resp_api_health.json()["service"] == "platform/todo"

    # 5. Todo REST API
    resp_todos = client.get("/api/todos")
    assert resp_todos.status_code == 200
    assert isinstance(resp_todos.json(), list)

