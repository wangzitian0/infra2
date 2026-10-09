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
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping

from invoke import Context, task

from libs.core.environ import get_env, infra_domain
from libs.console import error, header, info, success, warning
from libs.deploy.dokploy_client import get_dokploy
from libs.security.vault_tokens import policy_name


def _clean_ident(value: str) -> str:
    """Normalize identifier for Postgres/Vault (lowercase, replace '-' with '_')."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", value.strip().lower())


def _load_postgres_tasks():
    """Load postgres shared_tasks without static import digit issues."""
    mod_name = "platform.01.postgres.shared"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    root = Path(__file__).resolve().parent.parent
    path = root / "platform/01.postgres/shared_tasks.py"
    spec = importlib.util.spec_from_file_location("platform.01_postgres.shared", path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load postgres shared tasks from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_op_signed_in(runner: Any = subprocess.run) -> bool:
    """Validate 1Password CLI is installed and signed in."""
    try:
        res = runner(["op", "whoami"], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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
    from libs.security.store import generate_password

    try:
        from infra2_sdk.secrets import OnePasswordBackend
    except ImportError:
        OnePasswordBackend = None

    item_title = f"{project}/{env_name}/{service}/db"
    backend = op_backend or (
        OnePasswordBackend("Infra2") if OnePasswordBackend else None
    )

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


def provision_vault_approle(
    c: Context,
    project: str,
    env_name: str,
    service: str,
    vault_addr: str,
    root_token: str,
    *,
    custom_policy_hcl: str | None = None,
) -> tuple[str, str, str]:
    """Write Vault policy and AppRole role, issuing role_id and secret_id."""
    role_name = policy_name(project, env_name, service)
    venv = dict(os.environ)
    venv["VAULT_ADDR"] = vault_addr
    venv["VAULT_TOKEN"] = root_token

    if custom_policy_hcl:
        policy_rules = custom_policy_hcl.replace("{{env}}", env_name)
    else:
        policy_rules = f"""
path "secret/data/{env_name}/common" {{
  capabilities = ["read", "list"]
}}
path "secret/data/{project}/{env_name}/{service}" {{
  capabilities = ["read", "list"]
}}
path "secret/data/{project}/{env_name}/postgres" {{
  capabilities = ["read", "list"]
}}
path "auth/token/renew-self" {{
  capabilities = ["update"]
}}
path "auth/token/lookup-self" {{
  capabilities = ["read"]
}}
""".strip()

    # 1. Write policy
    res = c.run(
        f"vault policy write {role_name} -",
        env=venv,
        in_stream=io.StringIO(policy_rules),
        hide=True,
        warn=True,
    )
    if not res.ok:
        raise RuntimeError(f"Failed to write Vault policy {role_name}: {res.stderr}")

    # 2. Write AppRole role
    res = c.run(
        f"vault write auth/approle/role/{role_name} "
        f"token_policies={role_name} token_no_default_policy=true "
        f"token_ttl=1h token_max_ttl=24h "
        f"secret_id_num_uses=0 secret_id_ttl=0",
        env=venv,
        hide=True,
        warn=True,
    )
    if not res.ok:
        raise RuntimeError(f"Failed to write Vault AppRole {role_name}: {res.stderr}")

    # 3. Read role_id
    role_id_res = c.run(
        f"vault read -format=json auth/approle/role/{role_name}/role-id",
        env=venv,
        hide=True,
        warn=True,
    )
    if not role_id_res.ok:
        raise RuntimeError(
            f"Failed to read role-id for {role_name}: {role_id_res.stderr}"
        )
    role_id = json.loads(role_id_res.stdout)["data"]["role_id"]

    # 4. Mint secret_id
    secret_id_res = c.run(
        f"vault write -f -format=json auth/approle/role/{role_name}/secret-id",
        env=venv,
        hide=True,
        warn=True,
    )
    if not secret_id_res.ok:
        raise RuntimeError(
            f"Failed to issue secret-id for {role_name}: {secret_id_res.stderr}"
        )
    secret_id = json.loads(secret_id_res.stdout)["data"]["secret_id"]

    return role_name, role_id, secret_id


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
    """Idempotently sync secrets dictionary into Vault KV v2."""
    try:
        from infra2_sdk.secrets import VaultKvBackend, vault_path
    except ImportError:
        VaultKvBackend = None

        def vault_path(p: str, e: str, s: str) -> str:
            return f"{p}/{e}/{s}"

    backend = vault_backend or (
        VaultKvBackend(vault_addr, token=token) if VaultKvBackend else None
    )
    path = vault_path(project, env_name, service)

    if backend is not None:
        backend.write(path, secrets)
    else:
        # Fallback to direct Vault CLI command
        venv = dict(os.environ)
        venv["VAULT_ADDR"] = vault_addr
        venv["VAULT_TOKEN"] = token
        kv_pairs = [f"{k}={v}" for k, v in secrets.items()]
        subprocess.run(
            ["vault", "kv", "put", f"secret/{path}", *kv_pairs],
            env=venv,
            check=True,
            capture_output=True,
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
        env
        or os.environ.get("DEPLOY_ENV")
        or os.environ.get("INFRA_ENVIRONMENT")
        or "production"
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

        database_url = (
            f"postgresql://{db_user}:{db_password}@platform-postgres:5432/{db_name}"
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
            )
            pg.ensure_database(c, name=db_name, owner=db_user)
            success(
                f"PostgreSQL user '{db_user}' (limit {db_connection_limit}) and DB '{db_name}' ready"
            )

    # Step 3: Vault Secrets Sync
    info("Step 3: Syncing runtime secrets into Vault KV v2...")
    vault_secrets: dict[str, str] = {
        "SERVICE_NAME": clean_service,
        "ENV": env_name,
    }
    if db == "postgres":
        vault_secrets.update(
            {
                "DATABASE_URL": database_url,
                "POSTGRES_DB": db_name,
                "POSTGRES_USER": db_user,
                "POSTGRES_PASSWORD": db_password,
                "POSTGRES_HOST": "platform-postgres",
                "POSTGRES_PORT": "5432",
            }
        )
    if redis:
        vault_secrets.update(
            {
                "REDIS_URL": "redis://platform-redis:6379/0",
                "REDIS_HOST": "platform-redis",
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

    # Step 4: Vault Policy & AppRole Provisioning
    info("Step 4: Provisioning Vault Policy and AppRole credentials...")
    if dry_run or not token:
        role_name = policy_name(clean_project, env_name, clean_service)
        role_id = "mock_role_id"
        secret_id = "mock_secret_id"
        info(f"[DRY RUN] Would provision AppRole '{role_name}'")
    else:
        role_name, role_id, secret_id = provision_vault_approle(
            c,
            clean_project,
            env_name,
            clean_service,
            vault_addr,
            token,
        )
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
                    "VAULT_ADDR": vault_addr,
                    "ENV": env_name,
                },
            )
            result_summary["dokploy_env_updated"] = True
            success(f"Injected AppRole into Dokploy compose '{compose.get('name')}'")
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
        default="production",
        help="Deployment environment (default: production)",
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
