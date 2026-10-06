"""Canary depth for platform/30.todo (#991).

The canary exists to prove the platform's real paths. Before #991 it passed on `-NOAUTH`,
on a Postgres `E` reply and on S3 liveness, nobody read its status, and it sent no
telemetry. These tests pin the opposite behaviour:

1. Redis needs a real AUTH, then SETEX/GET/DEL of a random value. NOAUTH fails.
2. Postgres needs a real login as a dedicated role and `SELECT 1`. An `E` reply fails.
3. S3 needs PUT, GET and DELETE of one object under a canary prefix.
4. The probe runner consumes `/api/canary/status` through a ProbeFacet.
5. Only `/api/health` is routed without SSO.
6. The service configures OpenTelemetry from the identity the deploy issues.
"""

from __future__ import annotations

import contextlib
import contextvars
import http.server
import json
import importlib.metadata
import importlib.util
import re
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from libs import secrets_registry
from libs import service_registry as reg
from libs.observability.probe_specs import render_probe_spec_text
from libs.tests.compose_env import compose_services, container_env, resolve
from libs.tests.docker_host import DockerHost
from libs.tests.traefik_rules import routers_from_labels, serving_router

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "platform/30.todo"
COMPOSE = SERVICE_DIR / "compose.yaml"
COLLECTOR_OTLP_HTTP = "http://platform-signoz-otel-collector:4318"
# Credentials are generated per run. A committed literal would trip secret scanners and
# would prove nothing: no test depends on a particular value.
PG_PASSWORD = secrets.token_urlsafe(24)
REDIS_PW = secrets.token_urlsafe(16)
WRONG_PW = secrets.token_urlsafe(16)
S3_ACCESS_KEY = "AK" + secrets.token_hex(8).upper()
S3_SECRET_KEY = secrets.token_urlsafe(24)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def app():
    return _load("todo_app_depth", SERVICE_DIR / "app.py")


@pytest.fixture()
def deploy():
    return _load("todo_deploy_depth", SERVICE_DIR / "deploy.py")


# --------------------------------------------------------------------------- fakes


class FakeRedis:
    """An in-memory Redis speaking RESP, with `requirepass` semantics."""

    def __init__(self, password=None, *, ping_reply=None, get_reply=None):
        self.password = password
        self.ping_reply = ping_reply
        self.get_reply = get_reply
        self.store: dict[str, bytes] = {}
        self.commands: list[list[str]] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_command(rf) -> list[str] | None:
        header = rf.readline()
        if not header:
            return None
        assert header.startswith(b"*"), header
        parts = []
        for _ in range(int(header[1:])):
            length = int(rf.readline()[1:])
            parts.append(rf.read(length + 2)[:-2].decode())
        return parts

    def _handle(self, conn: socket.socket) -> None:
        authed = self.password is None
        with conn, conn.makefile("rb") as rf:
            while True:
                command = self._read_command(rf)
                if command is None:
                    return
                self.commands.append(command)
                name = command[0].upper()
                if name == "AUTH":
                    if self.password is None:
                        conn.sendall(
                            b"-ERR AUTH called without any password configured\r\n"
                        )
                    elif command[-1] == self.password:
                        authed = True
                        conn.sendall(b"+OK\r\n")
                    else:
                        conn.sendall(b"-WRONGPASS invalid username-password pair\r\n")
                elif not authed:
                    conn.sendall(b"-NOAUTH Authentication required.\r\n")
                elif name == "PING":
                    conn.sendall(self.ping_reply or b"+PONG\r\n")
                elif name == "SETEX":
                    self.store[command[1]] = command[3].encode()
                    conn.sendall(b"+OK\r\n")
                elif name == "GET":
                    value = self.store.get(command[1])
                    if self.get_reply is not None:
                        conn.sendall(self.get_reply)
                    elif value is None:
                        conn.sendall(b"$-1\r\n")
                    else:
                        conn.sendall(b"$%d\r\n%s\r\n" % (len(value), value))
                elif name == "DEL":
                    conn.sendall(
                        b":%d\r\n" % (1 if self.store.pop(command[1], None) else 0)
                    )
                else:
                    conn.sendall(b"-ERR unknown command\r\n")


class FakePostgresRefusal:
    """Answers a login with a Postgres ErrorResponse (`E`), as a full server does."""

    def __init__(self, message: str = "sorry, too many clients already"):
        body = b"SFATAL\0VFATAL\0C53300\0M" + message.encode() + b"\0\0"
        self.response = b"E" + struct.pack("!I", 4 + len(body)) + body
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        self.startup_params: bytes = b""
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError("client closed")
            data += chunk
        return data

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                with conn:
                    while True:
                        (length,) = struct.unpack("!I", self._recv_exact(conn, 4))
                        payload = self._recv_exact(conn, length - 4)
                        (code,) = struct.unpack("!I", payload[:4])
                        if code in (80877103, 80877104):  # SSLRequest, GSSENCRequest
                            conn.sendall(b"N")
                            continue
                        self.startup_params = payload[4:]
                        conn.sendall(self.response)
                        break
            except (ConnectionError, OSError):
                continue


class FakeS3:
    """A path-style S3 endpoint storing objects in memory."""

    def __init__(self, *, deny: tuple[str, ...] = (), corrupt_get: bool = False):
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                auth = self.headers.get("Authorization", "")
                access_key = re.search(r"Credential=([^/]+)/", auth)
                outer.requests.append(
                    (self.command, self.path, access_key.group(1) if access_key else "")
                )
                if self.command in deny:
                    self._reply(403, b"<Error><Code>AccessDenied</Code></Error>")
                elif self.command == "PUT":
                    outer.objects[self.path] = body
                    self._reply(200)
                elif self.command == "GET":
                    if self.path not in outer.objects:
                        self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")
                    elif corrupt_get:
                        self._reply(200, b"not-what-was-written")
                    else:
                        self._reply(200, outer.objects[self.path])
                elif self.command == "DELETE":
                    outer.objects.pop(self.path, None)
                    self._reply(204)
                else:
                    self._reply(405)

            do_PUT = do_GET = do_DELETE = do_HEAD = _handle

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ------------------------------------------------------------------------- 1. Redis


def test_redis_probe_reports_unconfigured_without_a_password(app) -> None:
    """The production canary ran with an empty REDIS_PASSWORD and passed. No password
    still keeps the check red, but as `unconfigured`: a configuration gap is not a
    write-path failure, and an operator must be able to tell the two apart."""
    redis = FakeRedis(password=REDIS_PW)
    try:
        result = app._probe_redis("127.0.0.1", redis.port, password=None, timeout=1.0)
    finally:
        redis.close()
    assert result["status"] == "unconfigured"
    assert "REDIS_PASSWORD" in result["detail"]
    assert redis.store == {}
    assert redis.commands == [], "an unconfigured check sends nothing"


