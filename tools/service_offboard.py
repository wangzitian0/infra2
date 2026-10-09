"""Symmetrical Service Offboarding Tool (Teardown & Cleanup).

Safely deprovisions service infrastructure (Dokploy, Vault AppRole, PostgreSQL).
By default, databases are preserved and database users are locked (NOLOGIN).
Passing --purge-data performs destructive cleanup of databases and secrets.

Usage:
    python -m tools.service_offboard <service> [--project apps] [--env production] [--purge-data]
    invoke service.offboard <service> [--project apps] [--env production] [--purge-data]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Any

from invoke import Context, task

from libs.core.environ import get_env, with_env_suffix
from libs.console import error, header, info, success, warning
from libs.deploy.dokploy_client import get_dokploy
from libs.security.vault_tokens import policy_name


def _clean_ident(value: str) -> str:
    """Normalize identifier for Postgres/Vault (lowercase, replace '-' with '_')."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", value.strip().lower())


def check_op_signed_in(runner: Any = subprocess.run) -> bool:
    """Validate 1Password CLI is installed and signed in."""
    try:
        res = runner(["op", "whoami"], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def offboard_service(
    c: Context | None = None,
    service: str = "",
    project: str = "apps",
    env: str | None = None,
    purge_data: bool = False,
    dokploy_project: str | None = None,
    dry_run: bool = False,
    op_runner: Any = subprocess.run,
    vault_token: str | None = None,
    dokploy_client: Any = None,
) -> dict[str, Any]:
    """Execute service offboarding."""
    if not service:
        raise ValueError("Service name is required")

    if c is None:
        c = Context()
    clean_service = _clean_ident(service)
    clean_project = _clean_ident(project)
    env_name = (
        env
        or os.environ.get("DEPLOY_ENV")
        or os.environ.get("INFRA_ENVIRONMENT")
        or "production"
    )
    e = get_env(env_name)
    vault_addr = e.get(
        "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
    )
    token = (
        vault_token
        or os.getenv("VAULT_ROOT_TOKEN")
        or os.getenv("VAULT_TOKEN")
        or e.get("VAULT_ROOT_TOKEN")
    )

    header(
        "Service Offboard Saga",
        f"Target: {clean_project}/{clean_service} (env={env_name}, purge={purge_data})",
    )

    # Step 0: Preflight checks
    info("Step 0: Preflight verification...")
    if not check_op_signed_in(op_runner):
        error("Preflight failed: 1Password CLI not signed in ('op whoami' failed).")
        raise RuntimeError("Preflight failed: 1Password CLI not signed in")
    success("1Password CLI signed in")

    result_summary: dict[str, Any] = {
        "service": clean_service,
        "project": clean_project,
        "env": env_name,
        "dokploy_removed": False,
        "vault_approle_removed": False,
        "db_locked": False,
        "db_purged": False,
    }

    # Step 1: Dokploy Service Cleanup
    info("Step 1: Inspecting Dokploy service...")
    client = dokploy_client or get_dokploy()
    target_dokploy_proj = dokploy_project or clean_project
    compose = None
    try:
        compose = client.find_compose_by_name(
            clean_service, project_name=target_dokploy_proj, env_name=env_name
        ) or client.find_compose_by_name(
            service, project_name=target_dokploy_proj, env_name=env_name
        )
    except Exception as exc:
        warning(f"Dokploy search query warning: {exc}")

    if compose and compose.get("composeId") and not dry_run:
        try:
            client.delete_compose(compose["composeId"], delete_volumes=purge_data)
            result_summary["dokploy_removed"] = True
            success(f"Deleted Dokploy compose application '{compose.get('name')}'")
        except Exception as exc:
            warning(f"Could not delete Dokploy compose: {exc}")
    elif compose and dry_run:
        info(f"[DRY RUN] Would delete Dokploy compose '{compose.get('name')}'")

    # Step 2: Vault AppRole & Policy Teardown
    info("Step 2: Revoking Vault AppRole & Policy...")
    role_name = policy_name(clean_project, env_name, clean_service)
    venv = dict(os.environ)
    venv["VAULT_ADDR"] = vault_addr
    if token:
        venv["VAULT_TOKEN"] = token

    if not dry_run and token:
        # Delete AppRole role
        c.run(
            f"vault delete auth/approle/role/{role_name}",
            env=venv,
            hide=True,
            warn=True,
            in_stream=False,
        )
        # Delete policy
        c.run(
            f"vault policy delete {role_name}",
            env=venv,
            hide=True,
            warn=True,
            in_stream=False,
        )
        result_summary["vault_approle_removed"] = True
        success(f"Vault AppRole and policy '{role_name}' removed")

        if purge_data:
            c.run(
                f"vault kv metadata delete secret/{clean_project}/{env_name}/{clean_service}",
                env=venv,
                hide=True,
                warn=True,
                in_stream=False,
            )
            success(
                f"Purged secret/data/{clean_project}/{env_name}/{clean_service} in Vault"
            )
    elif dry_run:
        info(f"[DRY RUN] Would remove Vault AppRole & Policy '{role_name}'")
    else:
        warning(
            f"VAULT_TOKEN not provided; skipping Vault AppRole teardown for '{role_name}'"
        )

    # Step 3: PostgreSQL Lockdown or Purge
    info("Step 3: Managing PostgreSQL database & user...")
    db_name = f"{clean_project}_{clean_service}_db"
    db_user = f"{clean_project}_{clean_service}_user"
    container = with_env_suffix("platform-postgres", e)

    if not dry_run:
        # First terminate active connections and lock account
        terminate_sql = (
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE usename = '{db_user}' AND pid <> pg_backend_pid();"
        )
        lock_sql = f"ALTER ROLE {db_user} NOLOGIN;"
        cmd_lock = (
            f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -c '
            f'\\"{terminate_sql} {lock_sql}\\""'
        )
        c.run(cmd_lock, hide=True, warn=True, in_stream=False)
        result_summary["db_locked"] = True
        success(f"PostgreSQL user '{db_user}' terminated and locked (NOLOGIN)")

        if purge_data:
            cmd_drop_db = (
                f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -c '
                f'\\"DROP DATABASE IF EXISTS {db_name};\\""'
            )
            cmd_drop_user = (
                f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -c '
                f'\\"DROP ROLE IF EXISTS {db_user};\\""'
            )
            c.run(cmd_drop_db, hide=True, warn=True, in_stream=False)
            c.run(cmd_drop_user, hide=True, warn=True, in_stream=False)
            result_summary["db_purged"] = True
            warning(f"PURGED PostgreSQL database '{db_name}' and role '{db_user}'")
    else:
        info(
            f"[DRY RUN] Would lock PostgreSQL user '{db_user}' (and purge DB if requested)"
        )

    success(f"Offboard complete for {clean_project}/{clean_service}!")
    return result_summary


@task(name="offboard")
def offboard_task(
    c,
    service: str,
    project: str = "apps",
    env: str | None = None,
    purge_data: bool = False,
    dokploy_project: str | None = None,
    dry_run: bool = False,
):
    """Safely offboard a service: locks DB user, cleans AppRole, removes Dokploy compose."""
    offboard_service(
        c,
        service=service,
        project=project,
        env=env,
        purge_data=purge_data,
        dokploy_project=dokploy_project,
        dry_run=dry_run,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Service Offboarding CLI")
    parser.add_argument("service", help="Service name (e.g. demo-app)")
    parser.add_argument(
        "--project", default="apps", help="Project name (default: apps)"
    )
    parser.add_argument(
        "--env",
        default="production",
        help="Deployment environment (default: production)",
    )
    parser.add_argument(
        "--purge-data",
        action="store_true",
        help="Permanently drop database and secret data",
    )
    parser.add_argument(
        "--dokploy-project", default=None, help="Dokploy project override"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate without mutating"
    )

    args = parser.parse_args()
    try:
        offboard_service(
            service=args.service,
            project=args.project,
            env=args.env,
            purge_data=args.purge_data,
            dokploy_project=args.dokploy_project,
            dry_run=args.dry_run,
        )
        return 0
    except Exception as exc:
        error(f"Offboarding failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
