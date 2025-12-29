"""Authentik shared tasks - DRY version"""
from invoke import task
from libs.common import check_docker_service, CONTAINER_NAMES


@task
def status(c):
    """Check Authentik status"""
    return check_docker_service(
        c,
        CONTAINER_NAMES["authentik"],
        "wget -q --spider http://localhost:9000/-/health/ready/",
        "Authentik",
    )
