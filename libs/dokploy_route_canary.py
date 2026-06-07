"""Fast-fail Dokploy dynamic route canary."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CANARY_IMAGE = "traefik/whoami:v1.10.3"
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_INTERVAL_SECONDS = 5


class DokployLike(Protocol):
    def find_compose_by_name(
        self,
        name: str,
        project_name: str | None = None,
        env_name: str | None = None,
    ) -> dict | None: ...

    def create_compose(
        self,
        environment_id: str,
        name: str,
        compose_file: str = "",
        env: str = "",
        compose_type: str = "docker-compose",
        app_name: str | None = None,
        source_type: str = "raw",
        **kwargs,
    ) -> dict: ...

    def update_compose(
        self,
        compose_id: str,
        compose_file: str | None = None,
        env: str | None = None,
        source_type: str | None = None,
        **kwargs,
    ) -> dict: ...

    def deploy_compose(self, compose_id: str) -> dict: ...

    def redeploy_compose(self, compose_id: str) -> dict: ...

    def get_compose(self, compose_id: str) -> dict: ...


HttpGetter = Callable[[str, float], tuple[int, str]]
CommandRunner = Callable[[str, float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RouteCanaryConfig:
    """Route canary inputs."""

    host: str
    environment_id: str
    project: str = "platform"
    env: str | None = None
    compose_name: str = "dokploy-route-canary"
    image: str = CANARY_IMAGE
    nonce: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    ssh_host: str = ""
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_path: str = ""
    delete_after: bool = False


@dataclass(frozen=True)
class CanaryStep:
    """One canary proof step."""

    name: str
    status: str
    detail: str
    elapsed_ms: int = 0
    data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteCanaryReport:
    """Machine-readable canary report."""

    status: str
    failure_domain: str
    compose_id: str
    public_url: str
    steps: list[CanaryStep]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def safe_slug(value: str, *, max_length: int = 48) -> str:
    """Return a Docker/Traefik-safe lowercase slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "canary")[:max_length]


def render_canary_compose(config: RouteCanaryConfig) -> str:
    """Render a minimal compose that proves same-host web and API routing."""
    router_slug = safe_slug(config.compose_name)
    return f"""services:
  web:
    image: {config.image}
    container_name: {router_slug}-web
    restart: unless-stopped
    networks:
      - dokploy-network
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=dokploy-network"
      - "traefik.http.routers.{router_slug}-web.rule=Host(`{config.host}`)"
      - "traefik.http.routers.{router_slug}-web.priority=10"
      - "traefik.http.routers.{router_slug}-web.entrypoints=websecure"
      - "traefik.http.routers.{router_slug}-web.tls.certresolver=letsencrypt"
      - "traefik.http.routers.{router_slug}-web.service={router_slug}-web"
      - "traefik.http.services.{router_slug}-web.loadbalancer.server.port=80"
      - "infra2.route-canary.nonce={config.nonce}"
  api:
    image: {config.image}
    container_name: {router_slug}-api
    restart: unless-stopped
    networks:
      - dokploy-network
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=dokploy-network"
      - "traefik.http.routers.{router_slug}-api.rule=Host(`{config.host}`) && PathPrefix(`/api`)"
      - "traefik.http.routers.{router_slug}-api.priority=100"
      - "traefik.http.routers.{router_slug}-api.entrypoints=websecure"
      - "traefik.http.routers.{router_slug}-api.tls.certresolver=letsencrypt"
      - "traefik.http.routers.{router_slug}-api.service={router_slug}-api"
      - "traefik.http.services.{router_slug}-api.loadbalancer.server.port=80"
      - "infra2.route-canary.nonce={config.nonce}"

networks:
  dokploy-network:
    external: true
"""


