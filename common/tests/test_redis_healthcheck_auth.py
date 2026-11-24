"""Both redis services run `--requirepass`, so every liveness probe must authenticate.

`redis-cli ping` against a password-protected server answers "NOAUTH Authentication
required." and exits 0. Docker's healthcheck and common.common.check_service both read
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


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _platform_status(service_dir: str, monkeypatch, seen: list) -> None:
    """platform/02.redis keeps its own shared_tasks.py `status` task."""
    module = _load(
        "redis_shared_tasks_platform", ROOT / service_dir / "shared_tasks.py"
    )
    monkeypatch.setattr(
        module, "check_service", lambda c, service, cmd: seen.append((service, cmd))
    )
    module.status.body(None)


def _preset_status(service_dir: str, monkeypatch, seen: list) -> None:
    """An App Redis stack gets `shared.status` from RedisStackDeployer (#1825)."""
    import common.common

    # deploy.py binds common.common.check_service at import: patch it first.
    monkeypatch.setattr(
        common.common,
        "check_service",
        lambda c, service, cmd: seen.append((service, cmd)),
    )
    module = _load(
        "redis_deploy_" + service_dir.replace("/", "_").replace(".", "_"),
        ROOT / service_dir / "deploy.py",
    )
    module.shared.tasks["status"].body(None)


_STATUS_LOADERS = {
    "platform/02.redis": _platform_status,
    "finance_report/finance_report/02.redis": _preset_status,
}


def _status_command(service_dir: str, monkeypatch) -> tuple[str, str]:
    """Capture what the service `status` task hands check_service."""
    seen: list[tuple[str, str]] = []
    _STATUS_LOADERS[service_dir](service_dir, monkeypatch, seen)
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
    assert ". /secrets/.env" in probe, (
        f"{service_dir}: probe does not load /secrets/.env"
    )
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
