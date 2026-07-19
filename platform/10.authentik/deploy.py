"""Authentik deployment with vault-init"""

import sys
import os
from libs.deploy.deployer import Deployer, make_tasks
from libs.common import with_env_suffix
from libs.env import VAULT_ROOT_TOKEN_OP_REF
from libs.console import success, warning, info, error, run_with_status
from libs.env import generate_password, get_secrets
from libs.service_facets import ProbeFacet, SecretsFacet

shared_tasks = sys.modules.get("platform.10.authentik.shared")


class AuthentikDeployer(Deployer):
    service = "authentik"
    compose_path = "platform/10.authentik/compose.yaml"
    data_path = "/data/platform/authentik"
    uid = "1000"
    gid = "1000"
    secret_key = "secret_key"

    # Domain configuration via Dokploy domains
    subdomain = "sso"
    service_port = 9000
    service_name = "server"

    # Infra probes (#541): rendered into INFRA_PROBE_SPECS by platform/alerting.
    probes = (
        ProbeFacet(
            name="authentik-internal-http",
            kind="http",
            target="http://platform-authentik-server${ENV_SUFFIX}:9000/-/health/live/",
            expected="200,204,302",
        ),
    )

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth per #257/#259).
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-authentik-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "platform-authentik-server${ENV_SUFFIX}",
                "platform-authentik-worker${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
    )

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        """Ensure every Vault key referenced by secrets.ctmpl exists."""
        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)
        if not pg_secrets.get("root_password"):
            warning("Postgres password not found in Vault")
            return False
        if not redis_secrets.get("password"):
            warning("Redis password not found in Vault")
            return False

        authentik_secrets = get_secrets(project, "authentik", env_name)

        secret_key = authentik_secrets.get("secret_key")
        if not secret_key:
            secret_key = generate_password(50)
            if not authentik_secrets.set("secret_key", secret_key):
                error("Failed to store Authentik secret key in Vault")
                return False
            warning("Generated new Authentik secret key in Vault")
        else:
            info("Authentik secret key exists in Vault")

        bootstrap_password = authentik_secrets.get("bootstrap_password")
        if not bootstrap_password:
            bootstrap_password = generate_password(24)
            if not authentik_secrets.set("bootstrap_password", bootstrap_password):
                error("Failed to store Authentik bootstrap password in Vault")
                return False
            warning("Generated new bootstrap admin password")

        bootstrap_email = authentik_secrets.get("bootstrap_email")
        if not bootstrap_email:
            bootstrap_email = e.get("ADMIN_EMAIL", "admin@localhost")
            if not authentik_secrets.set("bootstrap_email", bootstrap_email):
                error("Failed to store Authentik bootstrap email in Vault")
                return False
            warning(f"Set bootstrap admin email: {bootstrap_email}")

        return True

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories, check dependencies, ensure secrets exist in Vault."""
        from libs.console import fatal

        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        # Check Vault access
        if not os.getenv("VAULT_ROOT_TOKEN"):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for: 1) Reading postgres/redis passwords, 2) Storing authentik secrets\n"
                f"   Get token: op read '{VAULT_ROOT_TOKEN_OP_REF}' "
                "(or /Token; item: bootstrap/vault/Root Token)\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>",
            )

        # Check dependencies exist in Vault
        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)

        if not pg_secrets.get("root_password"):
            fatal(
                "Postgres password not found in Vault",
                "Deploy platform/postgres through deploy_v2 for this environment first.",
            )
        if not redis_secrets.get("password"):
            fatal(
                "Redis password not found in Vault",
                "Deploy platform/redis through deploy_v2 for this environment first.",
            )
        success("Verified postgres and redis secrets in Vault")

        # Create subdirs
        data_path = cls.data_path_for_env(e)
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'mkdir -p {data_path}/media {data_path}/certs'",
            "Create subdirs",
        )

        # Create database (idempotent)
        postgres_container = with_env_suffix("platform-postgres", e)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec {postgres_container} psql -U postgres -c 'CREATE DATABASE authentik;'\"",
            warn=True,
            hide=True,
        )
        if result.failed:
            # Check if database already exists
            stderr = result.stderr or ""
            if "already exists" in stderr:
                info("Database 'authentik' already exists")
            else:
                fatal("Failed to create 'authentik' database", f"Error: {stderr}")
        else:
            success("Database created")

        if not cls.ensure_runtime_secrets(c):
            fatal("Failed to ensure Authentik runtime secrets")

        # Return VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info(
            "\nNote: AppRole creds (VAULT_ROLE_ID/VAULT_SECRET_ID) auto-configured via 'invoke vault.setup-approle'"
        )
        return result


if shared_tasks:
    _tasks = make_tasks(AuthentikDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