def test_redis_probe_fails_on_noauth_after_auth_reports_ok(app) -> None:
    """A server that accepts AUTH but then answers `-NOAUTH` must fail, never pass."""
    redis = FakeRedis(
        password=REDIS_PW, ping_reply=b"-NOAUTH Authentication required.\r\n"
    )
    try:
        result = app._probe_redis(
            "127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0
        )
    finally:
        redis.close()
    assert result["status"] == "fail"
    assert "NOAUTH" in result["detail"]


def test_redis_probe_fails_on_rejected_credentials_without_leaking_them(app) -> None:
    redis = FakeRedis(password=REDIS_PW)
    try:
        result = app._probe_redis(
            "127.0.0.1", redis.port, password=WRONG_PW, timeout=1.0
        )
    finally:
        redis.close()
    assert result["status"] == "fail"
    assert "AUTH failed" in result["detail"]
    assert WRONG_PW not in result["detail"]


def test_redis_probe_writes_reads_and_deletes_a_random_value(app) -> None:
    redis = FakeRedis(password=REDIS_PW)
    try:
        result = app._probe_redis(
            "127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0
        )
    finally:
        redis.close()
    assert result["status"] == "pass", result
    names = [command[0].upper() for command in redis.commands]
    assert names == ["AUTH", "PING", "SETEX", "GET", "DEL"]
    setex = next(c for c in redis.commands if c[0].upper() == "SETEX")
    get = next(c for c in redis.commands if c[0].upper() == "GET")
    assert get[1] == setex[1] and setex[1].startswith("canary:")
    assert int(setex[2]) > 0, (
        "the key needs a TTL so a crashed probe leaves nothing behind"
    )
    assert redis.store == {}, "the probe key must be deleted"


def test_redis_probe_values_differ_between_runs(app) -> None:
    """A constant value lets a stale key from the previous run pass a broken SETEX."""
    redis = FakeRedis(password=REDIS_PW)
    try:
        for _ in range(2):
            app._probe_redis("127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0)
    finally:
        redis.close()
    values = [c[3] for c in redis.commands if c[0].upper() == "SETEX"]
    assert len(values) == 2 and values[0] != values[1]


def test_redis_probe_fails_when_get_returns_a_different_value(app) -> None:
    redis = FakeRedis(password=REDIS_PW, get_reply=b"$5\r\nstale\r\n")
    try:
        result = app._probe_redis(
            "127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0
        )
    finally:
        redis.close()
    assert result["status"] == "fail"
    assert "GET returned" in result["detail"]


# ---------------------------------------------------------------------- 2. Postgres


class _FakeConnection:
    def __init__(self):
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.statements.append(sql)
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        return None


@pytest.mark.parametrize(
    ("user", "password"), [("", ""), ("canary_ro", ""), ("", "x" * 20)]
)
def test_postgres_probe_reports_unconfigured_without_credentials(
    app, user, password
) -> None:
    result = app._probe_postgres(
        "127.0.0.1", 1, user=user, password=password, timeout=1.0
    )
    assert result["status"] == "unconfigured"
    assert "CANARY_POSTGRES" in result["detail"]


def test_postgres_probe_fails_when_the_server_answers_an_error_response(app) -> None:
    """Before #991 any `E` reply passed. A server that refuses the login is not healthy."""
    server = FakePostgresRefusal("sorry, too many clients already")
    try:
        result = app._probe_postgres(
            "127.0.0.1",
            server.port,
            user="canary_ro",
            password=PG_PASSWORD,
            timeout=2.0,
        )
    finally:
        server.close()
    assert result["status"] == "fail", result
    assert "too many clients" in result["detail"]
    assert PG_PASSWORD not in result["detail"]
    assert b"canary_ro" in server.startup_params, (
        "the probe must log in as the canary role"
    )


def test_postgres_probe_logs_in_as_the_canary_role_and_selects_one(app) -> None:
    seen: dict[str, object] = {}
    connection = _FakeConnection()

    def connector(dsn, **kwargs):
        seen["dsn"] = dsn
        seen["kwargs"] = kwargs
        return connection

    password = "p@ss:w/rd#" + secrets.token_hex(4) + "?x"
    result = app._probe_postgres(
        "platform-postgres-staging",
        5432,
        user="canary_ro",
        password=password,
        dbname="postgres",
        timeout=2.0,
        connector=connector,
    )
    assert result["status"] == "pass", result
    parts = urlsplit(seen["dsn"])
    assert (parts.hostname, parts.port, parts.path) == (
        "platform-postgres-staging",
        5432,
        "/postgres",
    )
    assert parts.username == "canary_ro"
    assert unquote(parts.password) == password
    assert connection.statements == ["SELECT 1"]
    assert "canary_ro" in result["detail"]


def test_postgres_probe_fails_when_the_connector_raises(app) -> None:
    def connector(dsn, **kwargs):
        raise ConnectionError(f"could not connect using {dsn}")

    result = app._probe_postgres(
        "h", 5432, user="canary_ro", password=PG_PASSWORD, connector=connector
    )
    assert result["status"] == "fail"
    assert PG_PASSWORD not in result["detail"], "the SDK redacts the DSN from errors"


# -------------------------------------------------------------------------- 3. S3


def _s3_args(server: FakeS3) -> dict[str, object]:
    return {
        "endpoint_url": server.url,
        "bucket": "platform-canary",
        "access_key": S3_ACCESS_KEY,
        "secret_key": S3_SECRET_KEY,
        "timeout": 3.0,
    }


def test_s3_probe_puts_gets_and_deletes_one_object_under_the_canary_prefix(app) -> None:
    server = FakeS3()
    try:
        result = app._probe_s3(**_s3_args(server))
        methods = [method for method, _path, _key in server.requests]
        paths = {path for _method, path, _key in server.requests}
        keys = {key for _method, _path, key in server.requests}
        leftover = dict(server.objects)
    finally:
        server.close()
    assert result["status"] == "pass", result
    assert methods == ["PUT", "GET", "DELETE"]
    assert len(paths) == 1
    assert next(iter(paths)).startswith("/platform-canary/canary/")
    assert keys == {S3_ACCESS_KEY}, "the request must be signed with the canary key"
    assert leftover == {}, "the canary object must be deleted"


def test_s3_probe_fails_when_the_put_is_denied(app) -> None:
    """Liveness passed while writes failed. A denied PUT must fail the check."""
    server = FakeS3(deny=("PUT",))
    try:
        result = app._probe_s3(**_s3_args(server))
    finally:
        server.close()
    assert result["status"] == "fail"
    assert "AccessDenied" in result["detail"]


def test_s3_probe_fails_on_corrupt_read_and_still_cleans_up(app) -> None:
    server = FakeS3(corrupt_get=True)
    try:
        result = app._probe_s3(**_s3_args(server))
        methods = [method for method, _path, _key in server.requests]
        leftover = dict(server.objects)
    finally:
        server.close()
    assert result["status"] == "fail"
    assert "differs" in result["detail"]
    assert methods[-1] == "DELETE" and leftover == {}


def test_s3_probe_fails_when_the_delete_is_denied(app) -> None:
    server = FakeS3(deny=("DELETE",))
    try:
        result = app._probe_s3(**_s3_args(server))
    finally:
        server.close()
    assert result["status"] == "fail"
    assert "AccessDenied" in result["detail"]


@pytest.mark.parametrize("missing", ["bucket", "access_key", "secret_key"])
def test_s3_probe_reports_unconfigured_when_credentials_are_missing(
    app, missing
) -> None:
    server = FakeS3()
    try:
        args = _s3_args(server)
        args[missing] = ""
        result = app._probe_s3(**args)
        requests = list(server.requests)
    finally:
        server.close()
    assert result["status"] == "unconfigured"
    assert "CANARY_S3" in result["detail"]
    assert requests == [], "no request may be sent without credentials"


def test_s3_probe_uses_a_new_key_every_run_and_deletes_only_its_own(app) -> None:
    """A fixed key lets one run delete the object another run is about to read, and lets
    a stale object from a crashed run pass the next read."""
    server = FakeS3()
    other = "/platform-canary/canary/written-by-someone-else"
    server.objects[other] = b"keep me"
    try:
        for _ in range(2):
            assert app._probe_s3(**_s3_args(server))["status"] == "pass"
        requests = list(server.requests)
        leftover = dict(server.objects)
    finally:
        server.close()
    puts = [path for method, path, _key in requests if method == "PUT"]
    deletes = [path for method, path, _key in requests if method == "DELETE"]
    assert len(puts) == 2 and puts[0] != puts[1]
    assert deletes == puts, "each run deletes its own key and no other"
    assert leftover == {other: b"keep me"}


def test_s3_probe_runs_in_parallel_do_not_share_a_key(app) -> None:
    server = FakeS3()
    results: list[dict] = []
    try:
        threads = [
            threading.Thread(
                target=lambda: results.append(app._probe_s3(**_s3_args(server)))
            )
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        puts = [path for method, path, _key in server.requests if method == "PUT"]
        leftover = dict(server.objects)
    finally:
        server.close()
    assert [r["status"] for r in results] == ["pass"] * 6, results
    assert len(set(puts)) == 6
    assert leftover == {}


# --------------------------------------------------------------- the status document


def _env(monkeypatch, **values: str) -> None:
    for key in (
        "REDIS_PASSWORD",
        "CANARY_POSTGRES_USER",
        "CANARY_POSTGRES_PASSWORD",
        "CANARY_S3_ENDPOINT_URL",
        "CANARY_S3_BUCKET",
        "CANARY_S3_ACCESS_KEY",
        "CANARY_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_run_all_checks_hands_each_probe_its_credentials(app, monkeypatch) -> None:
    _env(
        monkeypatch,
        ENV_SUFFIX="-staging",
        REDIS_PASSWORD=REDIS_PW,
        CANARY_POSTGRES_USER="canary_ro",
        CANARY_POSTGRES_PASSWORD=PG_PASSWORD,
        CANARY_S3_ENDPOINT_URL="http://platform-s3-staging:9000",
        CANARY_S3_BUCKET="platform-canary",
        CANARY_S3_ACCESS_KEY=S3_ACCESS_KEY,
        CANARY_S3_SECRET_KEY=S3_SECRET_KEY,
    )
    calls: dict[str, dict] = {}

    def record(name):
        def probe(*args, **kwargs):
            calls[name] = {"args": args, "kwargs": kwargs}
            return {"status": "pass", "latency_ms": 1.0, "detail": name}

        return probe

    monkeypatch.setattr(app, "_probe_redis", record("redis"))
    monkeypatch.setattr(app, "_probe_postgres", record("postgres"))
    monkeypatch.setattr(app, "_probe_s3", record("s3"))
    monkeypatch.setattr(app, "_probe_http", record("http"))

    report = app.run_all_checks()

    assert report["ok"] is True
    assert set(report["checks"]) == {
        "postgres",
        "redis",
        "s3",
        "minio",
        "signoz",
        "openpanel",
        "authentik",
    }
    assert calls["redis"]["args"][0] == "platform-redis-staging"
    assert calls["redis"]["kwargs"]["password"] == REDIS_PW
    assert calls["postgres"]["args"][0] == "platform-postgres-staging"
    assert calls["postgres"]["kwargs"]["user"] == "canary_ro"
    assert calls["postgres"]["kwargs"]["password"] == PG_PASSWORD
    assert calls["s3"]["kwargs"] == {
        "endpoint_url": "http://platform-s3-staging:9000",
        "bucket": "platform-canary",
        "access_key": S3_ACCESS_KEY,
        "secret_key": S3_SECRET_KEY,
    }


OK_RESULT = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}


def _patch_probes(app, monkeypatch, **overrides) -> None:
    """Every probe passes except the ones named: redis, postgres, s3, and http by host."""
    redis = overrides.get("redis", OK_RESULT)
    postgres = overrides.get("postgres", OK_RESULT)
    s3 = overrides.get("s3", OK_RESULT)
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(redis))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(postgres))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(s3))

    def http(url, **kwargs):
        for name in ("signoz", "openpanel", "authentik"):
            if name in url and name in overrides:
                return dict(overrides[name])
        return dict(OK_RESULT)

    monkeypatch.setattr(app, "_probe_http", http)


