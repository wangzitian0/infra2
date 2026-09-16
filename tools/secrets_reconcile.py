#!/usr/bin/env python3
"""Daily reconcile: is every store what its manifest says, and is every quota in budget?

For each registered service (libs/secrets_registry.py) and each environment it deploys
to, the infra2-sdk resolver compares the Vault path with the manifest — missing, empty,
unclassified keys — and, when 1Password answers, with the human-entered values (stale).
Capacity readings for the Cloudflare free tier behind the heartbeat worker are
evaluated against their limits (#616 flapping ate the KV write budget), and the KV
writes per UTC day over the last week plus today so far are reported with them, so a
budget that is creeping up shows before it trips. Names and counts only, never values.

    python3 tools/secrets_reconcile.py            # inside the iac-runner (Vault + op reachable)
    python3 tools/secrets_reconcile.py --json     # machine-readable, for the ops-check job

Exit 1 when anything is missing, empty, unclassified, stale, or over budget.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # runnable as a script

from infra2_sdk.capacity import (  # noqa: E402
    CLOUDFLARE_FREE_TIER,
    CapacityReading,
    CapacityReport,
    cloudflare_readings,
    evaluate,
)
from infra2_sdk.secrets import OnePasswordBackend, SecretsError, vault_path  # noqa: E402

from libs.secrets_registry import SERVICES, Service  # noqa: E402
from libs.secrets_supply import ONEPASSWORD_VAULT, resolver_for, vault_backend  # noqa: E402

# cloudflare/infra-watchdog/wrangler.toml; the token is the worker's own API token.
CLOUDFLARE_ACCOUNT = "6e27b6853de0131bea033d235e0a1139"
CLOUDFLARE_TOKEN_ITEM = ("bootstrap/cloudflare-worker", "CLOUDFLARE_WORKER_API_TOKEN")
# The reading whose daily trend the report carries: the free tier's tightest quota and
# the one the watchdog has tripped (#616; 1198 and 1157 puts on 2026-09-15/16).
TREND_READING = "cloudflare.kv.write"
TREND_DAYS = 7  # complete UTC days before today; today so far is appended
FINDING_KEYS = ("missing", "empty", "unclassified", "stale", "over_privileged")
# The object store's own service: every environment's root credential lives here, and
# nowhere else. #677: three application paths held it, so finance_report could read and
# delete every bucket on the production instance.
MINIO_SERVICE = ("platform", "minio")


def _digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def over_privileged(
    documents: Mapping[str, Mapping[str, str]],
    root_credentials: Mapping[str, Mapping[str, str]],
) -> dict[str, list[str]]:
    """Which stores hold a credential that belongs to the object store's root account.

    ``documents`` is ``{"project/service|env": {key: value}}`` and ``root_credentials`` is
    ``{env: {"root_user": ..., "root_password": ...}}``. Compares digests, returns key
    names: an application may hold its own scoped credential and nothing stronger (#677).
    """
    passwords = {
        _digest(creds.get("root_password", "")): env
        for env, creds in root_credentials.items()
        if creds.get("root_password")
    }
    users = {creds.get("root_user", "") for creds in root_credentials.values()} - {""}
    findings: dict[str, list[str]] = {}
    for coordinate, document in documents.items():
        service = coordinate.partition("|")[0]
        if service == "/".join(MINIO_SERVICE):
            continue  # the object store legitimately holds its own root credential
        hits = sorted(
            key
            for key, value in document.items()
            if isinstance(value, str)
            and value
            and (
                _digest(value) in passwords
                or (key.endswith("ACCESS_KEY") and value in users)
            )
        )
        if hits:
            findings[coordinate] = hits
    return findings


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
    documents: dict[str, Mapping[str, str]] = {}
    root_credentials: dict[str, Mapping[str, str]] = {}
    for service in services:
        if service.preview:
            continue
        for env in environments or service.environments:
            resolver = resolver_for(service, env, store=store, human=human)
            note = ""
            held: Mapping[str, str] = {}
            try:
                held = store.read(vault_path(service.project, env, service.service))
            except SecretsError:
                held = {}
            if (service.project, service.service) == MINIO_SERVICE:
                root_credentials[env] = held
            try:
                report = resolver.reconcile()
            except SecretsError as error:
                resolver.human = None
                report = resolver.reconcile()
                note = f"1Password unavailable: {error}"
            data = report.to_dict()
            # Operator-only keys are declared in the registry, not in the manifest (the
            # template must not render them): documented, not unknown.
            documents[f"{service.id}|{env}"] = held
            data["unclassified"] = [
                key
                for key in data.get("unclassified", [])
                if key not in service.store_only_keys
            ]
            row: dict[str, object] = {
                "service": service.id,
                "env": env,
                **data,
                "ok": not any(data.get(key) for key in FINDING_KEYS),
            }
            if note:
                row["note"] = note
            rows.append(row)
    for coordinate, keys in over_privileged(documents, root_credentials).items():
        service_id, _, env = coordinate.partition("|")
        for row in rows:
            if (row["service"], row["env"]) == (service_id, env):
                row["over_privileged"] = keys
                row["ok"] = False
    return rows


def cloudflare_token(human=None) -> str:
    """The worker's own API token (analytics read), from 1Password."""
    human = human if human is not None else OnePasswordBackend(ONEPASSWORD_VAULT)
    token = human.read(CLOUDFLARE_TOKEN_ITEM[0]).get(CLOUDFLARE_TOKEN_ITEM[1], "")
    if not token:
        raise SecretsError(
            f"{CLOUDFLARE_TOKEN_ITEM[0]} lacks {CLOUDFLARE_TOKEN_ITEM[1]}"
        )
    return token


def capacity_report(
    *,
    human=None,
    day: dt.date | None = None,
    readings: Callable[[], object] | None = None,
) -> CapacityReport:
    """Yesterday's Cloudflare usage against the free tier; the token comes from 1Password."""
    if readings is None:
        token = cloudflare_token(human)
        day = day or (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1))

        def readings():
            return cloudflare_readings(account=CLOUDFLARE_ACCOUNT, token=token, day=day)

    return evaluate(CLOUDFLARE_FREE_TIER, readings())


