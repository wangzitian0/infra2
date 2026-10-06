"""Frozen shim: the implementation lives in ``libs.deploy.contract`` (#955).

Import from ``libs.deploy.contract`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.deploy.contract import (
    DEPLOY_TYPES,
    DeployTarget,
    DeployTypeSpec,
    SERVICES,
    ServiceSpec,
    _SHA_RE,
    all_service_keys,
    deploy_type_spec,
    is_tag_only_iac_env,
    make_deploy_target,
    make_target,
    service_spec,
    sub_domain_for,
    validate_deploy_target,
    validate_iac_ref_form,
    validate_ref_form,
)

__all__ = [
    "DEPLOY_TYPES",
    "DeployTarget",
    "DeployTypeSpec",
    "SERVICES",
    "ServiceSpec",
    "_SHA_RE",
    "all_service_keys",
    "deploy_type_spec",
    "is_tag_only_iac_env",
    "make_deploy_target",
    "make_target",
    "service_spec",
    "sub_domain_for",
    "validate_deploy_target",
    "validate_iac_ref_form",
    "validate_ref_form",
]
