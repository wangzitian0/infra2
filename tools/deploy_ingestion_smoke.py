#!/usr/bin/env python3
"""Prove the *just-deployed* app version actually ingests into SigNoz.

This is the infra2-side home of the deployed-version ingestion proof. The app
(finance_report) is deliberately backend-agnostic — it only emits OTLP. Knowing
that telemetry actually landed (and detecting a stale image whose telemetry
carries an *older* ``service.version``) is the platform's job, because infra2 is
the side that knows which backend (SigNoz/ClickHouse) sits behind the collector.

Run on the iac-runner / host that has Docker-network access to ClickHouse, after
the deploy health-check passes. Queries ClickHouse directly (the same path the
continuous round-trip canary uses) and classifies distinctly, no SSH:

- **zero ingestion**  — no telemetry at all for the service/env in the window
  (exporter not reaching the collector, or the deployed version emits nothing);
- **stale image**     — telemetry flows for the env but none carries the
  just-deployed ``service.version`` (the running container is an older image);
- **absent traces**   — logs flow but traces do not.

Usage:
  python tools/deploy_ingestion_smoke.py \
    --service-name finance-report-backend \
    --deployment-environment production \
    --expected-version v0.1.20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.deploy.ingestion_verify import (  # noqa: E402
    DEFAULT_CLICKHOUSE_URL,
    DEFAULT_POLL_ATTEMPTS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WINDOW_MINUTES,
    IngestionSmokeError,
    _count,
    _logs_count_query,
    _poll,
    _resolve_clickhouse_url,
    _traces_count_query,
    verify_deploy_ingestion,
)

__all__ = [
    "DEFAULT_CLICKHOUSE_URL",
    "DEFAULT_POLL_ATTEMPTS",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_WINDOW_MINUTES",
    "IngestionSmokeError",
    "_count",
    "_logs_count_query",
    "_poll",
    "_resolve_clickhouse_url",
    "_traces_count_query",
    "verify_deploy_ingestion",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-name", default="finance-report-backend")
    parser.add_argument("--deployment-environment", required=True)
    parser.add_argument("--expected-version", default=None)
    parser.add_argument(
        "--clickhouse-url",
        default=None,
        help="override the ClickHouse URL (default: SIGNOZ_CLICKHOUSE_URL env, "
        f"else {DEFAULT_CLICKHOUSE_URL})",
    )
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    args = parser.parse_args(argv)
    try:
        passed = verify_deploy_ingestion(
            clickhouse_url=args.clickhouse_url,
            service_name=args.service_name,
            environment=args.deployment_environment,
            expected_version=args.expected_version,
            window_minutes=args.window_minutes,
        )
    except IngestionSmokeError as exc:
        print(f"deploy-ingestion-smoke FAILED: {exc}", file=sys.stderr)
        return 1
    for label in passed:
        print(f"OK: {label}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
