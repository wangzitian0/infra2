"""SigNoz ClickHouse query builders scoped to one service and one environment.

Both builders return a query with the same three predicates: the service name,
the environment (`deployment.environment.name` resource attribute), and a time
window. Each query returns two columns: the row count inside the window and the
latest timestamp inside the window, as `YYYY-MM-DD HH:MM:SS` in UTC.

An empty window returns count 0 and the epoch timestamp `1970-01-01 00:00:00`.
Callers must check the count before they read the timestamp.

Import this module directly. It is not exported from `common.observability`.
"""

from __future__ import annotations

import re

# One window for the SQL filter and for the freshness limit of the caller.
DEFAULT_WINDOW_SECONDS = 3600

ENVIRONMENT_ATTRIBUTE = "deployment.environment.name"

# The one definition of an error log: alert rules and `tools.diagnose` both use it.
ERROR_LOG_LEVELS = ("ERROR", "CRITICAL", "FATAL")

# Distributed tables: the same family that the other SigNoz queries in this
# repository use (common/deploy/ingestion_verify.py, platform/11.signoz).
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


def _levels() -> str:
    return ", ".join(f"'{level}'" for level in ERROR_LOG_LEVELS)


def _version(version: str | None) -> str:
    if version is None:
        return ""
    return f" AND resources_string['service.version'] = {_literal(version, 'version')}"


def traces_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    version: str | None = None,
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
        f"AND timestamp >= now() - INTERVAL {seconds} SECOND" + _version(version)
    )


def logs_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    version: str | None = None,
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
        + _version(version)
    )


def error_logs_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    limit: int = 10,
) -> str:
    """Build the newest error log lines (time, trace id, body) of one service."""
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        "SELECT toDateTime(intDiv(timestamp, 1000000000), 'UTC'), trace_id, "
        "replaceRegexpAll(substring(body, 1, 300), '[\\t\\n]', ' ') "
        f"FROM {LOGS_TABLE} "
        f"WHERE resources_string['service.name'] = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        f"AND severity_text IN ({_levels()}) "
        f"AND timestamp >= toUnixTimestamp64Nano(now64(3) - INTERVAL {seconds} SECOND) "
        f"ORDER BY timestamp DESC LIMIT {int(limit)}"
    )


def slow_traces_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    limit: int = 5,
) -> str:
    """Build the slowest root spans (trace id, duration in ms, name, status code)."""
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        "SELECT trace_id, round(duration_nano / 1000000, 1), name, response_status_code "
        f"FROM {TRACES_TABLE} "
        f"WHERE serviceName = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        "AND parent_span_id = '' "
        f"AND timestamp >= now() - INTERVAL {seconds} SECOND "
        f"ORDER BY duration_nano DESC LIMIT {int(limit)}"
    )


def status_5xx_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> str:
    """Build the root span count and the 5xx root span count of one service."""
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        "SELECT count(), countIf(toUInt16OrZero(response_status_code) >= 500) "
        f"FROM {TRACES_TABLE} "
        f"WHERE serviceName = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        "AND parent_span_id = '' "
        f"AND timestamp >= now() - INTERVAL {seconds} SECOND"
    )
