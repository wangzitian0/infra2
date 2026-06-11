"""Detect deploy jobs stuck 'running' too long, via Dokploy's deployment status.

Pure logic (no network, no clock) so it is unit-testable; the Dokploy API calls
and the loop live in `tools/deploy_queue_guard.py`.

Why this works at the Dokploy-API layer instead of poking `bull:deployments:*`
in Redis: the deploy queue is Dokploy's own BullMQ. We OBSERVE via the
deployment status API and (in the sidecar) REMEDIATE via Dokploy's own
`compose.killBuild` / `compose.cancelDeployment` / `compose.cleanQueues`. Raw
`LREM`/`DEL` on the BullMQ keys corrupts its bookkeeping; going through Dokploy
keeps the queue and the deployment DB record consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

RUNNING_STATUS = "running"


@dataclass(frozen=True)
class StuckDeploy:
    """A compose whose latest deployment has been `running` past the ceiling."""

    compose_id: str
    compose_name: str
    deployment_id: str
    age_seconds: float


def parse_epoch_seconds(value) -> float | None:
    """Best-effort parse of a Dokploy timestamp into epoch seconds.

    Accepts ISO-8601 strings (`2026-06-11T09:20:06.000Z` or naive), epoch
    seconds, or epoch milliseconds. Returns None when unparseable so callers can
    skip a deployment rather than mis-age it.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a timestamp
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v  # >~year 5138 in s -> it's millis
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return parse_epoch_seconds(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def deployment_start_epoch(deployment: dict) -> float | None:
    """Earliest known start time of a deployment, across Dokploy field variants."""
    for key in ("startedAt", "createdAt", "updatedAt"):
        epoch = parse_epoch_seconds(deployment.get(key))
        if epoch is not None:
            return epoch
    return None


def is_running(deployment: dict) -> bool:
    return str(deployment.get("status", "")).strip().lower() == RUNNING_STATUS


def find_stuck_deploys(composes, now_epoch: float, ceiling_seconds: float):
    """Return one StuckDeploy per compose whose running deploy exceeds the ceiling.

    `composes`: iterable of (compose_id, compose_name, [deployment dicts]). When a
    compose has several running records (stale ones can linger), the OLDEST that
    exceeds the ceiling is reported — that is the one blocking the FIFO queue.
    """
    stuck: list[StuckDeploy] = []
    for compose_id, compose_name, deployments in composes:
        oldest: StuckDeploy | None = None
        for deployment in deployments:
            if not is_running(deployment):
                continue
            start = deployment_start_epoch(deployment)
            if start is None:
                continue
            age = now_epoch - start
            if age <= ceiling_seconds:
                continue
            if oldest is None or age > oldest.age_seconds:
                oldest = StuckDeploy(
                    compose_id=compose_id,
                    compose_name=compose_name,
                    deployment_id=str(deployment.get("deploymentId", "")),
                    age_seconds=age,
                )
        if oldest is not None:
            stuck.append(oldest)
    return stuck


def build_deploy_guard_alert_payload(
    stuck,
    *,
    firing: bool = True,
    action_note: str = "",
    external_url: str = "infra2://platform/12.alerting/deploy-queue-guard",
) -> dict:
    """Alertmanager/SigNoz-shaped payload for the alert bridge (`format_signoz_alert`)."""
    status = "firing" if firing else "resolved"
    alerts = [
        {
            "status": status,
            "labels": {
                "alertname": "DeployQueueStuck",
                "service": s.compose_name,
                "severity": "critical",
                "failure_domain": "deploy-queue",
            },
            "annotations": {
                "summary": f"{s.compose_name} deploy stuck running {int(s.age_seconds)}s",
                "description": action_note
                or (
                    f"deployment {s.deployment_id} has been running > ceiling; "
                    "queue is single-concurrency FIFO so this blocks all deploys"
                ),
                "observed": f"compose={s.compose_id} age={int(s.age_seconds)}s",
            },
        }
        for s in stuck
    ]
    summary = (
        f"{len(stuck)} deploy(s) stuck running past the ceiling"
        if stuck
        else "No deploys stuck"
    )
    return {
        "status": status,
        "commonLabels": {
            "alertname": "DeployQueueStuck",
            "severity": "critical",
            "team": "infra",
        },
        "commonAnnotations": {"summary": summary},
        "groupLabels": {"alertname": "DeployQueueStuck"},
        "alerts": alerts,
        "externalURL": external_url,
    }
