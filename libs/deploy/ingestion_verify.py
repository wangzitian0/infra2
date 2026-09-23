"""Verify deployed application version ingestion into SigNoz / ClickHouse."""

from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request

DEFAULT_CLICKHOUSE_URL = "http://platform-clickhouse:8123"
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_POLL_ATTEMPTS = 6
DEFAULT_POLL_INTERVAL = 10.0


class IngestionSmokeError(RuntimeError):
    """Raised when the deployed version is not provably ingesting."""


def _ch_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _clickhouse_query(base_url: str, query: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        target = f"http://{base_url}"
    else:
        target = base_url
    request = urllib.request.Request(
        target,
        data=query.encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(  # noqa: S310
        request,
        timeout=float(os.getenv("OBS_ROUNDTRIP_HTTP_TIMEOUT_SECONDS", "10")),
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def _logs_count_query(
    *, service_name: str, environment: str, version: str | None, window_minutes: int
) -> str:
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
    count = 0
    for attempt in range(max(1, attempts)):
        count = counter(query)
        if count > 0:
            return count
        if attempt + 1 < max(1, attempts):
            sleeper(interval)
    return count


def _resolve_clickhouse_url(clickhouse_url: str | None) -> str:
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
