#!/usr/bin/env python3
"""Remove from each service's Vault path what nothing reads (store hygiene, #649).

A service's store may hold exactly two kinds of value: what its Vault Agent template
renders into the container (the manifest's store-backed fields) and what an operator
task reads directly (``Service.store_only_keys`` in libs/secrets_registry.py, each entry
naming its reader). Anything else is an orphan — a value written by a retired code path,
or a configuration key that moved into the compose file — and it is dangerous precisely
because it looks authoritative: platform/production/finance_report kept a PRIMARY_MODEL
the app stopped reading, and the day the template stopped rendering it, a stale Dokploy
copy surfaced instead (#649).

    python3 tools/secrets_prune.py                         # dry run over every service
    python3 tools/secrets_prune.py --service platform/alerting --env staging
    python3 tools/secrets_prune.py --apply                 # rewrite the documents

Names only, never values. ``--apply`` rewrites each document without the orphan keys
(KV v2 has no per-key delete); the previous version stays in Vault's history, so a prune
is reversible with ``vault kv rollback``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # runnable as a script

from infra2_sdk.secrets import SecretsError, vault_path  # noqa: E402

from libs.secrets_registry import SERVICES, Service, store_keys  # noqa: E402
from libs.secrets_supply import vault_backend  # noqa: E402

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", help="one service id, e.g. platform/alerting")
    parser.add_argument("--env", help="one environment, e.g. staging")
    parser.add_argument("--apply", action="store_true", help="rewrite the documents")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    services = tuple(SERVICES)
    if args.service:
        services = tuple(s for s in services if s.id == args.service)
        if not services:
            parser.error(f"unknown service {args.service!r}")
    environments = (args.env,) if args.env else None
    report = prune(services=services, environments=environments, apply=args.apply)
    print(json.dumps(report.to_dict(), indent=1) if args.json else report.render())
    failed = [plan for plan in report.plans if plan.error]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
