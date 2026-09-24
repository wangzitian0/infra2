"""Weekly positive stability report from the VPS availability ledger (#904).

Thin runner. It reads each environment's ledger file from the VPS over SSH (the
way ``tools/out_of_band_watchdog.py`` reads backup manifests), fetches the
production outage intervals the Cloudflare Worker recorded (``GET /outages``;
the VPS cannot count its own downtime), and delegates the math and the text to
``libs.availability_ledger``. It runs weekly from GitHub Actions, outside the
VPS, and sends Lark a positive-proof summary. See ops.observability.md §6.

A missing, empty or stale ledger, or an unreadable outage list, fails the run:
a report built without them would overstate availability.

Dry run from local files (print, no Lark):

    INFRA2_STABILITY_REPORT_DRY_RUN=1 python tools/stability_report.py \\
        --ledger production=prod.json --ledger staging=staging.json --outages outages.json
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import deliver_out_of_band_text  # noqa: E402
from libs.availability_ledger import (  # noqa: E402
    apply_outages,
    build_report_message,
    summarize_ledger,
)
from libs.observability.local_ledger import host_ledger_path, to_report_days  # noqa: E402

ENVIRONMENTS = ("production", "staging")
# Only the production heartbeat's outages are recorded by the Worker (#904).
OUTAGE_ENVIRONMENT = "production"
# The probe runner's loop (INFRA_PROBE_INTERVAL_SECONDS): one missed check per
# interval of outage. The real loop is ~66 s, so this slightly overcounts misses.
CHECK_INTERVAL_SECONDS = 60
# The runner rewrites its ledger every minute; older than this is a stopped writer.
MAX_LEDGER_AGE_SECONDS = 6 * 3600
# Mirrors out_of_band_watchdog.DEFAULT_WORKER_STATUS_URL: a sibling route on the
# same Worker, so a missing variable cannot silently skip the report (#1851 G4).
DEFAULT_OUTAGES_URL = (
    "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/outages"
)


def fetch_outages(url: str, token: str, *, timeout: float = 20.0) -> list[Any]:
    # Cloudflare answers urllib's default User-Agent with `403 error code: 1010`
    # before the Worker sees the request (measured 2026-09-08), so name ourselves.
    # A failure carries the body so the next reader sees which layer refused.
    headers = {
        "Accept": "application/json",
        "User-Agent": "infra2-stability-report/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as error:
        body = (
            error.read().decode(errors="replace")[:200]
            if hasattr(error, "read")
            else ""
        )
        raise RuntimeError(
            f"outages {url} answered HTTP {error.code}: {body!r}"
        ) from error
    return _outage_list(payload, url)


def _outage_list(payload: Any, source: str) -> list[Any]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("outages"), list):
        raise RuntimeError(f"outages from {source} have no `outages` list")
    return payload["outages"]


def read_vps_ledger(
    env: Mapping[str, str], environment: str, *, timeout: float = 30.0
) -> dict:
    """``environment``'s ledger file, read from the VPS over SSH."""
    # Imported here: the module pulls in the Dokploy client, which the offline
    # (--ledger) path does not need.
    from tools.out_of_band_watchdog import _ssh_argv, load_ssh_config

    config = load_ssh_config(env)
    if config is None:
        raise RuntimeError(
            "INFRA2_WATCHDOG_SSH_HOST / _USER / _KEY_PATH are required to read the ledger"
        )
    path = host_ledger_path(environment)
    completed = subprocess.run(
        _ssh_argv(config, f"cat {shlex.quote(path)}"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"reading {environment} ledger {path} over SSH exited "
            f"{completed.returncode}: {completed.stderr.strip()[:200]!r}"
        )
    return _ledger_document(completed.stdout, f"{environment} ledger {path}")


def _ledger_document(text: str, source: str) -> dict:
    try:
        ledger = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{source} is not JSON: {error}") from error
    if not isinstance(ledger, dict):
        raise RuntimeError(f"{source} is not a ledger document")
    return ledger


def build_report(
    ledgers: Mapping[str, Mapping[str, Any]], outages: list[Any], now: datetime
) -> str:
    """Merge the environments' ledgers, count the outages, render the report."""
    days: dict[str, dict[str, Any]] = {}
    for environment in ENVIRONMENTS:
        ledger = ledgers.get(environment)
        if ledger is None:
            raise RuntimeError(f"{environment} ledger is missing")
        env_days = to_report_days(ledger)
        if not any(day["signals"] for day in env_days):
            raise RuntimeError(f"{environment} ledger has no recorded signals")
        age = now.timestamp() - float(ledger.get("updated_at") or 0)
        if age > MAX_LEDGER_AGE_SECONDS:
            raise RuntimeError(
                f"{environment} ledger is stale: last round {int(age)}s ago "
                f"(max {MAX_LEDGER_AGE_SECONDS}s)"
            )
        for day in env_days:
            merged = days.setdefault(
                day["date"], {"date": day["date"], "runs": 0, "signals": {}}
            )
            merged["runs"] += int(day["runs"] or 0)
            merged["signals"].update(day["signals"])
    report = apply_outages(
        {
            "as_of": now.strftime("%Y-%m-%d"),
            "window_days": len(days),
            "ledger": sorted(days.values(), key=lambda day: day["date"], reverse=True),
        },
        outages,
        environment=OUTAGE_ENVIRONMENT,
        check_interval_seconds=CHECK_INTERVAL_SECONDS,
        now=now,
    )
    report["window_days"] = len(report["ledger"])
    return build_report_message(summarize_ledger(report))


def run(
    env: Mapping[str, str],
    *,
    ledger_files: Mapping[str, str] | None = None,
    outages_file: str | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    if ledger_files:
        ledgers = {
            environment: _ledger_document(Path(path).read_text(encoding="utf-8"), path)
            for environment, path in ledger_files.items()
        }
        outages = (
            _outage_list(
                json.loads(Path(outages_file).read_text(encoding="utf-8")), outages_file
            )
            if outages_file
            else []
        )
    else:
        outages_url = (
            env.get("INFRA2_WATCHDOG_OUTAGES_URL") or DEFAULT_OUTAGES_URL
        ).strip()
        if not outages_url:
            print(
                "INFRA2_WATCHDOG_OUTAGES_URL or --ledger is required", file=sys.stderr
            )
            return 2
        token = (env.get("INFRA2_WATCHDOG_WORKER_STATUS_TOKEN") or "").strip()
        ledgers = {
            environment: read_vps_ledger(env, environment)
            for environment in ENVIRONMENTS
        }
        outages = fetch_outages(outages_url, token)

    message = build_report(ledgers, outages, now)
    if env.get("INFRA2_STABILITY_REPORT_DRY_RUN") == "1":
        print(message)
        return 0
    deliver_out_of_band_text(env, message)
    print(message)
    return 0


def _ledger_arg(value: str) -> tuple[str, str]:
    environment, sep, path = value.partition("=")
    if not sep or environment not in ENVIRONMENTS or not path:
        raise argparse.ArgumentTypeError(
            f"expected ENV=PATH with ENV in {ENVIRONMENTS}"
        )
    return environment, path


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        action="append",
        type=_ledger_arg,
        default=[],
        help="ENV=PATH: a local ledger file instead of reading the VPS (repeat per env).",
    )
    parser.add_argument(
        "--outages", help="A local /outages JSON response (with --ledger)."
    )
    args = parser.parse_args(argv)
    return run(
        env or os.environ,
        ledger_files=dict(args.ledger) or None,
        outages_file=args.outages,
    )


if __name__ == "__main__":
    raise SystemExit(main())
