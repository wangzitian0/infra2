"""Wealthfolio shared tasks"""
from invoke import task
from libs.common import get_env
from libs.console import success, error


@task
def status(c):
    """Check Wealthfolio service status."""
    env = get_env()
    host = env.get("VPS_HOST")
    
    result = c.run(
        f"ssh root@{host} 'docker exec finance-wealthfolio curl -sf http://localhost:8088/'",
        warn=True, hide=True
    )
    
    if result.ok:
        success("wealthfolio: ready")
        return {"is_ready": True, "details": "Healthy"}
    
    error("wealthfolio: not ready")
    return {"is_ready": False, "details": "Unhealthy"}
