#!/usr/bin/env python3
"""Run the Dokploy dynamic route canary."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.dokploy import get_dokploy  # noqa: E402
from libs.dokploy_route_canary import (  # noqa: E402
    RouteCanaryConfig,
    report_to_json,
    run_route_canary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Public canary host")
    parser.add_argument("--environment-id", required=True, help="Dokploy environment ID")
    parser.add_argument("--project", default="platform")
    parser.add_argument("--env", default=None)
    parser.add_argument("--compose-name", default="dokploy-route-canary")
    parser.add_argument("--dokploy-host", default="")
    parser.add_argument("--image", default="traefik/whoami:v1.10.3")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--ssh-host", default=os.getenv("INFRA2_WATCHDOG_SSH_HOST", ""))
    parser.add_argument("--ssh-user", default=os.getenv("INFRA2_WATCHDOG_SSH_USER", "root"))
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.getenv("INFRA2_WATCHDOG_SSH_PORT", "22") or "22"),
    )
    parser.add_argument("--ssh-key-path", default=os.getenv("INFRA2_WATCHDOG_SSH_KEY_PATH", ""))
    args = parser.parse_args()

    config = RouteCanaryConfig(
        host=args.host,
        environment_id=args.environment_id,
        project=args.project,
        env=args.env,
        compose_name=args.compose_name,
        image=args.image,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_port=args.ssh_port,
        ssh_key_path=args.ssh_key_path,
    )
    client = get_dokploy(host=args.dokploy_host or None)
    report = run_route_canary(config, client)
    print(report_to_json(report))
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
