"""Weekly positive stability report from the watchdog availability ledger.

Thin runner: it fetches the ledger from the Cloudflare Worker ``/ledger``
endpoint (or a local file) and delegates all aggregation/rendering to
``libs.availability_ledger``. It runs weekly from GitHub Actions, external to the
infra2 VPS, and sends Lark a positive-proof summary. See
``docs/ssot/ops.availability-ledger.md``.

Dry run (print, no Lark):

    INFRA2_STABILITY_REPORT_DRY_RUN=1 python tools/stability_report.py --input ledger.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.alerting import deliver_out_of_band_text  # noqa: E402
from libs.availability_ledger import build_report_message, summarize_ledger  # noqa: E402

# Mirrors out_of_band_watchdog.DEFAULT_WORKER_STATUS_URL: the ledger lives on the
# same public Worker at a sibling route, so it needs no separate operator-configured
# secret to have a working default (finance_report#1851 G4 — a required config that
# has no default silently no-ops instead of failing loud; #1653/#1654/#1655/infra2#402).
DEFAULT_LEDGER_URL = (
    "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/ledger"
)


def fetch_ledger(url: str, token: str, *, timeout: float = 20.0) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    return payload if isinstance(payload, dict) else {}


def run(env: Mapping[str, str], *, input_path: str | None) -> int:
    if input_path:
        ledger = json.loads(Path(input_path).read_text())
    else:
        ledger_url = (
            env.get("INFRA2_WATCHDOG_LEDGER_URL") or DEFAULT_LEDGER_URL
        ).strip()
        if not ledger_url:
            print("INFRA2_WATCHDOG_LEDGER_URL or --input is required", file=sys.stderr)
            return 2
        token = (env.get("INFRA2_WATCHDOG_WORKER_STATUS_TOKEN") or "").strip()
        ledger = fetch_ledger(ledger_url, token)

    message = build_report_message(summarize_ledger(ledger))
    if env.get("INFRA2_STABILITY_REPORT_DRY_RUN") == "1":
        print(message)
        return 0
    deliver_out_of_band_text(env, message)
    print(message)
    return 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", help="Local ledger JSON instead of the Worker endpoint."
    )
    args = parser.parse_args(argv)
    return run(env or os.environ, input_path=args.input)


if __name__ == "__main__":
    raise SystemExit(main())