def _get_status(app) -> tuple[int, str]:
    """The raw status response as the probe runner receives it: (HTTP code, body)."""
    import urllib.error
    import urllib.request

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.TodoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/canary/status"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()
    finally:
        server.shutdown()
        server.server_close()


def test_a_failed_write_path_turns_the_document_red_as_503(app, monkeypatch) -> None:
    """A forced failure turns the status document red, which is what the probe reads."""
    _env(monkeypatch)
    failed = {"status": "fail", "latency_ms": 2.0, "detail": "AccessDenied"}
    _patch_probes(app, monkeypatch, s3=failed)

    code, raw = _get_status(app)
    body = json.loads(raw)

    assert code == 503
    assert body["ok"] is False
    assert body["failed"] == ["s3"] and body["unconfigured"] == []
    assert body["checks"]["s3"]["detail"] == "AccessDenied"
    assert body["checks"]["minio"]["status"] == "fail", "the legacy alias follows s3"


def test_an_unconfigured_write_path_is_red_but_never_a_failure(
    app, monkeypatch
) -> None:
    """A missing credential must keep the probe red (a gap is not green) and must not be
    reported as a failed write path (#991 audit: a false P1 page)."""
    _env(monkeypatch)
    gap = {
        "status": "unconfigured",
        "latency_ms": 0.1,
        "detail": "CANARY_S3_ACCESS_KEY, CANARY_S3_SECRET_KEY not configured",
    }
    _patch_probes(app, monkeypatch, s3=gap)

    code, raw = _get_status(app)
    body = json.loads(raw)

    assert code == 503, "an unconfigured check keeps the probe red"
    assert body["ok"] is False
    assert body["unconfigured"] == ["s3"]
    assert body["failed"] == [], "a configuration gap is not a write-path failure"
    assert body["checks"]["s3"]["status"] == "unconfigured"
    assert body["checks"]["redis"]["status"] == "pass"


