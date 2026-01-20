from invoke import task

from libs.common import check_service


@task
def status(c):
    """Check application health status (frontend + backend)."""
    backend_ok = check_service(
        c, "finance_report-backend", "curl -f http://localhost:8000/health"
    )
    frontend_ok = check_service(
        c, "finance_report-frontend", "curl -f http://localhost:3000"
    )
    return backend_ok and frontend_ok
