from invoke import task

from libs.common import check_service


@task
def status(c):
    """Check TrueAlpha PostgreSQL health status."""
    return check_service(c, "truealpha-postgres", "pg_isready -U postgres -d truealpha")
