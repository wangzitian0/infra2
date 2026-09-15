"""Both redis services run `--requirepass`, so every liveness probe must authenticate.

`redis-cli ping` against a password-protected server answers "NOAUTH Authentication
required." and exits 0. Docker's healthcheck and libs.common.check_service both read
exit 0 as healthy, so an unauthenticated probe can never fail on the data plane —
platform/02.redis carried exactly that probe (compose healthcheck and `status` task)
while finance_report/02.redis's compose authenticated (#713, batch D item 15).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REDIS_SERVICE_DIRS = ("platform/02.redis", "finance_report/finance_report/02.redis")


def _redis_service(service_dir: str) -> dict:
    compose = yaml.safe_load(
        (ROOT / service_dir / "compose.yaml").read_text(encoding="utf-8")
    )
    return compose["services"]["redis"]


def _status_command(service_dir: str, monkeypatch) -> tuple[str, str]:
    """Load the service's shared_tasks and capture what `status` hands check_service."""
    name = "redis_shared_tasks_" + service_dir.replace("/", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(
        name, ROOT / service_dir / "shared_tasks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module, "check_service", lambda c, service, cmd: seen.append((service, cmd))
    )
    module.status.body(None)
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("service_dir", REDIS_SERVICE_DIRS)
def test_server_requires_a_password(service_dir):
    command = " ".join(_redis_service(service_dir)["command"])
    assert '--requirepass "$$PASSWORD"' in command, (
        f"{service_dir}: redis-server no longer runs --requirepass — update this test"
    )


@pytest.mark.parametrize("service_dir", REDIS_SERVICE_DIRS)
def test_compose_healthcheck_authenticates(service_dir):
    test = _redis_service(service_dir)["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL", (
        f"{service_dir}: the probe must run in a shell to source the password"
    )
    probe = " ".join(test[1:])
    assert ". /secrets/.env" in probe, f"{service_dir}: probe does not load /secrets/.env"
    assert 'redis-cli -a "$$PASSWORD" ping' in probe, (
        f"{service_dir}: healthcheck pings without the password — NOAUTH exits 0 and "
        "reads as healthy"
    )


@pytest.mark.parametrize("service_dir", REDIS_SERVICE_DIRS)
def test_status_task_authenticates(service_dir, monkeypatch):
    service, command = _status_command(service_dir, monkeypatch)
    assert "redis" in service
    assert command.startswith(". /secrets/.env && "), (
        f"{service_dir}: status probe does not load /secrets/.env"
    )
    assert 'redis-cli -a "$PASSWORD" ping' in command, (
        f"{service_dir}: status pings without the password — NOAUTH exits 0 and "
        "check_service reports ready"
    )
    # check_service wraps the command in ssh + docker exec via shlex; a single quote
    # would be escaped correctly but reads badly in the remote log line. Keep it out.
    assert "'" not in command
