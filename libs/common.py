"""
Common utilities for deploy scripts

Simplified: uses libs/env.py for secrets, minimal API surface.
"""
from __future__ import annotations
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoke import Context


# Container name mapping (base names; ENV_SUFFIX appended at runtime when set)
CONTAINERS = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "platform-authentik-server",
    "minio": "platform-minio",
    "wealthfolio": "finance-wealthfolio",
    "clickhouse": "platform-clickhouse",
    "signoz": "platform-signoz",
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

# Cache for env config (simple dict, no lru_cache to avoid OpSecrets caching issues)
_env_cache: dict | None = None


def normalize_env_name(value: str | None) -> str:
    """Normalize environment name for consistent behavior."""
    if not value or not value.strip():
        return "production"
    value = value.strip().lower()
    if "-" in value or "/" in value:
        raise ValueError("ENV name must not include '-' or '/' (use '_')")
    if value in ("prod", "production"):
        return "production"
    return value


def get_env() -> dict[str, str | None]:
    """Get deployment environment config.

    Sources: 1Password init/env_vars → os.environ fallback
    """
    global _env_cache
    if _env_cache is not None:
        return _env_cache

    from libs.env import OpSecrets
    op = OpSecrets()

    env_name = normalize_env_name(os.environ.get("DEPLOY_ENV", "production"))
    env_dns = env_name.replace("_", "-")
    env_domain_suffix = "" if env_name == "production" else f"-{env_dns}"
    project = (os.environ.get("PROJECT") or "platform").strip()
    if not project:
        raise ValueError("PROJECT must not be empty")
    if "-" in project or "/" in project:
        raise ValueError("PROJECT must not include '-' or '/'")

    _env_cache = {
        "VPS_HOST": op.get("VPS_HOST") or os.environ.get("VPS_HOST"),
        "VPS_SSH_USER": op.get("VPS_SSH_USER") or os.environ.get("VPS_SSH_USER", "root"),
        "INTERNAL_DOMAIN": op.get("INTERNAL_DOMAIN") or os.environ.get("INTERNAL_DOMAIN"),
        "PROJECT": project,
        "ENV": env_name,
        "ENV_DOMAIN_SUFFIX": env_domain_suffix,
        "ENV_SUFFIX": os.environ.get("ENV_SUFFIX"),
        "DATA_PATH": os.environ.get("DATA_PATH"),
    }
    return _env_cache


def _domain_env_label(env_name: str) -> str:
    """Convert internal env name into a DNS-safe label."""
    return env_name.replace("_", "-")


def _domain_env_suffix(env_name: str) -> str:
    """Build env suffix for domains: '' for production, '-<env>' otherwise."""
    if env_name == "production":
        return ""
    return f"-{_domain_env_label(env_name)}"


def _build_domain(subdomain: str, env_name: str, domain: str) -> str:
    """Build domain as {subdomain}{env_suffix}.{domain}."""
    return f"{subdomain}{_domain_env_suffix(env_name)}.{domain}"


def get_service_url(service: str, domain: str | None = None, env: dict | None = None) -> str:
    """Get full HTTPS URL for a service.

    Args:
        service: Service key from SERVICE_SUBDOMAINS
        domain: Optional domain override (defaults to INTERNAL_DOMAIN from env)
        env: Optional env override (defaults to get_env())

    Returns:
        Full HTTPS URL for the service
    """
    e = env or get_env()
    if domain is None:
        domain = e.get("INTERNAL_DOMAIN")
    if not domain:
        raise ValueError("INTERNAL_DOMAIN not set")

    subdomain = SERVICE_SUBDOMAINS.get(service)
    if not subdomain:
        raise ValueError(f"Unknown service: {service}")
    return f"https://{_build_domain(subdomain, e.get('ENV', 'production'), domain)}"


def validate_env() -> list[str]:
    """Return list of missing required env vars"""
    env = get_env()
    required = ["VPS_HOST", "INTERNAL_DOMAIN"]
    return [k for k in required if not env.get(k)]


def with_env_suffix(name: str, env: dict | None = None) -> str:
    """Append ENV_SUFFIX to a base name."""
    e = env or get_env()
    suffix = e.get("ENV_SUFFIX", "")
    return f"{name}{suffix}" if suffix else name


def service_domain(subdomain: str, env: dict | None = None) -> str:
    """Build public domain with env suffix ('' for production)."""
    e = env or get_env()
    domain = e.get("INTERNAL_DOMAIN")
    if not subdomain or not domain:
        return ""
    return _build_domain(subdomain, e.get("ENV", "production"), domain)


def check_service(c: "Context", service: str, health_cmd: str) -> dict:
    """Check if a Docker service is healthy."""
    from libs.console import success, error

    env = get_env()
    container = CONTAINERS.get(service, f"platform-{service}")
    container = with_env_suffix(container, env)

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
