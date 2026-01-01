"""Shared tasks for SigNoz"""
from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check SigNoz health"""
    return check_service(
        c, 
        "signoz",
        "docker exec platform-signoz-query-service wget --spider -q localhost:8080/api/v1/health && echo 'SigNoz is healthy'"
    )