def test_failed_and_unconfigured_checks_are_listed_apart(app, monkeypatch) -> None:
    _env(monkeypatch)
    _patch_probes(
        app,
        monkeypatch,
        redis={"status": "fail", "latency_ms": 1.0, "detail": "AUTH failed"},
        s3={"status": "unconfigured", "latency_ms": 0.1, "detail": "gap"},
    )
    body = app.run_all_checks()
    assert body["failed"] == ["redis"] and body["unconfigured"] == ["s3"]
    assert body["ok"] is False


def test_the_probe_runner_sees_the_gap_in_its_observed_text(app, monkeypatch) -> None:
    """Run the real probe runner against the real handler. The runner keeps the first
    128 bytes of the body, so the classification must sit inside them."""
    from libs.observability.probes import ProbeSpec, run_probe

    _env(monkeypatch)
    gap = {"status": "unconfigured", "latency_ms": 0.1, "detail": "CANARY_S3 gap"}
    _patch_probes(app, monkeypatch, s3=gap)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.TodoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        spec = ProbeSpec(
            name="todo-canary-status",
            kind="http",
            target=f"http://127.0.0.1:{server.server_address[1]}/api/canary/status",
            expected="200",
            severity="warning",
            timeout_seconds=5,
        )
        result = run_probe(spec)
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is False, "the gap keeps the probe red"
    assert '"unconfigured": ["s3"]' in result.observed
    assert '"failed": []' in result.observed
    assert '"ok": false' in result.observed


@pytest.mark.parametrize("dependency", ["signoz", "openpanel", "authentik"])
def test_a_non_canary_dependency_never_gates_the_document(
    app, monkeypatch, dependency
) -> None:
    """SigNoz, OpenPanel and Authentik have their own probes and severities. Their outage
    must not page again as `todo-canary-status`. They stay visible as information."""
    _env(monkeypatch)
    down = {"status": "fail", "latency_ms": 3.0, "detail": f"{dependency} is down"}
    _patch_probes(app, monkeypatch, **{dependency: down})

    code, raw = _get_status(app)
    body = json.loads(raw)

    assert code == 200
    assert body["ok"] is True and body["failed"] == [] and body["unconfigured"] == []
    assert body["checks"][dependency]["status"] == "fail", "still visible"
    assert body["checks"][dependency]["detail"] == f"{dependency} is down"
    assert body["gating"] == ["postgres", "redis", "s3"]
    assert body["informational"] == ["signoz", "openpanel", "authentik"]


def test_every_write_path_gates_the_document(app, monkeypatch) -> None:
    _env(monkeypatch)
    down = {"status": "fail", "latency_ms": 1.0, "detail": "down"}
    for name in ("postgres", "redis", "s3"):
        _patch_probes(app, monkeypatch, **{name: down})
        body = app.run_all_checks()
        assert body["ok"] is False and body["failed"] == [name], name


