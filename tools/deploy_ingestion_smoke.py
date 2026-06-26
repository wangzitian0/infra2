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
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the round-trip canary's vetted ClickHouse helpers so there is a single
# source of truth for how infra2 talks to ClickHouse.
from tools.observability_roundtrip_probe import (  # noqa: E402
    _ch_string,
    _clickhouse_query,
)

DEFAULT_CLICKHOUSE_URL = "http://platform-clickhouse:8123"
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_POLL_ATTEMPTS = 6
DEFAULT_POLL_INTERVAL = 10.0


class IngestionSmokeError(RuntimeError):
    """Raised when the deployed version is not provably ingesting."""


def _logs_count_query(
    *, service_name: str, environment: str, version: str | None, window_minutes: int
) -> str:
    # distributed_logs_v2.timestamp is UInt64 nanoseconds: bound in nanoseconds to
    # avoid the DateTime64 coercion overflow (Code 407) the canary documents.
    where = [
        f"resources_string['service.name'] = {_ch_string(service_name)}",
        f"resources_string['deployment.environment'] = {_ch_string(environment)}",
        f"timestamp >= toUnixTimestamp64Nano(now64(3) - INTERVAL {int(window_minutes)} MINUTE)",
    ]
    if version:
        where.append(f"resources_string['service.version'] = {_ch_string(version)}")
    return "SELECT count() FROM signoz_logs.distributed_logs_v2 WHERE " + " AND ".join(
        where
    )


def _traces_count_query(
    *, service_name: str, environment: str, version: str | None, window_minutes: int
) -> str:
    where = [
        f"serviceName = {_ch_string(service_name)}",
        f"resources_string['deployment.environment'] = {_ch_string(environment)}",
        f"timestamp >= now() - INTERVAL {int(window_minutes)} MINUTE",
    ]
    if version:
        where.append(f"resources_string['service.version'] = {_ch_string(version)}")
    return (
        "SELECT count() FROM signoz_traces.distributed_signoz_index_v3 WHERE "
        + " AND ".join(where)
    )


def _count(clickhouse_url: str, query: str) -> int:
    body = _clickhouse_query(clickhouse_url, query).strip()
    if not body:
        return 0
    first = body.splitlines()[0].strip()
    try:
        return int(first)
    except ValueError as exc:
        raise IngestionSmokeError(
            f"unexpected ClickHouse count response: {body[:200]!r}"
        ) from exc


def _poll(counter, query: str, *, attempts: int, interval: float, sleeper) -> int:
    """Poll a count query until it is positive, tolerating post-deploy flush lag."""
    count = 0
    for attempt in range(max(1, attempts)):
        count = counter(query)
        if count > 0:
            return count
        if attempt + 1 < max(1, attempts):
            sleeper(interval)
    return count


def _resolve_clickhouse_url(clickhouse_url: str | None) -> str:
    """Single owner of the ClickHouse URL: explicit arg, else the canary's env."""
    return clickhouse_url or os.getenv("SIGNOZ_CLICKHOUSE_URL", DEFAULT_CLICKHOUSE_URL)


def verify_deploy_ingestion(
    *,
    clickhouse_url: str | None = None,
    service_name: str,
    environment: str,
    expected_version: str | None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    counter=None,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleeper=time.sleep,
) -> list[str]:
    """Verify the deployed version's logs and traces are queryable; classify failures."""
    if counter is None:
        resolved_url = _resolve_clickhouse_url(clickhouse_url)

        def counter(query: str) -> int:
            return _count(resolved_url, query)

    passed: list[str] = []
    for kind, build in (("logs", _logs_count_query), ("traces", _traces_count_query)):
        total = _poll(
            counter,
            build(
                service_name=service_name,
                environment=environment,
                version=None,
                window_minutes=window_minutes,
            ),
            attempts=poll_attempts,
            interval=poll_interval,
            sleeper=sleeper,
        )
        if total == 0:
            raise IngestionSmokeError(
                f"zero {kind}: no {service_name} {kind} in {environment} within "
                f"{window_minutes}m — the OTEL exporter is not reaching the collector "
                "or the deployed version emits nothing"
            )
        if not expected_version:
            passed.append(
                f"{kind} ingested ({environment}, {total} in {window_minutes}m)"
            )
            continue
        versioned = _poll(
            counter,
            build(
                service_name=service_name,
                environment=environment,
                version=expected_version,
                window_minutes=window_minutes,
            ),
            attempts=poll_attempts,
            interval=poll_interval,
            sleeper=sleeper,
        )
        if versioned == 0:
            raise IngestionSmokeError(
                f"stale image: {environment} has {kind} but none tagged "
                f"service.version={expected_version} within {window_minutes}m "
                "(the running container is an older image)"
            )
        passed.append(
            f"{kind} ingested ({environment} service.version={expected_version}, "
            f"{versioned} in {window_minutes}m)"
        )
    return passed


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
