"""Frozen shim: the implementation lives in ``libs.deploy.dokploy_client`` (#955).

Import from ``libs.deploy.dokploy_client`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.deploy.dokploy_client import (
    DokployClient,
    ensure_project,
    get_dokploy,
    httpx,
    time,
)

__all__ = [
    "DokployClient",
    "ensure_project",
    "get_dokploy",
    "httpx",
    "time",
]
