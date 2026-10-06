"""Infra2 Core Domain Package."""

from __future__ import annotations

from libs.core.constants import (
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
from libs.core.environ import (
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
