"""Redis deployment with vault-init"""
import sys
from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("platform.02.redis.shared")


class RedisDeployer(Deployer):
    service = "redis"
    compose_path = "platform/02.redis/compose.yaml"
    data_path = "/data/platform/redis"
    secret_key = "password"


if shared_tasks:
    _tasks = make_tasks(RedisDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
