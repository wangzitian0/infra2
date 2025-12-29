"""
Common utilities for deploy scripts

Simplified: uses libs/env.py for secrets, minimal API surface.
"""
from __future__ import annotations
import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoke import Context


# Container name mapping
CONTAINERS = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "authentik-server",
}


@lru_cache(maxsize=1)
def get_env() -> dict[str, str | None]:
    """Get deployment environment config.
    
    Sources: 1Password init/env_vars → os.environ fallback
    """
    from libs.env import OpSecrets
    op = OpSecrets()
    
    return {
        "VPS_HOST": op.get("VPS_HOST") or os.environ.get("VPS_HOST"),
        "VPS_SSH_USER": op.get("VPS_SSH_USER") or os.environ.get("VPS_SSH_USER", "root"),
        "INTERNAL_DOMAIN": op.get("INTERNAL_DOMAIN") or os.environ.get("INTERNAL_DOMAIN"),
        "PROJECT": os.environ.get("PROJECT", "platform"),
        "ENV": os.environ.get("DEPLOY_ENV", "production"),
    }


def validate_env() -> list[str]:
    """Return list of missing required env vars"""
    env = get_env()
    required = ["VPS_HOST", "INTERNAL_DOMAIN"]
    return [k for k in required if not env.get(k)]


def check_service(c: "Context", service: str, health_cmd: str) -> dict:
    """Check if a Docker service is healthy."""
    from libs.console import success, error
    
    container = CONTAINERS.get(service, f"platform-{service}")
    env = get_env()
    
    result = c.run(
        f"ssh root@{env['VPS_HOST']} 'docker exec {container} {health_cmd}'",
        warn=True, hide=True
    )
    
    if result.ok:
        success(f"{service}: ready")
        return {"is_ready": True, "details": "Healthy"}
    
    error(f"{service}: not ready")
    return {"is_ready": False, "details": "Unhealthy"}


def parse_env_file(path: str) -> list[str]:
    """Parse .env file and return list of keys"""
    if not os.path.exists(path):
        return []
    
    keys = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key = line.split('=', 1)[0].strip()
                if key.startswith('export '):
                    key = key[7:]
                keys.append(key)
    return keys
