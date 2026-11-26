"""The token-init script is inline in the Authentik compose, not a checkout mount (#1822).

A raw Dokploy compose has no checkout, so token-init cannot mount `./init-token.sh`.
The compose `command` holds the script. Compose interpolates `$`, so the file writes
each shell `$` as `$$`. These tests read the script as the container gets it.
"""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from common.deploy.compose_compile import load_stack, raw_ineligibility

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = "platform/10.authentik/compose.yaml"
SERVICE = "token-init"


def _spec() -> dict:
    text = (ROOT / COMPOSE).read_text(encoding="utf-8")
    return yaml.safe_load(text)["services"][SERVICE]


def _source_script() -> str:
    command = _spec()["command"]
    assert command[:2] == ["sh", "-c"] and len(command) == 3
    return command[2]


# The one Compose reference in the script: the server host of this environment.
SUFFIX_REF = "${ENV_SUFFIX}"


def _container_script(suffix: str = "-staging") -> str:
    """The script after Compose interpolation: `$$` becomes `$`, ENV_SUFFIX resolves."""
    parts = _source_script().split("$$")
    return "$".join(p.replace(SUFFIX_REF, suffix) for p in parts)


def test_token_init_mounts_nothing_from_the_checkout() -> None:
    assert _spec()["volumes"] == ["secrets:/secrets:ro"]
    compiled = load_stack(COMPOSE).compiled
    assert [
        r for r in raw_ineligibility(compiled) if r.startswith(f"services.{SERVICE} ")
    ] == []
    assert not (ROOT / "platform/10.authentik/init-token.sh").exists()


def test_every_shell_dollar_is_escaped_for_compose() -> None:
    # A single `$` is a Compose interpolation: `$ENV` would become the host value.
    script = _source_script()
    # 14 escaped `$` on 2026-11-26 (measured), after the failure path stopped
    # printing the token.
    assert script.count("$$") >= 14
    unescaped = re.sub(r"\$\$", "", script)
    assert unescaped.count(SUFFIX_REF) == 1
    assert "$" not in unescaped.replace(SUFFIX_REF, "")


def test_the_script_waits_for_the_server_of_its_environment() -> None:
    # The deploy runs token-init with no ENV_SUFFIX in the container env, so the host
    # comes from Compose (#161): staging must not wait on the production server.
    host = 'AUTHENTIK_SERVER_HOST="platform-authentik-server{}:9000"'
    assert host.format("-staging") in _container_script("-staging")
    assert host.format("") in _container_script("")


def test_the_container_script_is_valid_sh(tmp_path: Path) -> None:
    path = tmp_path / "init-token.sh"
    path.write_text(_container_script(), encoding="utf-8")
    result = subprocess.run(
        ["sh", "-n", str(path)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_token_init_passes_env_to_the_script() -> None:
    assert _spec()["environment"]["ENV"] == "${ENV}"


FAKE_PYTHON = """#!/bin/sh
# `python -m manage shell` prints the token; `python -` saves its stdin.
if [ "$1" = "-m" ]; then
  cat > /dev/null
  printf 'tok-123'
  exit 0
fi
n=$(ls "$CAPTURE" | wc -l | tr -d ' ')
cat > "$CAPTURE/$n.py"
exit 0
"""


def _run(
    tmp_path: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    capture = tmp_path / "capture"
    capture.mkdir()
    result = subprocess.run(
        ["sh", "-c", _container_script()],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "VAULT_INIT_TOKEN": "vault-tok",
            "VAULT_INIT_ADDR": "http://vault.test",
            **env,
        },
    )
    # 0.py is the readiness wait; 1.py is the Vault write.
    return result, (capture / "1.py").read_text(encoding="utf-8")


def test_the_script_writes_the_token_to_the_vault_path_of_env(tmp_path: Path) -> None:
    result, vault_write = _run(tmp_path, {"ENV": "staging"})
    assert result.returncode == 0, result.stderr
    assert (
        'vault_url = "http://vault.test/v1/secret/data/platform/staging/authentik"'
        in vault_write
    )
    assert 'token = """tok-123"""' in vault_write
    assert "Root token stored to Vault successfully" in result.stdout


def test_the_script_uses_production_when_env_is_unset(tmp_path: Path) -> None:
    result, vault_write = _run(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert "/v1/secret/data/platform/production/authentik" in vault_write


# --- the Vault write fails: the token never reaches the container log -------------------

ROOT_TOKEN = "ak-root-SECRET-" + "q" * 40

# `python -m manage shell` prints the token; the first `python -` (readiness wait) exits
# 0; the second `python -` (the Vault write) runs in the real interpreter.
REAL_VAULT_PYTHON = """#!/bin/sh
if [ "$1" = "-m" ]; then
  cat > /dev/null
  printf '%s' "$ROOT_TOKEN"
  exit 0
fi
if [ ! -e "$CAPTURE/ready" ]; then
  cat > /dev/null
  : > "$CAPTURE/ready"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
"""


class _EchoHandler(BaseHTTPRequestHandler):
    """A Vault that refuses the write and echoes the request body (with the token)."""

    status = 403

    def do_PATCH(self) -> None:  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b'{"errors":["permission denied"],"echo":' + body + b"}")

    def log_message(self, *_args) -> None:
        return None


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_failed_write(tmp_path: Path, vault_addr: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(REAL_VAULT_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    capture = tmp_path / "capture"
    capture.mkdir()
    return subprocess.run(
        ["sh", "-c", _container_script()],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "REAL_PYTHON": sys.executable,
            "ROOT_TOKEN": ROOT_TOKEN,
            "VAULT_INIT_TOKEN": "vault-tok",
            "VAULT_INIT_ADDR": vault_addr,
            "ENV": "staging",
        },
    )


@pytest.mark.parametrize("status", [403, 500])
def test_a_refused_vault_write_never_prints_the_token(tmp_path: Path, status) -> None:
    handler = type("Handler", (_EchoHandler,), {"status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_failed_write(tmp_path, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert ROOT_TOKEN not in output and "permission denied" not in output
    assert f"CRITICAL: Failed to store token to Vault (HTTP {status})" in result.stderr
    assert "Token was generated but not persisted" in result.stderr


def test_an_unreachable_vault_never_prints_the_token(tmp_path: Path) -> None:
    result = _run_failed_write(tmp_path, f"http://127.0.0.1:{_closed_port()}")
    assert result.returncode == 1
    assert ROOT_TOKEN not in result.stdout + result.stderr
    assert "CRITICAL: Failed to store token to Vault (URLError)" in result.stderr
