"""Backward-compatibility shim — the implementation lives in `libs.security.vault_tokens`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.security.vault_tokens import (
    VaultTokenTarget,
    normalize_selector,
    policy_name,
)

__all__ = [
    "VaultTokenTarget",
    "normalize_selector",
    "policy_name",
]
