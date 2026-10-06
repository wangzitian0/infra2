#!/usr/bin/env python3
"""Canary Todo List & Infrastructure Validation Service.

Serves todo.zitian.party as the acceptance and verification tool for the platform's
real runtime paths:
- Postgres: login as a dedicated read-only role, then SELECT 1
- Redis: AUTH, then SETEX/GET/DEL of a random value
- S3: PUT, GET and DELETE of one object under a canary prefix
- SigNoz, OpenPanel, Authentik: liveness over HTTP

`/api/canary/status` runs these checks. The probe runner reads it over the Docker
network (ProbeFacet `todo-canary-status`). Traefik does not route it without SSO.
The service sends traces and logs to SigNoz when the deploy issues an OTLP endpoint.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import html
import json
import logging
import math
import os
import secrets
import signal
import socket
import sys
import time
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List

LOGGER = logging.getLogger("platform-todo")
SERVICE_TRACER_NAME = "platform-todo"

# Object key prefix for the S3 write probe. The canary credential is scoped to one bucket.
CANARY_S3_PREFIX = "canary/"
# The probe runner waits ProbeFacet.timeout_seconds (15 s). One hung dependency must not
# hold the status document past it, so each check gets this hard deadline.
CHECK_DEADLINE_SECONDS = 8.0

# Check states. `unconfigured` means a credential the check needs is missing: the check
# is red (a gap is never green) but it is not a failure of the path it would prove.
PASS = "pass"
FAIL = "fail"
UNCONFIGURED = "unconfigured"

# Only the canary's own write paths gate `ok` and the HTTP status. SigNoz, OpenPanel and
# Authentik have their own probes and severities, so they stay informational here.
GATING_CHECKS = ("postgres", "redis", "s3")
INFORMATIONAL_CHECKS = ("signoz", "openpanel", "authentik")

# Each run does real writes and opens a Postgres connection, and the canary role allows 5.
# One run is in flight at a time, and a result is reused for this long. It stays below the
# probe runner's 60 s interval, so every probe round still sees a fresh run.
STATUS_CACHE_TTL_SECONDS = 30.0
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: tuple[float, Dict[str, Any]] | None = None

# Set by configure_service_telemetry(). None means no OTLP endpoint was issued.
_TRACER: Any | None = None

_todos_lock = threading.Lock()

# In-memory fallback if postgres is initializing or offline during probe
_FALLBACK_TODOS: List[Dict[str, Any]] = [
    {
        "id": 1,
        "title": "平台 Postgres 读写链路校验",
        "completed": True,
        "capability": "postgres",
        "created_at": "2026-09-24T20:00:00Z",
    },
    {
        "id": 2,
        "title": "平台 Redis 缓存与分布式锁校验",
        "completed": True,
        "capability": "redis",
        "created_at": "2026-09-24T20:01:00Z",
    },
    {
        "id": 3,
        "title": "平台 S3 兼容对象存储与预签名上传校验",
        "completed": True,
        "capability": "s3",
        "created_at": "2026-09-24T20:02:00Z",
    },
    {
        "id": 4,
        "title": "平台 SigNoz OpenTelemetry 追踪采集校验",
        "completed": True,
        "capability": "signoz",
        "created_at": "2026-09-24T20:03:00Z",
    },
    {
        "id": 5,
        "title": "平台 OpenPanel 事件打点分析校验",
        "completed": True,
        "capability": "openpanel",
        "created_at": "2026-09-24T20:04:00Z",
    },
    {
        "id": 6,
        "title": "平台 Authentik SSO 与 GitHub OAuth 单点登录受验",
        "completed": True,
        "capability": "authentik",
        "created_at": "2026-09-25T11:00:00Z",
    },
]

try:
    from ui import HTML_TEMPLATE
except ImportError:
    from pathlib import Path

    _todo_dir = str(Path(__file__).resolve().parent)
    if _todo_dir not in sys.path:
        sys.path.insert(0, _todo_dir)
    from ui import HTML_TEMPLATE


class _ProbeFailure(Exception):
    """A check failed for a stated reason. The message is safe to publish."""


class _RedisReplyError(Exception):
    """Redis answered with an error reply (`-ERR`, `-NOAUTH`, `-WRONGPASS`, ...)."""


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _outcome(status: str, start: float, detail: str) -> Dict[str, Any]:
    return {"status": status, "latency_ms": _elapsed_ms(start), "detail": detail}


def _unconfigured(start: float, detail: str) -> Dict[str, Any]:
    return _outcome(UNCONFIGURED, start, detail)


def _probe_postgres(
    host: str,
    port: int = 5432,
    *,
    user: str = "",
    password: str = "",
    dbname: str = "postgres",
    timeout: float = 2.0,
    connector: Callable[..., Any] | None = None,
) -> Dict[str, Any]:
    """Log in as the dedicated canary role and run `SELECT 1`.

    A server that answers the login with an ErrorResponse (`E`), such as "too many
    clients", fails the check. The password never appears in the detail: the SDK
    redacts the DSN from error text.
    """
    start = time.perf_counter()
    if not (user and password):
        return _unconfigured(
            start,
            "CANARY_POSTGRES_USER and CANARY_POSTGRES_PASSWORD are not configured",
        )
    try:
        from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres
        from infra2_sdk.runtime.probes import DependencyStatus

        dsn = (
            f"postgresql://{urllib.parse.quote(user, safe='')}"
            f":{urllib.parse.quote(password, safe='')}"
            f"@{host}:{int(port)}/{urllib.parse.quote(dbname, safe='')}"
        )
        settings = PostgresSettings(
            dsn=dsn, connect_timeout_seconds=max(1, min(60, math.ceil(timeout)))
        )
        result = probe_postgres(settings, connector=connector)
    except Exception as exc:  # noqa: BLE001 - a probe reports the failure
        return _outcome("fail", start, f"{type(exc).__name__}: {exc}")
    if result.status != DependencyStatus.PRESENT:
        return _outcome("fail", start, f"login as {user} failed: {result.detail}")
    return _outcome("pass", start, f"login as {user}: {result.detail}")


def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return _outcome("pass", start, f"Connected to {host}:{port}")
    except Exception as exc:
        return _outcome("fail", start, str(exc))


def _probe_http(url: str, timeout: float = 3.0) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "canary-todo-probe/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if 200 <= code < 400:
                return _outcome("pass", start, f"HTTP {code}")
            return _outcome("fail", start, f"HTTP {code}")
    except Exception as exc:
        return _outcome("fail", start, str(exc))


def _resp_command(*parts: str) -> bytes:
    out = [b"*%d\r\n" % len(parts)]
    for part in parts:
        data = part.encode("utf-8")
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def _read_reply(rf) -> str | int | bytes | None:
    """Read one RESP reply: a simple string, an integer, a bulk string or nil."""
    line = rf.readline()
    if not line:
        raise ConnectionError("Redis closed the connection")
    kind, body = line[:1], line[1:].rstrip(b"\r\n")
    if kind == b"+":
        return body.decode("latin1")
    if kind == b"-":
        raise _RedisReplyError(body.decode("latin1"))
    if kind == b":":
        return int(body)
    if kind == b"$":
        size = int(body)
        if size < 0:
            return None
        return rf.read(size + 2)[:-2]
    raise ConnectionError(f"unexpected RESP reply {line[:20]!r}")


def _probe_redis(
    host: str,
    port: int = 6379,
    password: str | None = None,
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """AUTH, PING, then SETEX, GET and DEL of a random value on a per-run key.

    No password reports `unconfigured`: the check stays red, but it is a configuration
    gap, not a failed write path. A `-NOAUTH` reply fails the check: the old probe counted
    that reply as a pass and never wrote to Redis in production. The random value stops a
    stale key from the previous run passing a broken write.
    """
    start = time.perf_counter()
    if not password:
        return _unconfigured(
            start,
            "REDIS_PASSWORD is not configured; the canary cannot prove an authenticated write",
        )
    key = f"canary:probe:{secrets.token_hex(8)}"
    value = secrets.token_hex(16)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with sock.makefile("rb") as rf:

                def call(step: str, *parts: str) -> str | int | bytes | None:
                    sock.sendall(_resp_command(*parts))
                    try:
                        return _read_reply(rf)
                    except _RedisReplyError as err:
                        raise _ProbeFailure(f"{step} failed: {err}") from None

                call("AUTH", "AUTH", password)
                if call("PING", "PING") != "PONG":
                    raise _ProbeFailure("PING did not answer PONG")
                call("SETEX", "SETEX", key, "10", value)
                got = call("GET", "GET", key)
                if got != value.encode("utf-8"):
                    raise _ProbeFailure(
                        f"GET returned {got!r} instead of the value written"
                    )
                call("DEL", "DEL", key)
        return _outcome("pass", start, "AUTH, PING, SETEX/GET/DEL verified")
    except _ProbeFailure as exc:
        return _outcome("fail", start, str(exc))
    except Exception as exc:  # noqa: BLE001 - a probe reports the failure
        return _outcome("fail", start, f"{type(exc).__name__}: {exc}")


def _probe_s3(
    *,
    endpoint_url: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    timeout: float = 3.0,
    client: Any | None = None,
) -> Dict[str, Any]:
    """PUT, GET and DELETE one object under the canary prefix, signed with the canary key.

    Liveness passed while writes failed in the past. A denied or corrupt write, read or
    delete fails the check. A written object is deleted even when the read-back fails.
    """
    start = time.perf_counter()
    missing = [
        name
        for name, value in (
            ("CANARY_S3_ENDPOINT_URL", endpoint_url),
            ("CANARY_S3_BUCKET", bucket),
            ("CANARY_S3_ACCESS_KEY", access_key),
            ("CANARY_S3_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        return _unconfigured(start, f"{', '.join(missing)} not configured")
    key = f"{CANARY_S3_PREFIX}{secrets.token_hex(8)}"
    body = secrets.token_hex(16).encode("utf-8")
    owned = client is None
    try:
        from infra2_sdk.runtime.s3 import (
            S3Settings,
            create_s3_client,
            read_object_bytes,
        )

        s3 = client or create_s3_client(
            S3Settings(
                bucket=bucket,
                endpoint_url=endpoint_url,
                access_key_id=access_key,
                secret_access_key=secret_key,
                addressing_style="path",
                connect_timeout_seconds=timeout,
                read_timeout_seconds=timeout,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a probe reports the failure
        return _outcome(
            "fail", start, f"S3 client setup failed: {type(exc).__name__}: {exc}"
        )
    failure: str | None = None
    written = False
    try:
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body)
        except Exception as exc:  # noqa: BLE001
            raise _ProbeFailure(f"PUT failed: {exc}") from exc
        written = True
        try:
            got = read_object_bytes(s3, bucket=bucket, key=key)
        except Exception as exc:  # noqa: BLE001
            raise _ProbeFailure(f"GET failed: {exc}") from exc
        if got != body:
            raise _ProbeFailure("GET returned data that differs from the PUT body")
    except _ProbeFailure as exc:
        failure = str(exc)
    if written:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            failure = failure or f"DELETE failed: {exc}"
    if owned and hasattr(s3, "close"):
        with contextlib.suppress(Exception):
            s3.close()
    if failure:
        return _outcome("fail", start, failure)
    return _outcome("pass", start, "PUT, GET and DELETE verified under canary prefix")


class _NullSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None


def _span(name: str, **attributes: Any):
    """A span context manager. It is a no-op when no OTLP endpoint was issued."""
    if _TRACER is None:
        return contextlib.nullcontext(_NullSpan())
    return _TRACER.start_as_current_span(name, attributes=attributes or None)


def _traced_check(name: str, probe: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    with _span(f"canary.check.{name}", **{"canary.check": name}) as span:
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001 - one crashed probe must not hide the rest
            result = {
                "status": "fail",
                "latency_ms": 0.0,
                "detail": f"probe raised {type(exc).__name__}: {exc}",
            }
        span.set_attribute("canary.status", result["status"])
        span.set_attribute("canary.latency_ms", result["latency_ms"])
        span.set_attribute("canary.detail", result["detail"])
    if result["status"] != PASS:
        LOGGER.warning(
            "canary check %s is %s: %s", name, result["status"], result["detail"]
        )
    return result


def _run_checks(
    plan: Dict[str, Callable[[], Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Run every check in parallel, each bounded by CHECK_DEADLINE_SECONDS."""
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(plan), thread_name_prefix="canary-check"
    )
    try:
        futures = {
            # one context copy per task: it carries the active span into the worker
            executor.submit(
                contextvars.copy_context().run, _traced_check, name, probe
            ): name
            for name, probe in plan.items()
        }
        done, pending = concurrent.futures.wait(futures, timeout=CHECK_DEADLINE_SECONDS)
        results = {futures[future]: future.result() for future in done}
        for future in pending:
            name = futures[future]
            detail = f"check did not finish within {CHECK_DEADLINE_SECONDS:g}s"
            LOGGER.warning("canary check %s failed: %s", name, detail)
            results[name] = {
                "status": "fail",
                "latency_ms": round(CHECK_DEADLINE_SECONDS * 1000, 1),
                "detail": detail,
            }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return {name: results[name] for name in plan}


