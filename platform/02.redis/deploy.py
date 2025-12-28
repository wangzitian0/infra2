"""Redis deployment - DRY version"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from invoke import task
from libs.deployer import Deployer
from libs.console import success


class RedisDeployer(Deployer):
    service = "redis"
    compose_path = "platform/02.redis/compose.yaml"
    data_path = "/data/platform/redis"
    secret_key = "password"
    env_var_name = "REDIS_PASSWORD"


@task
def pre_compose(c):
    return RedisDeployer.pre_compose(c)


@task
def composing(c):
    RedisDeployer.composing(c)


@task
def post_compose(c):
    from . import shared_tasks
    return RedisDeployer.post_compose(c, shared_tasks)


@task(pre=[pre_compose, composing, post_compose])
def setup(c):
    success(f"{RedisDeployer.service} setup complete!")
