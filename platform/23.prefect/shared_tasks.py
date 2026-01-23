"""Prefect status check"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check if Prefect server is running and healthy"""
    return check_service(
        c, "prefect-server", "curl -f http://localhost:4200/api/health"
    )
