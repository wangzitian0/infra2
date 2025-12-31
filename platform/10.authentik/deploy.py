"""Authentik deployment with vault-init"""
import sys
import os
from libs.deployer import Deployer, make_tasks
from libs.common import get_env
from libs.console import success, warning, info, run_with_status
from libs.env import generate_password, get_secrets

shared_tasks = sys.modules.get("platform.10.authentik.shared")


class AuthentikDeployer(Deployer):
    service = "authentik"
    compose_path = "platform/10.authentik/compose.yaml"
    data_path = "/data/platform/authentik"
    uid = "1000"
    gid = "1000"
    secret_key = "secret_key"
    
    # Domain configuration
    subdomain = "sso"
    service_port = 9000
    service_name = "server"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories, check dependencies, ensure secrets exist in Vault."""
        from libs.console import fatal, check_failed
        
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        env_name = e.get('ENV', 'production')
        project = e.get('PROJECT', 'platform')
        
        # Check Vault access
        if not os.getenv('VAULT_ROOT_TOKEN'):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for: 1) Reading postgres/redis passwords, 2) Storing authentik secrets\n"
                "   Get token: op read 'op://Infra2/bootstrap/vault/Root Token/Root Token'\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>"
            )
        
        # Check dependencies exist in Vault
        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)
        
        if not pg_secrets.get("root_password"):
            fatal("Postgres password not found in Vault", "Run: export VAULT_ROOT_TOKEN=<token> && invoke postgres.setup")
        if not redis_secrets.get("password"):
            fatal("Redis password not found in Vault", "Run: export VAULT_ROOT_TOKEN=<token> && invoke redis.setup")
        success("Verified postgres and redis secrets in Vault")
        
        # Create subdirs
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/media {cls.data_path}/certs'", "Create subdirs")
        
        # Create database (idempotent)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c 'CREATE DATABASE authentik;'\"",
            warn=True,
            hide=True
        )
        if result.failed:
            # Check if database already exists
            stderr = result.stderr or ""
            if "already exists" in stderr:
                info("Database 'authentik' already exists")
            else:
                fatal(
                    "Failed to create 'authentik' database",
                    f"Error: {stderr}"
                )
        else:
            success("Database created")
        
        # Ensure Authentik secrets exist
        authentik_secrets = get_secrets(project, "authentik", env_name)
        
        # Secret key
        secret_key = authentik_secrets.get("secret_key")
        if not secret_key:
            secret_key = generate_password(50)
            if not authentik_secrets.set("secret_key", secret_key):
                fatal("Failed to store Authentik secret key in Vault")
            warning("Generated new Authentik secret key in Vault")
        else:
            info("Authentik secret key exists in Vault")
        
        # Bootstrap admin credentials (generate independently if missing)
        bootstrap_password = authentik_secrets.get("bootstrap_password")
        bootstrap_email = authentik_secrets.get("bootstrap_email")
        
        if not bootstrap_password:
            bootstrap_password = generate_password(24)
            if not authentik_secrets.set("bootstrap_password", bootstrap_password):
                fatal("Failed to store Authentik bootstrap password in Vault")
            warning("Generated new bootstrap admin password")
            info("Bootstrap password stored in Vault (key: bootstrap_password)")
        
        if not bootstrap_email:
            bootstrap_email = e.get("ADMIN_EMAIL", "admin@localhost")
            if not authentik_secrets.set("bootstrap_email", bootstrap_email):
                fatal("Failed to store Authentik bootstrap email in Vault")
            warning(f"Set bootstrap admin email: {bootstrap_email}")
        
        if bootstrap_password and bootstrap_email:
            info(f"Bootstrap credentials ready: {bootstrap_email}")
        
        # Return VAULT_ADDR for vault-init pattern
        result = {
            "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"),
        }
        
        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

    @classmethod
    def post_compose(cls, c, shared_tasks):
        """Verify deployment and initialize API token"""
        from libs.console import header, success, info, warning
        
        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if not result["is_ready"]:
            warning("Service not ready yet, token init will run on next healthy state")
            return False
        
        success("Service healthy")
        
        # Trigger token-init container
        e = cls.env()
        vault_token = os.getenv('VAULT_ROOT_TOKEN')
        
        if vault_token:
            info("Triggering token initialization...")
            compose_dir = "/etc/dokploy/compose/platform-authentik-*/code/platform/10.authentik"
            init_cmd = f"cd {compose_dir} && VAULT_INIT_TOKEN={vault_token} VAULT_ADDR=https://vault.{e['INTERNAL_DOMAIN']} docker compose up token-init"
            
            result = c.run(
                f"ssh root@{e['VPS_HOST']} '{init_cmd}'",
                warn=True,
                hide=True
            )
            
            if result.ok:
                success("API token initialized and stored in Vault")
            else:
                warning("Token initialization skipped or failed")
                info("You can manually create token later via: invoke authentik.shared.create-api-token")
        else:
            info("VAULT_ROOT_TOKEN not set, skipping automatic token creation")
            info("Set VAULT_ROOT_TOKEN and run: invoke authentik.post-compose")
        
        success("post_compose complete")
        return True


if shared_tasks:
    _tasks = make_tasks(AuthentikDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
