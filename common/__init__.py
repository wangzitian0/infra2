"""Infra2 Common Repo-Specific Domain Package (#1270).

Canonical home for repo-maintained domain logic shared between ``platform/`` and ``tools/``:
- ``common.constants``: Repo domain constants and stateful environment helpers.
- ``common.environ``: Repo environment resolution, container names, and service URLs.
- ``common.facets``: Typed per-service operational facet declarations.
- ``common.registry``: AST-derived service registry from ``deploy.py`` classes.
- ``common.service_identity``: Cross-plane service identity model.
"""

from __future__ import annotations

from common.constants import (
    DEPLOYMENT_ENV_PREVIEW,
    DEPLOYMENT_ENV_PRODUCTION,
    DEPLOYMENT_ENV_STAGING,
    DOCKER_LABEL_PREFIX,
    IDENTITY_SCHEMA_VERSION,
    MANAGED_BY,
    PREVIEW,
    PRODUCTION,
    REPO_ROOT,
    STAGING,
    STATEFUL_DEPLOY_ENVIRONMENTS,
)
from common.environ import (
    DeploymentEnvironment,
    get_environment,
    with_env_suffix,
)

__all__ = [
    "DEPLOYMENT_ENV_PREVIEW",
    "DEPLOYMENT_ENV_PRODUCTION",
    "DEPLOYMENT_ENV_STAGING",
    "DOCKER_LABEL_PREFIX",
    "DeploymentEnvironment",
    "IDENTITY_SCHEMA_VERSION",
    "MANAGED_BY",
    "PREVIEW",
    "PRODUCTION",
    "REPO_ROOT",
    "STAGING",
    "STATEFUL_DEPLOY_ENVIRONMENTS",
    "get_environment",
    "with_env_suffix",
]