def test_run_all_checks_bounds_a_hung_probe(app, monkeypatch) -> None:
    """The probe runner waits a fixed time. One hung dependency must not hold the whole
    status document past it, and it must show as a failed check."""
    _env(monkeypatch)
    monkeypatch.setattr(app, "CHECK_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(OK_RESULT))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(OK_RESULT))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(OK_RESULT))

    def hung_http(url, **kwargs):
        if "openpanel" in url:
            time.sleep(1.5)
        return dict(OK_RESULT)

    monkeypatch.setattr(app, "_probe_http", hung_http)
    monkeypatch.setenv(
        "OPENPANEL_API_URL", "http://platform-openpanel-api:3000/healthcheck"
    )

    started = time.monotonic()
    report = app.run_all_checks()
    elapsed = time.monotonic() - started

    assert elapsed < 1.2, (
        f"the status document waited for the hung probe ({elapsed:.2f}s)"
    )
    assert report["checks"]["openpanel"]["status"] == "fail"
    assert "did not finish" in report["checks"]["openpanel"]["detail"]
    assert report["checks"]["redis"]["status"] == "pass"
    assert report["ok"] is True, "OpenPanel is informational and does not gate"


def test_a_hung_write_path_fails_the_document_within_the_deadline(
    app, monkeypatch
) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(app, "CHECK_DEADLINE_SECONDS", 0.3)
    _patch_probes(app, monkeypatch)

    def hung_redis(*args, **kwargs):
        time.sleep(1.5)
        return dict(OK_RESULT)

    monkeypatch.setattr(app, "_probe_redis", hung_redis)
    started = time.monotonic()
    report = app.run_all_checks()
    assert time.monotonic() - started < 1.2
    assert report["ok"] is False and report["failed"] == ["redis"]


# ---------------------------------------------- one run in flight, a short-lived cache


def test_concurrent_requests_share_one_run(app, monkeypatch) -> None:
    """Each run does real writes and opens a Postgres connection. The canary role holds 5
    connections, so five parallel page loads must not become five runs."""
    runs: list[int] = []
    release = threading.Event()

    def slow_run():
        runs.append(1)
        release.wait(timeout=5)
        return {"ok": True, "timestamp": "t", "checks": {}}

    monkeypatch.setattr(app, "run_all_checks", slow_run)
    answers: list[dict] = []
    threads = [
        threading.Thread(target=lambda: answers.append(app.cached_canary_status()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while not runs and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(runs) == 1, f"{len(runs)} runs for 8 concurrent requests"
    assert len(answers) == 8 and all(a["ok"] is True for a in answers)


def test_requests_inside_the_ttl_reuse_the_result_and_say_how_old_it_is(
    app, monkeypatch
) -> None:
    runs: list[int] = []

    def run():
        runs.append(1)
        return {"ok": True, "timestamp": "t", "checks": {}}

    monkeypatch.setattr(app, "run_all_checks", run)
    first = app.cached_canary_status()
    second = app.cached_canary_status()
    assert len(runs) == 1
    assert first["age_seconds"] == 0.0
    assert 0.0 <= second["age_seconds"] < app.STATUS_CACHE_TTL_SECONDS, (
        "a reused result must state its age, never pass as fresh"
    )


def test_a_result_older_than_the_ttl_is_run_again(app, monkeypatch) -> None:
    runs: list[int] = []
    monkeypatch.setattr(app, "STATUS_CACHE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(
        app,
        "run_all_checks",
        lambda: runs.append(1) or {"ok": True, "timestamp": "t", "checks": {}},
    )
    app.cached_canary_status()
    time.sleep(0.12)
    app.cached_canary_status()
    assert len(runs) == 2


def test_the_ttl_is_shorter_than_the_probe_interval(app) -> None:
    """Every probe round must see a fresh run. The runner polls every
    INFRA_PROBE_INTERVAL_SECONDS (a cache as long as that would show one run twice)."""
    alerting = compose_services(ROOT / "platform/12.alerting/compose.yaml")
    raw = alerting["infra-probe-runner"]["environment"]["INFRA_PROBE_INTERVAL_SECONDS"]
    interval = float(resolve(raw, {}))
    assert interval == 60.0
    assert 0 < app.STATUS_CACHE_TTL_SECONDS < interval


def test_parallel_http_requests_run_the_probes_once(app, monkeypatch) -> None:
    import urllib.request

    _env(monkeypatch)
    calls: list[int] = []

    def counting_postgres(*args, **kwargs):
        calls.append(1)
        time.sleep(0.2)
        return dict(OK_RESULT)

    _patch_probes(app, monkeypatch)
    monkeypatch.setattr(app, "_probe_postgres", counting_postgres)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.TodoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    codes: list[int] = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/canary/status"

        def fetch():
            with urllib.request.urlopen(url, timeout=10) as response:
                codes.append(response.status)

        threads = [threading.Thread(target=fetch) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
    finally:
        server.shutdown()
        server.server_close()
    assert codes == [200] * 6
    assert len(calls) == 1, "six page loads must open one Postgres connection"


def test_probe_budget_fits_inside_the_declared_probe_timeout(app) -> None:
    """The ProbeFacet timeout must exceed the deadline the status document enforces."""
    facet = reg.service_attrs()["platform/todo"].probes[0]
    assert app.CHECK_DEADLINE_SECONDS < facet.timeout_seconds


# ------------------------------------------------------------------ 4. probe consumer


def test_todo_declares_a_probe_on_the_internal_canary_status(deploy) -> None:
    attrs = reg.service_attrs()["platform/todo"]
    assert not any(e.check_id == "probes" for e in attrs.exemptions), (
        "the probes exemption said 'self-proving'; nothing read the status (#991)"
    )
    by_name = {probe.name: probe for probe in attrs.probes}
    assert set(by_name) == {"todo-canary-status"}
    probe = by_name["todo-canary-status"]
    assert probe.kind == "http"
    assert probe.expected == "200"
    assert probe.severity == "warning", (
        "P2 until staging and production acceptance; raise to `error` only then"
    )
    assert probe.depends_on == ""
    assert probe.service_id == ""


def test_probe_target_matches_the_compose_container_port_and_app_route(app) -> None:
    probe = reg.service_attrs()["platform/todo"].probes[0]
    target = urlsplit(probe.target.replace("${ENV_SUFFIX}", "-staging"))
    service = compose_services(COMPOSE)["todo"]
    assert target.scheme == "http", "the probe stays on the Docker network"
    assert target.hostname == service["container_name"].replace(
        "${ENV_SUFFIX}", "-staging"
    )
    assert target.port == int(resolve(service["environment"]["PORT"], {}))
    assert target.path == "/api/canary/status"
    assert target.path in _served_paths()


def test_probe_renders_into_the_probe_runner_specs() -> None:
    lines = [
        line
        for line in render_probe_spec_text().splitlines()
        if line.startswith("todo-canary-status|")
    ]
    assert lines == [
        "todo-canary-status|http|http://platform-todo${ENV_SUFFIX}:8000/api/canary/status"
        "|200|warning|15||platform/todo"
    ]


def test_todo_signal_is_a_debounced_minute_alert() -> None:
    from libs.observability.signal_entries import render_internal_signal_entries

    entries = [
        entry
        for entry in render_internal_signal_entries()
        if entry["signal"] == "todo-canary-status"
    ]
    assert {entry["environment"] for entry in entries} == {"production", "staging"}
    for entry in entries:
        assert entry["service_id"] == "platform/todo"
        assert entry["component"] == "todo"
        assert entry["severity"] == "warning"
        assert entry["tier"] == "minute" and entry["type"] == "alert"
        assert entry["consecutive_failures"] == 3
        assert entry["renotify_window_sec"] == 0


# --------------------------------------------------------------- 5. public surface


def _served_paths() -> set[str]:
    """Every exact path the handler compares against, read from the app source."""
    source = (SERVICE_DIR / "app.py").read_text(encoding="utf-8")
    return set(re.findall(r'path == "(/[^"]*)"', source))


@pytest.mark.parametrize(
    ("domain_suffix", "env_suffix"), [("", ""), ("-staging", "-staging")]
)
def test_only_the_health_path_is_served_without_sso(
    app, domain_suffix, env_suffix
) -> None:
    """Resolve the real routing decision for every path the app serves.

    Substring checks on the rule text passed while `/api/canary/status` was public. This
    evaluates the Traefik rule language and the priority order instead.
    """
    service = compose_services(COMPOSE)["todo"]
    issued = {
        "ENV_DOMAIN_SUFFIX": domain_suffix,
        "ENV_SUFFIX": env_suffix,
        "INTERNAL_DOMAIN": "zitian.party",
    }
    labels = [resolve(label, issued) for label in service["labels"]]
    routers = routers_from_labels(labels)
    host = f"todo{domain_suffix}.zitian.party"

    forward_auth = {
        label.split("=", 1)[0].split(".")[3]
        for label in labels
        if ".forwardauth.address=" in label
    }
    assert forward_auth, "the SSO middleware must be declared"

    served_by_app = _served_paths()
    assert {"/api/health", "/api/canary/status", "/api/todos", "/"} <= served_by_app
    candidates = sorted(
        served_by_app
        | {
            "/api/todos/1",
            "/api/todos/1/toggle",
            "/api/canary/status/",
            "/api/canary/status/extra",
            "/api/health/extra",
            "/api/healthz",
            "/api/canary",
            "/api/auth/me",
        }
    )

    unauthenticated = set()
    for path in candidates:
        router = serving_router(routers, host=host, path=path)
        assert router is not None, (
            f"no router serves {path}; the SSO router must catch all"
        )
        guarded = any(
            middleware.split("@")[0] in forward_auth
            for middleware in router.middlewares
        )
        if not guarded:
            unauthenticated.add(path)

    assert unauthenticated == {"/api/health"}


def test_public_router_does_not_name_the_canary_status() -> None:
    """A second guard on the label itself: the public rule must not mention the path."""
    labels = compose_services(COMPOSE)["todo"]["labels"]
    public_rules = [
        label
        for label in labels
        if "-public" in label and label.split("=", 1)[0].endswith(".rule")
    ]
    assert len(public_rules) == 1
    assert "canary" not in public_rules[0]


# ---------------------------------------------------------------------- 6. telemetry


def _issued_telemetry_env(environment: str) -> dict[str, str]:
    from libs.service_identity import ServiceIdentity

    identity = ServiceIdentity.build(
        "platform/todo",
        environment,
        component="todo",
        service_name="platform-todo",
        version="a" * 40,
        iac_ref="a" * 40,
    )
    return {
        "ENV": environment,
        "OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR_OTLP_HTTP,
        "OTEL_SERVICE_NAME": identity.service_name,
        "OTEL_RESOURCE_ATTRIBUTES": identity.otel_resource_attributes(),
    }


@pytest.fixture()
def telemetry_calls(app, monkeypatch):
    """Replace the SDK exporter bootstrap so the test needs no OpenTelemetry install."""
    import infra2_sdk.runtime.otel as sdk_otel

    monkeypatch.setattr(app, "_acquire_tracer", lambda: "tracer")

    calls: list[dict] = []

    class _Providers:
        def shutdown(self):
            calls.append({"shutdown": True})

    def fake_configure(settings, **kwargs):
        calls.append({"settings": settings, **kwargs})
        return _Providers()

    monkeypatch.setattr(sdk_otel, "configure_telemetry", fake_configure)
    for key in (
        "ENV",
        "ENVIRONMENT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    return calls


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_startup_configures_telemetry_from_the_issued_identity(
    app, monkeypatch, telemetry_calls, environment
) -> None:
    for key, value in _issued_telemetry_env(environment).items():
        monkeypatch.setenv(key, value)

    providers = app.configure_service_telemetry()

    assert providers is not None
    assert app._TRACER == "tracer", "spans start only after the providers exist"
    (call,) = telemetry_calls
    settings = call["settings"]
    assert call["set_global"] is True
    assert settings.service_name == "platform-todo"
    assert settings.endpoint == COLLECTOR_OTLP_HTTP
    assert settings.deployment_environment == environment
    assert settings.resource_attributes["infra.service.id"] == "platform/todo"
    assert settings.enabled is True


def test_startup_skips_telemetry_without_an_endpoint(app, telemetry_calls) -> None:
    assert app.configure_service_telemetry() is None
    assert telemetry_calls == []
    assert app._TRACER is None


def test_startup_fails_fast_when_the_endpoint_comes_without_an_identity(
    app, monkeypatch, telemetry_calls
) -> None:
    """The endpoint turns export on. Without the issued identity the data would land under
    `unknown_service`, so the service refuses to start (finance_report's contract)."""
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", COLLECTOR_OTLP_HTTP)
    with pytest.raises(RuntimeError, match="OTEL_SERVICE_NAME"):
        app.configure_service_telemetry()
    assert telemetry_calls == []


def test_startup_fails_fast_when_the_environment_is_unknown(
    app, monkeypatch, telemetry_calls
) -> None:
    env = _issued_telemetry_env("staging")
    env.pop("ENV")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="ENVIRONMENT"):
        app.configure_service_telemetry()


class _FakeSpan:
    def __init__(self, name, attributes):
        self.name = name
        self.attributes = dict(attributes or {})
        self.parent = None

    def set_attribute(self, key, value):
        self.attributes[key] = value


class _FakeTracer:
    """Tracks the current span in a ContextVar, as OpenTelemetry's context does."""

    def __init__(self):
        self.spans: list[_FakeSpan] = []
        self._current: contextvars.ContextVar[_FakeSpan | None] = (
            contextvars.ContextVar("fake_current_span", default=None)
        )

    def start_as_current_span(self, name, attributes=None, **kwargs):
        @contextlib.contextmanager
        def manager():
            span = _FakeSpan(name, attributes)
            parent = self._current.get()
            span.parent = parent.name if parent else None
            self.spans.append(span)
            token = self._current.set(span)
            try:
                yield span
            finally:
                self._current.reset(token)

        return manager()


def test_canary_run_emits_a_root_span_and_one_span_per_check(app, monkeypatch) -> None:
    _env(monkeypatch)
    tracer = _FakeTracer()
    monkeypatch.setattr(app, "_TRACER", tracer)
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(ok))
    monkeypatch.setattr(
        app,
        "_probe_s3",
        lambda **k: {"status": "fail", "latency_ms": 3.0, "detail": "denied"},
    )

    app.run_all_checks()

    by_name = {span.name: span for span in tracer.spans}
    assert "canary.run" in by_name
    checks = {n for n in by_name if n.startswith("canary.check.")}
    assert checks == {
        "canary.check.postgres",
        "canary.check.redis",
        "canary.check.s3",
        "canary.check.signoz",
        "canary.check.openpanel",
        "canary.check.authentik",
    }
    assert all(by_name[name].parent == "canary.run" for name in checks), (
        "check spans must nest under the run span across worker threads"
    )
    s3 = by_name["canary.check.s3"]
    assert s3.attributes["canary.status"] == "fail"
    assert s3.attributes["canary.detail"] == "denied"
    assert by_name["canary.run"].attributes["canary.ok"] is False


def test_canary_run_works_without_a_tracer(app, monkeypatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(app, "_TRACER", None)
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(ok))
    assert app.run_all_checks()["ok"] is True


def test_deployer_issues_the_telemetry_identity_and_compose_hands_it_to_the_container(
    deploy, monkeypatch
) -> None:
    """Run the real `Deployer.sync` and resolve the compose against the env it pushes.

    Asserting the deploy-side value alone proves nothing reaches the container, and
    asserting the compose text alone proves a reference. Only the join does.
    """
    from unittest.mock import MagicMock

    import libs.deploy.deployer as d

    deployer = deploy.TodoDeployer
    pushed: dict[str, str] = {}
    release = "c" * 40

    class _Matches:
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

        __hash__ = object.__hash__

    class _Remote(dict):
        def __getitem__(self, key):
            return {"runtime_hash": "old", "deploy_ref": release}.get(key, _Matches())

        def get(self, key, default=None):
            return self[key]

    def composing(cls, c, env_vars):
        pushed.update(env_vars)
        return "compose-1"

    for name, value in {
        "env": classmethod(
            lambda cls: {
                "ENV": "staging",
                "ENV_SUFFIX": "-staging",
                "ENV_DOMAIN_SUFFIX": "-staging",
                "INTERNAL_DOMAIN": "zitian.party",
                "VPS_HOST": "vps",
            }
        ),
        "verify_vault_app_token": classmethod(lambda cls: {"valid": True}),
        "ensure_runtime_secrets": classmethod(lambda cls, c, env=None: True),
        "apply_secret_supply": classmethod(lambda cls, c, env=None: True),
        "compute_local_config_hash": classmethod(lambda cls, c, env: "new"),
        "get_remote_config_identity": classmethod(lambda cls: _Remote()),
        "_await_effective_config_hash": classmethod(lambda cls, h: "new"),
        "composing": classmethod(composing),
        "verify_runtime_applied": classmethod(lambda cls, c, env: None),
        "verify_in_service": classmethod(lambda cls, c, cid: None),
        "restart_dependents": classmethod(lambda cls, c, e: []),
    }.items():
        monkeypatch.setattr(deployer, name, value)
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setenv("IAC_DEPLOY_REF", release)

    result = deployer.sync(MagicMock())
    assert result["action"] in ("created", "updated"), result

    container = container_env(COMPOSE, "todo", pushed)
    assert container["OTEL_EXPORTER_OTLP_ENDPOINT"] == COLLECTOR_OTLP_HTTP
    assert container["OTEL_SERVICE_NAME"] == "platform-todo"
    attributes = dict(
        pair.split("=", 1) for pair in container["OTEL_RESOURCE_ATTRIBUTES"].split(",")
    )
    assert attributes["deployment.environment.name"] == "staging"
    assert attributes["service.name"] == "platform-todo"
    assert attributes["infra.service.id"] == "platform/todo"
    assert attributes["infra.iac.ref"] == release
    assert container["ENV"] == "staging", "the SDK resolves the tier from ENV"
    # export turns on with the endpoint: the vault-agent sidecar has no SDK and gets none
    vault_agent = container_env(COMPOSE, "vault-agent", pushed)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in vault_agent


def test_compose_hands_the_container_whatever_identity_the_deploy_issues() -> None:
    """The deploy is the only issuer. A hardcoded or defaulted name would pass the test
    above while the container ignored what the deploy issued."""
    issued = {
        "ENV": "staging",
        "OTEL_SERVICE_NAME": "issued-by-the-deploy",
        "OTEL_RESOURCE_ATTRIBUTES": "service.name=issued-by-the-deploy,k=v",
    }
    container = container_env(COMPOSE, "todo", issued)
    assert container["OTEL_SERVICE_NAME"] == "issued-by-the-deploy"
    assert container["OTEL_RESOURCE_ATTRIBUTES"] == issued["OTEL_RESOURCE_ATTRIBUTES"]
    bare = container_env(COMPOSE, "todo", {"ENV": "staging"})
    assert bare["OTEL_SERVICE_NAME"] == "" and bare["OTEL_RESOURCE_ATTRIBUTES"] == "", (
        "with no issued identity the container must get none, so the service fails fast"
    )


def test_registry_names_the_telemetry_service() -> None:
    assert (
        reg.service_attrs()["platform/todo"].telemetry_service_name == "platform-todo"
    )


def test_dockerfile_installs_the_sdk_otel_extra_requirements_as_plain_pins() -> None:
    """pyproject keeps `infra2-sdk @ <wheel>` extras-free, so the Dockerfile lists the
    `otel` extra's requirements itself. They must equal what the SDK declares."""
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")
    declared = [
        requirement.split(";")[0].strip()
        for requirement in (importlib.metadata.requires("infra2-sdk") or [])
        if 'extra == "otel"' in requirement
    ]
    assert declared, "the installed SDK declares no otel extra"
    from packaging.requirements import Requirement

    for requirement in declared:
        parsed = Requirement(requirement)
        pins = re.findall(rf'"({re.escape(parsed.name)}[^"\s]*)"', dockerfile)
        assert pins, f"Dockerfile lacks {parsed.name}"
        installed_spec = Requirement(pins[0]).specifier
        assert installed_spec == parsed.specifier, (
            f"{parsed.name}: Dockerfile pins {installed_spec}, SDK needs {parsed.specifier}"
        )


# ------------------------------------------------------------- secrets and the role


def test_todo_is_registered_with_the_secret_registry_and_templates_are_generated() -> (
    None
):
    from tools import secrets_render

    service = secrets_registry.lookup("platform", "todo")
    assert service is not None
    assert service.directory == "platform/30.todo"
    assert secrets_render.check((service,)) == []


def test_todo_manifest_sources_each_secret_from_the_right_class() -> None:
    manifest = secrets_registry.merged_manifest(
        secrets_registry.lookup("platform", "todo")
    )
    fields = {field.env: field for field in manifest.fields}

    redis = fields["REDIS_PASSWORD"]
    assert redis.source == "runtime"
    assert redis.provided_by == "platform/redis:password"
    assert redis.sensitive

    pg = fields["CANARY_POSTGRES_PASSWORD"]
    assert pg.source == "runtime" and not pg.empty_ok and pg.sensitive
    assert not pg.provided_by, "a generated value, not the Postgres superuser password"

    for env in ("CANARY_S3_ACCESS_KEY", "CANARY_S3_SECRET_KEY"):
        assert fields[env].source == "human"
        assert fields[env].empty_ok and fields[env].sensitive


def test_todo_policy_never_reads_the_superuser_secrets() -> None:
    """The canary is reachable over SSO from the internet. It must not be able to read
    the Postgres or S3 root credentials, only its own path and the Redis password."""
    policy = (SERVICE_DIR / "vault-policy.hcl").read_text(encoding="utf-8")
    paths = set(re.findall(r'path "([^"]+)"', policy))
    assert "secret/data/platform/{{env}}/todo" in paths
    assert "secret/data/platform/{{env}}/redis" in paths
    for forbidden in ("postgres", "minio", "s3", "alerting"):
        assert not any(forbidden in path for path in paths), (forbidden, paths)


def test_compose_runs_a_vault_agent_and_mounts_its_secrets_read_only() -> None:
    services = compose_services(COMPOSE)
    assert set(services) == {"vault-agent", "todo"}
    todo = services["todo"]
    assert "secrets:/secrets:ro" in todo["volumes"]
    assert "vault-agent" in todo["depends_on"]
    assert "REDIS_PASSWORD" not in todo["environment"], (
        "the password comes from the rendered file, never from compose env"
    )
    entrypoint = " ".join(todo["entrypoint"])
    assert ". /secrets/.env" in entrypoint and "exec python" in entrypoint
    agent = services["vault-agent"]
    assert agent["container_name"] == "platform-todo-vault-agent${ENV_SUFFIX}"
    assert agent.get("mem_limit"), "a new container may not add resource-limit debt"
    assert "./secrets.ctmpl:/etc/vault/secrets.ctmpl:ro" in agent["volumes"]


def test_secrets_facet_names_the_compose_containers() -> None:
    attrs = reg.service_attrs()["platform/todo"]
    (facet,) = attrs.secrets
    services = compose_services(COMPOSE)
    assert facet.auth_method == "approle"
    assert facet.vault_agent_container == services["vault-agent"]["container_name"]
    assert facet.app_containers == (services["todo"]["container_name"],)


def test_canary_role_name_is_one_fact_across_deployer_and_compose(deploy) -> None:
    env = compose_services(COMPOSE)["todo"]["environment"]
    assert env["CANARY_POSTGRES_USER"] == deploy.TodoDeployer.CANARY_PG_ROLE


class _Run:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok, self.failed = ok, not ok
        self.stdout, self.stderr = stdout, stderr


class _Host:
    def __init__(self, ok=True, stderr=""):
        self.commands: list[str] = []
        self.stdin: list[str] = []
        self._ok, self._stderr = ok, stderr

    def run(self, command, in_stream=None, hide=True, warn=True, **kwargs):
        self.commands.append(command)
        self.stdin.append(in_stream.read() if in_stream is not None else "")
        return _Run(self._ok, stderr=self._stderr)


class _Store:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def _stub_role_env(monkeypatch, deployer, store_values) -> None:
    monkeypatch.setattr(
        deployer,
        "env",
        classmethod(
            lambda cls: {
                "ENV": "staging",
                "ENV_SUFFIX": "-staging",
                "VPS_HOST": "vps.example",
            }
        ),
    )
    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: _Store(store_values)),
    )


def test_canary_role_sql_is_idempotent_least_privilege_and_read_only(deploy) -> None:
    sql = deploy.TodoDeployer.canary_role_sql(PG_PASSWORD)
    normalized = " ".join(sql.split())
    create_at = normalized.index("CREATE ROLE")
    guard_at = normalized.index("IF NOT EXISTS")
    assert guard_at < create_at, "CREATE ROLE must sit behind an existence guard"
    assert (
        "ALTER ROLE canary_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        in normalized
    )
    assert f"PASSWORD '{PG_PASSWORD}'" in normalized, (
        "a rotated password must be re-applied"
    )
    assert "default_transaction_read_only = on" in normalized
    assert "CONNECTION LIMIT" in normalized
    assert "GRANT" not in normalized, (
        "the canary needs CONNECT only, which PUBLIC holds"
    )
    assert "DROP" not in normalized


@pytest.mark.parametrize(
    "password",
    [
        "",
        "short",
        "has'quote0123456789",
        "semi;colon0123456789",
        "back\\slash0123456789",
        "dollar$$0123456789",
    ],
)
def test_canary_role_sql_rejects_passwords_it_cannot_quote_safely(
    deploy, password
) -> None:
    with pytest.raises(ValueError):
        deploy.TodoDeployer.canary_role_sql(password)


def test_ensure_role_runs_the_sql_over_stdin_so_the_password_never_hits_argv(
    deploy, monkeypatch
) -> None:
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"CANARY_POSTGRES_PASSWORD": PG_PASSWORD})
    host = _Host()

    assert deployer._ensure_canary_postgres_role(host) is True

    (command,) = host.commands
    assert command.startswith("ssh root@vps.example ")
    assert "docker exec -i platform-postgres-staging psql -U postgres" in command
    assert "ON_ERROR_STOP=1" in command
    assert PG_PASSWORD not in command
    assert PG_PASSWORD in host.stdin[0]
    assert host.stdin[0] == deployer.canary_role_sql(PG_PASSWORD)


