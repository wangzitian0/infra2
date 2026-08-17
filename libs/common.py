"""
Common utilities for deploy scripts

Simplified: uses libs/env.py for secrets, minimal API surface.
"""

from __future__ import annotations
import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from invoke import Context


# Container name mapping (base names; ENV_SUFFIX appended at runtime when set)
CONTAINERS = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "platform-authentik-server",
    "minio": "platform-minio",
    "clickhouse": "platform-clickhouse",
    "signoz": "platform-signoz",
    "openpanel-api": "platform-openpanel-api",
    "openpanel-dashboard": "platform-openpanel-dashboard",
    "openpanel-ch": "platform-openpanel-ch",
}

# Service subdomain mapping (subdomain prefix -> description)
# These are the canonical subdomains for each service
SERVICE_SUBDOMAINS = {
    # Bootstrap services
    "dokploy": "cloud",  # cloud.{domain}
    "1password": "op",  # op.{domain}
    "vault": "vault",  # vault.{domain}
    "sso": "sso",  # sso.{domain} (Authentik)
    # Platform services
    "signoz": "signoz",  # signoz.{domain}
    "minio_console": "minio",  # minio.{domain} -> Console (9001)
    "minio_api": "s3",  # s3.{domain} -> S3 API (9000)
    "portal": "portal",  # portal.{domain}
}

# Bootstrap-plane services with no deploy.py / no service_registry entry at all —
# installed once, directly, before Dokploy/deploy_v2 exist to deploy anything
# "through" them, so there is no per-env instance and no registry entry to
# derive "shared" from. Genuinely, currently irreducible to a derived fact —
# see infra2#596 for what giving them a real registry entry would take.
_BOOTSTRAP_ONLY_SHARED_SERVICES = frozenset({"vault", "dokploy"})

# SERVICE_SUBDOMAINS short name -> the service_registry key it's backed by, for
# every short name that IS a registered platform service (i.e. not bootstrap-plane).
_REGISTRY_BACKED_SHORT_NAMES = {
    "sso": "platform/authentik",
    "signoz": "platform/signoz",
    "minio_api": "platform/minio",
    "minio_console": "platform/minio",
    "portal": "platform/portal",
}


def SHARED_PLATFORM_SERVICES() -> set[str]:
    """SERVICE_SUBDOMAINS short names that carry NO environment suffix in their
    public URL — one shared endpoint across staging/prod, not a per-env
    deployment. Environment isolation for these is via buckets/databases/paths
    instead of subdomains.

    Derived, not hand-maintained: a short name is shared iff it's a
    bootstrap-plane singleton (_BOOTSTRAP_ONLY_SHARED_SERVICES — no registry
    entry exists to derive this from) OR it maps to a registered service whose
    Deployer declares ``prod_only = True`` (only ever deployed to prod, so
    there is no staging instance to ever need a suffix against).

    This used to be a hand-maintained literal that had silently drifted from
    reality: `sso`/`minio_api`/`minio_console` were hardcoded in even though
    authentik/minio are actually `prod_only=False` — real, separate staging
    instances exist and always have (`sso-staging.zitian.party`,
    `s3-staging.zitian.party`). A function, not a module-level constant, so it
    stays fresh (and importing this module never performs a filesystem scan as
    a side effect) — matches `service_registry.service_attrs()`'s own
    recompute-on-each-call style rather than caching.
    """
    from libs import service_registry

    shared_keys = service_registry.shared_services()
    registry_derived = {
        short_name
        for short_name, key in _REGISTRY_BACKED_SHORT_NAMES.items()
        if key in shared_keys
    }
    return set(_BOOTSTRAP_ONLY_SHARED_SERVICES) | registry_derived


