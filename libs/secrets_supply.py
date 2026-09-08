"""The one writer of the deployment secret store (plan PR-E).

Every value a service reads is declared in its manifest with a source class. On every
deployment this module applies that declaration through the infra2-sdk resolver:

- human values are copied from 1Password into Vault (only when different);
- required runtime values that Vault does not hold yet are generated once;
- runtime values flagged ``mirror_to_1password`` are copied back for humans;
- what the store still lacks is reported by name and fails the deploy.

When 1Password cannot be reached the deploy proceeds on what Vault already holds
(#625) and the daily reconcile reports the drift instead. Nobody types into Vault.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from infra2_sdk.secrets import (
    OnePasswordBackend,
    SecretsError,
    SecretsResolver,
    VaultKvBackend,
)

from libs.secrets_registry import Service, merged_manifest

ONEPASSWORD_VAULT = "Infra2"


@dataclass(frozen=True)
class SupplyReport:
    service: str
    env: str
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        parts = [f"{self.service} ({self.env}):"]
        parts.append(f"changed={list(self.changed)}" if self.changed else "no changes")
        if self.missing:
            parts.append(f"MISSING={list(self.missing)}")
        parts.extend(self.notes)
        return " ".join(parts)


def vault_backend(environ: Mapping[str, str] | None = None) -> VaultKvBackend:
    """Vault over VAULT_ADDR + VAULT_TOKEN, or the runner's AppRole (VAULT_ROLE_ID/SECRET_ID)."""
    env = dict(environ or os.environ)
    if not env.get("VAULT_ADDR"):
        domain = env.get("INTERNAL_DOMAIN", "localhost")
        env["VAULT_ADDR"] = f"https://vault.{domain}"
    if not env.get("VAULT_TOKEN") and env.get("VAULT_ROOT_TOKEN"):
        env["VAULT_TOKEN"] = env[
            "VAULT_ROOT_TOKEN"
        ]  # transition alias, removed with #640
    return VaultKvBackend.from_environ(env)


def resolver_for(
    service: Service,
    env: str,
    *,
    store: VaultKvBackend | None = None,
    human: OnePasswordBackend | None = None,
) -> SecretsResolver:
    return SecretsResolver(
        merged_manifest(service),
        project=service.project,
        service=service.service,
        env=env,
        store=store or vault_backend(),
        human=human if human is not None else OnePasswordBackend(ONEPASSWORD_VAULT),
    )


def apply(
    service: Service,
    env: str,
    *,
    resolver: SecretsResolver | None = None,
    restart: Callable[[tuple[str, ...]], None] | None = None,
) -> SupplyReport:
    """sync human → ensure runtime → mirror → reconcile; restart consumers when a value changed."""
    resolver = resolver or resolver_for(service, env)
    changed: set[str] = set()
    notes: list[str] = []
    human_reachable = True
    try:
        human = resolver.sync_human()
        changed.update(human.changed)
        if human.missing:
            notes.append(f"1Password lacks {list(human.missing)}")
    except SecretsError as error:
        human_reachable = False
        notes.append(f"1Password unavailable ({error}); deploying on Vault (#625)")
    runtime = resolver.ensure_runtime()
    changed.update(runtime.changed)
    if human_reachable:
        try:
            mirror = resolver.mirror()
            changed_mirror = mirror.changed
            if changed_mirror:
                notes.append(f"mirrored to 1Password: {list(changed_mirror)}")
        except SecretsError as error:
            notes.append(f"mirror skipped ({error})")
    if not human_reachable:
        resolver.human = None  # reconcile without an expected set
    report = resolver.reconcile()
    if restart is not None and changed:
        restart(tuple(sorted(changed)))
    return SupplyReport(
        service=f"{service.project}/{service.service}",
        env=env,
        changed=tuple(sorted(changed)),
        missing=report.missing,
        notes=tuple(notes),
    )
