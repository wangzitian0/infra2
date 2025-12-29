"""
Common utilities shared across deploy scripts

Uses libs/env.py for 1Password access - no duplicate code.
"""
from __future__ import annotations
import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoke import Context


# Magic strings - extracted as constants
CONTAINER_NAMES = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "authentik-server",
}

# Required fields for bootstrap
REQUIRED_INIT_FIELDS = ["VPS_HOST", "INTERNAL_DOMAIN"]


@lru_cache(maxsize=1)
def _load_init_config() -> dict[str, str]:
    """Load init config from 1Password (cached with lru_cache)"""
    from libs.env import EnvManager
    mgr = EnvManager(project='init', env='production')
    return mgr.get_all_env(level='service')


def load_env_keys(path: str) -> list[str]:
    """Parse .env file and return list of keys"""
    if not os.path.exists(path):
        return []
    
    keys = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Handle default values/comments in line
            # e.g. KEY=VAL # comment
            if '=' in line:
                key = line.split('=', 1)[0].strip()
                # Remove export prefix if present
                if key.upper().startswith('EXPORT '):
                    key = key[7:].strip()
                keys.append(key)
    return keys


def get_env() -> dict[str, str | None]:
    """Get environment config from 1Password (no local .env needed)
    
    Falls back to os.environ for CI environments.
    """
    op_vars = _load_init_config()
    
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
    return [k for k in REQUIRED_INIT_FIELDS if not env.get(k)]


def check_docker_service(c: "Context", container: str, health_cmd: str, service_name: str) -> dict:
    """Check if a Docker service is healthy"""
    from libs.console import success, error
    env = get_env()
    result = c.run(f"ssh root@{env['VPS_HOST']} 'docker exec {container} {health_cmd}'", warn=True, hide=True)
    if result.ok:
        success(f"{service_name}: ready")
        return {"is_ready": True, "details": "Healthy"}
    error(f"{service_name}: not ready")
    return {"is_ready": False, "details": "Unhealthy"}


# Re-export generate_password from libs.env for backward compatibility
def generate_password(length: int = 24) -> str:
    """Generate a secure random password (re-exports from libs.env)"""
    from libs.env import generate_password as _gen
    return _gen(length)


# Public API
__all__ = [
    'get_env',
    'validate_env',
    'load_env_keys',
    'generate_password',
    'check_docker_service',
    'CONTAINER_NAMES',
    'REQUIRED_INIT_FIELDS',
]
