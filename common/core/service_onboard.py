"""Atomic Service Onboarding Saga (Zero-Compromise Automation).

Consolidates manual onboarding steps (1Password, Vault KV, PostgreSQL, Vault AppRole,
Dokploy env) into a single idempotent saga that runs in ~10 seconds.

Usage:
    python -m tools.service_onboard <service> [--project apps] [--env production] [--db postgres]
    invoke service.onboard <service> [--project apps] [--env production] [--db postgres]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from invoke import Context, task

from common.security.approle import (
    default_approle_policy,
    destroy_secret_ids,
    reconcile_approle,
    revoke_earlier_role_tokens,
)
from common.security.backends import policy_name
from common.security.supply import (
    vault_backend as build_vault_backend,
    write_runtime_secrets,
)

from common.console import error, header, info, success, warning
from common.core.environ import get_env, infra_domain, with_env_suffix
from common.deploy.dokploy_client import get_dokploy
from common.security.store import (
    check_op_signed_in,
    clean_ident as _clean_ident,
)


def _load_postgres_tasks():
    """Load postgres shared_tasks without static import digit issues."""
    mod_name = "platform.01.postgres.shared"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    root = Path(__file__).resolve().parents[2]
    path = root / "platform/01.postgres/shared_tasks.py"
    spec = importlib.util.spec_from_file_location("platform.01_postgres.shared", path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load postgres shared tasks from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_or_create_db_credentials(
    project: str,
    env_name: str,
    service: str,
    db_name: str,
    db_user: str,
    *,
    op_backend: Any = None,
) -> tuple[str, bool]:
    """Retrieve existing DB password from 1Password or generate and store a new one.

    Returns (password, is_created).
    """
    from common.security.store import generate_password
    from common.security.backends import OnePasswordBackend

    item_title = f"{project}/{env_name}/{service}/db"
    backend = op_backend or OnePasswordBackend("Infra2")

    if backend is not None:
        try:
            existing = backend.read(item_title)
            if existing and existing.get("password"):
                return existing["password"], False
        except Exception as exc:
            warning(f"Could not read 1Password item '{item_title}': {exc}")

    # Not found or empty: generate fresh 32-char cryptographically secure password
    new_password = generate_password(32)
    if backend is not None:
        try:
            backend.write(
                item_title,
                {
                    "username": db_user,
                    "password": new_password,
                    "database": db_name,
                },
            )
        except Exception as exc:
            warning(f"Could not write 1Password item '{item_title}': {exc}")
    return new_password, True


def _default_onboard_policy(
    project: str, env_name: str, service: str, custom_policy_hcl: str | None = None
) -> str:
    if custom_policy_hcl:
        return custom_policy_hcl
    return default_approle_policy(project, env_name, service)


def provision_vault_approle(
    c: Context,
    project: str,
    env_name: str,
    service: str,
    vault_addr: str,
    root_token: str,
    *,
    custom_policy_hcl: str | None = None,
    cleanup_stale: bool = True,
) -> tuple[str, str, str]:
    """Write Vault policy and AppRole role using ``common.security.approle`` (24h/168h TTL)."""
    venv = dict(os.environ)
    venv["VAULT_ADDR"] = vault_addr
    venv["VAULT_TOKEN"] = root_token

    policy_rules = _default_onboard_policy(
        project, env_name, service, custom_policy_hcl
    )
    res = reconcile_approle(
        c,
        venv,
        project=project,
        env_name=env_name,
        service=service,
        policy_rules=policy_rules,
        issue_credentials=True,
    )
    if cleanup_stale:
        stale = [a for a in res.prior_accessors if a != res.new_accessor]
        if stale:
            destroy_secret_ids(c, venv, res.role_name, stale)
        revoke_earlier_role_tokens(c, venv, res.role_name, res.issue_tag)
    return res.role_name, res.role_id, res.secret_id


def sync_vault_secrets(
    project: str,
    env_name: str,
    service: str,
    vault_addr: str,
    token: str,
    secrets: Mapping[str, str],
    *,
    vault_backend: Any = None,
) -> None:
    """Idempotently sync secrets dictionary into Vault KV v2 via ``supply`` (``update`` mode)."""
    write_runtime_secrets(
        project,
        env_name,
        service,
        secrets,
        store=vault_backend,
        environ={**os.environ, "VAULT_ADDR": vault_addr, "VAULT_TOKEN": token},
    )


def onboard_service(
    c: Context | None = None,
    service: str = "",
    project: str = "apps",
    env: str | None = None,
    db: str = "postgres",
    db_connection_limit: int = 8,
    redis: bool = False,
    dokploy_project: str | None = None,
    dry_run: bool = False,
    op_runner: Any = subprocess.run,
    op_backend: Any = None,
    vault_backend: Any = None,
    vault_token: str | None = None,
    dokploy_client: Any = None,
) -> dict[str, Any]:
    """Execute the end-to-end atomic onboarding saga.

    Returns dictionary summarizing provisioned resources.
    """
    if not service:
        raise ValueError("Service name is required")

    if c is None:
        c = Context()
    clean_service = _clean_ident(service)
    clean_project = _clean_ident(project)
    env_name = (
        env or os.environ.get("DEPLOY_ENV") or os.environ.get("INFRA_ENVIRONMENT")
    )
    if not env_name:
        raise ValueError(
            "Explicit environment is required (pass env=... or set DEPLOY_ENV)"
        )
    e = get_env(env_name)
    domain = e.get("INTERNAL_DOMAIN") or infra_domain()
    vault_addr = e.get("VAULT_ADDR") or f"https://vault.{domain}"
    token = (
        vault_token
        or os.getenv("VAULT_ROOT_TOKEN")
        or os.getenv("VAULT_TOKEN")
        or e.get("VAULT_ROOT_TOKEN")
    )

    header(
        "Service Onboard Saga",
        f"Target: {clean_project}/{clean_service} (env={env_name})",
    )

    # Step 0: Preflight checks
    info("Step 0: Preflight verification...")
    if not check_op_signed_in(op_runner):
        error("Preflight failed: 1Password CLI not signed in ('op whoami' failed).")
        error("Please run 'op signin' before onboarding services.")
        raise RuntimeError("Preflight failed: 1Password CLI not signed in")
    success("1Password CLI signed in and responsive")

    if not token and not dry_run:
        error("VAULT_ROOT_TOKEN is required for onboarding AppRole and policies.")
        raise RuntimeError("Missing VAULT_ROOT_TOKEN")

    result_summary: dict[str, Any] = {
        "service": clean_service,
        "project": clean_project,
        "env": env_name,
        "db": None,
        "vault_approle": None,
        "dokploy_env_updated": False,
    }

    # Step 1 & 2: Database Provisioning & 1Password Oracle
    db_name = f"{clean_project}_{clean_service}_db"
    db_user = f"{clean_project}_{clean_service}_user"
    database_url = ""

    if db == "postgres":
        info(f"Step 1: 1Password Oracle & Database provisioning ({db_name})...")
        if dry_run:
            db_password = "dry_run_password_1234567890123456"
            info("[DRY RUN] Would retrieve/generate password in 1Password")
        else:
            db_password, created = get_or_create_db_credentials(
                clean_project,
                env_name,
                clean_service,
                db_name,
                db_user,
                op_backend=op_backend,
            )
            if created:
                success(
                    f"Generated new credentials in 1Password (item: {clean_project}/{env_name}/{clean_service}/db)"
                )
            else:
                info("Reusing existing credentials from 1Password SSOT")

        postgres_host = with_env_suffix("platform-postgres", e)
        database_url = (
            f"postgresql://{db_user}:{db_password}@{postgres_host}:5432/{db_name}"
        )
        result_summary["db"] = {
            "database": db_name,
            "user": db_user,
            "connection_limit": db_connection_limit,
            "database_url": database_url,
        }

        if not dry_run:
            info("Step 2: Ensuring PostgreSQL database & user with resource limits...")
            pg = _load_postgres_tasks()
            pg.ensure_user(
                c,
                username=db_user,
                database=db_name,
                password=db_password,
                connection_limit=db_connection_limit,
                env=env_name,
            )
            pg.ensure_database(c, name=db_name, owner=db_user, env=env_name)
            if hasattr(pg, "grant_database"):
                pg.grant_database(c, username=db_user, database=db_name, env=env_name)
            success(
                f"PostgreSQL user '{db_user}' (limit {db_connection_limit}) and DB '{db_name}' ready on {postgres_host}"
            )

    # Step 3: Vault Secrets Sync
    info("Step 3: Syncing runtime secrets into Vault KV v2...")
    redis_host = with_env_suffix("platform-redis", e)
    vault_secrets: dict[str, str] = {
        "ENV": env_name,
    }
    if db == "postgres":
        vault_secrets.update(
            {
                "DATABASE_URL": database_url,
                "POSTGRES_DB": db_name,
                "POSTGRES_USER": db_user,
                "POSTGRES_PASSWORD": db_password,
                "POSTGRES_HOST": postgres_host,
                "POSTGRES_PORT": "5432",
            }
        )
    if redis:
        if dry_run or not token:
            redis_password = "DRY_RUN_REDIS_PASSWORD"
        else:
            redis_store = vault_backend or build_vault_backend(
                {**os.environ, "VAULT_ADDR": vault_addr, "VAULT_TOKEN": token}
            )
            redis_data = redis_store.read(f"platform/{env_name}/redis") or {}
            redis_password = redis_data.get("password")
            if not redis_password:
                raise RuntimeError(
                    f"platform/redis password not found in Vault for env={env_name!r}"
                )
        vault_secrets.update(
            {
                "REDIS_URL": f"redis://:{redis_password}@{redis_host}:6379/0",
                "REDIS_HOST": redis_host,
                "REDIS_PORT": "6379",
            }
        )

    if not dry_run and token:
        sync_vault_secrets(
            clean_project,
            env_name,
            clean_service,
            vault_addr,
            token,
            vault_secrets,
            vault_backend=vault_backend,
        )
        success(
            f"Runtime secrets synced to secret/data/{clean_project}/{env_name}/{clean_service}"
        )

    # Step 4: Vault Policy & AppRole Provisioning (do NOT destroy prior secret_ids until
    # Dokploy env / consumers are updated!)
    info("Step 4: Provisioning Vault Policy and AppRole credentials...")
    approle_res = None
    if dry_run or not token:
        role_name = policy_name(clean_project, env_name, clean_service)
        role_id = "mock_role_id"
        secret_id = "mock_secret_id"
        info(f"[DRY RUN] Would provision AppRole '{role_name}'")
    else:
        venv = {**os.environ, "VAULT_ADDR": vault_addr, "VAULT_TOKEN": token}
        approle_res = reconcile_approle(
            c,
            venv,
            project=clean_project,
            env_name=env_name,
            service=clean_service,
            policy_rules=_default_onboard_policy(
                clean_project, env_name, clean_service
            ),
            issue_credentials=True,
        )
        role_name = approle_res.role_name
        role_id = approle_res.role_id
        secret_id = approle_res.secret_id
        success(f"Vault AppRole '{role_name}' active with fresh role_id and secret_id")

    result_summary["vault_approle"] = {
        "role_name": role_name,
        "role_id": role_id,
        "secret_id": secret_id,
    }

    # Step 5: Dokploy Environment Injection
    info("Step 5: Updating Dokploy compose environment...")
    client = dokploy_client or get_dokploy()
    compose = None
    target_dokploy_proj = dokploy_project or clean_project
    try:
        # Search by clean_service or original service
        compose = client.find_compose_by_name(
            clean_service, project_name=target_dokploy_proj, env_name=env_name
        ) or client.find_compose_by_name(
            service, project_name=target_dokploy_proj, env_name=env_name
        )
    except Exception as exc:
        warning(f"Dokploy search query warning: {exc}")

    if compose and compose.get("composeId") and not dry_run:
        try:
            client.update_compose_env(
                compose["composeId"],
                env_vars={
                    "VAULT_ROLE_ID": role_id,
                    "VAULT_SECRET_ID": secret_id,
                    "VAULT_ADDR": e.get("VAULT_ADDR", "http://vault:8200"),
                    "ENV": env_name,
                },
                replace=False,
            )
            result_summary["dokploy_env_updated"] = True
            success(f"Injected AppRole into Dokploy compose '{compose.get('name')}'")
            if approle_res is not None:
                stale = [
                    a
                    for a in approle_res.prior_accessors
                    if a != approle_res.new_accessor
                ]
                if stale:
                    destroy_secret_ids(c, venv, approle_res.role_name, stale)
        except Exception as exc:
            warning(f"Dokploy env update failed: {exc}")
    else:
        if not compose:
            info(
                f"Dokploy compose for '{clean_service}' not found yet (will receive creds on initial deploy)"
            )
        elif dry_run:
            info(f"[DRY RUN] Would update Dokploy compose '{clean_service}'")

    success(
        f"Service onboard complete for {clean_project}/{clean_service} in {env_name}!"
    )
    return result_summary


@task(name="onboard")
def onboard_task(
    c,
    service: str,
    project: str = "apps",
    env: str | None = None,
    db: str = "postgres",
    connection_limit: int = 8,
    redis: bool = False,
    dokploy_project: str | None = None,
    dry_run: bool = False,
):
    """Idempotently onboard a new service with 1Password, Postgres, Vault, and Dokploy."""
    onboard_service(
        c,
        service=service,
        project=project,
        env=env,
        db=db,
        db_connection_limit=connection_limit,
        redis=redis,
        dokploy_project=dokploy_project,
        dry_run=dry_run,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zero-Compromise Service Onboarding CLI"
    )
    parser.add_argument("service", help="Service name (e.g. demo-app)")
    parser.add_argument(
        "--project", default="apps", help="Project name (default: apps)"
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Deployment environment (staging or production)",
    )
    parser.add_argument(
        "--db", choices=["postgres", "none"], default="postgres", help="Database engine"
    )
    parser.add_argument(
        "--connection-limit",
        type=int,
        default=8,
        help="PostgreSQL connection limit (default: 8)",
    )
    parser.add_argument(
        "--redis", action="store_true", help="Include platform Redis configuration"
    )
    parser.add_argument(
        "--dokploy-project", default=None, help="Dokploy project override"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate without writing"
    )

    args = parser.parse_args()
    try:
        onboard_service(
            service=args.service,
            project=args.project,
            env=args.env,
            db=args.db,
            db_connection_limit=args.connection_limit,
            redis=args.redis,
            dokploy_project=args.dokploy_project,
            dry_run=args.dry_run,
        )
        return 0
    except Exception as exc:
        error(f"Onboarding failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
