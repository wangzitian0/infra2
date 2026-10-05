"""Dokploy environment variable parsing and runtime preservation utilities.

Part of libs.deploy domain decomposition (#955, #1009).
"""

from __future__ import annotations

from collections.abc import Iterable

# Runtime-only AppRole credentials injected into Dokploy env out-of-band (by
# bootstrap/05.vault setup-approle), not present in the git-derived desired env.
# They must survive a redeploy that regenerates the env, otherwise the
# vault-agent loses its credentials and crash-loops (#257/#259/#369).
RUNTIME_ENV_KEYS_TO_PRESERVE: tuple[str, ...] = (
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID",
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
        if key not in desired and key in existing:
            desired[key] = existing[key]
    return "\n".join(f"{key}={value}" for key, value in desired.items())


__all__ = [
    "RUNTIME_ENV_KEYS_TO_PRESERVE",
    "_parse_env_text",
    "_preserve_runtime_env",
]
