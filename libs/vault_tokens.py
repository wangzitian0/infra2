"""Vault app-token lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os

from libs.common import normalize_env_name


TOKEN_PERIOD_HOURS = 168
TOKEN_PERIOD = f"{TOKEN_PERIOD_HOURS}h"
ACCESSOR_KV_PREFIX = "secret/bootstrap"


@dataclass(frozen=True)
class VaultTokenTarget:
    project: str
    service: str
    service_dir: str
    project_dir: str
    dokploy_project: str


def normalize_selector(value: str | None, *, label: str) -> str | None:
    """Normalize an optional project/service selector."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if "-" in normalized or "/" in normalized:
        raise ValueError(f"{label} must not include '-' or '/'")
    return normalized


def policy_name(project: str, env: str, service: str) -> str:
    """Return the per-environment Vault policy name for an app token."""
    env_name = normalize_env_name(env)
    return f"{project}-{env_name}-{service}"


def display_name(project: str, env: str, service: str) -> str:
    """Return a human-readable token display name with full ownership."""
    env_name = normalize_env_name(env)
    return f"{project}/{env_name}/{service}"


def accessor_kv_path(project: str, env: str, service: str) -> str:
    """Return the Vault KV v2 CLI path used to track the active accessor."""
    env_name = normalize_env_name(env)
    return f"{ACCESSOR_KV_PREFIX}/{env_name}/vault_token_accessors/{project}/{service}"


def mask_token(token: str) -> str:
    """Mask a Vault token for console output."""
    if not token:
        return "<empty>"
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


def should_show_tokens() -> bool:
    """Whether operator explicitly requested full token output."""
    return os.getenv("VAULT_SHOW_TOKENS") == "1"


def token_for_output(token: str) -> str:
    """Return the token in a form safe for normal logs."""
    return token if should_show_tokens() else mask_token(token)