def run_all_checks() -> Dict[str, Any]:
    """Run the real probes against the platform's infrastructure capabilities."""
    suffix = os.environ.get("ENV_SUFFIX", "")
    pg_host = os.environ.get("POSTGRES_HOST", f"platform-postgres{suffix}")
    pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
    pg_user = os.environ.get("CANARY_POSTGRES_USER", "")
    pg_password = os.environ.get("CANARY_POSTGRES_PASSWORD", "")
    pg_database = os.environ.get("CANARY_POSTGRES_DATABASE", "postgres")
    redis_host = os.environ.get("REDIS_HOST", f"platform-redis{suffix}")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_password = os.environ.get("REDIS_PASSWORD") or None
    s3_endpoint = os.environ.get(
        "CANARY_S3_ENDPOINT_URL", f"http://platform-s3{suffix}:9000"
    )
    s3_bucket = os.environ.get("CANARY_S3_BUCKET", "")
    s3_access_key = os.environ.get("CANARY_S3_ACCESS_KEY", "")
    s3_secret_key = os.environ.get("CANARY_S3_SECRET_KEY", "")
    signoz_url = os.environ.get(
        "SIGNOZ_OTEL_COLLECTOR", "http://platform-signoz-otel-collector:13133"
    )
    openpanel_url = os.environ.get(
        "OPENPANEL_API_URL", "http://platform-openpanel-api:3000/healthcheck"
    )
    authentik_url = os.environ.get(
        "AUTHENTIK_ENDPOINT", "http://platform-authentik-server:9000/-/health/live/"
    )

    plan: Dict[str, Callable[[], Dict[str, Any]]] = {
        "postgres": lambda: _probe_postgres(
            pg_host, pg_port, user=pg_user, password=pg_password, dbname=pg_database
        ),
        "redis": lambda: _probe_redis(redis_host, redis_port, password=redis_password),
        "s3": lambda: _probe_s3(
            endpoint_url=s3_endpoint,
            bucket=s3_bucket,
            access_key=s3_access_key,
            secret_key=s3_secret_key,
        ),
        "signoz": lambda: _probe_http(signoz_url),
        "openpanel": lambda: _probe_http(openpanel_url),
        "authentik": lambda: _probe_http(authentik_url),
    }
    with _span("canary.run") as run_span:
        results = _run_checks(plan)
        checks = {
            "postgres": results["postgres"],
            "redis": results["redis"],
            "s3": results["s3"],
            # backward-compatibility alias for existing dashboards
            "minio": results["s3"],
            "signoz": results["signoz"],
            "openpanel": results["openpanel"],
            "authentik": results["authentik"],
        }
        unconfigured = [
            n for n in GATING_CHECKS if results[n]["status"] == UNCONFIGURED
        ]
        failed = [
            n for n in GATING_CHECKS if results[n]["status"] not in (PASS, UNCONFIGURED)
        ]
        informational_down = [
            n for n in INFORMATIONAL_CHECKS if results[n]["status"] != PASS
        ]
        # A status other than `pass` or `unconfigured` counts as a failure (fail closed).
        all_pass = not failed and not unconfigured
        run_span.set_attribute("canary.ok", all_pass)
        run_span.set_attribute("canary.failed_checks", ",".join(failed))
        run_span.set_attribute("canary.unconfigured_checks", ",".join(unconfigured))
        run_span.set_attribute(
            "canary.informational_down", ",".join(informational_down)
        )
        # inside the span, so the exported log record carries the trace id
        if all_pass:
            LOGGER.info("canary run ok: %d write paths passed", len(GATING_CHECKS))
        else:
            LOGGER.error(
                "canary run red: failed=%s unconfigured=%s",
                ",".join(failed) or "-",
                ",".join(unconfigured) or "-",
            )
        if informational_down:
            LOGGER.warning(
                "canary informational checks not passing: %s",
                ",".join(informational_down),
            )
    # The probe runner keeps only the first 128 bytes of this body as its observed text, so
    # the verdict and its classification come first.
    return {
        "ok": all_pass,
        "unconfigured": unconfigured,
        "failed": failed,
        "service": "platform/todo",
        "domain": "todo.zitian.party",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gating": list(GATING_CHECKS),
        "informational": list(INFORMATIONAL_CHECKS),
        "checks": checks,
    }


