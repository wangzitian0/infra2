"""
Common utilities shared across deploy scripts
"""
from __future__ import annotations
import os
import secrets
import string
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoke import Context


# Magic strings - extracted as constants
CONTAINER_NAMES = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "authentik-server",
}


# Cache for 1Password values
_op_cache: dict | None = None


def _load_from_1password() -> dict[str, str]:
    """Load env vars from 1Password init/env_vars (cached)"""
    global _op_cache
    if _op_cache is not None:
        return _op_cache
    
    import subprocess
    import json
    
    try:
        result = subprocess.run(
            'op item get "init/env_vars" --vault="Infra2" --format=json',
            shell=True, capture_output=True, text=True, check=True
        )
        item = json.loads(result.stdout)
        _op_cache = {f["label"]: f.get("value", "") for f in item.get("fields", [])
                     if f.get("label") and f.get("value")}
        return _op_cache
    except:
        _op_cache = {}
        return _op_cache


def get_env() -> dict[str, str | None]:
    """Get environment config from 1Password (no local .env needed)
    
    Falls back to os.environ for CI environments.
    """
    # Try 1Password first
    op_vars = _load_from_1password()
    
    # Merge with os.environ (CI fallback), 1Password takes priority
    return {
        "VPS_HOST": op_vars.get("VPS_HOST") or os.environ.get("VPS_HOST"),
        "VPS_SSH_USER": op_vars.get("VPS_SSH_USER") or os.environ.get("VPS_SSH_USER", "root"),
        "INTERNAL_DOMAIN": op_vars.get("INTERNAL_DOMAIN") or os.environ.get("INTERNAL_DOMAIN"),
        "PROJECT": os.environ.get("PROJECT", "platform"),
        "ENV": os.environ.get("DEPLOY_ENV", "production"),
    }


def validate_env() -> list[str]:
    """Validate required environment variables, returns list of missing vars"""
    env = get_env()
    missing = []
    if not env["VPS_HOST"]:
        missing.append("VPS_HOST")
    if not env["INTERNAL_DOMAIN"]:
        missing.append("INTERNAL_DOMAIN")
    return missing


def generate_password(length: int = 24) -> str:
    """Generate a random password"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def check_docker_service(c: "Context", container: str, health_cmd: str, service_name: str) -> dict:
    """
    Check if a Docker service is healthy
    
    Args:
        c: invoke context
        container: container name
        health_cmd: command to check health
        service_name: display name
    
    Returns:
        {is_ready: bool, details: str}
    """
    from libs.console import success, error
    env = get_env()
    result = c.run(f"ssh root@{env['VPS_HOST']} 'docker exec {container} {health_cmd}'", warn=True, hide=True)
    if result.ok:
        success(f"{service_name}: ready")
        return {"is_ready": True, "details": "Healthy"}
    error(f"{service_name}: not ready")
    return {"is_ready": False, "details": "Unhealthy"}
