"""Unit tests for platform/30.todo — Canary Infrastructure Validation Tool."""

from __future__ import annotations

import importlib.util
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from starlette.testclient import TestClient


from common.core import registry as reg
from common.deploy.deployer import discover_services
from common.tests.ambient_env import clear_telemetry_env

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_ambient_telemetry_env(monkeypatch):
    """The app starts telemetry while it loads. The shell of a developer must not decide
    whether it loads (#1209)."""
    clear_telemetry_env(monkeypatch)


class _FakeTodoCursor:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeTodoPgConnection:
    def __init__(self, store: "_FakeTodoPgStore") -> None:
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _FakeTodoCursor:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT to_regclass"):
            return _FakeTodoCursor([("todos" if self._store.table_created else None,)])
        if normalized.startswith("CREATE TABLE IF NOT EXISTS todos"):
            self._store.table_created = True
            return _FakeTodoCursor()
        if normalized.startswith(
            "SELECT id, title, completed, capability, created_at FROM todos"
        ):
            return _FakeTodoCursor(
                [
                    (
                        item["id"],
                        item["title"],
                        item["completed"],
                        item["capability"],
                        item["created_at"],
                    )
                    for item in sorted(self._store.rows, key=lambda r: r["id"])
                ]
            )
        if normalized.startswith("INSERT INTO todos"):
            title, completed, capability = params
            self._store.next_id += 1
            row = {
                "id": self._store.next_id,
                "title": title,
                "completed": bool(completed),
                "capability": capability,
                "created_at": datetime(2026, 11, 17, 5, 0, 0, tzinfo=timezone.utc),
            }
            self._store.rows.append(row)
            return _FakeTodoCursor(
                [
                    (
                        row["id"],
                        row["title"],
                        row["completed"],
                        row["capability"],
                        row["created_at"],
                    )
                ]
            )
        if normalized.startswith("UPDATE todos SET"):
            set_clause = normalized.split("UPDATE todos SET ", 1)[1].split(
                " WHERE id = %s", 1
            )[0]
            todo_id = params[-1]
            values = list(params[:-1])
            target = next((r for r in self._store.rows if r["id"] == todo_id), None)
            if target is None:
                return _FakeTodoCursor([])
            idx = 0
            for assignment in (part.strip() for part in set_clause.split(",")):
                if assignment == "completed = NOT completed":
                    target["completed"] = not target["completed"]
                elif assignment == "title = %s":
                    target["title"] = values[idx]
                    idx += 1
                elif assignment == "completed = %s":
                    target["completed"] = bool(values[idx])
                    idx += 1
                elif assignment == "capability = %s":
                    target["capability"] = values[idx]
                    idx += 1
            return _FakeTodoCursor(
                [
                    (
                        target["id"],
                        target["title"],
                        target["completed"],
                        target["capability"],
                        target["created_at"],
                    )
                ]
            )
        if normalized.startswith("DELETE FROM todos WHERE id = %s"):
            todo_id = params[0]
            for pos, row in enumerate(self._store.rows):
                if row["id"] == todo_id:
                    removed = self._store.rows.pop(pos)
                    return _FakeTodoCursor(
                        [
                            (
                                removed["id"],
                                removed["title"],
                                removed["completed"],
                                removed["capability"],
                                removed["created_at"],
                            )
                        ]
                    )
            return _FakeTodoCursor([])
        raise AssertionError(f"Unexpected SQL in fake todo DB: {sql}")


class _FakeTodoPgStore:
    def __init__(self) -> None:
        self.table_created = False
        self.next_id = 0
        self.rows: list[dict] = []

    def connect(self, *args, **kwargs) -> _FakeTodoPgConnection:
        return _FakeTodoPgConnection(self)


