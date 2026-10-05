"""Canonical service identity shared by deploy, runtime, telemetry, and alerts.

Compatibility re-export facade. Real domain implementation lives in libs.core.service_identity.
"""

from __future__ import annotations

from libs.core.service_identity import (
    DEPLOYMENT_ENV_PREVIEW,
    DEPLOYMENT_ENV_PRODUCTION,
    DEPLOYMENT_ENV_STAGING,
    DOCKER_LABEL_PREFIX,
    IDENTITY_SCHEMA_VERSION,
    MANAGED_BY,
    STATEFUL_DEPLOY_ENVIRONMENTS,
    ServiceIdentity,
    _canonical_token,
    _optional_token,
    _optional_value,
    is_stateful_deploy_env,
)

__all__ = [
    "DEPLOYMENT_ENV_PREVIEW",
    "DEPLOYMENT_ENV_PRODUCTION",
    "DEPLOYMENT_ENV_STAGING",
    "DOCKER_LABEL_PREFIX",
    "IDENTITY_SCHEMA_VERSION",
    "MANAGED_BY",
    "STATEFUL_DEPLOY_ENVIRONMENTS",
    "ServiceIdentity",
    "_canonical_token",
    "_optional_token",
    "_optional_value",
    "is_stateful_deploy_env",
]
