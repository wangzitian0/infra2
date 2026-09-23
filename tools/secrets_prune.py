#!/usr/bin/env python3
"""Remove from each service's Vault path what nothing reads (store hygiene, #649).

A service's store may hold exactly three kinds of value: what its Vault Agent template
renders into the container (the manifest's store-backed fields), what an operator task
reads directly (``Service.store_only_keys`` in libs/secrets_registry.py, each entry
naming its reader), and what a probe or an operator parks under a reserved prefix
(``RESERVED_PREFIXES`` below — the same names the SDK's reconcile ignores), which this
tool keeps and never counts as an orphan. Anything else is an orphan — a value written
by a retired code path, or a configuration key that moved into the compose file — and
it is dangerous precisely because it looks authoritative:
platform/production/finance_report kept a PRIMARY_MODEL the app stopped reading, and the
day the template stopped rendering it, a stale Dokploy copy surfaced instead (#649).

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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.secrets_registry import SERVICES  # noqa: E402
from libs.security.prune import (  # noqa: E402
    RESERVED_PREFIXES,
    PrunePlan,
    PruneReport,
    plan_for,
    prune,
)

__all__ = [
    "RESERVED_PREFIXES",
    "SERVICES",
    "PrunePlan",
    "PruneReport",
    "plan_for",
    "prune",
]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for secrets prune."""
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
