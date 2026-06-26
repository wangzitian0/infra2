"""Prefect status check"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check if Prefect server is running and healthy"""
    return check_service(
        c,
        "prefect-server",
        "python -c \"import urllib.request as u; exit(0 if u.urlopen(\\\"http://localhost:4200/api/health\\\", timeout=1).read().strip() == b\\\"true\\\" else 1)\"",
    )
