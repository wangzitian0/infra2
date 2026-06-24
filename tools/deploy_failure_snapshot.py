"""Platform-health snapshot for a failed fixed-compose deploy (#768).

When a staging/prod ``deploy_v2`` rollout fails, emit the platform-layer state —
Dokploy compose status, the latest deployment status/error, and a
platform-vs-app *failure-domain* classification — into the GitHub step summary,
so triage can tell a platform failure (Dokploy/host/rollout) apart from an
application failure WITHOUT SSHing the host.

This runs on the infra side, which owns the platform and already holds the
resolved ``compose_id`` (from ``env_config``) and ``DOKPLOY_API_KEY`` — the app
must not reach across the App/Infra boundary (#876) to diagnose the platform.

Best-effort by contract: every entry point swallows its own errors and returns a
marker. A diagnostic must NEVER mask or replace the original deploy failure.

Optional host-probe fields (host load/memory, vault-agent error-loop, container
restart count) are additive and currently have no producer; they are simply
omitted until a host probe feeds them in.
"""

from __future__ import annotations

import os

# Compose/deployment states Dokploy reports once a rollout has actually started.
_ACTIVE_STATES = {"running", "done", "success", "successful"}

_SNAPSHOT_FIELDS = (
    "compose_id",
    "compose_status",
    "deployment_count",
    "latest_deployment_status",
    "latest_deployment_error",
    "platform_failure_domain",
    "error",
)


def _deployment_timestamp(deployment: dict) -> str:
    """A sortable timestamp for a Dokploy deployment record.

    Dokploy records vary in which timestamp field they carry, so fall back across
    the known variants rather than keying on ``startedAt`` alone (which can be
    absent and would then pick the wrong "latest" record).
    """
    for field in ("startedAt", "createdAt", "updatedAt", "finishedAt"):
        value = deployment.get(field)
        if value:
            return str(value)
    return ""


def _latest_deployment(deployments: list[dict]) -> dict:
    if not deployments:
        return {}
    return sorted(deployments, key=_deployment_timestamp, reverse=True)[0]


def classify(compose_status: str, deployments: list[dict]) -> str:
    """Classify the platform failure domain from compose + deployment state."""
    # Normalize case: Dokploy may return `Running`/`DONE`/`ERROR` — compare lower.
    compose_status = str(compose_status or "").lower()
    latest_status = str(_latest_deployment(deployments).get("status") or "").lower()
    if compose_status == "error" or latest_status == "error":
        return "dokploy-deployment-error"
    if not deployments or compose_status in {"idle", ""}:
        return "no-deployment-record"
    if compose_status in _ACTIVE_STATES and latest_status in _ACTIVE_STATES:
        # Dokploy says the rollout is fine — the failure is above the platform
        # (app health / E2E), not a platform-layer fault.
        return "platform-ok-app-failure"
    return "platform-indeterminate"


def build_snapshot(client, compose_id: str) -> dict:
    """Read the compose via the Dokploy client and build the snapshot dict.

    Never raises: an unreachable/unknown API yields a marked snapshot instead.
    """
    try:
        data = client.get_compose(compose_id) or {}
    except Exception as exc:  # noqa: BLE001 - diagnostics never raise
        return {
            "compose_id": compose_id,
            "error": f"could not read Dokploy compose: {type(exc).__name__}",
            "platform_failure_domain": "dokploy-api-unreachable",
        }

    deployments = [d for d in (data.get("deployments") or []) if isinstance(d, dict)]
    compose_status = str(data.get("composeStatus") or "unknown")
    latest = _latest_deployment(deployments)
    return {
        "compose_id": compose_id,
        "compose_status": compose_status,
        "deployment_count": len(deployments),
        "latest_deployment_status": str(latest.get("status") or "none"),
        "latest_deployment_error": str(latest.get("errorMessage") or "")[:500],
        "platform_failure_domain": classify(compose_status, deployments),
    }


def _md_cell(value: object) -> str:
    """Escape a value for a single Markdown table cell.

    Dokploy error messages can contain ``|`` or newlines, which would otherwise
    break the table layout in the GitHub step summary.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_markdown(snapshot: dict) -> str:
    """Render the snapshot as a GitHub step-summary Markdown table."""
    lines = [
        "## Platform-health snapshot (deploy failure)",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in _SNAPSHOT_FIELDS:
        if key in snapshot:
            lines.append(f"| `{key}` | {_md_cell(snapshot[key])} |")
    return "\n".join(lines) + "\n"


def emit_failure_snapshot(
    client, compose_id: str, *, summary_path: str | None = None
) -> dict:
    """Build + write the platform-health snapshot to the GitHub step summary.

    Best-effort: returns the snapshot dict and NEVER raises, so a diagnostic
    failure can't mask the deploy error that triggered it.
    """
    try:
        snapshot = build_snapshot(client, compose_id)
        markdown = render_markdown(snapshot)
        target = summary_path or os.getenv("GITHUB_STEP_SUMMARY")
        if target:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(markdown)
        else:
            print(markdown)
        return snapshot
    except Exception as exc:  # noqa: BLE001 - must never mask the original deploy error
        return {
            "compose_id": compose_id,
            "platform_failure_domain": "snapshot-error",
            "error": type(exc).__name__,
        }
