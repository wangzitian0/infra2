"""Operator helpers for deploy tasks, over ``libs.core.environ``.

The environment, domain and routing helpers live in ``libs.core.environ`` and are
re-exported here for the many task modules that import ``libs.common`` (#955).
``check_service`` stays here: it is an operator task helper that runs a health command
over SSH and prints through ``libs.console``, which domain packages must not import.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from libs.core.environ import (
    CONTAINERS,
    DEPLOYMENT_ENV_PREVIEW,
    DEPLOYMENT_ENV_PRODUCTION,
    DEPLOYMENT_ENV_STAGING,
    DeploymentEnvironment,
    OTEL_INGEST_SUBDOMAIN,
    OTLP_TRACES_PATH,
    SERVICE_SUBDOMAINS,
    SHARED_PLATFORM_SERVICES,
    STATEFUL_DEPLOY_ENVIRONMENTS,
    _BOOTSTRAP_ONLY_SHARED_SERVICES,
    _REGISTRY_BACKED_SHORT_NAMES,
    get_env,
    get_environment,
    get_service_url,
    infra_domain,
    is_stateful_deploy_env,
    normalize_env_name,
    otel_ingest_endpoint,
    reset_env_cache,
    service_domain,
    set_deploy_env,
    validate_env,
    with_env_suffix,
)

if TYPE_CHECKING:
    from invoke import Context

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
    "_BOOTSTRAP_ONLY_SHARED_SERVICES",
    "_REGISTRY_BACKED_SHORT_NAMES",
    "check_service",
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


def check_service(c: "Context", service: str, health_cmd: str) -> dict:
    """Check if a Docker service is healthy.

    Args:
        service: Either a key from CONTAINERS mapping or a full container name (e.g., 'finance_report-postgres')
        health_cmd: Command to run inside container to check health

    Returns:
        dict with is_ready and details keys
    """
    from libs.console import error, success

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
