"""PostgreSQL deployment with vault-init - only ensures secrets exist in Vault"""
import os
import sys
from dotenv import load_dotenv
from libs.deployer import Deployer, make_tasks
from libs.env import generate_password

shared_tasks = sys.modules.get("platform.01.postgres.shared")


class PostgresDeployer(Deployer):
    service = "postgres"
    compose_path = "platform/01.postgres/compose.yaml"
    data_path = "/data/platform/postgres"
    uid = "70"  # Alpine postgres user
    chmod = "700"
    secret_key = "root_password"
    env_var_name = "POSTGRES_PASSWORD"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and ensure secrets exist in Vault
        
        Note: With vault-init, secrets are fetched at container runtime.
        deploy.py only ensures they exist in Vault and passes VAULT_APP_TOKEN to Dokploy.
        """
        if not cls._prepare_dirs(c):
            return None
        
        from libs.console import env_vars, success, warning, info
        from libs.common import get_env
        
        e = get_env()
        
        # Ensure secrets exist in Vault (vault-init will fetch at runtime)
        secrets_backend = cls.secrets()
        password = secrets_backend.get(cls.secret_key)
        if not password:
            password = generate_password(24)
            secrets_backend.set(cls.secret_key, password)
            warning(f"Generated new password in Vault")
        else:
            info(f"Vault secret exists: {cls.secret_key}")
        
        # For vault-init pattern: pass VAULT_ADDR at project level, VAULT_APP_TOKEN at service level
        # VAULT_APP_TOKEN should be set in Dokploy service environment variables
        result = {
            "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'zitian.party')}"),
            # VAULT_APP_TOKEN is read from Dokploy service env (not passed here)
        }
        
        env_vars("DOKPLOY ENV (vault-init)", result)
        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result


if shared_tasks:
    _tasks = make_tasks(PostgresDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
