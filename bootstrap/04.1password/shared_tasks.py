"""1Password Connect shared tasks - uses libs/ system"""
from __future__ import annotations
from invoke import task
from libs.common import get_env
from libs.console import success, error


@task
def status(c) -> dict:
    """Check 1Password Connect status
    
    Returns:
        dict: {is_ready: bool, details: str}
    """
    e = get_env()
    result = c.run(f"curl -sf https://op.{e['INTERNAL_DOMAIN']}/health", warn=True, hide=True)
    if result.ok:
        success("1Password Connect: ready")
        return {"is_ready": True, "details": "Health check passed"}
    error("1Password Connect: not ready")
    return {"is_ready": False, "details": "Health check failed"}


@task
def get_secret(c, vault: str, item: str, field: str) -> str | None:
    """Get a secret from 1Password"""
    from libs.env import OpSecrets
    return OpSecrets(item=item, vault=vault).get(field)
