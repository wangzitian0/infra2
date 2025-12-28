"""
1Password Connect shared tasks
"""
import os
from invoke import task

INTERNAL_DOMAIN = os.environ.get("INTERNAL_DOMAIN")


@task
def status(c):
    """
    Check 1Password Connect status
    
    Returns:
        dict: {is_ready: bool, details: str}
    """
    result = c.run(f"curl -sf https://op.{INTERNAL_DOMAIN}/health", warn=True, hide=True)
    if result.ok:
        print("✅ 1Password Connect: ready")
        return {"is_ready": True, "details": "Health check passed"}
    else:
        print("❌ 1Password Connect: not ready")
        return {"is_ready": False, "details": "Health check failed"}


@task
def get_secret(c, vault, item, field):
    """Get a secret from 1Password"""
    print(f"Getting {vault}/{item}/{field} from 1Password")
    # TODO: Implement actual API call
    return "secret_value"
