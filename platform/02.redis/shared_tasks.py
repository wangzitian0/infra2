"""Redis shared tasks"""
from invoke import task
from libs.common import check_service

# Authenticated, like the compose healthcheck: the server runs --requirepass and a
# bare `redis-cli ping` answers NOAUTH with exit 0, which check_service reads as
# ready (#713). check_service runs this under `sh -lc` inside the container.
REDIS_HEALTH_COMMAND = '. /secrets/.env && redis-cli -a "$PASSWORD" ping'


@task
def status(c):
    """Check Redis status"""
    return check_service(c, "redis", REDIS_HEALTH_COMMAND)