def cached_canary_status() -> Dict[str, Any]:
    """The status document, with one run in flight and a short-lived reuse.

    Requests queue on the lock. The first one runs the probes. The others get that result
    until STATUS_CACHE_TTL_SECONDS pass. `age_seconds` says how old the result is, so a
    reused result never reads as a fresh one.
    """
    global _STATUS_CACHE
    with _STATUS_LOCK:
        cached = _STATUS_CACHE
        if cached is None or time.monotonic() - cached[0] >= STATUS_CACHE_TTL_SECONDS:
            report = run_all_checks()
            cached = (time.monotonic(), report)
            _STATUS_CACHE = cached
        document = dict(cached[1])
        document["age_seconds"] = round(time.monotonic() - cached[0], 1)
        return document


def _acquire_tracer() -> Any:
    from opentelemetry import trace

    return trace.get_tracer(SERVICE_TRACER_NAME)


def configure_service_telemetry() -> Any | None:
    """Configure OTLP traces, metrics and logs when the deploy issued an endpoint.

    The deploy issues OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME and
    OTEL_RESOURCE_ATTRIBUTES. The endpoint turns export on. Without the issued identity
    the data would land under `unknown_service`, so the service refuses to start.
    """
    global _TRACER
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return None
    if not os.environ.get("OTEL_SERVICE_NAME", "").strip():
        raise RuntimeError(
            "OTEL_SERVICE_NAME is required when OTEL_EXPORTER_OTLP_ENDPOINT is set; "
            "the deploy issues the telemetry identity"
        )
    from infra2_sdk.runtime import otel as sdk_otel

    settings = sdk_otel.OtelSettings.from_env(os.environ, strict=True)
    providers = sdk_otel.configure_telemetry(settings, set_global=True)
    _TRACER = _acquire_tracer()
    return providers


class TodoHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: Any, send_body: bool = True):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, HEAD"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, HEAD"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_HEAD(self):
        self._handle_get(send_body=False)

    def do_GET(self):
        self._handle_get(send_body=True)

    def _handle_get(self, send_body: bool = True):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            username = self.headers.get("X-authentik-username", "")
            name = self.headers.get("X-authentik-name", "")
            email = self.headers.get("X-authentik-email", "")
            user_display = name or username or email
            if username:
                safe_display = html.escape(user_display)
                auth_badge = (
                    f'<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">'
                    f'<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>'
                    f"SSO 认证通过: {safe_display}"
                    f"</span>"
                )
            else:
                auth_badge = (
                    '<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-slate-800 text-slate-400 border border-slate-700">'
                    '<span class="w-2 h-2 rounded-full bg-slate-500"></span>'
                    "未检测到 ForwardAuth 头 (开发/直连模式)"
                    "</span>"
                )
            sha = os.environ.get("GIT_COMMIT_SHA", "dev-local")[:7]
            rendered = (
                HTML_TEMPLATE.replace("${GIT_COMMIT_SHA}", sha)
                .replace("${AUTH_BADGE}", auth_badge)
                .encode("utf-8")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(rendered)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, HEAD"
            )
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, Authorization"
            )
            self.end_headers()
            if send_body:
                self.wfile.write(rendered)
            return

        if path == "/api/auth/me":
            username = self.headers.get("X-authentik-username", "")
            groups = [
                g.strip()
                for g in self.headers.get("X-authentik-groups", "").split(",")
                if g.strip()
            ]
            self._send_json(
                200,
                {
                    "authenticated": bool(username),
                    "username": username or None,
                    "name": self.headers.get("X-authentik-name") or None,
                    "email": self.headers.get("X-authentik-email") or None,
                    "groups": groups,
                },
                send_body=send_body,
            )
            return

        if path == "/api/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "status": "healthy",
                    "service": "platform/todo",
                    "domain": "todo.zitian.party",
                    "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
                },
                send_body=send_body,
            )
            return

        if path == "/api/canary/status":
            result = cached_canary_status()
            status_code = 200 if result["ok"] else 503
            self._send_json(status_code, result, send_body=send_body)
            return

        if path == "/api/todos":
            with _todos_lock:
                items = [dict(t) for t in _FALLBACK_TODOS]
            self._send_json(200, items, send_body=send_body)
            return

        self._send_json(404, {"error": "Not Found"}, send_body=send_body)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/todos":
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as err:
                    self._send_json(
                        400, {"error": "Invalid JSON body", "detail": str(err)}
                    )
                    return
            else:
                payload = {}

            if "completed" in payload:
                if not isinstance(payload["completed"], bool):
                    self._send_json(
                        400, {"error": "Field 'completed' must be a boolean"}
                    )
                    return
                completed_val = payload["completed"]
            else:
                completed_val = False

            if "title" in payload and not isinstance(payload["title"], str):
                self._send_json(400, {"error": "Field 'title' must be a string"})
                return

            with _todos_lock:
                new_id = max((t["id"] for t in _FALLBACK_TODOS), default=0) + 1
                title = (
                    str(payload["title"]).strip()
                    if "title" in payload
                    else f"Todo #{new_id}"
                )
                item = {
                    "id": new_id,
                    "title": title,
                    "completed": completed_val,
                    "capability": str(payload.get("capability", "general")),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                _FALLBACK_TODOS.append(item)
                item_copy = dict(item)

            self._send_json(201, item_copy)
            return

        self._send_json(404, {"error": "Not Found"})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "todos"
            and parts[3] == "toggle"
        ):
            try:
                todo_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid todo ID"})
                return

            item_copy = None
            with _todos_lock:
                for t in _FALLBACK_TODOS:
                    if t["id"] == todo_id:
                        t["completed"] = not t["completed"]
                        item_copy = dict(t)
                        break

            if item_copy is not None:
                self._send_json(200, item_copy)
            else:
                self._send_json(404, {"error": "Todo not found"})
            return

        self._send_json(404, {"error": "Not Found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "todos":
            try:
                todo_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid todo ID"})
                return

            removed_copy = None
            with _todos_lock:
                for idx, t in enumerate(_FALLBACK_TODOS):
                    if t["id"] == todo_id:
                        removed = _FALLBACK_TODOS.pop(idx)
                        removed_copy = dict(removed)
                        break

            if removed_copy is not None:
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "deleted_id": todo_id,
                        "item": removed_copy,
                    },
                )
            else:
                self._send_json(404, {"error": "Todo not found"})
            return

        self._send_json(404, {"error": "Not Found"})


SERVER_START_TIME = time.time()


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    providers = configure_service_telemetry()
    # PID 1 ignores SIGTERM without a handler. Exit cleanly so telemetry flushes.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), TodoHandler)
    LOGGER.info(
        "Canary Todo service running at http://0.0.0.0:%d (telemetry %s)",
        port,
        "on" if providers is not None else "off",
    )
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        if providers is not None:
            providers.shutdown()


if __name__ == "__main__":
    main()
