"""Detect crash-looping / unhealthy containers and explain *why* from their logs.

SSOT for ``libs.observability``'s container breakdown triage; ``libs.container_breakdown``
is a backward-compatibility shim over this module.

The layered monitoring already in place catches the *symptom* but not the *cause*:

  * cloudflare/infra-watchdog (black-box HTTP) — "public route down"
  * tools/infra_probe_runner.py — "service probe failed"
  * libs/deploy_queue_guard.py — "deploy stuck in the queue"

None of them says *"container X is restart-looping because <reason>"*. So when an
internal sidecar (e.g. a vault-agent) crash-loops on missing creds, the only
signal is the eventual downstream public-route failure — minutes-to-hours later,
and without the cause. This module fills that gap: given the Docker Engine
container list + a log fetcher, it flags the broken containers and extracts the
breakdown reason from their logs, so the alert reads "down **because** Vault creds
missing" instead of "down, unknown".

Pure/dependency-free on purpose — the I/O (Docker socket, alert bridge) lives in
libs/container_breakdown_watch.py (a watcher plugin in the single
resident sidecar since #543) so this stays unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from libs.deploy_env_config import PREVIEW_KINDS

from libs.service_identity import DOCKER_LABEL_PREFIX, ServiceIdentity
from libs.service_registry import resolve_container_host

# (substring, human-readable cause) — ordered, first match wins. These are the
# concrete breakdown signals seen in the finance_report outage + adjacent ones.
HOST_MEMORY_CAUSE = "内存耗尽(宿主机 CGroup 触发 OOM kill)"
BREAKDOWN_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "VAULT_ROLE_ID and VAULT_SECRET_ID are required",
        "Vault AppRole 凭据缺失(VAULT_ROLE_ID / VAULT_SECRET_ID)",
    ),
    ("VAULT_APP_TOKEN is required", "Vault 应用 token 缺失(VAULT_APP_TOKEN)"),
    ("VAULT_ROLE_ID", "Vault AppRole 凭据缺失"),
    ("permission denied", "权限被拒(Vault / secret 访问)"),
    ("no such host", "DNS / 服务名解析失败"),
    ("connection refused", "依赖不可达(connection refused)"),
    ("out of memory", HOST_MEMORY_CAUSE),
    ("oom-killer", HOST_MEMORY_CAUSE),
    ("killed process", "进程被系统终止(SIGKILL)"),
)
# The cause is shown on the pager card (#905), so it is written in Chinese; the log
# markers it is matched on, and every identifier in it, stay verbatim.

_MAX_DETAIL = 200
_LOG_TAIL_LINES = 5
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# A preview slot's containers carry `-<kind>-<value>` (libs.deploy_env_config:
# -pr-5, -branch-main, -commit-1ab32d5, -tag-v1-2-3).
_PREVIEW_SLOT_NAME = re.compile(
    r"-(?:" + "|".join(re.escape(kind) for kind in PREVIEW_KINDS) + r")-"
)


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
    #: the last lines of its log, for the card's 日志 (#905)
    log_tail: str = ""
    #: epoch seconds this container was first seen broken in this incident; 0 unknown
    since: float = 0.0


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
        pat_lower = pattern.lower()
        for line in logs.splitlines():
            if pat_lower in line.lower():
                return cause, line.strip()[:_MAX_DETAIL]
    for line in reversed(logs.splitlines()):
        if line.strip():
            return "崩溃循环 / 不健康(见日志尾)", line.strip()[:_MAX_DETAIL]
    return "崩溃循环 / 不健康(未取到日志)", ""


def log_tail(logs: str, detail: str = "") -> str:
    """The last few non-empty log lines, each cut to 200 characters (#905).

    The line the reason was read from comes first when it is not among them, so the
    card shows both the evidence and what the container said last.
    """
    lines = [
        _CONTROL_CHARS.sub("", line).strip()[:_MAX_DETAIL]
        for line in logs.splitlines()
        if _CONTROL_CHARS.sub("", line).strip()
    ][-_LOG_TAIL_LINES:]
    evidence = _CONTROL_CHARS.sub("", detail).strip()
    if evidence and evidence not in lines:
        lines = [evidence, "…", *lines]
    return "\n".join(lines)


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
        elif (
            "preview" in coordinate
            or "pr-" in coordinate
            or _PREVIEW_SLOT_NAME.search(name.lower())
        ):
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
        logs = logs_fn(str(entry.get("Id", "")))
        reason, detail = classify_reason(logs)
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
                log_tail=log_tail(logs, detail),
            )
        )
    return found


def build_breakdown_alert_payload(
    breakdowns,
    *,
    firing: bool = True,
    external_url: str = "infra2://platform/12.alerting/container-breakdown",
    severity: str = "critical",
    alertname: str = "ContainerBreakdown",
    now: float | None = None,
) -> dict:
    """Alertmanager/SigNoz-shaped payload for the alert bridge (``format_signoz_alert``).

    ``severity`` / ``alertname`` default to the paging ContainerBreakdown; the chronic
    digest (#658) posts as ``ContainerBreakdownChronic`` at ``warning`` so the routing
    that pages on critical does not fire for a once-a-day summary.

    #905: each alert names its container, carries the symptom and the log tail the
    card shows, ``startsAt`` when the breakdown's ``since`` is known and, resolved,
    ``endsAt`` (``now``).
    """
    import time

    status = "firing" if firing else "resolved"
    now = time.time() if now is None else now

    def rfc3339(epoch: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))

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
        combined_text = (b.reason + " " + b.detail).lower()
        domain = (
            "host-memory"
            if b.reason == HOST_MEMORY_CAUSE
            or any(
                k in combined_text
                for k in ("out of memory", "oom-killer", "cgroup oom")
            )
            else "runtime"
        )
        alerts.append(
            {
                "status": status,
                "labels": {
                    "alertname": alertname,
                    **identity.alert_labels(severity=severity, failure_domain=domain),
                    "state": b.state,
                },
                "annotations": {
                    "summary": f"{b.container} {b.state} — {b.reason}",
                    "description": b.reason,
                    "observed": f"state={b.state} log={b.detail}",
                    "container": b.container,
                    "symptom": f"{b.state}: {b.reason}",
                    "log_tail": b.log_tail or b.detail,
                },
                **({"startsAt": rfc3339(b.since)} if b.since else {}),
                **({} if firing else {"endsAt": rfc3339(now)}),
            }
        )
    return {
        "status": status,
        "commonLabels": {
            "alertname": alertname,
            "identity_schema": "v1",
            "managed_by": "infra2",
            "severity": severity,
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


@dataclass(frozen=True)
class BreakdownVerdict:
    """O-01: Container log breakdown diagnostic verdict."""

    cause: str
    detail: str
    raw_logs: str = ""


def analyze_container_logs(container: Any, log_chunk: str) -> BreakdownVerdict:
    """O-01: Analyze container logs to determine failure root cause."""
    cause, detail = classify_reason(log_chunk)
    return BreakdownVerdict(cause=cause, detail=detail, raw_logs=log_chunk)


# Compatibility alias
find_breakdown_reason = classify_reason


__all__ = [
    "BREAKDOWN_PATTERNS",
    "Breakdown",
    "BreakdownVerdict",
    "analyze_container_logs",
    "broken_state",
    "build_breakdown_alert_payload",
    "classify_reason",
    "container_identity",
    "container_name",
    "find_breakdown_containers",
    "find_breakdown_reason",
    "log_tail",
]
