"""Infra2 Unified Service Domain Entity (SSOT)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from libs.core.constants import REPO_ROOT

if TYPE_CHECKING:
    from libs.service_registry import ServiceMeta


@dataclass(frozen=True)
class Service:
    """Infra2 全局唯一的微服务实体 (Unified Service Domain Entity).

    Consolidates the four legacy representations:
    1. libs.service_registry.ServiceMeta
    2. libs.service_identity.ServiceIdentity
    3. libs.secrets_registry.Service
    4. libs.deploy_contract.ServiceSpec
    """

    id: str  # e.g. "platform/postgres", "truealpha/app"
    project: str  # e.g. "platform", "truealpha", "finance_report"
    service: str  # e.g. "postgres", "app"
    directory: Path  # Root directory of the service

    facets: tuple[Any, ...] = ()
    manifest_paths: tuple[Path, ...] = ()
    store_only_keys: tuple[str, ...] = ()
    companions: tuple[str, ...] = ()
    meta: Any = None  # Reference to underlying ServiceMeta if loaded via Deployer

    @property
    def container_name(self) -> str:
        """派生标准容器名，取代 common.py 中的硬编码字典 (C-05)."""
        if self.meta and getattr(self.meta, "service_name", None):
            name = self.meta.service_name
            if self.project != "platform":
                return f"{self.project}-{name}"
            return f"platform-{name}"
        return f"{self.project}-{self.service}"

    @property
    def identity(self) -> str:
        return f"{self.project}/{self.service}"

    @property
    def prod_only(self) -> bool:
        return bool(self.meta and getattr(self.meta, "prod_only", False))

    @property
    def subdomain(self) -> str | None:
        return getattr(self.meta, "subdomain", None) if self.meta else None

    @property
    def domain(self) -> str | None:
        return getattr(self.meta, "domain", None) if self.meta else None

    @property
    def storage(self) -> tuple[Any, ...]:
        return getattr(self.meta, "storage", ()) if self.meta else ()

    @property
    def probes(self) -> tuple[Any, ...]:
        return getattr(self.meta, "probes", ()) if self.meta else ()

    @property
    def secrets(self) -> tuple[Any, ...]:
        return getattr(self.meta, "secrets", ()) if self.meta else ()

    @property
    def signals(self) -> tuple[Any, ...]:
        return getattr(self.meta, "signals", ()) if self.meta else ()

    @property
    def backups(self) -> tuple[Any, ...]:
        return getattr(self.meta, "backups", ()) if self.meta else ()


def _service_from_meta(service_id: str, meta: ServiceMeta) -> Service:
    """Construct a Service domain entity from a ServiceMeta."""
    project = meta.project or service_id.split("/", 1)[0]
    service_name = meta.service or service_id.split("/", 1)[1]
    
    # Resolve directory
    if meta.compose_path:
        directory = (REPO_ROOT / meta.compose_path).parent
    else:
        directory = REPO_ROOT / service_id

    facets = (
        *meta.probes,
        *meta.public_routes,
        *meta.signals,
        *meta.backups,
        *meta.secrets,
        *meta.storage,
        *meta.restart_after,
        *meta.exemptions,
    )

    return Service(
        id=service_id,
        project=project,
        service=service_name,
        directory=directory,
        facets=facets,
        meta=meta,
    )


def load_service_registry() -> Mapping[str, Service]:
    """C-01: Load all services as unified Service domain entities."""
    from libs import service_registry

    raw_metas = service_registry.service_attrs()
    return {
        service_id: _service_from_meta(service_id, meta)
        for service_id, meta in raw_metas.items()
    }


def get_service(service_id_or_parts: str | tuple[str, str]) -> Service | None:
    """C-02: Get service by canonical ID or (project, service) tuple."""
    if isinstance(service_id_or_parts, tuple):
        service_id = f"{service_id_or_parts[0]}/{service_id_or_parts[1]}"
    else:
        service_id = str(service_id_or_parts)

    registry = load_service_registry()
    return registry.get(service_id)


__all__ = [
    "Service",
    "get_service",
    "load_service_registry",
]