def _wire_fake_todo_pg(
    monkeypatch, store: _FakeTodoPgStore | None = None
) -> _FakeTodoPgStore:
    pg_store = store or _FakeTodoPgStore()
    monkeypatch.setenv("POSTGRES_HOST", "platform-postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("TODO_POSTGRES_USER", "todo_app")
    monkeypatch.setenv("TODO_POSTGRES_PASSWORD", "test_todo_pg_pass_1234")
    monkeypatch.setenv("TODO_POSTGRES_DATABASE", "postgres")
    monkeypatch.setattr(psycopg, "connect", pg_store.connect)
    return pg_store


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
    assert data["environment"] == "unknown"
    assert data["configuration_sha256"] == "unknown"
    assert data["version"] == "unknown"
    assert data["git_sha"] == "unknown"

    monkeypatch.setenv("GIT_COMMIT_SHA", "abcdef1234567890")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("CONFIGURATION_SHA256", "deadbeef01234567")
    reloaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reloaded)
    reloaded_client = TestClient(reloaded.fastapi_app, raise_server_exceptions=False)
    resp_ver = reloaded_client.get("/api/health")
    assert resp_ver.status_code == 200
    data_ver = resp_ver.json()
    assert data_ver["environment"] == "staging"
    assert data_ver["configuration_sha256"] == "deadbeef01234567"
    assert data_ver["version"] == "abcdef1234567890"
    assert data_ver["git_sha"] == "abcdef1234567890"
    assert reloaded_client.get("/health").json()["git_sha"] == "abcdef1234567890"
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("CONFIGURATION_SHA256", raising=False)

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
        "signoz",
        "openpanel",
        "authentik",
    }
    assert "minio" not in checks
    for name, c in checks.items():
        assert c["status"] == "pass"

    # 3. /api/todos fails closed with 503 before PostgreSQL is configured
    assert client.get("/api/todos").status_code == 503

    # Wire fake persistent PostgreSQL store for CRUD roundtrips
    _wire_fake_todo_pg(monkeypatch)

    # 3b. /api/todos (GET on clean database)
    resp = client.get("/api/todos")
    assert resp.status_code == 200
    assert resp.json() == []

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

    # 6. /api/todos/{id} (PATCH empty toggle + explicit field update)
    resp = client.patch(f"/api/todos/{created_id}")
    assert resp.status_code == 200
    toggled = resp.json()
    assert toggled["id"] == created_id
    assert toggled["completed"] is True

    resp = client.patch(
        f"/api/todos/{created_id}",
        json={"title": "Updated Canary Task", "completed": False},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["title"] == "Updated Canary Task"
    assert updated["completed"] is False

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
    import common.deploy.deployer as d
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
    from common.tests.traefik_rules import rule_matches

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


def _raise_boom() -> dict:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ({"ok": False, "unconfigured": ["s3"]}, "database: unconfigured=s3"),
        ({"ok": False, "failed": ["redis"]}, "database: failed=redis"),
        (
            {"ok": False, "failed": ["redis"], "unconfigured": ["s3"]},
            "database: failed=redis; unconfigured=s3",
        ),
        ({"ok": False}, "database: canary checks not ok"),
        ({}, "database: canary checks not ok"),
        (_raise_boom, "database: canary status check failed: boom"),
    ],
    ids=[
        "unconfigured",
        "failed",
        "failed-and-unconfigured",
        "not-ok-without-a-list",
        "empty-document",
        "status-call-raises",
    ],
)
@pytest.mark.parametrize("path", ["/readyz", "/health"])
def test_todo_readiness_fails_closed_and_names_the_reason(
    monkeypatch, path: str, status, reason: str
) -> None:
    """A red, unconfigured or unreadable canary status is never ready. `ok` is the verdict:
    an empty `failed` list does not clear a document that says `ok: false`, and a status
    call that raises is not ready (#1209)."""
    todo_mod = _load_todo_module("todo_app_fail_closed")
    client = TestClient(todo_mod.fastapi_app)
    monkeypatch.setattr(
        todo_mod,
        "cached_canary_status",
        status if callable(status) else (lambda: dict(status)),
    )

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["reasons"] == [reason]
    assert response.json()["status"] in ("not_ready", "unhealthy")


@pytest.mark.parametrize("path", ["/readyz", "/health"])
def test_todo_readiness_passes_only_on_an_ok_document(monkeypatch, path: str) -> None:
    todo_mod = _load_todo_module("todo_app_ready")
    client = TestClient(todo_mod.fastapi_app)
    monkeypatch.setattr(todo_mod, "cached_canary_status", lambda: {"ok": True})
    assert client.get(path).status_code == 200


