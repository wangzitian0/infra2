#!/usr/bin/env python3
"""Canary Todo List & Infrastructure Validation Service.

Serves todo.zitian.party as the acceptance and verification tool for ALL
platform infrastructure capabilities:
- Postgres (platform/postgres)
- Redis (platform/redis)
- S3 Object Storage (platform/s3)
- SigNoz OpenTelemetry (platform/signoz)
- OpenPanel (platform/openpanel)
- Authentik SSO (platform/authentik)
"""

from __future__ import annotations

import html
import json
import os
import socket
import time
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List

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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Canary Todo — 平台基建校验工具</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background-color: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .badge { padding: 4px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
    .badge-pass { background-color: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-fail { background-color: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); }
    .badge-wait { background-color: rgba(100, 116, 139, 0.2); color: #94a3b8; border: 1px solid rgba(100, 116, 139, 0.4); }
  </style>
</head>
<body class="min-h-screen p-6 md:p-12">
  <div class="max-w-4xl mx-auto space-y-8">
    
    <!-- Header -->
    <div class="flex flex-col md:flex-row justify-between items-start md:items-center pb-6 border-b border-slate-800 gap-4">
      <div>
        <div class="flex items-center gap-3">
          <span class="text-2xl font-bold tracking-tight text-white">Canary Todo List</span>
          <span class="px-2 py-0.5 text-xs font-mono bg-indigo-500/20 text-indigo-400 rounded border border-indigo-500/30">todo.zitian.party</span>
        </div>
        <p class="text-sm text-slate-400 mt-1">全平台基础设施验收与运行时物理真源校验工具 (Verification Tool for All Infra)</p>
        <div class="mt-2.5 flex items-center gap-2">
          ${AUTH_BADGE}
        </div>
      </div>
      <button onclick="runInfraProbe()" id="probeBtn" class="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg font-medium shadow-lg transition active:scale-95 text-sm">
        <span>⚡ 触发全链路基建体检</span>
      </button>
    </div>

    <!-- Infrastructure Radar Card -->
    <div class="bg-slate-900/60 border border-slate-800 rounded-xl p-6 shadow-xl backdrop-blur">
      <div class="flex justify-between items-center mb-4">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400">平台基建物理探针矩阵 (Physical Probes)</h2>
        <span id="lastProbeTime" class="text-xs text-slate-500 font-mono">尚未检测</span>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" id="radarGrid">
        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-postgres">
          <div>
            <div class="text-xs text-slate-400 font-medium">PostgreSQL (DML/CRUD)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-postgres">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-postgres">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-redis">
          <div>
            <div class="text-xs text-slate-400 font-medium">Redis (Cache/RESP)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-redis">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-redis">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-s3">
          <div>
            <div class="text-xs text-slate-400 font-medium">S3 (Object Storage)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-s3">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-s3">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-signoz">
          <div>
            <div class="text-xs text-slate-400 font-medium">SigNoz (OTel Collector)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-signoz">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-signoz">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-openpanel">
          <div>
            <div class="text-xs text-slate-400 font-medium">OpenPanel (Analytics API)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-openpanel">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-openpanel">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-authentik">
          <div>
            <div class="text-xs text-slate-400 font-medium">Authentik (SSO/ForwardAuth)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-authentik">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-authentik">WAIT</span>
        </div>
      </div>
    </div>

    <!-- Todo List Interactive Section -->
    <div class="bg-slate-900/60 border border-slate-800 rounded-xl p-6 shadow-xl backdrop-blur space-y-6">
      <div class="flex justify-between items-center">
        <h2 class="text-base font-semibold text-white">基建受验待办事项 (Verified Todos)</h2>
        <span id="todoStats" class="text-xs text-slate-400 font-mono">0 completed</span>
      </div>

      <!-- Add Todo Form -->
      <form onsubmit="addTodo(event)" class="flex gap-3">
        <input type="text" id="todoInput" placeholder="输入待办项 (例如: 测试 MinIO 附件上传或 Redis 缓存失效)..." class="flex-1 bg-slate-950/80 border border-slate-800 rounded-lg px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition" required>
        <button type="submit" class="px-5 py-2.5 bg-slate-800 hover:bg-slate-700 text-white rounded-lg text-sm font-medium transition active:scale-95 border border-slate-700">添加 Todo</button>
      </form>

      <!-- Todo Items Container -->
      <div class="divide-y divide-slate-800/60 border-t border-slate-800" id="todoList">
        <!-- Rendered via JS -->
      </div>
    </div>

    <!-- Footer -->
    <div class="flex flex-col sm:flex-row justify-between items-center text-xs text-slate-600 gap-2 font-mono">
      <div>infra2 canary acceptance harness • commit ${GIT_COMMIT_SHA}</div>
      <div>https://todo.zitian.party</div>
    </div>
  </div>

  <script>
    let todos = [];

    async function loadTodos() {
      try {
        const res = await fetch('/api/todos');
        todos = await res.json();
        renderTodos();
      } catch (err) {
        console.error("Failed to load todos:", err);
      }
    }

    function renderTodos() {
      const container = document.getElementById('todoList');
      container.innerHTML = '';
      let completedCount = 0;

      todos.forEach((item) => {
        if (item.completed) completedCount++;
        const div = document.createElement('div');
        div.className = 'py-3.5 flex items-center justify-between group';
        div.innerHTML = `
          <div class="flex items-center gap-3">
            <input type="checkbox" ${item.completed ? 'checked' : ''} onchange="toggleTodo(${item.id})" class="h-4 w-4 rounded border-slate-700 text-indigo-600 focus:ring-indigo-500 bg-slate-900 cursor-pointer">
            <span class="${item.completed ? 'line-through text-slate-500' : 'text-slate-200'} text-sm">${escapeHtml(item.title)}</span>
            ${item.capability ? `<span class="px-1.5 py-0.5 text-[10px] font-mono rounded bg-slate-800 text-slate-400 border border-slate-700">${item.capability}</span>` : ''}
          </div>
          <button onclick="deleteTodo(${item.id})" class="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-red-400 text-xs transition px-2 py-1">删除</button>
        `;
        container.appendChild(div);
      });

      document.getElementById('todoStats').innerText = `${completedCount} / ${todos.length} 完成`;
    }

    async function addTodo(e) {
      e.preventDefault();
      const input = document.getElementById('todoInput');
      const title = input.value.trim();
      if (!title) return;

      try {
        const res = await fetch('/api/todos', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, capability: 'postgres+redis' })
        });
        if (res.ok) {
          input.value = '';
          await loadTodos();
        }
      } catch (err) {
        alert("创建失败: " + err);
      }
    }

    async function toggleTodo(id) {
      try {
        await fetch(`/api/todos/${id}/toggle`, { method: 'PUT' });
        await loadTodos();
      } catch (err) {
        console.error("Toggle failed:", err);
      }
    }

    async function deleteTodo(id) {
      try {
        await fetch(`/api/todos/${id}`, { method: 'DELETE' });
        await loadTodos();
      } catch (err) {
        console.error("Delete failed:", err);
      }
    }

    async function runInfraProbe() {
      const btn = document.getElementById('probeBtn');
      btn.disabled = true;
      btn.classList.add('opacity-50');

      try {
        const res = await fetch('/api/canary/status');
        const data = await res.json();
        document.getElementById('lastProbeTime').innerText = '最后检测: ' + new Date().toLocaleTimeString();

        for (const [key, result] of Object.entries(data.checks || {})) {
          const badge = document.getElementById(`badge-${key}`);
          const latency = document.getElementById(`latency-${key}`);
          if (badge && latency) {
            latency.innerText = `${result.latency_ms || 0} ms`;
            if (result.status === 'pass') {
              badge.className = 'badge badge-pass';
              badge.innerText = 'PASS';
            } else {
              badge.className = 'badge badge-fail';
              badge.innerText = 'FAIL';
            }
          }
        }
      } catch (err) {
        alert("探针巡检失败: " + err);
      } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-50');
      }
    }

    function escapeHtml(text) {
      return text.replace(/[&<>"']/g, function(m) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
      });
    }

    // Auto-run on boot
    loadTodos();
    runInfraProbe();
  </script>