def run_route_canary(
    config: RouteCanaryConfig,
    client: DokployLike,
    *,
    http_get: HttpGetter | None = None,
    command_runner: CommandRunner | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RouteCanaryReport:
    """Run the canary and stop at the first classified failure domain."""
    steps: list[CanaryStep] = []
    compose_id = ""
    public_url = f"https://{config.host}"

    def fail(domain: str, step: CanaryStep) -> RouteCanaryReport:
        steps.append(step)
        return RouteCanaryReport("fail", domain, compose_id, public_url, steps)

    started = monotonic()
    compose_file = render_canary_compose(config)
    env = (
        f"CANARY_HOST={config.host}\n"
        f"CANARY_IMAGE={config.image}\n"
        f"CANARY_DEPLOY_NONCE={config.nonce}\n"
    )
    try:
        existing = client.find_compose_by_name(
            config.compose_name,
            project_name=config.project,
            env_name=config.env,
        )
        if existing:
            compose_id = str(existing["composeId"])
            client.update_compose(
                compose_id,
                compose_file=compose_file,
                env=env,
                source_type="raw",
            )
            detail = f"updated compose {compose_id}"
        else:
            created = client.create_compose(
                config.environment_id,
                config.compose_name,
                compose_file=compose_file,
                env=env,
                app_name=config.compose_name,
                source_type="raw",
            )
            compose_id = str(created.get("composeId") or "")
            detail = f"created compose {compose_id}"
        if not compose_id:
            return fail("dokploy-control-plane", CanaryStep("compose-upsert", "fail", "missing composeId"))
        steps.append(_step("compose-upsert", "pass", detail, started, monotonic))
    except Exception as exc:  # noqa: BLE001 - canary must classify API failures.
        return fail(
            "dokploy-control-plane",
            _step("compose-upsert", "fail", _safe_detail(exc), started, monotonic),
        )

    before_ids = _deployment_ids(_safe_compose(client, compose_id))
    started = monotonic()
    try:
        client.deploy_compose(compose_id)
        steps.append(_step("deploy-trigger", "pass", "deploy request accepted", started, monotonic))
    except Exception as exc:  # noqa: BLE001
        return fail(
            "dokploy-control-plane",
            _step("deploy-trigger", "fail", _safe_detail(exc), started, monotonic),
        )

    deployment = _wait_for_new_deployment(
        client,
        compose_id,
        before_ids,
        timeout_seconds=config.timeout_seconds,
        interval_seconds=config.interval_seconds,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    steps.append(deployment)
    if deployment.status != "pass":
        before_ids = _deployment_ids(_safe_compose(client, compose_id))
        started = monotonic()
        try:
            client.redeploy_compose(compose_id)
            steps.append(
                _step(
                    "redeploy-trigger",
                    "pass",
                    "redeploy request accepted after missing deployment record",
                    started,
                    monotonic,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return fail(
                "dokploy-control-plane",
                _step("redeploy-trigger", "fail", _safe_detail(exc), started, monotonic),
            )

        deployment = _wait_for_new_deployment(
            client,
            compose_id,
            before_ids,
            timeout_seconds=config.timeout_seconds,
            interval_seconds=config.interval_seconds,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        steps.append(deployment)
        if deployment.status != "pass":
            return RouteCanaryReport(
                "fail",
                "dokploy-worker-or-deployment-record",
                compose_id,
                public_url,
                steps,
            )

    if config.ssh_host:
        docker_step = _probe_docker_containers(
            config,
            command_runner=command_runner,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        steps.append(docker_step)
        if docker_step.status != "pass":
            return RouteCanaryReport("fail", "docker-runtime", compose_id, public_url, steps)

    public_step = _probe_public_routes(
        public_url,
        timeout_seconds=config.timeout_seconds,
        interval_seconds=config.interval_seconds,
        http_get=http_get,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    steps.append(public_step)
    if public_step.status != "pass":
        return RouteCanaryReport("fail", "traefik-public-route", compose_id, public_url, steps)

    return RouteCanaryReport("pass", "", compose_id, public_url, steps)


def _wait_for_new_deployment(
    client: DokployLike,
    compose_id: str,
    previous_ids: set[str],
    *,
    timeout_seconds: int,
    interval_seconds: int,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> CanaryStep:
    started = monotonic()
    deadline = started + timeout_seconds
    attempts = 0
    last_summary: dict[str, object] = {}
    while monotonic() <= deadline:
        attempts += 1
        data = _safe_compose(client, compose_id)
        deployments = data.get("deployments")
        current_ids = _deployment_ids(data)
        new_ids = sorted(current_ids - previous_ids)
        last_summary = {
            "composeStatus": data.get("composeStatus") or data.get("status") or "",
            "deployment_count": len(deployments) if isinstance(deployments, list) else 0,
            "new_deployment_ids": new_ids,
            "attempts": attempts,
        }
        if new_ids:
            latest = _latest_deployment(deployments, set(new_ids))
            latest_status = str(latest.get("status") or "unknown")
            if latest_status == "error":
                return _step(
                    "deployment-record",
                    "fail",
                    "new deployment entered error",
                    started,
                    monotonic,
                    {**last_summary, "latest_status": latest_status},
                )
            if latest_status in {"running", "done"}:
                return _step(
                    "deployment-record",
                    "pass",
                    "new deployment reached running/done",
                    started,
                    monotonic,
                    {**last_summary, "latest_status": latest_status},
                )
        sleeper(interval_seconds)
    return _step(
        "deployment-record",
        "fail",
        "deploy request did not produce a new running/done deployment record",
        started,
        monotonic,
        last_summary,
    )


def _probe_docker_containers(
    config: RouteCanaryConfig,
    *,
    command_runner: CommandRunner | None,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> CanaryStep:
    started = monotonic()
    deadline = started + config.timeout_seconds
    slug = safe_slug(config.compose_name)
    names = [f"{slug}-web", f"{slug}-api"]
    command = (
        "docker ps --filter name='"
        + slug
        + "' --format '{{.Names}} {{.Status}}' "
        "&& docker inspect "
        + " ".join(names)
        + " --format '{{.Name}} {{json .Config.Labels}}'"
    )
    attempts = 0
    last_detail = ""
    last_data: dict[str, object] = {}

    while monotonic() <= deadline:
        attempts += 1
        result = _run_ssh(config, command, command_runner=command_runner)
        output = (result.stdout + result.stderr).strip()
        output_line = _one_line(output)
        last_data = {"attempts": attempts, "output": output_line}
        if result.returncode != 0:
            last_detail = output_line or "docker inspection command failed"
        else:
            missing = [name for name in names if name not in output]
            if missing:
                last_detail = f"missing containers: {','.join(missing)}"
            elif "traefik.http.routers." not in output:
                last_detail = "containers exist but Traefik labels were not visible"
            elif f"Host(`{config.host}`)" not in output:
                last_detail = "containers exist but Traefik labels do not match the canary host"
            elif "PathPrefix(`/api`)" not in output:
                last_detail = "containers exist but API PathPrefix label was not visible"
            elif config.nonce and f"infra2.route-canary.nonce\":\"{config.nonce}" not in output:
                last_detail = "containers exist but canary nonce label was not visible"
            else:
                return _step(
                    "docker-containers",
                    "pass",
                    "containers and exact Traefik labels visible",
                    started,
                    monotonic,
                    {"attempts": attempts},
                )
        sleeper(config.interval_seconds)

    return _step(
        "docker-containers",
        "fail",
        last_detail or "containers and Traefik labels did not become visible",
        started,
        monotonic,
        last_data,
    )


def _probe_public_routes(
    public_url: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
    http_get: HttpGetter | None,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> CanaryStep:
    started = monotonic()
    deadline = started + timeout_seconds
    attempts = 0
    last: dict[str, object] = {}
    while monotonic() <= deadline:
        attempts += 1
        web = _http_get(f"{public_url}/", timeout=10, http_get=http_get)
        api = _http_get(f"{public_url}/api/health", timeout=10, http_get=http_get)
        last = {
            "attempts": attempts,
            "web_status": web[0],
            "api_status": api[0],
            "web_body": web[1][:120],
            "api_body": api[1][:120],
        }
        if 200 <= web[0] < 400 and 200 <= api[0] < 400:
            return _step("public-routes", "pass", "web and API routes returned 2xx/3xx", started, monotonic, last)
        sleeper(interval_seconds)
    return _step(
        "public-routes",
        "fail",
        "public web/API routes did not both become reachable",
        started,
        monotonic,
        last,
    )


def _http_get(
    url: str,
    *,
    timeout: float,
    http_get: HttpGetter | None,
) -> tuple[int, str]:
    if http_get:
        return http_get(url, timeout)
    request = Request(url, headers={"User-Agent": "infra2-dokploy-route-canary/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(512).decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read(512).decode("utf-8", errors="replace")
    except URLError as exc:
        return 0, str(exc.reason)


def _run_ssh(
    config: RouteCanaryConfig,
    command: str,
    *,
    command_runner: CommandRunner | None,
) -> subprocess.CompletedProcess[str]:
    if command_runner:
        return command_runner(command, 20.0)
    ssh_command = [
        "ssh",
        "-p",
        str(config.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]
    if config.ssh_key_path:
        ssh_command.extend(["-i", config.ssh_key_path])
    ssh_command.extend([f"{config.ssh_user}@{config.ssh_host}", command])
    return subprocess.run(
        ssh_command,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _safe_compose(client: DokployLike, compose_id: str) -> dict:
    try:
        data = client.get_compose(compose_id)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _deployment_ids(compose: dict) -> set[str]:
    deployments = compose.get("deployments")
    if not isinstance(deployments, list):
        return set()
    return {
        str(item.get("deploymentId"))
        for item in deployments
        if isinstance(item, dict) and item.get("deploymentId")
    }


def _latest_deployment(
    deployments: object,
    deployment_ids: set[str],
) -> dict[str, object]:
    if not isinstance(deployments, list):
        return {}
    candidates = [
        item
        for item in deployments
        if isinstance(item, dict) and str(item.get("deploymentId") or "") in deployment_ids
    ]
    if not candidates:
        return {}
    return max(candidates, key=lambda item: str(item.get("createdAt") or item.get("startedAt") or ""))


def _step(
    name: str,
    status: str,
    detail: str,
    started: float,
    monotonic: Callable[[], float],
    data: dict[str, object] | None = None,
) -> CanaryStep:
    return CanaryStep(
        name=name,
        status=status,
        detail=detail[:500],
        elapsed_ms=int((monotonic() - started) * 1000),
        data=data or {},
    )


def _safe_detail(exc: Exception) -> str:
    return _one_line(str(exc) or exc.__class__.__name__)


def _one_line(value: str) -> str:
    return " ".join(value.split())[:500]


def report_to_json(report: RouteCanaryReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def render_github_summary(report: RouteCanaryReport) -> str:
    """Render a compact GitHub step summary for the canary proof."""
    lines = [
        "## Dokploy Route Canary",
        "",
        f"- Status: `{report.status}`",
        f"- Failure domain: `{report.failure_domain or 'none'}`",
        f"- Compose ID: `{report.compose_id or 'unknown'}`",
        f"- Public URL: {report.public_url}",
        "",
        "| Phase | Status | Detail | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for step in report.steps:
        evidence = json.dumps(step.data, sort_keys=True) if step.data else "{}"
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(step.name),
                    _md_cell(step.status),
                    _md_cell(step.detail),
                    _md_cell(evidence[:500]),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _md_cell(value: object) -> str:
    text = str(value).replace("\n", " ")
    return text.replace("|", "\\|")
