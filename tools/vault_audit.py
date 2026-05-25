"""Vault self-refresh audit invoke tasks."""

from __future__ import annotations

import json
from pathlib import Path

from invoke import Exit, task

from libs.console import console, error, header, success
from libs.vault_self_refresh_audit import (
    audit_from_observations,
    collect_live_observations,
    load_inventory,
    write_report,
)


@task
def self_refresh(
    c,
    env: str = "production",
    service: str | None = None,
    observations: str | None = None,
    json_output: bool = False,
    host: str | None = None,
):
    """Audit Vault app-token self-refresh without mutating live services."""
    header("Vault Self-Refresh Audit", env)
    services = load_inventory()
    if service:
        services = [item for item in services if item.id == service]
        if not services:
            error(f"Unknown service id: {service}")
            raise Exit(code=1)

    if observations:
        observed = json.loads(Path(observations).read_text(encoding="utf-8"))
    else:
        observed = collect_live_observations(services, env=env, host=host)

    report = audit_from_observations(services, observed, env=env)
    console.print(write_report(report, as_json=json_output))
    if report["status"] == "pass":
        success("Vault self-refresh audit passed")
        return
    error("Vault self-refresh audit failed")
    raise Exit(code=1)