def test_ensure_role_fails_closed_when_the_password_is_not_in_vault(
    deploy, monkeypatch
) -> None:
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {})
    host = _Host()
    assert deployer._ensure_canary_postgres_role(host) is False
    assert host.commands == []


def test_ensure_role_fails_closed_when_psql_fails(deploy, monkeypatch) -> None:
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"CANARY_POSTGRES_PASSWORD": PG_PASSWORD})
    host = _Host(ok=False, stderr="psql: error: connection refused")
    assert deployer._ensure_canary_postgres_role(host) is False


def test_supply_hook_provisions_the_role_only_after_the_supply_succeeded(
    deploy, monkeypatch
) -> None:
    from libs.deploy.deployer import Deployer

    deployer = deploy.TodoDeployer
    order: list[str] = []

    def supply(cls, c, *, env=None):
        order.append("supply")
        return supply.result

    supply.result = True
    monkeypatch.setattr(Deployer, "apply_secret_supply", classmethod(supply))
    monkeypatch.setattr(
        deployer,
        "_ensure_canary_postgres_role",
        classmethod(lambda cls, c, env=None: order.append("role") or True),
    )

    assert deployer.apply_secret_supply(object(), env="staging") is True
    assert order == ["supply", "role"]

    order.clear()
    supply.result = False
    assert deployer.apply_secret_supply(object(), env="staging") is False
    assert order == ["supply"], "no database change while the supply is incomplete"


