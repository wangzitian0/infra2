"""Backward-compatibility shim — the implementation lives in `libs.core.harness.manifest`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.core.harness.manifest import (
    ALLOWED_CHECKOUTS,
    ALLOWED_CONTRACTS,
    ALLOWED_GOVERNANCE,
    ALLOWED_ROLES,
    ALLOWED_RULES_LAYER,
    CheckResult,
    Finding,
    HarnessManifestError,
    Runner,
    SCHEMA_VERSION,
    check_workspace,
    load_manifest,
    validate_manifest,
)

__all__ = [
    "ALLOWED_CHECKOUTS",
    "ALLOWED_CONTRACTS",
    "ALLOWED_GOVERNANCE",
    "ALLOWED_ROLES",
    "ALLOWED_RULES_LAYER",
    "CheckResult",
    "Finding",
    "HarnessManifestError",
    "Runner",
    "SCHEMA_VERSION",
    "check_workspace",
    "load_manifest",
    "validate_manifest",
]
