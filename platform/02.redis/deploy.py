"""Redis deployment with vault-init - only ensures secrets exist in Vault"""
import os
import sys
from dotenv import load_dotenv
from libs.deployer import Deployer, make_tasks
from libs.env import generate_password

shared_tasks = sys.modules.get("platform.02.redis.shared")


class RedisDeployer(Deployer):
    service = "redis"
    compose_path = "platform/02.redis/compose.yaml"
    data_path = "/data/platform/redis"
    secret_key = "password"
    env_var_name = "REDIS_PASSWORD"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and ensure secrets exist in Vault"""
        if not cls._prepare_dirs(c):
            return None
        
        from libs.console import env_vars, success, warning, info
        from libs.common import get_env
        
        e = get_env()
        
        # Ensure secrets exist in Vault
        secrets_backend = cls.secrets()
        password = secrets_backend.get(cls.secret_key)
        if not password:
            password = generate_password(24)
            secrets_backend.set(cls.secret_key, password)
            warning(f"Generated new password in Vault")
        else:
            info(f"Vault secret exists: {cls.secret_key}")
        
        result = {
            "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'zitian.party')}"),
        }
        
        if not cls.env_var_name and "VAULT_APP_TOKEN" not in result: # Avoid duplicate message if handled elsewhere
             # Actually deployer doesn't print for result items, only pre_compose success
             pass
        
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result


if shared_tasks:
    _tasks = make_tasks(RedisDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
