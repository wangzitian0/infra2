#!/usr/bin/env python3
"""Daily reconcile: is every store what its manifest says, and is every quota in budget?

For each registered service (libs/secrets_registry.py) and each environment it deploys
to, the infra2-sdk resolver compares the Vault path with the manifest — missing, empty,
unclassified keys — and, when 1Password answers, with the human-entered values (stale).
Capacity readings for the Cloudflare free tier behind the heartbeat worker are
evaluated against their limits (#616 flapping ate the KV write budget). Names and
counts only, never values.

    python3 tools/secrets_reconcile.py            # inside the iac-runner (Vault + op reachable)
    python3 tools/secrets_reconcile.py --json     # machine-readable, for the ops-check job

Exit 1 when anything is missing, empty, unclassified, stale, or over budget.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # runnable as a script

from infra2_sdk.capacity import (  # noqa: E402
    CLOUDFLARE_FREE_TIER,
    CapacityReport,
    cloudflare_readings,
    evaluate,
)
from infra2_sdk.secrets import OnePasswordBackend, SecretsError  # noqa: E402

from libs.secrets_registry import SERVICES, Service  # noqa: E402
from libs.secrets_supply import ONEPASSWORD_VAULT, resolver_for, vault_backend  # noqa: E402

# cloudflare/infra-watchdog/wrangler.toml; the token is the worker's own API token.
CLOUDFLARE_ACCOUNT = "6e27b6853de0131bea033d235e0a1139"
CLOUDFLARE_TOKEN_ITEM = ("bootstrap/cloudflare-worker", "CLOUDFLARE_WORKER_API_TOKEN")
FINDING_KEYS = ("missing", "empty", "unclassified", "stale")


def reconcile_stores(
    *,
    services: Iterable[Service] = SERVICES,
    environments: tuple[str, ...] | None = None,
    store=None,
    human=None,
) -> list[dict[str, object]]:
    """One row per fixed service × environment; previews read another environment."""
    store = store or vault_backend()
    human = human if human is not None else OnePasswordBackend(ONEPASSWORD_VAULT)
    rows: list[dict[str, object]] = []
    for service in services:
        if service.preview:
            continue
        for env in environments or service.environments:
            resolver = resolver_for(service, env, store=store, human=human)
            note = ""
            try:
                report = resolver.reconcile()
            except SecretsError as error:
                resolver.human = None
                report = resolver.reconcile()
                note = f"1Password unavailable: {error}"
            row: dict[str, object] = {
                "service": service.id,
                "env": env,
                **report.to_dict(),
                "ok": report.ok,
            }
            if note:
                row["note"] = note
            rows.append(row)
    return rows


def capacity_report(
    *,
    human=None,
    day: dt.date | None = None,
    readings: Callable[[], object] | None = None,
) -> CapacityReport:
    """Yesterday's Cloudflare usage against the free tier; the token comes from 1Password."""
    if readings is None:
        human = human if human is not None else OnePasswordBackend(ONEPASSWORD_VAULT)
        token = human.read(CLOUDFLARE_TOKEN_ITEM[0]).get(CLOUDFLARE_TOKEN_ITEM[1], "")
        if not token:
            raise SecretsError(
                f"{CLOUDFLARE_TOKEN_ITEM[0]} lacks {CLOUDFLARE_TOKEN_ITEM[1]}"
            )
        day = day or (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1))

        def readings():
            return cloudflare_readings(account=CLOUDFLARE_ACCOUNT, token=token, day=day)

    return evaluate(CLOUDFLARE_FREE_TIER, readings())


def findings(row: dict[str, object]) -> str:
    return ", ".join(f"{key}={row[key]}" for key in FINDING_KEYS if row.get(key))


def render(
    rows: list[dict[str, object]],
    capacity: CapacityReport | None,
    capacity_error: str | None = None,
) -> str:
    bad = [row for row in rows if not row["ok"]]
    lines = [f"secrets reconcile: {len(rows) - len(bad)} ok, {len(bad)} with findings"]
    for row in bad:
        suffix = f" ({row['note']})" if row.get("note") else ""
        lines.append(f"  {row['service']} {row['env']}: {findings(row)}{suffix}")
    if capacity_error:
        lines.append(f"capacity: unavailable ({capacity_error})")
    elif capacity is not None:
        hot = capacity.at_level("warn", "exceeded")
        lines.append(
            "capacity: ok"
            if not hot
            else "capacity: "
            + ", ".join(
                f"{i.name} {i.used}/{i.limit}/{i.window} {i.level}" for i in hot
            )
        )
    return "\n".join(lines)


def build_report(*, with_capacity: bool = True) -> dict[str, object]:
    rows = reconcile_stores()
    capacity: CapacityReport | None = None
    capacity_error: str | None = None
    if with_capacity:
        try:
            capacity = capacity_report()
        except Exception as error:  # noqa: BLE001 - a quota read must not hide store drift
            capacity_error = str(error)
    exceeded = bool(capacity and capacity.at_level("exceeded"))
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "ok": all(row["ok"] for row in rows) and not exceeded,
        "stores": rows,
        "capacity": capacity.to_dict() if capacity else None,
        "capacity_error": capacity_error,
        "rendered": render(rows, capacity, capacity_error),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--no-capacity", action="store_true", help="skip the quota readings"
    )
    args = parser.parse_args(argv)
    report = build_report(with_capacity=not args.no_capacity)
    print(json.dumps(report, indent=1) if args.json else report["rendered"])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
