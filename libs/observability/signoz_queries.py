"""SigNoz ClickHouse query builders scoped to one service and one environment.

Both builders return a query with the same three predicates: the service name,
the environment (`deployment.environment.name` resource attribute), and a time
window. Each query returns two columns: the row count inside the window and the
latest timestamp inside the window, as `YYYY-MM-DD HH:MM:SS` in UTC.

An empty window returns count 0 and the epoch timestamp `1970-01-01 00:00:00`.
Callers must check the count before they read the timestamp.

Import this module directly. It is not exported from `libs.observability`.
"""

from __future__ import annotations

import re

# One window for the SQL filter and for the freshness limit of the caller.
DEFAULT_WINDOW_SECONDS = 3600

ENVIRONMENT_ATTRIBUTE = "deployment.environment.name"

# Distributed tables: the same family that the other SigNoz queries in this
# repository use (libs/deploy/ingestion_verify.py, platform/11.signoz).
TRACES_TABLE = "signoz_traces.distributed_signoz_index_v3"
LOGS_TABLE = "signoz_logs.distributed_logs_v2"

_SAFE_VALUE = re.compile(r"[A-Za-z0-9._-]+")


def _literal(value: str, what: str) -> str:
    """Return `value` as a quoted ClickHouse string literal.

    Accept only characters that need no escaping. Raise `ValueError` otherwise.
    """
    if not isinstance(value, str) or not _SAFE_VALUE.fullmatch(value):
        raise ValueError(f"unsafe {what}: {value!r}")
    return f"'{value}'"


def _window(window_seconds: int) -> int:
    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds <= 0
    ):
        raise ValueError(
            f"window_seconds must be a positive integer: {window_seconds!r}"
        )
    return window_seconds


def traces_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> str:
    """Build the span count and latest span time for one service and environment."""
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        "SELECT count(), toDateTime(max(timestamp), 'UTC') "
        f"FROM {TRACES_TABLE} "
        f"WHERE serviceName = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        f"AND timestamp >= now() - INTERVAL {seconds} SECOND"
    )


def logs_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> str:
    """Build the log count and latest log time for one service and environment.

    `timestamp` of the logs table is UInt64 nanoseconds. The time bound must be
    in nanoseconds too, or ClickHouse raises DECIMAL_OVERFLOW.
    """
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        "SELECT count(), toDateTime(intDiv(max(timestamp), 1000000000), 'UTC') "
        f"FROM {LOGS_TABLE} "
        f"WHERE resources_string['service.name'] = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        f"AND timestamp >= toUnixTimestamp64Nano(now64(3) - INTERVAL {seconds} SECOND)"
    )
