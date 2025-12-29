"""Redis shared tasks - DRY version"""
from invoke import task
from libs.common import check_docker_service, CONTAINER_NAMES


@task
def status(c):
    """Check Redis status"""
    return check_docker_service(c, CONTAINER_NAMES["redis"], "redis-cli ping", "Redis")
