#!/usr/bin/env python3
"""Run the Dokploy dynamic route canary."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.dokploy import get_dokploy  # noqa: E402
from libs.dokploy_route_canary import (  # noqa: E402
    RouteCanaryConfig,
    render_github_summary,
    report_to_json,
    run_route_canary,
)


def _alert_failure(report) -> None:
    """Best-effort out-of-band page that the Dokploy route canary is RED (#369).

    Uses the SAME out-of-band Feishu path the watchdog / deploy_v2 canary use (it
    survives infra2 being down). NEVER raises — a delivery error must not change the
    probe's exit code. A red route canary means Dokploy routing / Traefik / cert
    renewal is broken, so real public traffic is likely failing the same way; before
    this, a failure was only visible in the hourly workflow log (≈23h blind spot).
    """
    from libs.alerting import deliver_out_of_band_text

    text = (
        "🔴 Dokploy route canary FAILED — public routing probe is RED\n"
        f"status: {report.status}\n"
        f"failure_domain: {report.failure_domain or 'none'}\n"
        "→ Dokploy/Traefik routing (labels, public HTTPS, or cert renewal) is likely "
        "broken; real public traffic may be failing the same way."
    )
    try:
        deliver_out_of_band_text(os.environ, text)
        print("out-of-band alert delivered", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the probe
        print(f"WARNING: out-of-band alert delivery failed: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Public canary host")
    parser.add_argument(
        "--environment-id", required=True, help="Dokploy environment ID"
    )
    parser.add_argument("--project", default="platform")
    parser.add_argument("--env", default=None)
    parser.add_argument(
        "--alert-on-failure",
        action="store_true",
        help="On a non-pass report, page out-of-band (Feishu) like the deploy_v2 "
        "canary. Omit on PRs so a PR run never pages.",
    )
    parser.add_argument("--compose-name", default="dokploy-route-canary")
    parser.add_argument("--dokploy-host", default="")
    parser.add_argument("--image", default="traefik/whoami:v1.10.3")
    parser.add_argument(
        "--nonce",
        default=os.getenv("DOKPLOY_ROUTE_CANARY_NONCE", ""),
        help="Non-sensitive deploy nonce label used to force materialized canary deploys",
    )
    parser.add_argument(
        "--repair-stale-compose",
        action="store_true",
        help="Delete and recreate only guarded route-canary compose assets after deploy/redeploy no-op",
    )
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--ssh-host", default=os.getenv("INFRA2_WATCHDOG_SSH_HOST", ""))
    parser.add_argument(
        "--ssh-user", default=os.getenv("INFRA2_WATCHDOG_SSH_USER", "root")
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.getenv("INFRA2_WATCHDOG_SSH_PORT", "22") or "22"),
    )
    parser.add_argument(
        "--ssh-key-path", default=os.getenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "")
    )
    args = parser.parse_args()

    config = RouteCanaryConfig(
        host=args.host,
        environment_id=args.environment_id,
        project=args.project,
        env=args.env,
        compose_name=args.compose_name,
        image=args.image,
        nonce=args.nonce or str(int(time.time())),
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_port=args.ssh_port,
        ssh_key_path=args.ssh_key_path,
        repair_stale_compose=args.repair_stale_compose,
    )
    client = get_dokploy(host=args.dokploy_host or None)
    report = run_route_canary(config, client)
    print(report_to_json(report))
    if summary_path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(render_github_summary(report))
    if report.status != "pass" and args.alert_on_failure:
        _alert_failure(report)
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
