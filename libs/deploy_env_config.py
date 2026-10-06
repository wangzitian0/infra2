"""Frozen shim: the implementation lives in ``libs.deploy.env_config`` (#955).

Import from ``libs.deploy.env_config`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.deploy.env_config import (
    CANARY_SLOT,
    ComposeTarget,
    ENVIRONMENTS,
    EnvConfig,
    PREVIEW_ENVIRONMENT,
    PREVIEW_KINDS,
    PreviewAlias,
    PreviewReadinessProbe,
    PreviewServiceConfig,
    _PREVIEW_SERVICE_CONFIGS,
    app_compose_env_config,
    bespoke_app_compose_targets,
    cors_allowed_origins,
    env_config,
    otel_env,
    otel_ingest_endpoint,
    preview_alias,
    preview_service_config,
    services_without_prod_compose,
)

__all__ = [
    "CANARY_SLOT",
    "ComposeTarget",
    "ENVIRONMENTS",
    "EnvConfig",
    "PREVIEW_ENVIRONMENT",
    "PREVIEW_KINDS",
    "PreviewAlias",
    "PreviewReadinessProbe",
    "PreviewServiceConfig",
    "_PREVIEW_SERVICE_CONFIGS",
    "app_compose_env_config",
    "bespoke_app_compose_targets",
    "cors_allowed_origins",
    "env_config",
    "otel_env",
    "otel_ingest_endpoint",
    "preview_alias",
    "preview_service_config",
    "services_without_prod_compose",
]
