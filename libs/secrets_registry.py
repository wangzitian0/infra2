"""Frozen shim: the implementation lives in ``libs.security.registry`` (#955).

Import from ``libs.security.registry`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.security.registry import (
    CACHE_DIR,
    ENVIRONMENTS,
    ROOT,
    SERVICES,
    Service,
    load_manifest,
    lookup,
    manifest_file,
    merged_manifest,
    store_keys,
)

__all__ = [
    "CACHE_DIR",
    "ENVIRONMENTS",
    "ROOT",
    "SERVICES",
    "Service",
    "load_manifest",
    "lookup",
    "manifest_file",
    "merged_manifest",
    "store_keys",
]