def test_supply_hook_fails_when_the_role_cannot_be_provisioned(
    deploy, monkeypatch
) -> None:
    from libs.deploy.deployer import Deployer

    deployer = deploy.TodoDeployer
    monkeypatch.setattr(
        Deployer, "apply_secret_supply", classmethod(lambda cls, c, *, env=None: True)
    )
    monkeypatch.setattr(
        deployer,
        "_ensure_canary_postgres_role",
        classmethod(lambda cls, c, env=None: False),
    )
    assert deployer.apply_secret_supply(object(), env="staging") is False


def test_first_sync_creates_the_role_although_no_consumer_exists_yet(
    deploy, monkeypatch
) -> None:
    """The first deploy: the supply writes the generated password and asks for a restart of
    a vault-agent and an app container that do not exist. The sync must still reach the
    role creation (#991 audit: it failed on `docker restart` first)."""
    from libs.security import supply as supply_module

    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"CANARY_POSTGRES_PASSWORD": PG_PASSWORD})

    def apply(service, env_name, restart=None, resolver=None):
        restart(("CANARY_POSTGRES_PASSWORD",))
        from types import SimpleNamespace

        return SimpleNamespace(
            ok=True, notes=(), missing=(), summary=lambda: "generated the password"
        )

    monkeypatch.setattr(supply_module, "apply", apply)
    host = DockerHost(containers={"platform-postgres-staging"})

    assert deployer.apply_secret_supply(host, env="staging") is True

    assert host.restart_commands == []
    psql = [
        (command, stdin)
        for command, stdin in zip(host.commands, host.stdin)
        if "psql" in DockerHost.remote(command)
    ]
    assert len(psql) == 1, "the role is created on the first sync"
    assert psql[0][1] == deployer.canary_role_sql(PG_PASSWORD)


