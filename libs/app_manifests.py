"""Frozen shim: the implementation lives in ``libs.security.app_manifests`` (#955).

Import from ``libs.security.app_manifests`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.security.app_manifests import (
    CACHE_DIR,
    Fetcher,
    GITHUB_OWNER,
    ROOT,
    cached_path,
    ensure_present,
    fetch_missing,
    manifest_file,
    submodule_commit,
)

__all__ = [
    "CACHE_DIR",
    "Fetcher",
    "GITHUB_OWNER",
    "ROOT",
    "cached_path",
    "ensure_present",
    "fetch_missing",
    "manifest_file",
    "submodule_commit",
]
