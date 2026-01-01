"""Wealthfolio portfolio tracker deployment"""
import sys

from libs.deployer import Deployer, make_tasks
from libs.console import header

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
        
        # Authentication Setup
        auth_password = secrets.get("WF_AUTH_PASSWORD")
        auth_hash = secrets.get("WF_AUTH_PASSWORD_HASH")
        
        if not auth_password:
            import secrets as py_secrets
            auth_password = py_secrets.token_urlsafe(16)
            if secrets.set("WF_AUTH_PASSWORD", auth_password):
                from libs.console import warning
                warning(f"Generated new Login Password in Vault: WF_AUTH_PASSWORD")
                # Invalidate hash if password changed (though here it's new)
                auth_hash = None
            else:
                from libs.console import error
                error("Failed to store WF_AUTH_PASSWORD in Vault")
                return None
        
        if not auth_hash:
            from libs.console import info
            from libs.common import get_env
            import shlex
            
            info("Generating Argon2 hash for password...")
            host = get_env().get("VPS_HOST")
            salt = py_secrets.token_hex(16)
            # Use Alpine to run argon2 since it's not in the app container
            cmd = (
                f"ssh root@{host} 'docker run --rm alpine:latest sh -c "
                f"\"apk add --no-cache argon2 >/dev/null 2>&1 && "
                f"echo -n {shlex.quote(auth_password)} | argon2 {shlex.quote(salt)} -id -e\"'"
            )
            
            result = c.run(cmd, warn=True, hide=True)
            if result.ok and result.stdout:
                # Extract hash, preferring the last line starting with $argon2id$
                for line in result.stdout.splitlines():
                    if line.strip().startswith("$argon2id$"):
                        auth_hash = line.strip()
            
            if auth_hash:
                if secrets.set("WF_AUTH_PASSWORD_HASH", auth_hash):
                    info("Stored WF_AUTH_PASSWORD_HASH in Vault")
                else:
                    from libs.console import error
                    error("Failed to store hash in Vault")
                    return None
            else:
                from libs.console import error
                error("Failed to generate argon2 hash.")
                return None

        header("WEALTHFOLIO", "Service Ready")
        from libs.console import env_vars
        env_vars("Top Secrets", {
            "Login Password": "Stored securely in Vault as WF_AUTH_PASSWORD"
        })
        
        return {
            cls.secret_key: secret_key,
            "WF_AUTH_PASSWORD_HASH": auth_hash.replace("$", "$$") if auth_hash else None,
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
