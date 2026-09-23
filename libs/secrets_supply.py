"""Backward-compatibility shim — the implementation lives in `libs.security.supply`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.security.supply import (
    ONEPASSWORD_VAULT,
    SupplyReport,
    TransientTransportError,
    apply,
    apply_secret_supply,
    create_secrets_resolver,
    resolver_for,
    retrying_transport,
    vault_backend,
)

__all__ = [
    "ONEPASSWORD_VAULT",
    "SupplyReport",
    "TransientTransportError",
    "apply",
    "apply_secret_supply",
    "create_secrets_resolver",
    "resolver_for",
    "retrying_transport",
    "vault_backend",
]
