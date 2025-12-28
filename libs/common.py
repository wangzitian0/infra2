"""
Common utilities shared across deploy scripts
"""
import os
import secrets
import string


def get_env():
    """Get environment config (lazy evaluation)"""
    return {
        "VPS_HOST": os.environ.get("VPS_HOST"),
        "INTERNAL_DOMAIN": os.environ.get("INTERNAL_DOMAIN"),
        "PROJECT": os.environ.get("PROJECT", "platform"),
        "ENV": os.environ.get("DEPLOY_ENV", "production"),
    }


def validate_env():
    """Validate required environment variables, returns list of missing vars"""
    env = get_env()
    missing = []
    if not env["VPS_HOST"]:
        missing.append("VPS_HOST")
    if not env["INTERNAL_DOMAIN"]:
        missing.append("INTERNAL_DOMAIN")
    return missing


def generate_password(length=24):
    """Generate a random password"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def check_docker_service(c, container: str, health_cmd: str, service_name: str) -> dict:
    """
    Check if a Docker service is healthy (Fix #4 - DRY)
    
    Args:
        c: invoke context
        container: container name
        health_cmd: command to check health
        service_name: display name
    
    Returns:
        {is_ready: bool, details: str}
    """
    from tools.console import success, error
    env = get_env()
    result = c.run(f"ssh root@{env['VPS_HOST']} 'docker exec {container} {health_cmd}'", warn=True, hide=True)
    if result.ok:
        success(f"{service_name}: ready")
        return {"is_ready": True, "details": "Healthy"}
    else:
        error(f"{service_name}: not ready")
        return {"is_ready": False, "details": "Unhealthy"}
