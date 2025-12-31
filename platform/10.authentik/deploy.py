"""Authentik deployment with vault-init"""
import sys
from libs.deployer import Deployer, make_tasks
from libs.common import get_env
from libs.console import success, warning, error, info, run_with_status
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
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        env_name = e.get('ENV', 'production')
        project = e.get('PROJECT', 'platform')
        
        # Check dependencies exist in Vault
        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)
        
        if not pg_secrets.get("root_password"):
            error("Postgres password not found in Vault - run postgres.setup first")
            return None
        if not redis_secrets.get("password"):
            error("Redis password not found in Vault - run redis.setup first")
            return None
        success("Verified postgres and redis secrets in Vault")
        
        # Create subdirs
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/media/public {cls.data_path}/certs'", "Create subdirs")
        
        # Create database (ignore if exists)
        run_with_status(c, f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c 'CREATE DATABASE authentik;'\"", "Create database", warn=True)
        
        # Ensure Authentik secret exists
        authentik_secrets = get_secrets(project, "authentik", env_name)
        secret_key = authentik_secrets.get("secret_key")
        if not secret_key:
            secret_key = generate_password(50)
            if not authentik_secrets.set("secret_key", secret_key):
                error("Failed to store Authentik secret key in Vault")
                return None
            warning("Generated new Authentik secret key in Vault")
        else:
            info("Authentik secret key exists in Vault")
        
        # Return VAULT_ADDR for vault-init pattern
        result = {
            "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"),
        }
        
        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result


if shared_tasks:
    _tasks = make_tasks(AuthentikDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
