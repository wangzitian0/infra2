from invoke import task

from libs.common import check_service


@task
def status(c):
    """Check PostgreSQL health status."""
    return check_service(c, "finance_report-postgres", "pg_isready -U postgres -d finance_report")
