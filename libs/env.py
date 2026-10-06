"""Frozen shim: the implementation lives in ``libs.security.store`` (#955).

Import from ``libs.security.store`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.security.store import (
    CredentialType,
    OpSecrets,
    VaultSecrets,
    generate_password,
    generate_secret_token,
    get_secrets,
    resolve_vault_token,
    vault_address,
    vault_token,
    verify_vault_token,
)

__all__ = [
    "CredentialType",
    "OpSecrets",
    "VaultSecrets",
    "generate_password",
    "generate_secret_token",
    "get_secrets",
    "resolve_vault_token",
    "vault_address",
    "vault_token",
    "verify_vault_token",
]
