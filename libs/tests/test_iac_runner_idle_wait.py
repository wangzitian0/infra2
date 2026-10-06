"""#666: the runner bootstrap waits for zero in-flight deploys before it recreates the runner.

A recreate drops the runner's in-memory `_in_flight_deploys`, and the deploy in flight is
lost. These tests pin three parts:

1. The runner reports the count at the top level of `/health`, outside `checks`.
2. `wait_for_runner_idle` (bootstrap/06.iac_runner/wait_for_idle.sh) waits on that count
   with a limit, and continues when the count is unknown.
3. scripts/deploy_iac_runner_bootstrap.sh builds the image, waits, and only then recreates.

The helper runs through `bash -s` with the script on stdin, as the workflow runs it over ssh.
The fake docker reads all stdin, so a helper command that reads stdin eats the later lines.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IAC_RUNNER = ROOT / "bootstrap/06.iac_runner"
HELPER = IAC_RUNNER / "wait_for_idle.sh"
BOOTSTRAP_SCRIPT = ROOT / "scripts/deploy_iac_runner_bootstrap.sh"
DEPLOY_SHA = "a" * 40
AFTER_LINE = "after wait_for_runner_idle"


# --- 1. The runner reports the in-flight count -------------------------------------------


def _load_webhook_server(monkeypatch, tmp_path, name: str):
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(headers={}, data=b"", json={})
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("DOKPLOY_API_KEY", "test-dokploy-key")
    spec = importlib.util.spec_from_file_location(
        name, IAC_RUNNER / "webhook_server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("VAULT_APP_TOKEN=redacted\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "infra2").mkdir(parents=True)
    monkeypatch.setattr(module, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(module, "WORKSPACE", workspace)
    monkeypatch.setattr(module, "op_service_account_works", lambda: True)
    monkeypatch.setattr(
        module, "_dependency_checks", lambda: {"python:flask": True, "binary:op": True}
    )
    return module


def test_health_reports_the_in_flight_count_outside_checks(
    monkeypatch, tmp_path
) -> None:
    webhook_server = _load_webhook_server(
        monkeypatch, tmp_path, "webhook_server_idle_count"
    )

    body, status_code = webhook_server.health()

    # A count of 0 must not turn a healthy runner into "degraded" (0 is falsy).
    assert status_code == 200
    assert body["status"] == "healthy"
    assert body["in_flight_deploys"] == 0
    assert "in_flight_deploys" not in body["checks"]
    idle_checks = dict(body["checks"])

    webhook_server._in_flight_deploys.add(
        webhook_server._deployment_key("staging", DEPLOY_SHA)
    )
    body, status_code = webhook_server.health()

    assert status_code == 200
    assert body["status"] == "healthy"
    assert body["in_flight_deploys"] == 1
    assert body["checks"] == idle_checks


def test_degraded_health_still_reports_the_in_flight_count(
    monkeypatch, tmp_path
) -> None:
    """The helper reads the 503 body, so a degraded runner must still report the count."""
    webhook_server = _load_webhook_server(
        monkeypatch, tmp_path, "webhook_server_idle_count_degraded"
    )
    monkeypatch.delenv("DOKPLOY_API_KEY")
    webhook_server._in_flight_deploys.add(
        webhook_server._deployment_key("staging", DEPLOY_SHA)
    )

    body, status_code = webhook_server.health()

    assert status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["dokploy_api_key"] is False
    assert body["in_flight_deploys"] == 1


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
def test_an_interrupted_deploy_leaves_no_key_in_flight(
    monkeypatch, tmp_path, interruption
) -> None:
    """A BaseException must not leave the key in flight until a restart.

    A leaked key keeps `in_flight_deploys` above 0, so each later bootstrap waits the full
    limit. It also answers every retry of the same coordinates with 202 "in progress".
    """
    webhook_server = _load_webhook_server(
        monkeypatch, tmp_path, f"webhook_server_interrupted_{interruption.__name__}"
    )
    fake_sync_runner = types.ModuleType("sync_runner")

    def interrupted_sync(*_args, **_kwargs):
        raise interruption(3)

    fake_sync_runner.sync_services_by_version = interrupted_sync
    monkeypatch.setitem(sys.modules, "sync_runner", fake_sync_runner)
    key = webhook_server._deployment_key("staging", DEPLOY_SHA)
    webhook_server._in_flight_deploys.add(key)

    with pytest.raises(interruption):
        webhook_server._run_deployment("staging", DEPLOY_SHA, "ci")

    assert key not in webhook_server._in_flight_deploys
    assert webhook_server.health()[0]["in_flight_deploys"] == 0
    # A poll learns that the run ended; a retry deploys again (a failure is not reused).
    monkeypatch.setattr(webhook_server, "verify_iac_request", lambda: True)
    webhook_server.request.json = {
        "env": "staging",
        "ref": DEPLOY_SHA,
        "triggered_by": "ci",
    }
    status_body, status_code = webhook_server.deployment_status()
    assert status_code == 200
    assert status_body["status"] == "failed"
    assert f"Deployment interrupted by {interruption.__name__}" in status_body["error"]
    assert webhook_server._reusable_result(key) is None


def test_a_sync_runner_import_failure_leaves_no_key_in_flight(
    monkeypatch, tmp_path
) -> None:
    webhook_server = _load_webhook_server(
        monkeypatch, tmp_path, "webhook_server_import_failure"
    )
    # A module without the function: `from sync_runner import ...` raises ImportError.
    monkeypatch.setitem(sys.modules, "sync_runner", types.ModuleType("sync_runner"))
    key = webhook_server._deployment_key("staging", DEPLOY_SHA)
    webhook_server._in_flight_deploys.add(key)

    webhook_server._run_deployment("staging", DEPLOY_SHA, "ci")

    assert key not in webhook_server._in_flight_deploys
    result = webhook_server._recent_result(key)
    assert result["status"] == "failed"
    assert "sync_services_by_version" in result["error"]


# --- 2. The helper waits on the count ----------------------------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker for the idle-wait tests. It logs each call. Like any command that reads
# stdin, it consumes the rest of a script streamed to `bash -s` unless stdin is redirected.
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  inspect)
    cat >/dev/null
    case "${FAKE_CONTAINER_STATE:-running}" in
      missing) echo "Error: No such object: iac-runner" >&2; exit 1 ;;
      running) echo true ;;
      *) echo false ;;
    esac
    ;;
  exec)
    # Only `docker exec -i iac-runner python -` forwards the heredoc program to python.
    if [ "$*" != "exec -i iac-runner python -" ]; then
      echo "fake docker: unexpected exec shape: $*" >&2
      exit 98
    fi
    exec "$FAKE_PYTHON" -
    ;;
  *)
    echo "fake docker: unexpected call: $*" >&2
    exit 97
    ;;
esac
"""


