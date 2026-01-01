"""Wealthfolio shared tasks"""
from invoke import task
from libs.common import get_env
from libs.console import success, error


@task
def status(c):
    """Check Wealthfolio service status."""
    from libs.common import check_service
    return check_service(c, "wealthfolio", "curl -sf http://localhost:8088/")
