"""Frozen shim: the implementation lives in ``libs.core.facets`` (#955).

Import from ``libs.core.facets`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.core.facets import (
    BackupFacet,
    ENV_SUFFIX_PLACEHOLDER,
    Exemption,
    FACET_CLASSES,
    ProbeFacet,
    PublicRouteFacet,
    RestartAfterFacet,
    SecretsFacet,
    SignalFacet,
    StorageFacet,
)

__all__ = [
    "BackupFacet",
    "ENV_SUFFIX_PLACEHOLDER",
    "Exemption",
    "FACET_CLASSES",
    "ProbeFacet",
    "PublicRouteFacet",
    "RestartAfterFacet",
    "SecretsFacet",
    "SignalFacet",
    "StorageFacet",
]
