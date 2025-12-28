"""Authentik shared tasks - DRY version"""
from invoke import task
from libs.common import check_docker_service


@task
def status(c):
    """Check Authentik status"""
    return check_docker_service(c, "authentik-server", "wget -q --spider http://localhost:9000/-/health/ready/", "Authentik")
