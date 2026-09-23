"""Infra2 Security Secret Pruning SSOT."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infra2_sdk.secrets import SecretsError, vault_path

from libs.secrets_registry import SERVICES, Service, store_keys
from libs.security.supply import vault_backend

# infra2_sdk.runtime.config_schema.reconcile ignores these; so must the prune.
RESERVED_PREFIXES = ("_",)


@dataclass
class PrunePlan:
    """What one ``project/env/service`` path holds that nothing reads."""

    service: str
    env: str
    path: str
    keep: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()
    error: str = ""
    applied: bool = False

    @property
    def clean(self) -> bool:
        return not self.orphans and not self.error

    def line(self) -> str:
        if self.error:
            return f"  {self.service} {self.env}: unreadable ({self.error})"
        verb = "removed" if self.applied else "orphans"
        detail = f"{verb}={list(self.orphans)}" if self.orphans else "clean"
        empty = " (path now holds nothing)" if self.applied and not self.keep else ""
        return f"  {self.service} {self.env}: {detail}{empty}"


@dataclass
class PruneReport:
    plans: list[PrunePlan] = field(default_factory=list)

    @property
    def orphan_count(self) -> int:
        return sum(len(plan.orphans) for plan in self.plans)

    def render(self) -> str:
        dirty = [plan for plan in self.plans if not plan.clean]
        head = (
            f"secrets prune: {len(self.plans) - len(dirty)} clean, {len(dirty)} with "
            f"orphans, {self.orphan_count} key(s) total"
        )
        return "\n".join([head, *(plan.line() for plan in dirty)])

    def to_dict(self) -> dict[str, object]:
        return {
            "orphan_count": self.orphan_count,
            "paths": [
                {
                    "service": plan.service,
                    "env": plan.env,
                    "path": plan.path,
                    "orphans": list(plan.orphans),
                    "kept": len(plan.keep),
                    "applied": plan.applied,
                    **({"error": plan.error} if plan.error else {}),
                }
                for plan in self.plans
            ],
        }


def plan_for(
    service: Service, env: str, *, store, root: Path | None = None
) -> PrunePlan:
    """Read one path and split it into what may stay and what nothing reads."""
    path = vault_path(service.project, env, service.service)
    allowed = store_keys(service, root=root) if root else store_keys(service)
    try:
        held = dict(store.read(path))
    except SecretsError as error:
        return PrunePlan(service.id, env, path, error=str(error))

    def kept(key: str) -> bool:
        # RESERVED_PREFIXES mirrors the SDK reconcile's ignore list: a key a probe or an
        # operator parks outside the manifest on purpose is not an orphan, and a prune
        # that deleted it would quietly disagree with the report that found it.
        return key in allowed or key.startswith(RESERVED_PREFIXES)

    keep = {key: value for key, value in held.items() if kept(key)}
    orphans = tuple(sorted(key for key in held if not kept(key)))
    return PrunePlan(service.id, env, path, tuple(sorted(keep)), orphans)


def prune(
    *,
    services: Iterable[Service] = SERVICES,
    environments: tuple[str, ...] | None = None,
    apply: bool = False,
    store=None,
    root: Path | None = None,
) -> PruneReport:
    store = store or vault_backend()
    report = PruneReport()
    for service in services:
        if service.preview:
            continue  # a preview reads another environment's path; it owns none
        for env in environments or service.environments:
            plan = plan_for(service, env, store=store, root=root)
            if apply and plan.orphans:
                held = dict(store.read(plan.path))
                try:
                    store.replace(
                        plan.path, {k: v for k, v in held.items() if k in plan.keep}
                    )
                    plan.applied = True
                except SecretsError as error:
                    plan.error = f"write refused: {error}"
            report.plans.append(plan)
    return report


def prune_orphan_secrets(
    services: Any = None,
    *,
    store: Any = None,
    dry_run: bool = True,
    environments: tuple[str, ...] | None = None,
    **kwargs: Any,
) -> PruneReport:
    """S-05: Prune orphan secrets from Vault with safe dry_run default."""
    target_services = SERVICES if services is None else services
    return prune(
        services=target_services,
        environments=environments,
        apply=not dry_run,
        store=store,
        **kwargs,
    )


__all__ = [
    "RESERVED_PREFIXES",
    "PrunePlan",
    "PruneReport",
    "plan_for",
    "prune",
    "prune_orphan_secrets",
]
