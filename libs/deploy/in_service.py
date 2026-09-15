"""Is the stack in service after a deploy Dokploy reports done? (#629, #691, #698)

Dokploy's record reaching ``done`` proves ``docker compose up`` returned, not that the
services are up: ``truealpha/postgres`` reported a green deploy that created no container
(#691), and ``finance_report`` v0.1.50 left ``finance_report-frontend`` in ``Created``
for thirteen minutes because its dependency never became healthy (#698). This module is
the pure half of that check — what to expect from a compose file and what a ``docker ps``
listing says about it — so the verdict can be tested without a host.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import yaml

DOCKER_PS_FORMAT = '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}'

_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")


@dataclass(frozen=True)
class ContainerObservation:
    service: str
    state: str  # docker's State: running, created, exited, restarting, paused, dead
    status: str  # docker's Status line: "Up 4 days (healthy)", "Created", ...

    @property
    def health(self) -> str | None:
        match = _HEALTH.search(self.status)
        return match.group(1) if match else None


@dataclass(frozen=True)
class InServiceVerdict:
    ok: bool
    settling: bool  # not ok yet, but every shortfall is a health check still starting
    message: str


def expected_running_services(compose_content: str) -> tuple[str, ...]:
    """The services a compose file expects to stay up: the ones with a healthcheck.

    Every long-running service in this repo declares one; the one-shots (authentik
    ``token-init``, clickhouse ``init-clickhouse``, signoz ``schema-migrator``) do not
    and exit by design, so the compose file itself says which containers must be found
    running.
    """
    doc = yaml.safe_load(compose_content) or {}
    services = doc.get("services") or {}
    return tuple(
        sorted(
            name
            for name, spec in services.items()
            if isinstance(spec, dict) and spec.get("healthcheck")
        )
    )


def parse_docker_ps(output: str) -> dict[str, ContainerObservation]:
    """One observation per compose service from ``docker ps -a --format DOCKER_PS_FORMAT``.

    ``docker ps`` lists newest first; the first line per service is the container the
    deploy just created, and a leftover from an earlier generation does not shadow it.
    """
    observed: dict[str, ContainerObservation] = {}
    for line in output.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        service, state, status = (part.strip() for part in parts)
        observed.setdefault(service, ContainerObservation(service, state, status))
    return observed


def in_service_verdict(
    expected: tuple[str, ...], observed: dict[str, ContainerObservation]
) -> InServiceVerdict:
    """Every expected service has a running container whose health is not failing.

    ``settling`` is true only when the sole shortfall is ``health: starting`` — the
    caller may wait for that; a missing, ``Created``, exited, restarting or unhealthy
    container is a verdict, not a delay.
    """
    missing: list[str] = []
    down: list[str] = []
    starting: list[str] = []
    for name in expected:
        found = observed.get(name)
        if found is None:
            missing.append(name)
        elif found.state != "running":
            down.append(f"{name} is {found.state} ({found.status})")
        elif found.health == "unhealthy":
            down.append(f"{name} is unhealthy ({found.status})")
        elif found.health == "health: starting":
            starting.append(name)
    if not missing and not down and not starting:
        return InServiceVerdict(
            True, False, f"{len(expected)} service(s) running and healthy"
        )
    parts: list[str] = []
    if missing:
        parts.append(f"no container for {', '.join(missing)}")
    if down:
        parts.append("; ".join(down))
    if starting:
        parts.append(f"health still starting for {', '.join(starting)}")
    return InServiceVerdict(False, not missing and not down, "; ".join(parts))


def expected_running_containers(
    compose_content: str, env_suffix: str
) -> dict[str, str]:
    """service -> container name for every service expected running, with the compose's
    ``${ENV_SUFFIX}`` resolved for this environment.

    The promote tier watches the stack through Dokploy's ``docker.getContainers``, which
    reports container names rather than compose labels; every service in this repo
    declares a fixed ``container_name``.
    """
    doc = yaml.safe_load(compose_content) or {}
    services = doc.get("services") or {}
    names: dict[str, str] = {}
    for service in expected_running_services(compose_content):
        raw = str(services[service].get("container_name") or "")
        if not raw:
            continue
        names[service] = re.sub(r"\$\{ENV_SUFFIX(?::-[^}]*)?\}", env_suffix, raw)
    return names


def observe_containers(
    expected: dict[str, str], containers: Iterable[dict]
) -> dict[str, ContainerObservation]:
    """Observations keyed by service from a ``docker.getContainers`` listing
    (``{"name", "state", "status"}`` per container). A container Dokploy does not list
    (stopped, ``Created``) is simply absent, which the verdict reports as missing."""
    by_name = {}
    for container in containers:
        name = str(container.get("name") or "")
        if name:
            by_name.setdefault(name, container)
    observed: dict[str, ContainerObservation] = {}
    for service, name in expected.items():
        found = by_name.get(name)
        if found is not None:
            observed[service] = ContainerObservation(
                service, str(found.get("state") or ""), str(found.get("status") or "")
            )
    return observed
