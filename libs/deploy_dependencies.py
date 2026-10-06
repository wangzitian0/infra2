"""Frozen shim: the implementation lives in ``libs.deploy.dependencies`` (#955).

Import from ``libs.deploy.dependencies`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.deploy.dependencies import (
    DEFAULT_MANIFEST,
    FanoutDecision,
    SHARED_TREES,
    autodeploy_violations,
    dockerfile_baked_shared_trees,
    explain_fanout,
    extra_dependency_globs,
    fanout_coverage_violations,
    load_dependency_manifest,
    match_changed_services,
    service_key_from_path,
)

__all__ = [
    "DEFAULT_MANIFEST",
    "FanoutDecision",
    "SHARED_TREES",
    "autodeploy_violations",
    "dockerfile_baked_shared_trees",
    "explain_fanout",
    "extra_dependency_globs",
    "fanout_coverage_violations",
    "load_dependency_manifest",
    "match_changed_services",
    "service_key_from_path",
]