def reading_trend(
    readings_for: Callable[[dt.date], Iterable[CapacityReading]],
    *,
    today: dt.date,
    name: str = TREND_READING,
    days: int = TREND_DAYS,
) -> list[dict[str, object]]:
    """``name`` used per UTC day, oldest first: ``days`` complete days, then today so far.

    ``used`` is None when Cloudflare answered nothing for the account that day; an
    account row without the reading means none were used (0).
    """
    rows: list[dict[str, object]] = []
    for offset in range(days, -1, -1):
        day = today - dt.timedelta(days=offset)
        readings = tuple(readings_for(day))
        used = (
            next((r.used for r in readings if r.name == name), 0) if readings else None
        )
        rows.append({"date": day.isoformat(), "used": used, "partial": offset == 0})
    return rows


def render_trend(trend: Mapping[str, object]) -> str:
    """One line: ``cloudflare.kv.write/day (limit 1000; ! = over): 09-15 1198! · …``."""
    cells = []
    for row in trend["days"]:
        used = row["used"]
        value = "?" if used is None else str(used)
        if used is not None and used >= trend["limit"]:
            value += "!"
        if row["partial"]:
            value += " so far"
        cells.append(f"{str(row['date'])[5:]} {value}")
    return f"{trend['name']}/day (limit {trend['limit']}; ! = over): " + " · ".join(
        cells
    )


def capacity_section(
    *,
    human=None,
    today: dt.date | None = None,
    fetch: Callable[[dt.date], Iterable[CapacityReading]] | None = None,
) -> tuple[CapacityReport, dict[str, object] | None, str | None]:
    """Yesterday against the free tier, plus the ``TREND_READING`` trend around it.

    One token read and one analytics query per day (yesterday's serves both). A trend
    that cannot be read is reported beside the verdict, never instead of it.
    """
    today = today or dt.datetime.now(dt.UTC).date()
    if fetch is None:
        token = cloudflare_token(human)

        def fetch(day: dt.date) -> Iterable[CapacityReading]:
            return cloudflare_readings(account=CLOUDFLARE_ACCOUNT, token=token, day=day)

    cache: dict[dt.date, tuple[CapacityReading, ...]] = {}

    def readings_for(day: dt.date) -> tuple[CapacityReading, ...]:
        if day not in cache:
            cache[day] = tuple(fetch(day))
        return cache[day]

    yesterday = today - dt.timedelta(days=1)
    report = capacity_report(readings=lambda: readings_for(yesterday))
    limit = next(
        item.limit for item in CLOUDFLARE_FREE_TIER if item.name == TREND_READING
    )
    try:
        days = reading_trend(readings_for, today=today)
    except Exception as error:  # noqa: BLE001 - the verdict above still stands
        return report, None, str(error)
    trend: dict[str, object] = {"name": TREND_READING, "limit": limit, "days": days}
    trend["rendered"] = render_trend(trend)
    return report, trend, None


def findings(row: dict[str, object]) -> str:
    return ", ".join(f"{key}={row[key]}" for key in FINDING_KEYS if row.get(key))


def render(
    rows: list[dict[str, object]],
    capacity: CapacityReport | None,
    capacity_error: str | None = None,
    trend: Mapping[str, object] | None = None,
    trend_error: str | None = None,
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
    if trend:
        lines.append(f"  {trend['rendered']}")
    elif trend_error:
        lines.append(f"  {TREND_READING}/day trend unavailable ({trend_error})")
    return "\n".join(lines)


def build_report(*, with_capacity: bool = True) -> dict[str, object]:
    rows = reconcile_stores()
    capacity: CapacityReport | None = None
    capacity_error: str | None = None
    trend: dict[str, object] | None = None
    trend_error: str | None = None
    if with_capacity:
        try:
            capacity, trend, trend_error = capacity_section()
        except Exception as error:  # noqa: BLE001 - a quota read must not hide store drift
            capacity_error = str(error)
    exceeded = bool(capacity and capacity.at_level("exceeded"))
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "ok": all(row["ok"] for row in rows) and not exceeded,
        "stores": rows,
        "capacity": capacity.to_dict() if capacity else None,
        "capacity_error": capacity_error,
        "capacity_trend": trend,
        "capacity_trend_error": trend_error,
        "rendered": render(rows, capacity, capacity_error, trend, trend_error),
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
