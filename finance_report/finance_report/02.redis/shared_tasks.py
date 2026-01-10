from invoke import task

from libs.common import check_service


@task
def status(c):
    """Check Redis health status."""
    return check_service(c, "finance_report-redis", "redis-cli ping")