def test_role_sql_keeps_the_password_out_of_the_server_log(deploy) -> None:
    """A failed ALTER ROLE is logged with its statement text by default. The session turns
    statement logging off before the password statement runs."""
    sql = " ".join(deploy.TodoDeployer.canary_role_sql(PG_PASSWORD).split())
    alter = sql.index("ALTER ROLE canary_ro WITH")
    for setting in (
        "SET log_min_error_statement = 'panic';",
        "SET log_statement = 'none';",
        "SET log_min_duration_statement = -1;",
    ):
        assert setting in sql, setting
        assert sql.index(setting) < alter, f"{setting} must come before the password"
    assert sql.index("SET log_min_error_statement") < sql.index("DO $do$")


def test_a_failed_role_sync_never_prints_the_password(deploy, monkeypatch) -> None:
    """psql prints the failing statement with a caret under the error, and that statement
    holds the password."""
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"CANARY_POSTGRES_PASSWORD": PG_PASSWORD})
    stderr = (
        'psql:<stdin>:12: ERROR:  syntax error at or near "x"\n'
        f"LINE 1: ALTER ROLE canary_ro WITH LOGIN PASSWORD '{PG_PASSWORD}';\n"
        "                                                  ^\n"
        f"DETAIL: rejected {PG_PASSWORD}"
    )
    host = DockerHost(other_ok=False, other_stderr=stderr)
    printed: list[str] = []
    monkeypatch.setattr(
        deploy,
        "error",
        lambda *args, **kwargs: printed.append(" ".join(map(str, args))),
    )

    assert deployer._ensure_canary_postgres_role(host) is False

    assert printed, "the failure must be reported"
    assert all(PG_PASSWORD not in line for line in printed), printed
    assert any("<redacted>" in line for line in printed), printed
    assert any("syntax error" in line for line in printed), "the cause stays readable"


def test_redaction_removes_any_password_clause_not_only_the_known_value(deploy) -> None:
    redact = deploy.TodoDeployer.redact_password
    text = "x PASSWORD 'some-other-value' y PASSWORD  'and-this' z known-secret-1"
    cleaned = redact(text, "known-secret-1")
    assert "some-other-value" not in cleaned and "and-this" not in cleaned
    assert "known-secret-1" not in cleaned
    assert cleaned.count("<redacted>") == 3