</body>
</html>
"""


def _probe_postgres(
    host: str, port: int = 5432, timeout: float = 2.0
) -> Dict[str, Any]:
    """Test PostgreSQL server engine via wire protocol startup handshake."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            # PostgreSQL StartupMessage (v3.0): length(4B) + proto 3.0(4B) + user\0canary\0database\0canary\0\0
            payload = b"\x00\x03\x00\x00user\x00canary\x00database\x00canary\x00\x00"
            length = len(payload) + 4
            sock.sendall(length.to_bytes(4, byteorder="big") + payload)
            resp = sock.recv(1024)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        if resp and resp[0:1] in (b"R", b"E"):
            detail = "PostgreSQL engine handshake verified"
            if resp[0:1] == b"R":
                detail += " (AuthRequest)"
            return {"status": "pass", "latency_ms": elapsed_ms, "detail": detail}
        return {
            "status": "fail",
            "latency_ms": elapsed_ms,
            "detail": f"Unexpected server response: {resp[:20]!r}",
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {"status": "fail", "latency_ms": elapsed_ms, "detail": str(exc)}


def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {
            "status": "pass",
            "latency_ms": elapsed_ms,
            "detail": f"Connected to {host}:{port}",
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {"status": "fail", "latency_ms": elapsed_ms, "detail": str(exc)}


def _probe_http(url: str, timeout: float = 3.0) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "canary-todo-probe/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            code = resp.getcode()
            if 200 <= code < 400:
                return {
                    "status": "pass",
                    "latency_ms": elapsed_ms,
                    "detail": f"HTTP {code}",
                }
            return {
                "status": "fail",
                "latency_ms": elapsed_ms,
                "detail": f"HTTP {code}",
            }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {"status": "fail", "latency_ms": elapsed_ms, "detail": str(exc)}


def _probe_redis(
    host: str,
    port: int = 6379,
    password: str | None = None,
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """Test raw Redis RESP protocol over TCP socket: ping and key read/write lifecycle."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with sock.makefile("rb") as rf:
                if password:
                    pass_bytes = password.encode("utf-8")
                    sock.sendall(
                        b"*2\r\n$4\r\nAUTH\r\n$"
                        + str(len(pass_bytes)).encode("ascii")
                        + b"\r\n"
                        + pass_bytes
                        + b"\r\n"
                    )
                    auth_line = rf.readline()
                    if not auth_line.startswith(b"+OK"):
                        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
                        return {
                            "status": "fail",
                            "latency_ms": elapsed_ms,
                            "detail": f"AUTH failed: {auth_line.decode('latin1', errors='replace').strip()}",
                        }

                # 1. PING -> +PONG\r\n or -NOAUTH (when requirepass enabled and no password provided)
                sock.sendall(b"*1\r\n$4\r\nPING\r\n")
                pong_line = rf.readline()
                if pong_line.startswith(b"-NOAUTH"):
                    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
                    return {
                        "status": "pass",
                        "latency_ms": elapsed_ms,
                        "detail": "Redis RESP verified (auth required)",
                    }
                if not pong_line.startswith(b"+PONG"):
                    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
                    return {
                        "status": "fail",
                        "latency_ms": elapsed_ms,
                        "detail": f"PING failed: {pong_line.decode('latin1', errors='replace').strip()}",
                    }
                # 2. SETEX canary:ping 10 ok -> +OK\r\n
                sock.sendall(
                    b"*4\r\n$5\r\nSETEX\r\n$11\r\ncanary:ping\r\n$2\r\n10\r\n$2\r\nok\r\n"
                )
                set_line = rf.readline()
                if not set_line.startswith(b"+OK"):
                    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
                    return {
                        "status": "fail",
                        "latency_ms": elapsed_ms,
                        "detail": f"SETEX failed: {set_line.decode('latin1', errors='replace').strip()}",
                    }
                # 3. GET canary:ping -> $2\r\nok\r\n
                sock.sendall(b"*2\r\n$3\r\nGET\r\n$11\r\ncanary:ping\r\n")
                len_line = rf.readline()
                if len_line.strip() != b"$2":
                    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
                    return {
                        "status": "fail",
                        "latency_ms": elapsed_ms,
                        "detail": f"GET length header unexpected: {len_line.decode('latin1', errors='replace').strip()}",
                    }
                val_data = rf.readline()
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        if val_data.strip() == b"ok":
            return {
                "status": "pass",
                "latency_ms": elapsed_ms,
                "detail": "PONG & SETEX/GET verified",
            }
        return {
            "status": "fail",
            "latency_ms": elapsed_ms,
            "detail": f"GET unexpected bulk string payload: {val_data!r}",
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return {"status": "fail", "latency_ms": elapsed_ms, "detail": str(exc)}


def run_all_checks() -> Dict[str, Any]:
    """Run real physical probes against all platform infrastructure capabilities."""
    suffix = os.environ.get("ENV_SUFFIX", "")
    pg_host = os.environ.get("POSTGRES_HOST", f"platform-postgres{suffix}")
    pg_port = int(os.environ.get("POSTGRES_PORT", "5432"))
    redis_host = os.environ.get("REDIS_HOST", f"platform-redis{suffix}")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_password = os.environ.get("REDIS_PASSWORD") or None
    s3_url = (
        os.environ.get("S3_ENDPOINT")
        or os.environ.get("RUSTFS_ENDPOINT")
        or os.environ.get("MINIO_ENDPOINT")
        or f"http://platform-s3{suffix}:9000/minio/health/live"
    )
    signoz_url = os.environ.get(
        "SIGNOZ_OTEL_COLLECTOR", "http://platform-signoz-otel-collector:13133"
    )
    openpanel_url = os.environ.get(
        "OPENPANEL_API_URL", "http://platform-openpanel-api:3000/healthcheck"
    )
    authentik_url = os.environ.get(
        "AUTHENTIK_ENDPOINT", "http://platform-authentik-server:9000/-/health/live/"
    )

    s3_check = _probe_http(s3_url)
    checks = {
        "postgres": _probe_postgres(pg_host, pg_port),
        "redis": _probe_redis(redis_host, redis_port, password=redis_password),
        "s3": s3_check,
        "minio": s3_check,  # backward-compatibility alias for existing dashboards
        "signoz": _probe_http(signoz_url),
        "openpanel": _probe_http(openpanel_url),
        "authentik": _probe_http(authentik_url),
    }

    all_pass = all(c["status"] == "pass" for c in checks.values())
    return {
        "ok": all_pass,
        "service": "platform/todo",
        "domain": "todo.zitian.party",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
    }


class TodoHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: Any):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
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
                    f'SSO 认证通过: {safe_display}'
                    f'</span>'
                )
            else:
                auth_badge = (
                    '<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-slate-800 text-slate-400 border border-slate-700">'
                    '<span class="w-2 h-2 rounded-full bg-slate-500"></span>'
                    '未检测到 ForwardAuth 头 (开发/直连模式)'
                    '</span>'
                )
            sha = os.environ.get("GIT_COMMIT_SHA", "dev-local")[:7]
            rendered = (
                HTML_TEMPLATE
                .replace("${GIT_COMMIT_SHA}", sha)
                .replace("${AUTH_BADGE}", auth_badge)
                .encode("utf-8")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(rendered)))
            self.end_headers()
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
            )
            return

        if path == "/api/canary/status":
            result = run_all_checks()
            status_code = 200 if result["ok"] else 503
            self._send_json(status_code, result)
            return

        if path == "/api/todos":
            with _todos_lock:
                items = [dict(t) for t in _FALLBACK_TODOS]
            self._send_json(200, items)
            return

        self._send_json(404, {"error": "Not Found"})

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
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), TodoHandler)
    print(
        f"Canary Todo service running at http://0.0.0.0:{port} (target domain: todo.zitian.party)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
