"""OpenPanel shared tasks"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check OpenPanel status"""
    # 1. API readiness probe (port 3000); its /healthcheck also asserts that
    #    Postgres, Redis and the dedicated ClickHouse (op-ch) are reachable.
    api_status = check_service(
        c, "openpanel-api", "curl -sf http://localhost:3000/healthcheck"
    )
    if not api_status["is_ready"]:
        return api_status

    # 2. Dashboard readiness probe (port 3000, Next.js /api/healthcheck route).
    dashboard_status = check_service(
        c, "openpanel-dashboard", "curl -sf http://localhost:3000/api/healthcheck"
    )
    return dashboard_status
