"""Unit tests for platform/30.todo — Canary Infrastructure Validation Tool."""

from __future__ import annotations

import importlib.util
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
    """AC-canary.concurrency: a slow canary run must not block a health call.

    Both requests go through ONE ASGI client on ONE event loop. A `TestClient` per request
    gives each request its own loop in its own thread, so a blocked loop would go unseen.
    The canary endpoint is a sync `def`: FastAPI runs it in a worker thread and the loop
    stays free. An `async def` endpoint that blocks would hold the loop, the canary call
    would finish first, and this test would fail.
    """
    import asyncio

    import httpx

    todo_mod = _load_todo_module("todo_app_concurrency")

    # Simulate slow probes: each check sleeps 0.5 s inside its worker thread
    def _slow_probe(*args, **kwargs):
        time.sleep(0.5)
        return {"status": "pass", "latency_ms": 500.0, "detail": "slow mock"}

    monkeypatch.setattr(todo_mod, "_probe_postgres", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_redis", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_http", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_s3", _slow_probe)

    finished: list[str] = []

    async def scenario():
        transport = httpx.ASGITransport(app=todo_mod.fastapi_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://todo.test"
        ) as client:

            async def canary():
                response = await client.get("/api/canary/status")
                finished.append("canary")
                return response

            async def health():
                await asyncio.sleep(0.05)  # the canary request is in flight by now
                response = await client.get("/api/health")
                finished.append("health")
                return response

            return await asyncio.gather(canary(), health())

    canary_resp, health_resp = asyncio.run(scenario())

    assert health_resp.status_code == 200
    assert finished == ["health", "canary"], (
        "the health call must finish while the slow canary run is still going"
    )
    assert canary_resp.status_code == 200, canary_resp.text
    assert "checks" in canary_resp.json()


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


def _load_todo_module(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "platform/30.todo/app.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_todo_readyz_and_health_follow_the_canary_status(monkeypatch) -> None:
    """/readyz and /health answer from the canary status: 200 when every write path
    passes, 503 with the failed checks as the reason when one fails (#1209)."""
    todo_mod = _load_todo_module("todo_app_readiness")
    client = TestClient(todo_mod.fastapi_app)

    # `_canary_readiness_checker` looks `cached_canary_status` up at call time
    monkeypatch.setattr(todo_mod, "cached_canary_status", lambda: {"ok": True})
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "service": "platform-todo"}
    healthy = client.get("/health")
    assert healthy.status_code == 200
    assert healthy.json()["status"] == "healthy"
    assert healthy.json()["service"] == "platform-todo"
    assert healthy.json()["reasons"] == []
    assert healthy.json()["checks"]["database"]["detail"] == (
        "canary write paths passed"
    )

    monkeypatch.setattr(
        todo_mod,
        "cached_canary_status",
        lambda: {"ok": False, "failed": ["redis"]},
    )
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json() == {
        "status": "not_ready",
        "service": "platform-todo",
        "reasons": ["database: failed=redis"],
    }
    unhealthy = client.get("/health")
    assert unhealthy.status_code == 503
    assert unhealthy.json()["status"] == "unhealthy"
    assert unhealthy.json()["service"] == "platform-todo"
    assert unhealthy.json()["reasons"] == ["database: failed=redis"]
    assert unhealthy.json()["checks"]["database"]["status"] == "absent"


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

    # 2. /readyz and 3. /health follow the canary status; the next test pins both answers

    # 4. Backwards-compatible /api/health
    resp_api_health = client.get("/api/health")
    assert resp_api_health.status_code == 200
    assert resp_api_health.json()["service"] == "platform/todo"

    # 5. Todo REST API
    resp_todos = client.get("/api/todos")
    assert resp_todos.status_code == 200
    assert isinstance(resp_todos.json(), list)
