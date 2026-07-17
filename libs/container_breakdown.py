"""Detect crash-looping / unhealthy containers and explain *why* from their logs.

The layered monitoring already in place catches the *symptom* but not the *cause*:

  * cloudflare/infra-watchdog (black-box HTTP) — "public route down"
  * tools/infra_probe_runner.py — "service probe failed"
  * tools/deploy_queue_guard.py — "deploy stuck in the queue"

None of them says *"container X is restart-looping because <reason>"*. So when an
internal sidecar (e.g. a vault-agent) crash-loops on missing creds, the only
signal is the eventual downstream public-route failure — minutes-to-hours later,
and without the cause. This module fills that gap: given the Docker Engine
container list + a log fetcher, it flags the broken containers and extracts the
breakdown reason from their logs, so the alert reads "down **because** Vault creds
missing" instead of "down, unknown".

Pure/dependency-free on purpose — the I/O (Docker socket, alert bridge) lives in
tools/container_breakdown_watch.py so this stays unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from libs.service_identity import DOCKER_LABEL_PREFIX, ServiceIdentity
from libs.service_registry import resolve_container_host

# (substring, human-readable cause) — ordered, first match wins. These are the
# concrete breakdown signals seen in the finance_report outage + adjacent ones.
BREAKDOWN_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "VAULT_ROLE_ID and VAULT_SECRET_ID are required",
        "Vault AppRole creds missing (VAULT_ROLE_ID / VAULT_SECRET_ID)",
    ),
    ("VAULT_APP_TOKEN is required", "Vault app token missing (VAULT_APP_TOKEN)"),
    ("VAULT_ROLE_ID", "Vault AppRole creds missing"),
    ("permission denied", "permission denied (Vault / secret access)"),
    ("no such host", "DNS / service resolution failure"),
    ("connection refused", "dependency unreachable (connection refused)"),
)

_MAX_DETAIL = 200


@dataclass(frozen=True)
class Breakdown:
    """A container that is crash-looping / unhealthy, with the extracted cause."""

    container: str
    state: str  # "restarting" | "unhealthy"
    reason: str  # human-readable cause
    detail: str  # the matched (or last) log line
    service_id: str = ""
    component: str = "container"
    environment: str = "production"


def container_name(entry: dict) -> str:
    """Friendly name from a Docker Engine ``/containers/json`` entry."""
    names = entry.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return str(entry.get("Id", ""))[:12]


def broken_state(entry: dict) -> str | None:
    """Return the broken-state label (``"restarting"``/``"unhealthy"``/``"exited"``/
    ``"dead"``) if the container is broken, else None.

    ``entry`` is a Docker Engine ``/containers/json`` element. ``State`` is the
    lifecycle string ("running"/"restarting"/"exited"/"dead"/...); ``Status`` carries
    health ("... (unhealthy)") and the exit code ("Exited (137) ...").
    """
    state = str(entry.get("State", "")).lower()
    status = str(entry.get("Status", "")).lower()
    if state == "restarting":
        return "restarting"
    if "unhealthy" in status:
        return "unhealthy"
    # A container that crashed and STOPPED (restart exhausted / restart:no) is a
    # steadier, more dangerous failure than active crash-looping — nothing is
    # retrying it, and the brief "restarting" window above is easy to miss at a 60s
    # sample. Flag non-zero exits and dead; a clean "Exited (0)" intentional stop is
    # ignored.
    if state == "dead":
        return "dead"
    if state == "exited" and "exited (0)" not in status:
        return "exited"
    return None


def classify_reason(logs: str) -> tuple[str, str]:
    """Extract ``(reason, detail)`` from recent container logs.

    Known breakdown patterns win; otherwise fall back to the last non-empty line
    so the alert always carries *something* actionable.
    """
    for pattern, cause in BREAKDOWN_PATTERNS:
        for line in logs.splitlines():
            if pattern in line:
                return cause, line.strip()[:_MAX_DETAIL]
    for line in reversed(logs.splitlines()):
        if line.strip():
            return "crash-loop / unhealthy (see log tail)", line.strip()[:_MAX_DETAIL]
    return "crash-loop / unhealthy (no logs captured)", ""


def container_identity(entry: dict) -> tuple[str, str, str]:
    """Resolve service_id/component/environment from Docker-owned metadata.

    Canonical reverse-DNS labels win. Existing containers remain observable via
    Compose's automatic service label plus the registry's container-host index.
    Unknown containers are deliberately kept unregistered, never guessed.
    """
    labels = entry.get("Labels") or {}
    service_id = str(labels.get(f"{DOCKER_LABEL_PREFIX}.service-id", ""))
    component = str(
        labels.get(f"{DOCKER_LABEL_PREFIX}.component")
        or labels.get("com.docker.compose.service")
        or "container"
    )
    environment = str(labels.get(f"{DOCKER_LABEL_PREFIX}.environment", ""))
    name = container_name(entry)

    if not service_id:
        meta = resolve_container_host(name)
        service_id = meta.service_id if meta else ""
    if not environment:
        project = str(labels.get("com.docker.compose.project", ""))
        coordinate = f"{project}/{name}".lower()
        if "staging" in coordinate:
            environment = "staging"
        elif "preview" in coordinate or "pr-" in coordinate:
            environment = "preview"
        else:
            environment = "production"
    return service_id, component, environment


def find_breakdown_containers(containers, logs_fn) -> list[Breakdown]:
    """Flag broken containers and attach the cause.

    ``containers``: iterable of Docker Engine ``/containers/json`` entries.
    ``logs_fn``: ``callable(container_id: str) -> str`` returning recent logs.
    """
    found: list[Breakdown] = []
    for entry in containers:
        state = broken_state(entry)
        if not state:
            continue
        reason, detail = classify_reason(logs_fn(str(entry.get("Id", ""))))
        service_id, component, environment = container_identity(entry)
        found.append(
            Breakdown(
                container=container_name(entry),
                state=state,
                reason=reason,
                detail=detail,
                service_id=service_id,
                component=component,
                environment=environment,
            )
        )
    return found


def build_breakdown_alert_payload(
    breakdowns,
    *,
    firing: bool = True,
    external_url: str = "infra2://platform/12.alerting/container-breakdown",
) -> dict:
    """Alertmanager/SigNoz-shaped payload for the alert bridge (``format_signoz_alert``)."""
    status = "firing" if firing else "resolved"
    alerts = []
    for b in breakdowns:
        identity = ServiceIdentity.build(
            b.service_id or "infra/unregistered",
            b.environment,
            component=b.component,
            service_name=(
                b.service_id.split("/", 1)[-1] if b.service_id else "unregistered"
            ),
        )
        alerts.append(
            {
                "status": status,
                "labels": {
                    "alertname": "ContainerBreakdown",
                    **identity.alert_labels(
                        severity="critical", failure_domain="runtime"
                    ),
                    "state": b.state,
                },
                "annotations": {
                    "summary": f"{b.container} {b.state} — {b.reason}",
                    "description": b.reason,
                    "observed": f"state={b.state} log={b.detail}",
                },
            }
        )
    return {
        "status": status,
        "commonLabels": {
            "alertname": "ContainerBreakdown",
            "identity_schema": "v1",
            "managed_by": "infra2",
            "severity": "critical",
            "team": "infra",
        },
        "commonAnnotations": {
            "summary": (
                f"{len(breakdowns)} container(s) crash-looping / unhealthy"
                if breakdowns
                else "No containers crash-looping"
            ),
        },
        "groupLabels": {"alertname": "ContainerBreakdown"},
        "alerts": alerts,
        "externalURL": external_url,
    }
