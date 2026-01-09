"""Activepieces shared tasks"""
from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check Activepieces status"""
    return check_service(c, "activepieces", "curl -sf http://localhost:80/api/v1/flags")
