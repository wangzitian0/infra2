"""Infra2 Core Domain Constants (SSOT)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCTION = "production"
STAGING = "staging"
PREVIEW = "preview"

DEPLOYMENT_ENV_PRODUCTION = PRODUCTION
DEPLOYMENT_ENV_STAGING = STAGING
DEPLOYMENT_ENV_PREVIEW = PREVIEW
STATEFUL_DEPLOY_ENVIRONMENTS = (
    DEPLOYMENT_ENV_PREVIEW,
    DEPLOYMENT_ENV_STAGING,
    DEPLOYMENT_ENV_PRODUCTION,
)

MANAGED_BY = "infra2"
DOCKER_LABEL_PREFIX = "party.zitian.infra"
IDENTITY_SCHEMA_VERSION = "v1"


def is_stateful_deploy_env(env: str | None, *, strict: bool = False) -> bool:
    """Return True if env represents one of the 3 stateful deploy environments.

    If strict=True, requires the normalized tier to be exactly 'preview', 'staging', or 'production'.
    If strict=False (default), also recognizes dynamic Dokploy preview instances
    following the SSOT preview alias model ('pr-<N>', 'commit-<sha>', 'branch-<name>', 'tag-<v>', or 'preview-*').
    """
    if not env or not isinstance(env, str) or not env.strip():
        return False
    val = env.strip().lower()
    if val in ("prod", "production", "staging", "stg", "preview"):
        return True
    if not strict and (
        val.startswith("pr-")
        or val.startswith("commit-")
        or val.startswith("branch-")
        or val.startswith("tag-")
        or val.startswith("preview-")
    ):
        return True
    return False


__all__ = [
    "DEPLOYMENT_ENV_PREVIEW",
    "DEPLOYMENT_ENV_PRODUCTION",
    "DEPLOYMENT_ENV_STAGING",
    "DOCKER_LABEL_PREFIX",
    "IDENTITY_SCHEMA_VERSION",
    "MANAGED_BY",
    "PREVIEW",
    "PRODUCTION",
    "REPO_ROOT",
    "STAGING",
    "STATEFUL_DEPLOY_ENVIRONMENTS",
    "is_stateful_deploy_env",
]
