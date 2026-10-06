"""Backward-compatibility shim — the implementation lives in `libs.deploy.release_markers`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.deploy.release_markers import (
    MINIMUM_PRODUCTION_MARKER,
    MarkerStatus,
    PRODUCTION_MARKER_PREFIX,
    marker_status,
    newest_release_tag,
    production_marker,
    version_key,
)

__all__ = [
    "MINIMUM_PRODUCTION_MARKER",
    "MarkerStatus",
    "PRODUCTION_MARKER_PREFIX",
    "marker_status",
    "newest_release_tag",
    "production_marker",
    "version_key",
]
