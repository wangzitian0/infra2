"""Infra2 Security Secret Supply SSOT."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from libs.secrets_supply import (
    SupplyReport,
    apply as _legacy_apply,
    resolver_for as _legacy_resolver_for,
)

if TYPE_CHECKING:
    pass


def apply_secret_supply(
    service: Any,
    env_name: str,
    *,
    store: Any = None,
    resolver: Any = None,
    restart: Any = None,
    **kwargs: Any,
) -> SupplyReport:
    """S-03: Apply secrets resolution and synchronization for a service."""
    if resolver is None and store is not None:
        resolver = _legacy_resolver_for(service, env_name, store=store)
    return _legacy_apply(
        service, env_name, resolver=resolver, restart=restart, **kwargs
    )


def create_secrets_resolver(
    service: Any,
    env_name: str,
    *,
    store: Any = None,
    **kwargs: Any,
) -> Any:
    """S-04: Construct a secrets resolver for a service and environment."""
    return _legacy_resolver_for(service, env_name, store=store, **kwargs)


__all__ = [
    "SupplyReport",
    "apply_secret_supply",
    "create_secrets_resolver",
]
