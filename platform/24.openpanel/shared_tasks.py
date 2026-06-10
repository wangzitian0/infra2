"""OpenPanel shared tasks"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check OpenPanel status"""
    # 1. Check API health check endpoint inside the container
    api_status = check_service(
        c, "openpanel-api", "curl -sf http://localhost:3333/healthcheck"
    )
    if not api_status["is_ready"]:
        return api_status

    # 2. Check Dashboard status inside the container (port 3000)
    dashboard_status = check_service(
        c, "openpanel-dashboard", "curl -I -f http://localhost:3000"
    )
    return dashboard_status
