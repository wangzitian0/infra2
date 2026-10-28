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
PREVIEW_KINDS = ("branch", "pr", "commit", "tag", "canary")

# GitHub Repository Configuration
GITHUB_OWNER = "wangzitian0"
GITHUB_REPO = "infra2"
GITHUB_BRANCH = "main"

APP_SOURCES: dict[str, str] = {
    "finance_report/app": "wangzitian0/finance_report",
    "truealpha/app": "wangzitian0/truealpha",
}
PROJECT_REPOS: dict[str, str] = {
    service.split("/", 1)[0]: repo for service, repo in APP_SOURCES.items()
}


__all__ = [
    "APP_SOURCES",
    "DEPLOYMENT_ENV_PREVIEW",
    "DEPLOYMENT_ENV_PRODUCTION",
    "DEPLOYMENT_ENV_STAGING",
    "DOCKER_LABEL_PREFIX",
    "GITHUB_BRANCH",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "IDENTITY_SCHEMA_VERSION",
    "MANAGED_BY",
    "PREVIEW",
    "PREVIEW_KINDS",
    "PRODUCTION",
    "PROJECT_REPOS",
    "REPO_ROOT",
    "STAGING",
    "STATEFUL_DEPLOY_ENVIRONMENTS",
]
