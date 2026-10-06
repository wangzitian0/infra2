"""Backward-compatibility shim — the implementation lives in `libs.deploy.app_deploy_request`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.deploy.app_deploy_request import (
    ALLOWED_SENDERS,
    APP_SOURCES,
    DeployPlan,
    FIXED_DEPLOY_TYPES,
    MINIMUM_PRODUCTION_MARKER,
    PREFLIGHT_CANARY_DEPLOY_TYPES,
    make_plan,
    marker_status,
    newest_release_tag,
    parse_request,
    production_marker,
    select_iac_ref,
    validate_request_authority,
    verify_production_evidence,
)

__all__ = [
    "ALLOWED_SENDERS",
    "APP_SOURCES",
    "DeployPlan",
    "FIXED_DEPLOY_TYPES",
    "MINIMUM_PRODUCTION_MARKER",
    "PREFLIGHT_CANARY_DEPLOY_TYPES",
    "make_plan",
    "marker_status",
    "newest_release_tag",
    "parse_request",
    "production_marker",
    "select_iac_ref",
    "validate_request_authority",
    "verify_production_evidence",
]
