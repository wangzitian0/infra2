"""Backward-compatibility shim — the implementation lives in `libs.backup.verification`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.backup.verification import (
    INVENTORY_DEFAULTS,
    REPO_ROOT,
    BackupCheck,
    BackupEntry,
    BackupManifestError,
    _parse_timestamp,
    build_backup_alert_payload,
    discover_deployer_data_paths,
    inventory_data_paths,
    load_backup_inventory,
    verify_backup_manifest,
)

__all__ = [
    "BackupCheck",
    "BackupEntry",
    "BackupManifestError",
    "INVENTORY_DEFAULTS",
    "REPO_ROOT",
    # `_parse_timestamp` is private to the domain but was already importable from this
    # path before the move; the shim's job is that no old import stops resolving.
    "_parse_timestamp",
    "build_backup_alert_payload",
    "discover_deployer_data_paths",
    "inventory_data_paths",
    "load_backup_inventory",
    "verify_backup_manifest",
]
