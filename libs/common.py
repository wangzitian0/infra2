"""
Common utilities for deploy scripts

Simplified: uses libs/env.py for secrets, minimal API surface.
"""
from __future__ import annotations
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoke import Context


# Container name mapping
CONTAINERS = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "platform-authentik-server",
    "minio": "platform-minio",
    "wealthfolio": "finance-wealthfolio",
}

# Service subdomain mapping (subdomain prefix -> description)
# These are the canonical subdomains for each service
SERVICE_SUBDOMAINS = {
    # Bootstrap services
    "dokploy": "cloud",      # cloud.{domain}
    "1password": "op",       # op.{domain}
    "vault": "vault",        # vault.{domain}
    "sso": "sso",            # sso.{domain} (Authentik)
    # Platform services
    "minio_console": "minio",  # minio.{domain} -> Console (9001)
    "minio_api": "s3",         # s3.{domain} -> S3 API (9000)
    "portal": "portal",        # portal.{domain}
    # Finance apps
    "wealthfolio": "wealth",   # wealth.{domain}
}


def get_service_url(service: str, domain: str = None) -> str:
    """Get full URL for a service.
    
    Args:
        service: Service key from SERVICE_SUBDOMAINS
        domain: Optional domain override (defaults to INTERNAL_DOMAIN from env)
        
    Returns:
        Full HTTPS URL for the service
    """
    if domain is None:
        domain = get_env().get("INTERNAL_DOMAIN")
    if not domain:
        raise ValueError("INTERNAL_DOMAIN not set")
    
    subdomain = SERVICE_SUBDOMAINS.get(service)
    if not subdomain:
        raise ValueError(f"Unknown service: {service}")
    
    return f"https://{subdomain}.{domain}"

# Cache for env config (simple dict, no lru_cache to avoid OpSecrets caching issues)
_env_cache: dict | None = None


def get_env() -> dict[str, str | None]:
    """Get deployment environment config.
    
    Sources: 1Password init/env_vars → os.environ fallback
    """
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    
    from libs.env import OpSecrets
    op = OpSecrets()
    
    _env_cache = {
        "VPS_HOST": op.get("VPS_HOST") or os.environ.get("VPS_HOST"),
        "VPS_SSH_USER": op.get("VPS_SSH_USER") or os.environ.get("VPS_SSH_USER", "root"),
        "INTERNAL_DOMAIN": op.get("INTERNAL_DOMAIN") or os.environ.get("INTERNAL_DOMAIN"),
        "PROJECT": os.environ.get("PROJECT", "platform"),
        "ENV": os.environ.get("DEPLOY_ENV", "production"),
    }
    return _env_cache


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
                if key.lower().startswith('export '):
                    key = key[7:]
                keys.append(key)
    return keys


# Re-export generate_password for backward compatibility
def generate_password(length: int = 24) -> str:
    """Generate secure random password (re-exported from libs.env)"""
    from libs.env import generate_password as _gen
    return _gen(length)
