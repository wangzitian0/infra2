"""Infra2 Security Secret Pruning SSOT."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


def prune_orphan_secrets(
    services: Any = None,
    *,
    store: Any = None,
    dry_run: bool = True,
    environments: tuple[str, ...] | None = None,
    **kwargs: Any,
) -> Any:
    """S-05: Prune orphan secrets from Vault with safe dry_run default."""
    from tools.secrets_prune import SERVICES, prune as _legacy_prune

    target_services = SERVICES if services is None else services
    return _legacy_prune(
        services=target_services,
        environments=environments,
        apply=not dry_run,
        store=store,
        **kwargs,
    )


__all__ = [
    "prune_orphan_secrets",
]