def infra_domain() -> str:
    """The ONE shared control-plane domain — cloud./vault./otel./sso./op./signoz./...
    (SHARED_PLATFORM_SERVICES above) — independent of any per-service app-routing domain.

    finance_report/app and truealpha/app route their own public traffic under different
    domains (Deployer.domain, #550), but the platform behind them is a single shared
    Dokploy instance and must never follow that override — passing an app's own domain
    into a cloud./vault./otel. host build produces a nonexistent hostname with no
    Traefik router or cert (a Cloudflare 526, in truealpha's case; #561).

    Reads INTERNAL_DOMAIN from the environment — the same variable every platform
    Deployer (``e.get("INTERNAL_DOMAIN")``, see ``libs.deploy.deployer``) and CI workflow
    already sets this from, and the same fallback literal already used at every other
    ``INTERNAL_DOMAIN``-reading call site (``tools/reconcile_iac_inputs.py``,
    ``tools/signoz_alert_rule_probe.py``). Deliberately takes NO caller-supplied
    fallback — accepting one reintroduces the exact bug this closes the moment a caller
    passes its own app domain as that fallback.

    The SOLE implementation (a follow-up consolidated a near-duplicate,
    ``tools.deploy_v2``'s ``_dokploy_host_domain``, into this — #561 fixed the Dokploy-
    client-host case only; this closed vault./otel. call sites in promote.py and
    preview.py that #561 didn't cover, and functions like ``preflight_vault_token`` now
    call this internally instead of accepting a caller-supplied ``domain`` at all).
    """
    return os.environ.get("INTERNAL_DOMAIN", "").strip() or "zitian.party"


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
        "VPS_SSH_USER": op.get("VPS_SSH_USER")
        or os.environ.get("VPS_SSH_USER", "root"),
        "INTERNAL_DOMAIN": op.get("INTERNAL_DOMAIN")
        or os.environ.get("INTERNAL_DOMAIN"),
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


def get_service_url(
    service: str, domain: str | None = None, env: dict | None = None
) -> str:
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

    env_name = e.get("ENV", "production")
    if service in SHARED_PLATFORM_SERVICES():
        env_name = "production"

    return f"https://{_build_domain(subdomain, env_name, domain)}"


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


# --------------------------------------------------------------------------- #
# Public browser-OTLP ingest endpoint — ONE source (#368)
# --------------------------------------------------------------------------- #
# The browser frontend exports OTLP traces to a single public Dokploy-managed
# ingest domain (Infra-014). The subdomain, the OTLP HTTP traces path, and the
# way the full endpoint is assembled used to be duplicated across two compose
# files and platform/11.signoz/deploy.py (which had its own literal separate
# from service_domain()). They now live here, once, and every consumer derives
# the endpoint from this single source instead of re-constructing the URL.
#
#   - OTEL_INGEST_SUBDOMAIN — the `otel` subdomain (NOT env-suffixed; the ingest
#     domain is shared across envs, like signoz/sso/minio). SigNoz's deploy.py
#     registers this Dokploy domain and otel_ingest_endpoint() builds the FE URL.
#   - OTLP_TRACES_PATH — the standard OTLP/HTTP traces signal path.
OTEL_INGEST_SUBDOMAIN = "otel"
OTLP_TRACES_PATH = "/v1/traces"


def otel_ingest_endpoint(env: dict | None = None) -> str:
    """Build the public browser-OTLP traces endpoint, once.

    Returns ``https://<otel-subdomain>.<domain>/v1/traces`` (e.g.
    ``https://otel.zitian.party/v1/traces``), or ``""`` when INTERNAL_DOMAIN is
    unset. The ingest is a SINGLE shared instance, so the domain is **never**
    env-suffixed (always ``otel.<domain>``, not ``otel-staging.<domain>``) — built
    directly from INTERNAL_DOMAIN, not via the suffix-applying ``service_domain``.
    This is the SINGLE construction point: compose files consume the injected value
    and deploy.py reuses this instead of a literal.
    """
    domain = (env or {}).get("INTERNAL_DOMAIN")
    if not domain:
        return ""
    return f"https://{OTEL_INGEST_SUBDOMAIN}.{domain}{OTLP_TRACES_PATH}"


def check_service(c: "Context", service: str, health_cmd: str) -> dict:
    """Check if a Docker service is healthy.

    Args:
        service: Either a key from CONTAINERS mapping or a full container name (e.g., 'finance_report-postgres')
        health_cmd: Command to run inside container to check health

    Returns:
        dict with is_ready and details keys
    """
    from libs.console import success, error

    env = get_env()

    if service in CONTAINERS:
        container = CONTAINERS[service]
    elif "-" in service:
        container = service
    else:
        container = f"platform-{service}"

    container = with_env_suffix(container, env)

    # Build the local and remote shells independently.  Health commands often
    # contain their own quotes (python -c + URLs); interpolating them inside one
    # outer single-quoted SSH string silently changes the command and produces a
    # false-unhealthy result.  ``sh -lc`` preserves the intended command while
    # shlex.quote protects both shell boundaries.
    remote_command = shlex.join(["docker", "exec", container, "sh", "-lc", health_cmd])
    command = shlex.join(["ssh", f"root@{env['VPS_HOST']}", remote_command])
    result = c.run(command, warn=True, hide=True)

    if result.ok:
        success(f"{container}: ready")
        return {"is_ready": True, "details": "Healthy"}

    error(f"{container}: not ready")
    return {"is_ready": False, "details": "Unhealthy"}
