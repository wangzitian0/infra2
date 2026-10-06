"""Frozen shim: the implementation lives in ``libs.core.registry`` (#955).

Import from ``libs.core.registry`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.core.registry import (
    DOKPLOY_IDENTITY_MAP_PATH,
    PRODUCTION,
    ServiceMeta,
    _BOOTSTRAP_COMPOSE_IDS,
    _EXTERNAL_COMPONENT_IDS,
    _LAYERS,
    _facet_seq,
    all_services,
    bootstrap_facet_attrs,
    dokploy_identity_map,
    domain_for_service,
    probe_container_bases,
    resolve_container_host,
    restart_after_containers,
    service_attrs,
    service_id_for_component,
    service_id_for_dokploy,
    service_identity,
    services_in_env,
    shared_services,
    subdomains,
)

__all__ = [
    "DOKPLOY_IDENTITY_MAP_PATH",
    "PRODUCTION",
    "ServiceMeta",
    "_BOOTSTRAP_COMPOSE_IDS",
    "_EXTERNAL_COMPONENT_IDS",
    "_LAYERS",
    "_facet_seq",
    "all_services",
    "bootstrap_facet_attrs",
    "dokploy_identity_map",
    "domain_for_service",
    "probe_container_bases",
    "resolve_container_host",
    "restart_after_containers",
    "service_attrs",
    "service_id_for_component",
    "service_id_for_dokploy",
    "service_identity",
    "services_in_env",
    "shared_services",
    "subdomains",
]
