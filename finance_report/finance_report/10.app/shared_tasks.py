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
    # check_service returns dicts: `dict and dict` is the second dict, so a dead
    # backend used to read as ready whenever the frontend was up.
    return {
        "is_ready": backend_ok["is_ready"] and frontend_ok["is_ready"],
        "details": f"backend={backend_ok['details']}, frontend={frontend_ok['details']}",
    }
