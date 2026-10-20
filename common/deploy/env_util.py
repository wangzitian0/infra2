"""Dokploy environment variable parsing and runtime preservation utilities.

Part of common.deploy domain decomposition (#955, #1009).
"""

from __future__ import annotations

from collections.abc import Iterable

# Runtime-only AppRole credentials and the promoted image tag injected into
# Dokploy env out-of-band (by bootstrap/05.vault setup-approle and
# common.deploy.promote.deploy), not present in the git-derived desired env
# from Deployer.compose_env_base. They must survive a Deployer.composing()
# sync that regenerates the env, otherwise vault-agent loses its credentials
# (#257/#259/#369) or app composes lose their pinned IMAGE_TAG (#569).
RUNTIME_ENV_KEYS_TO_PRESERVE: tuple[str, ...] = (
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID",
    "VAULT_ADDR",
    "IMAGE_TAG",
)


def _parse_env_text(env_text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines from .env text format into a dictionary."""
    env: dict[str, str] = {}
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _preserve_runtime_env(
    env_str: str,
    existing_env: str | None,
    keys: Iterable[str] = RUNTIME_ENV_KEYS_TO_PRESERVE,
) -> str:
    """Merge existing runtime keys into desired env string while preserving order."""
    desired = _parse_env_text(env_str)
    existing = _parse_env_text(existing_env or "")
    for key in keys:
        if (key not in desired or not desired[key]) and key in existing:
            desired[key] = existing[key]
    return "\n".join(f"{key}={value}" for key, value in desired.items())


__all__ = [
    "RUNTIME_ENV_KEYS_TO_PRESERVE",
    "_parse_env_text",
    "_preserve_runtime_env",
]
