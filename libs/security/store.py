"""Infra2 Security Secret Store SSOT."""

from __future__ import annotations

import os
import secrets as _secrets
import string
from collections.abc import Mapping
from typing import TYPE_CHECKING

from libs.env import VaultSecrets

if TYPE_CHECKING:
    pass


def generate_secret_token(
    length: int = 24, *, alphabet: str | None = None
) -> str:
    """S-01: Generate cryptographically secure random token."""
    charset = alphabet or (string.ascii_letters + string.digits)
    return "".join(_secrets.choice(charset) for _ in range(length))


def resolve_vault_token(env: Mapping[str, str] | None = None) -> str | None:
    """S-02: Resolve Vault token from environment (VAULT_TOKEN or VAULT_ROOT_TOKEN)."""
    environ = os.environ if env is None else env
    return environ.get("VAULT_TOKEN") or environ.get("VAULT_ROOT_TOKEN") or None


__all__ = [
    "VaultSecrets",
    "generate_secret_token",
    "resolve_vault_token",
]
