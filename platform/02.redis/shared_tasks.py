"""Redis shared tasks - DRY version"""
from invoke import task
from libs.common import check_docker_service


@task
def status(c):
    """Check Redis status"""
    return check_docker_service(c, "platform-redis", "redis-cli ping", "Redis")
