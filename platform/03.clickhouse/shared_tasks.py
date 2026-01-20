"""Shared tasks for ClickHouse"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check ClickHouse health"""
    return check_service(
        c,
        "clickhouse",
        "wget --spider -q localhost:8123/ping && echo 'ClickHouse is healthy'",
    )
