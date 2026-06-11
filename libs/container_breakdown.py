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


def container_name(entry: dict) -> str:
    """Friendly name from a Docker Engine ``/containers/json`` entry."""
    names = entry.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return str(entry.get("Id", ""))[:12]


def broken_state(entry: dict) -> str | None:
    """Return ``"restarting"``/``"unhealthy"`` if the container is broken, else None.

    ``entry`` is a Docker Engine ``/containers/json`` element. ``State`` is the
    lifecycle string ("running"/"restarting"/...); ``Status`` carries health
    ("... (unhealthy)").
    """
    state = str(entry.get("State", "")).lower()
    status = str(entry.get("Status", "")).lower()
    if state == "restarting":
        return "restarting"
    if "unhealthy" in status:
        return "unhealthy"
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
        found.append(
            Breakdown(
                container=container_name(entry),
                state=state,
                reason=reason,
                detail=detail,
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
    alerts = [
        {
            "status": status,
            "labels": {
                "alertname": "ContainerBreakdown",
                "service": b.container,
                "severity": "critical",
                "failure_domain": "runtime",
                "state": b.state,
            },
            "annotations": {
                "summary": f"{b.container} {b.state} — {b.reason}",
                "description": b.reason,
                "observed": f"state={b.state} log={b.detail}",
            },
        }
        for b in breakdowns
    ]
    return {
        "status": status,
        "commonLabels": {
            "alertname": "ContainerBreakdown",
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
