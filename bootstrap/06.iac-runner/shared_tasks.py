"""
IaC Runner health check
"""
from invoke import task
from libs.common import get_env, check_service


@task
def status(c):
    """Check IaC Runner health status."""
    return check_service(c, "iac-runner", "curl -sf http://localhost:8080/health")
