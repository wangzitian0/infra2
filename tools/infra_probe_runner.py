#!/usr/bin/env python3
"""Run infra service probes and alert through the internal bridge."""

from __future__ import annotations

import argparse
import json
import os
import traceback
import time
from pathlib import Path

from libs.infra_probes import (
    build_probe_alert_payload,
    failed_results,
    parse_probe_specs,
    post_alert_bridge_payload,
    run_probes,
)


DEFAULT_PROBE_SPECS = """
alert-bridge-http|http|http://platform-alerting:8080/health|200|critical|5
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _load_env_file(Path(os.getenv("ALERTING_ENV_FILE", "/secrets/.env")))
    interval = int(os.getenv("INFRA_PROBE_INTERVAL_SECONDS", "300"))

    while True:
        try:
            exit_code = run_once(as_json=args.json)
        except Exception as exc:  # noqa: BLE001 - looped probes must keep running.
            print(f"infra probe iteration failed: {exc}", flush=True)
            traceback.print_exc()
            exit_code = 1
        if not args.loop:
            return exit_code
        time.sleep(interval)


def run_once(*, as_json: bool = False) -> int:
    raw_specs = os.getenv("INFRA_PROBE_SPECS", DEFAULT_PROBE_SPECS)
    specs = parse_probe_specs(raw_specs)
    results = run_probes(specs)
    failures = failed_results(results)
    if as_json:
        print(json.dumps([result.to_dict() for result in results], indent=2))

    if not failures:
        return 0

    bridge_url = os.getenv(
        "ALERT_BRIDGE_URL",
        "http://platform-alerting:8080/signoz/webhook",
    )
    if os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1":
        print(json.dumps(build_probe_alert_payload(results), indent=2))
        return 1

    post_alert_bridge_payload(
        bridge_url,
        build_probe_alert_payload(results),
        username=os.getenv("BRIDGE_BASIC_AUTH_USERNAME", ""),
        password=os.getenv("BRIDGE_BASIC_AUTH_PASSWORD", ""),
    )
    return 1


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
