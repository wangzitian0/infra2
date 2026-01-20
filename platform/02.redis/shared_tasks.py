"""Redis shared tasks"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check Redis status"""
    return check_service(c, "redis", "redis-cli ping")
