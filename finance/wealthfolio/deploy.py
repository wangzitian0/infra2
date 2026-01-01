"""Wealthfolio portfolio tracker deployment"""
import sys

from libs.deployer import Deployer, make_tasks
from libs.console import header, success

# Get shared_tasks from sys.modules (loaded by tools/loader.py)
shared_tasks = sys.modules.get("finance.wealthfolio.shared")


class WealthfolioDeployer(Deployer):
    service = "wealthfolio"
    compose_path = "finance/wealthfolio/compose.yaml"
    data_path = "/data/finance/wealthfolio"
    project = "finance"  # New Dokploy project
    uid = "1000"
    gid = "1000"
    
    # Secret key for encryption (stored in Vault)
    secret_key = "WF_SECRET_KEY"
    env_var_name = "WF_SECRET_KEY"
    
    # Domain configuration - uses Dokploy API
    subdomain = "wealth"
    service_port = 8088
    service_name = "wealthfolio"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and secrets."""
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        secrets = cls.secrets()
        
        # Get or generate WF_SECRET_KEY (32-byte base64)
        secret_key = secrets.get(cls.secret_key)
        if not secret_key:
            import base64
            import secrets as py_secrets
            secret_key = base64.b64encode(py_secrets.token_bytes(32)).decode()
            if secrets.set(cls.secret_key, secret_key):
                from libs.console import warning
                warning(f"Generated new {cls.secret_key} in Vault")
            else:
                from libs.console import error
                error(f"Failed to store {cls.secret_key} in Vault")
                return None
        
        header("WEALTHFOLIO", "Service Ready")
        return {
            cls.secret_key: secret_key,
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN"),
        }


# Generate tasks
if shared_tasks:
    _tasks = make_tasks(WealthfolioDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
