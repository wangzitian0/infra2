"""Contract: the prefect-worker healthcheck reads the work pool over HTTP (#961).

The old check spawned `prefect work-pool inspect default -o json` every 30s with an 8s
subprocess timeout. Measured in the container the CLI took 7.7-10.3s (it imports all of
prefect), so the probe itself flapped `platform-prefect-worker` unhealthy under load and
burned ~70% of a core per run. The same pool status is one urllib GET (~0.3s) away.

These tests run the real healthcheck command from compose.yaml against a local HTTP
server, so they fail if the check is reverted to the CLI, stops requiring
`status == READY`, or swallows errors/timeouts into success.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform/23.prefect/compose.yaml"

# urlopen's timeout inside the healthcheck is 3s; the hang case must finish well after
# that (proving the timeout fires) and well before docker's own 10s kill.
HANG_RELEASE_SECONDS = 15
HANG_MAX_ELAPSED_SECONDS = 8


def _worker() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][
        "prefect-worker"
    ]


def _healthcheck_script() -> str:
    test = _worker()["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL" and len(test) == 2
    # Compose turns `$$` into a literal `$`; a surviving `${` would need compose-time
    # interpolation that this harness does not emulate, so the check must not use it.
    script = test[1].replace("$$", "$")
    assert "${" not in script
    return script


def _worker_pool() -> str:
    args = shlex.split(_worker()["command"])
    assert args[:3] == ["prefect", "worker", "start"]
    return args[args.index("--pool") + 1]


class _Pool:
    """A fake Prefect API serving one canned response for any GET."""

    def __init__(self) -> None:
        self.status_code = 200
        self.body = b""
        self.hang = False
        self.paths: list[str] = []
        self.release = threading.Event()
        pool = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                pool.paths.append(self.path)
                if pool.hang:
                    pool.release.wait(HANG_RELEASE_SECONDS)
                    return
                self.send_response(pool.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(pool.body)))
                self.end_headers()
                self.wfile.write(pool.body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def pool():
    fake = _Pool()
    yield fake
    fake.close()


@pytest.fixture
def run_healthcheck(tmp_path):
    """Run the compose healthcheck command with `python` bound to this interpreter."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").symlink_to(Path(sys.executable))
    script = _healthcheck_script()

    def run(api_url: str | None) -> tuple[int, float]:
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            # Never route the loopback probe through an ambient proxy.
            "NO_PROXY": "*",
        }
        if api_url is not None:
            env["PREFECT_API_URL"] = api_url
        started = time.monotonic()
        done = subprocess.run(
            ["sh", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return done.returncode, time.monotonic() - started

    return run


def test_healthcheck_does_not_spawn_the_prefect_cli():
    script = _healthcheck_script()
    for spawner in ("subprocess", "os.system", "os.popen", "Popen", "exec"):
        assert spawner not in script, (
            f"the healthcheck must not spawn a process: {spawner}"
        )
    # The env var name `PREFECT_API_URL` is the only legitimate mention of prefect.
    assert "prefect" not in script.replace("PREFECT_API_URL", "").lower(), (
        "the healthcheck must not invoke the prefect CLI (7.7-10.3s per run, #961)"
    )


def test_healthcheck_reads_the_pool_through_the_api_url_env():
    assert "PREFECT_API_URL" in _worker()["environment"]
    script = _healthcheck_script()
    assert "os.environ['PREFECT_API_URL']" in script
    assert "/work_pools/" in script
    assert "'READY'" in script


def test_docker_timeout_exceeds_the_http_timeout_and_the_interval_is_unchanged():
    healthcheck = _worker()["healthcheck"]
    assert healthcheck["timeout"] == "10s"  # > urlopen timeout=3 in the script
    assert "timeout=3" in _healthcheck_script()
    assert healthcheck["interval"] == "30s"
    assert healthcheck["retries"] == 3
    assert healthcheck["start_period"] == "60s"


def test_ready_pool_is_healthy_and_the_probe_is_fast(pool, run_healthcheck):
    pool.body = b'{"name": "default", "status": "READY"}'
    code, elapsed = run_healthcheck(pool.api_url)
    assert code == 0
    # Real-system acceptance is <1s; here only prove it is not a timeout (3s) or the
    # old CLI (7.7-10.3s), with headroom for a loaded CI runner.
    assert elapsed < 2.5
    # Same pool the worker actually polls (`--pool` in command), same API root.
    assert pool.paths == [f"/api/work_pools/{_worker_pool()}"]


def test_trailing_slash_on_the_api_url_is_tolerated(pool, run_healthcheck):
    pool.body = b'{"status": "READY"}'
    code, _ = run_healthcheck(pool.api_url + "/")
    assert code == 0
    assert pool.paths == [f"/api/work_pools/{_worker_pool()}"]


@pytest.mark.parametrize(
    "status_code, body",
    [
        (200, b'{"status": "NOT_READY"}'),
        (200, b'{"status": "PAUSED"}'),
        (200, b'{"status": "ready"}'),
        (200, b'{"status": null}'),
        (200, b"{}"),
        (200, b"not json"),
        (200, b""),
        (200, b'["READY"]'),
        (404, b'{"status": "READY"}'),
        (500, b'{"status": "READY"}'),
    ],
    ids=[
        "not-ready",
        "paused",
        "lowercase-ready",
        "null-status",
        "no-status",
        "not-json",
        "empty-body",
        "json-list",
        "http-404-even-with-ready-body",
        "http-500-even-with-ready-body",
    ],
)
def test_anything_other_than_http_200_ready_is_unhealthy(
    pool, run_healthcheck, status_code, body
):
    pool.status_code = status_code
    pool.body = body
    code, _ = run_healthcheck(pool.api_url)
    assert code != 0
    assert pool.paths, "the probe must actually have reached the fake API"


def test_unreachable_api_is_unhealthy(pool, run_healthcheck):
    dead_url = pool.api_url
    pool.close()  # port now refuses connections
    code, _ = run_healthcheck(dead_url)
    assert code != 0


def test_missing_api_url_env_is_unhealthy(run_healthcheck):
    code, _ = run_healthcheck(None)
    assert code != 0


def test_a_hanging_api_times_out_unhealthy_before_the_docker_kill(
    pool, run_healthcheck
):
    pool.hang = True
    code, elapsed = run_healthcheck(pool.api_url)
    assert code != 0
    assert pool.paths, "the probe must actually have reached the fake API"
    assert 2.5 < elapsed < HANG_MAX_ELAPSED_SECONDS, (
        f"urlopen timeout=3 should fire at ~3s, took {elapsed:.1f}s"
    )
