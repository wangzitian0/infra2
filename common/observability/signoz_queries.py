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
METRICS_TABLE = "signoz_metrics.distributed_time_series_v4"

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


def _signal_count_query(
    *,
    select_expr: str,
    table: str,
    service_col: str,
    time_expr: str,
    service_name: str,
    environment: str,
    window_seconds: int,
    version: str | None,
) -> str:
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        f"SELECT {select_expr} "
        f"FROM {table} "
        f"WHERE {service_col} = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        f"AND timestamp >= {time_expr.format(seconds=seconds)}" + _version(version)
    )


def traces_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    version: str | None = None,
) -> str:
    """Build the span count and latest span time for one service and environment."""
    return _signal_count_query(
        select_expr="count(), toDateTime(max(timestamp), 'UTC')",
        table=TRACES_TABLE,
        service_col="serviceName",
        time_expr="now() - INTERVAL {seconds} SECOND",
        service_name=service_name,
        environment=environment,
        window_seconds=window_seconds,
        version=version,
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
    return _signal_count_query(
        select_expr="count(), toDateTime(intDiv(max(timestamp), 1000000000), 'UTC')",
        table=LOGS_TABLE,
        service_col="resources_string['service.name']",
        time_expr="toUnixTimestamp64Nano(now64(3) - INTERVAL {seconds} SECOND)",
        service_name=service_name,
        environment=environment,
        window_seconds=window_seconds,
        version=version,
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


def gen_ai_spans_query(
    *,
    service_name: str,
    environment: str,
    call_site: str = "todo.summary",
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> str:
    """Build GenAI span counts, aligned attrs, privacy leak, and cost checks."""
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    site = _literal(call_site, "call_site")
    seconds = _window(window_seconds)
    return (
        "SELECT "
        f"countIf(name = concat('invoke_agent ', {site})), "
        "countIf(startsWith(name, 'chat ') "
        "AND attributes_string['gen_ai.operation.name'] = 'chat'), "
        f"countIf(attributes_string['infra.llm.call_site'] = {site} "
        "AND attributes_string['infra.llm.decoding_version'] != '' "
        "AND attributes_string['infra.llm.prompt_sha256'] != ''), "
        "countIf(mapContains(attributes_string, 'gen_ai.system_instructions') "
        "OR mapContains(attributes_string, 'model_request_parameters') "
        "OR mapContains(attributes_string, 'final_result') "
        "OR position(attributes_string['gen_ai.input.messages'], '\"content\":') > 0 "
        "OR position(attributes_string['gen_ai.output.messages'], '\"content\":') > 0 "
        "OR position(attributes_string['gen_ai.output.messages'], '\"arguments\":') > 0), "
        "countIf(mapContains(attributes_number, 'operation.cost') "
        "OR mapContains(attributes_string, 'operation.cost')) "
        f"FROM {TRACES_TABLE} "
        f"WHERE serviceName = {service} "
        f"AND resources_string['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        f"AND timestamp >= now() - INTERVAL {seconds} SECOND"
    )


def gen_ai_metrics_query(
    *,
    service_name: str,
    environment: str,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> str:
    """Build GenAI metric time-series presence counts for one service."""
    service = _literal(service_name, "service_name")
    env = _literal(environment, "environment")
    seconds = _window(window_seconds)
    return (
        "SELECT "
        "countIf(startsWith(metric_name, 'gen_ai.client.token.usage')), "
        "countIf(startsWith(metric_name, 'gen_ai.client.operation.duration')) "
        f"FROM {METRICS_TABLE} "
        f"WHERE resource_attrs['service.name'] = {service} "
        f"AND resource_attrs['{ENVIRONMENT_ATTRIBUTE}'] = {env} "
        f"AND unix_milli >= toUnixTimestamp64Milli(now64(3) - INTERVAL {seconds} SECOND)"
    )
