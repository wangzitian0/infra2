"""Shared tasks for ClickHouse"""
from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check ClickHouse health via the WRITE path, not a read-only /ping.

    A `/ping` (or `SELECT 1`) stays green when the data dir is unwritable, so it
    reports "healthy" while ClickHouse can no longer store data. Exercise a real
    part-write so operator status matches the container healthcheck's truth.
    """
    return check_service(
        c,
        "clickhouse",
        "clickhouse-client -n -q 'CREATE TABLE IF NOT EXISTS default.__hc (t DateTime) "
        "ENGINE=MergeTree ORDER BY t TTL t + INTERVAL 10 MINUTE; INSERT INTO default.__hc "
        "VALUES (now()); SELECT 1 FROM default.__hc LIMIT 1' && echo 'ClickHouse write-path OK'",
    )