def test_todo_fastapi_endpoints_contract(monkeypatch) -> None:
    """Verify Canary Todo exposes FastAPI endpoints with strict OpenAPI schemas."""
    _wire_fake_todo_pg(monkeypatch)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        todo_mod = _load_todo_module("todo_app_fastapi")
    assert caught == [], [str(w.message) for w in caught]

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

    # 5. OpenAPI schemas on all JSON routes, typed request bodies, and unique operationIds
    openapi_res = client.get("/openapi.json")
    assert openapi_res.status_code == 200
    spec_doc = openapi_res.json()
    paths = spec_doc["paths"]
    for rp, method in (
        ("/health", "get"),
        ("/livez", "get"),
        ("/readyz", "get"),
        ("/api/health", "get"),
        ("/api/canary/status", "get"),
        ("/api/todos", "post"),
        ("/api/todos/{todo_id}", "patch"),
        ("/api/todos/{todo_id}", "delete"),
        ("/api/auth/me", "get"),
    ):
        resp_200 = paths[rp][method]["responses"].get("200") or paths[rp][method][
            "responses"
        ].get("201")
        schema = resp_200["content"]["application/json"]["schema"]
        assert "$ref" in schema, f"{method.upper()} {rp} missing $ref schema: {schema}"
    todos_get_schema = paths["/api/todos"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert todos_get_schema["type"] == "array"
    assert "$ref" in todos_get_schema["items"]
    for write_method, write_path in (
        ("post", "/api/todos"),
        ("patch", "/api/todos/{todo_id}"),
    ):
        req_schema = paths[write_path][write_method]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert "$ref" in req_schema or "anyOf" in req_schema, (
            f"{write_method.upper()} {write_path} missing Pydantic requestBody schema: {req_schema}"
        )
    assert "/api/todos/{todo_id}/toggle" not in paths
    op_ids = [
        op["operationId"]
        for path_item in paths.values()
        for op in path_item.values()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert len(op_ids) == len(set(op_ids))

    # 6. Todo REST API
    resp_todos = client.get("/api/todos")
    assert resp_todos.status_code == 200
    assert isinstance(resp_todos.json(), list)


def test_todo_persists_across_module_reloads(monkeypatch) -> None:
    """Todos stored via one app instance remain visible after reloading app.py."""
    store = _wire_fake_todo_pg(monkeypatch)
    mod_a = _load_todo_module("todo_app_reload_a")
    client_a = TestClient(mod_a.fastapi_app)
    created = client_a.post(
        "/api/todos",
        json={"title": "Survive Restart", "capability": "postgres"},
    )
    assert created.status_code == 201
    todo_id = created.json()["id"]

    mod_b = _load_todo_module("todo_app_reload_b")
    client_b = TestClient(mod_b.fastapi_app)
    listed = client_b.get("/api/todos")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [todo_id]
    assert listed.json()[0]["title"] == "Survive Restart"
    assert len(store.rows) == 1


def test_todo_db_unavailable_fails_closed_with_503(monkeypatch) -> None:
    """Missing DB credentials or psycopg OperationalError returns HTTP 503."""
    todo_mod = _load_todo_module("todo_app_503")
    client = TestClient(todo_mod.fastapi_app, raise_server_exceptions=False)
    for key in (
        "POSTGRES_HOST",
        "TODO_POSTGRES_USER",
        "TODO_POSTGRES_PASSWORD",
        "TODO_POSTGRES_DATABASE",
    ):
        monkeypatch.delenv(key, raising=False)
    res_unconfigured = client.get("/api/todos")
    assert res_unconfigured.status_code == 503
    assert "error" in res_unconfigured.json()

    monkeypatch.setenv("POSTGRES_HOST", "platform-postgres")
    monkeypatch.setenv("TODO_POSTGRES_USER", "todo_app")
    monkeypatch.setenv("TODO_POSTGRES_PASSWORD", "test_todo_pg_pass_1234")
    monkeypatch.setenv("TODO_POSTGRES_DATABASE", "postgres")

    def _boom(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _boom)
    res_down = client.post("/api/todos", json={"title": "Should Fail Closed"})
    assert res_down.status_code == 503
    assert "error" in res_down.json()
