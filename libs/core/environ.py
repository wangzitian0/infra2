"""Infra2 core environment and configuration (SSOT).

The deployment environment of this process: its name, suffixes and domains
(``get_env``), the public host of each shared platform service, and the typed
``DeploymentEnvironment`` view. ``libs.common`` is a frozen shim over this module
(#955 moved the implementation here, which removed the ``common`` <-> ``core.environ``
import cycle).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from libs.core.constants import (
    DEPLOYMENT_ENV_PREVIEW,
    DEPLOYMENT_ENV_PRODUCTION,
    DEPLOYMENT_ENV_STAGING,
    PRODUCTION,
    STAGING,
    STATEFUL_DEPLOY_ENVIRONMENTS,
    is_stateful_deploy_env,
)

__all__ = [
    "CONTAINERS",
    "DEPLOYMENT_ENV_PREVIEW",
    "DEPLOYMENT_ENV_PRODUCTION",
    "DEPLOYMENT_ENV_STAGING",
    "DeploymentEnvironment",
    "OTEL_INGEST_SUBDOMAIN",
    "OTLP_TRACES_PATH",
    "SERVICE_SUBDOMAINS",
    "SHARED_PLATFORM_SERVICES",
    "STATEFUL_DEPLOY_ENVIRONMENTS",
    "get_env",
    "get_environment",
    "get_service_url",
    "infra_domain",
    "is_stateful_deploy_env",
    "normalize_env_name",
    "otel_ingest_endpoint",
    "reset_env_cache",
    "service_domain",
    "set_deploy_env",
    "validate_env",
    "with_env_suffix",
]

# Container name mapping (base names; ENV_SUFFIX appended at runtime when set)
CONTAINERS = {
    "postgres": "platform-postgres",
    "redis": "platform-redis",
    "authentik": "platform-authentik-server",
    "s3": "platform-s3",
    "minio": "platform-minio",
    "clickhouse": "platform-clickhouse",
    "signoz": "platform-signoz",
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
    "s3": "s3",  # s3.{domain} -> S3 API (9000)
    "s3_console": "s3-console",  # s3-console.{domain} -> Console (9001)
    "minio_console": "minio",  # minio.{domain} -> Console (9001, legacy)
    "minio_api": "s3",  # s3.{domain} -> S3 API (9000, legacy)
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
    "s3": "platform/s3",
    "s3_console": "platform/s3",
    "minio_api": "platform/s3",
    "minio_console": "platform/s3",
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
    from libs.core import registry

    shared_keys = registry.shared_services()
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


def reset_env_cache() -> None:
    """Forget the memoized deployment config (after DEPLOY_ENV changes in-process)."""
    global _env_cache
    _env_cache = None


def set_deploy_env(env_name: str) -> None:
    """Point this process at ``env_name`` the way the iac-runner does for its children:
    INFRA_ENVIRONMENT and DEPLOY_ENV are synchronized; ENV_SUFFIX / ENV_DOMAIN_SUFFIX follow
    from it (staging → ``-staging``), and the memoized config is dropped."""
    name = normalize_env_name(env_name)
    suffix = "" if name == "production" else f"-{name.replace('_', '-')}"
    os.environ["INFRA_ENVIRONMENT"] = name
    os.environ["DEPLOY_ENV"] = name
    os.environ["ENV_SUFFIX"] = suffix
    os.environ["ENV_DOMAIN_SUFFIX"] = suffix
    reset_env_cache()


def get_env() -> dict[str, str | None]:
    """Get deployment environment config.

    Sources: 1Password init/env_vars → os.environ fallback
    """
    global _env_cache
    if _env_cache is not None:
        return _env_cache

    from libs.security.store import OpSecrets

    op = OpSecrets()

    raw_env = (
        os.environ.get("INFRA_ENVIRONMENT")
        or os.environ.get("DEPLOY_ENV")
        or "production"
    )
    env_name = normalize_env_name(raw_env)
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
        "ENV_SUFFIX": os.environ.get("ENV_SUFFIX") or env_domain_suffix,
        "DATA_PATH": os.environ.get("DATA_PATH"),
    }
    return _env_cache


def get_service_url(
    service: str, domain: str | None = None, env: dict | None = None
) -> str:
    """Get full HTTPS URL for a service via infra2_sdk.routing.

    Args:
        service: Service key from SERVICE_SUBDOMAINS
        domain: Optional domain override (defaults to INTERNAL_DOMAIN from env)
        env: Optional env override (defaults to get_env())

    Returns:
        Full HTTPS URL for the service (no trailing slash)
    """
    e = env or get_env()
    if domain is None:
        domain = e.get("INTERNAL_DOMAIN")
    if not domain:
        raise ValueError("INTERNAL_DOMAIN not set")

    subdomain = SERVICE_SUBDOMAINS.get(service)
    if not subdomain:
        raise ValueError(f"Unknown service: {service}")

    norm = normalize_env_name(e.get("ENV"))
    if service in SHARED_PLATFORM_SERVICES():
        tier = "production"
    elif norm in ("production", "staging"):
        tier = norm
    else:
        tier = "preview"

    from infra2_sdk.routing import resolve_service_url

    return resolve_service_url(subdomain, tier=tier, base_domain=domain).rstrip("/")


def validate_env() -> list[str]:
    """Return list of missing required env vars"""
    env = get_env()
    required = ["VPS_HOST", "INTERNAL_DOMAIN"]
    return [k for k in required if not env.get(k)]


def service_domain(subdomain: str, env: dict | None = None) -> str:
    """Build public domain with env suffix ('' for production) via infra2_sdk.routing."""
    e = env or get_env()
    domain = e.get("INTERNAL_DOMAIN")
    if not subdomain or not domain:
        return ""
    norm = normalize_env_name(e.get("ENV"))
    tier = norm if norm in ("production", "staging") else "preview"
    from infra2_sdk.routing import resolve_app_hostname

    return resolve_app_hostname(subdomain, tier=tier, base_domain=domain)


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


def normalize_env_name(value: str | None) -> str:
    """Normalize environment name for consistent behavior."""
    if not value or not value.strip():
        return "production"
    val = value.strip().lower()
    if val in ("prod", "production"):
        return "production"
    if val in ("stg", "staging"):
        return "staging"
    if val in ("preview", "preview_env", "preview-env"):
        return "preview"
    if val in ("canary", "canary-preview", "canary_preview"):
        return "canary_preview"
    if val.startswith("pr-") or val.startswith("pr_"):
        return val.replace("-", "_")
    if val.startswith("preview-") or val.startswith("preview_"):
        return val.replace("-", "_")
    if val.startswith("branch-") or val.startswith("branch_"):
        return val.replace("-", "_")
    if val.startswith("commit-") or val.startswith("commit_"):
        return val.replace("-", "_")
    if val.startswith("tag-") or val.startswith("tag_"):
        return val.replace("-", "_")
    if "/" in val or "\\" in val or " " in val or "\t" in val or "\n" in val:
        raise ValueError("ENV name must not include '/' or whitespace")
    return val.replace("-", "_")


@dataclass(frozen=True)
class DeploymentEnvironment:
    """Strong-typed operational deployment environment entity (C-03)."""

    name: str = STAGING
    env_suffix: str = "-staging"
    internal_domain: str = "zitian.party"
    raw_env: Mapping[str, str | None] | None = None

    @property
    def is_production(self) -> bool:
        return self.name == PRODUCTION

    @property
    def is_staging(self) -> bool:
        return self.name == STAGING

    def get(self, key: str, default: str | None = None) -> str | None:
        if self.raw_env is not None and key in self.raw_env:
            val = self.raw_env[key]
            return val if val is not None else default
        return default

    def __getitem__(self, key: str) -> str | None:
        return self.get(key)


def get_environment() -> DeploymentEnvironment:
    """C-03: Retrieve current DeploymentEnvironment as a typed domain object."""
    raw = get_env()
    env_name = raw.get("ENV") or STAGING
    env_suffix = raw.get("ENV_SUFFIX")
    if env_suffix is None:
        env_suffix = "" if env_name == PRODUCTION else f"-{env_name}"
    internal_domain = raw.get("INTERNAL_DOMAIN") or "zitian.party"
    return DeploymentEnvironment(
        name=env_name,
        env_suffix=env_suffix,
        internal_domain=internal_domain,
        raw_env=raw,
    )


def with_env_suffix(
    name: str, env: DeploymentEnvironment | Mapping[str, str | None] | None = None
) -> str:
    """C-04: Append the environment suffix to a service or container base name.

    ``env`` is a ``DeploymentEnvironment``, a ``get_env()``-shaped mapping, or ``None``
    for the current process environment.
    """
    if isinstance(env, DeploymentEnvironment):
        suffix = env.env_suffix
    else:
        suffix = (env or get_env()).get("ENV_SUFFIX", "") or ""
    return f"{name}{suffix}" if suffix else name