class _HealthSequence:
    """Serve one queued (status, body) per request; repeat the last one at the end."""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.requests = 0
        self.url = ""

    def next(self) -> tuple[int, object]:
        with self._lock:
            self.requests += 1
            if len(self._responses) > 1:
                return self._responses.pop(0)
            return self._responses[0]


@pytest.fixture
def health_server():
    servers: list[ThreadingHTTPServer] = []

    def start(responses: list[tuple[int, object]]) -> _HealthSequence:
        sequence = _HealthSequence(responses)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                status, body = sequence.next()
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        sequence.url = f"http://127.0.0.1:{server.server_address[1]}/health"
        return sequence

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/health"


def _run_helper(
    tmp_path: Path,
    env: dict[str, str],
    *,
    later_lines: str = f'echo "{AFTER_LINE}"\n',
    timeout: float = 60,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(FAKE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)
    docker_log = tmp_path / "docker.log"
    docker_log.write_text("", encoding="utf-8")

    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("IAC_RUNNER_", "FAKE_"))
    }
    run_env = {
        **base_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_PYTHON": sys.executable,
        "IAC_RUNNER_IDLE_POLL_SECONDS": "0.05",
        **env,
    }
    # The same shape as the workflow: the caller script arrives on stdin of `bash -s`.
    script = f'set -euo pipefail\n. "{HELPER}"\nwait_for_runner_idle\n{later_lines}'
    proc = subprocess.run(
        ["bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
        env=run_env,
        timeout=timeout,
        check=False,
    )
    return proc, docker_log.read_text(encoding="utf-8").splitlines()


def _count_lines(output: str) -> list[str]:
    return re.findall(r"in_flight_deploys=(\d+)", output)


def test_the_wait_polls_until_the_count_is_zero(tmp_path, health_server) -> None:
    sequence = health_server(
        [
            (200, {"status": "healthy", "checks": {}, "in_flight_deploys": 2}),
            (200, {"status": "healthy", "checks": {}, "in_flight_deploys": 1}),
            (200, {"status": "healthy", "checks": {}, "in_flight_deploys": 0}),
        ]
    )

    proc, docker_calls = _run_helper(tmp_path, {"IAC_RUNNER_HEALTH_URL": sequence.url})

    assert proc.returncode == 0, proc.stderr
    assert _count_lines(proc.stdout) == ["2", "1", "0"]
    assert "no in-flight deploys; continuing to the recreate" in proc.stdout
    assert "WARNING" not in proc.stdout
    assert proc.stdout.rstrip().endswith(AFTER_LINE)
    assert sequence.requests == 3
    assert docker_calls.count("exec -i iac-runner python -") == 3


def test_the_wait_reads_the_count_from_a_degraded_503_body(
    tmp_path, health_server
) -> None:
    sequence = health_server(
        [
            (503, {"status": "degraded", "checks": {}, "in_flight_deploys": 1}),
            (503, {"status": "degraded", "checks": {}, "in_flight_deploys": 0}),
        ]
    )

    proc, _ = _run_helper(tmp_path, {"IAC_RUNNER_HEALTH_URL": sequence.url})

    assert proc.returncode == 0, proc.stderr
    assert _count_lines(proc.stdout) == ["1", "0"]
    assert sequence.requests == 2
    assert AFTER_LINE in proc.stdout


@pytest.mark.parametrize("state", ["missing", "stopped"])
def test_no_running_container_continues_at_once(tmp_path, health_server, state) -> None:
    sequence = health_server([(200, {"in_flight_deploys": 5})])

    proc, docker_calls = _run_helper(
        tmp_path,
        {"IAC_RUNNER_HEALTH_URL": sequence.url, "FAKE_CONTAINER_STATE": state},
    )

    assert proc.returncode == 0, proc.stderr
    assert "container iac-runner is not running" in proc.stdout
    assert AFTER_LINE in proc.stdout
    assert sequence.requests == 0
    assert not [call for call in docker_calls if call.startswith("exec")]


def test_an_older_runner_without_the_field_continues_at_once(
    tmp_path, health_server
) -> None:
    sequence = health_server([(200, {"status": "healthy", "checks": {"http": True}})])

    proc, _ = _run_helper(tmp_path, {"IAC_RUNNER_HEALTH_URL": sequence.url})

    assert proc.returncode == 0, proc.stderr
    assert "in-flight deploy count is unknown" in proc.stdout
    assert "continuing without a wait" in proc.stdout
    assert _count_lines(proc.stdout) == []
    assert AFTER_LINE in proc.stdout
    assert sequence.requests == 1


@pytest.mark.parametrize(
    "body",
    [
        {"in_flight_deploys": "2"},
        {"in_flight_deploys": True},
        {"in_flight_deploys": -1},
        {"in_flight_deploys": 1.5},
        {"in_flight_deploys": None},
        b"<html>not json</html>",
    ],
    ids=["string", "bool", "negative", "float", "null", "not-json"],
)
def test_a_count_that_is_not_a_whole_number_is_unknown(
    tmp_path, health_server, body
) -> None:
    sequence = health_server([(200, body)])

    proc, _ = _run_helper(tmp_path, {"IAC_RUNNER_HEALTH_URL": sequence.url})

    assert proc.returncode == 0, proc.stderr
    assert "in-flight deploy count is unknown" in proc.stdout
    assert _count_lines(proc.stdout) == []
    assert AFTER_LINE in proc.stdout
    assert sequence.requests == 1


def test_a_failed_health_request_is_unknown(tmp_path) -> None:
    proc, docker_calls = _run_helper(
        tmp_path, {"IAC_RUNNER_HEALTH_URL": _closed_port_url()}
    )

    assert proc.returncode == 0, proc.stderr
    assert "in-flight deploy count is unknown" in proc.stdout
    assert AFTER_LINE in proc.stdout
    assert docker_calls.count("exec -i iac-runner python -") == 1


def test_the_wait_stops_at_the_limit_with_a_warning(tmp_path, health_server) -> None:
    """The wait must never block the self-update: at the limit it warns and continues."""
    sequence = health_server([(200, {"in_flight_deploys": 1})])

    proc, _ = _run_helper(
        tmp_path,
        {
            "IAC_RUNNER_HEALTH_URL": sequence.url,
            "IAC_RUNNER_IDLE_WAIT_SECONDS": "2",
            "IAC_RUNNER_IDLE_POLL_SECONDS": "0.2",
        },
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    warning = re.search(
        r"WARNING: runner still reports 1 in-flight deploy\(s\) after (\d+)s; "
        r"rebuilding anyway",
        proc.stdout,
    )
    assert warning, proc.stdout
    assert int(warning.group(1)) >= 2
    assert sequence.requests >= 2
    assert set(_count_lines(proc.stdout)) == {"1"}
    assert proc.stdout.rstrip().endswith(AFTER_LINE)


def test_an_invalid_limit_falls_back_to_the_default(tmp_path, health_server) -> None:
    sequence = health_server([(200, {"in_flight_deploys": 0})])

    proc, _ = _run_helper(
        tmp_path,
        {"IAC_RUNNER_HEALTH_URL": sequence.url, "IAC_RUNNER_IDLE_WAIT_SECONDS": "15m"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "IAC_RUNNER_IDLE_WAIT_SECONDS='15m' is not a whole number; using 900" in (
        proc.stdout
    )
    assert "limit=900s" in proc.stdout
    assert AFTER_LINE in proc.stdout


def test_the_helper_leaves_the_streamed_script_on_stdin(
    tmp_path, health_server
) -> None:
    """The workflow runs the bootstrap script as `bash -s` over ssh.

    The fake docker reads all of its stdin. If a docker call in the helper inherited the
    script's stdin, it would consume these later lines and they would never run.
    """
    sequence = health_server(
        [
            (200, {"in_flight_deploys": 1}),
            (200, {"in_flight_deploys": 0}),
        ]
    )

    proc, _ = _run_helper(
        tmp_path,
        {"IAC_RUNNER_HEALTH_URL": sequence.url},
        later_lines='echo "later line 1"\necho "later line 2"\n',
    )

    assert proc.returncode == 0, proc.stderr
    assert _count_lines(proc.stdout) == ["1", "0"]
    assert "later line 1" in proc.stdout
    assert "later line 2" in proc.stdout


# --- 3. The bootstrap script builds, waits, and then recreates ---------------------------


def _logical_commands(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations and drop blank and comment lines."""
    commands: list[tuple[int, str]] = []
    parts: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not parts and (not line or line.startswith("#")):
            continue
        if not parts:
            start = number
        if line.endswith("\\"):
            parts.append(line[:-1].strip())
            continue
        parts.append(line)
        commands.append((start, " ".join(parts)))
        parts = []
    return commands


def _only(commands: list[tuple[int, str]], pattern: str) -> tuple[int, re.Match[str]]:
    matches = [
        (number, match)
        for number, command in commands
        if (match := re.fullmatch(pattern, command))
    ]
    assert len(matches) == 1, f"{pattern!r} matched {len(matches)} commands"
    return matches[0]


def test_the_bootstrap_script_builds_then_waits_then_recreates() -> None:
    commands = _logical_commands(BOOTSTRAP_SCRIPT.read_text(encoding="utf-8"))

    checkout_line, _ = _only(
        commands,
        r'git -C "\$code_dir" checkout -f "\$INFRA2_DEPLOY_SHA" -- bootstrap/06\.iac_runner',
    )
    source_line, _ = _only(
        commands, r'\. "\$code_dir/bootstrap/06\.iac_runner/wait_for_idle\.sh"'
    )
    build_line, build = _only(commands, r"docker compose (.+) build </dev/null")
    wait_line, _ = _only(commands, r"wait_for_runner_idle")
    recreate_line, recreate = _only(
        commands, r"docker compose (.+) up -d --build --force-recreate"
    )

    # The checkout pins the helper to the deploy SHA before the script sources it.
    assert checkout_line < source_line < build_line
    # A deploy that starts during a slow build must also delay the recreate.
    assert build_line < wait_line < recreate_line
    # The build and the recreate target the same compose project and files.
    assert build.group(1) == recreate.group(1)
    assert '-p "$project"' in build.group(1)
    assert '--env-file "$env_file"' in build.group(1)


def test_the_sourced_helper_defines_the_wait_function() -> None:
    assert HELPER.is_file()
    assert re.search(r"^wait_for_runner_idle\(\) \{$", HELPER.read_text(), re.MULTILINE)
