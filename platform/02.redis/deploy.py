"""Redis deployment using make_tasks() for DRY"""
from libs.deployer import Deployer, make_tasks, load_shared_tasks


class RedisDeployer(Deployer):
    service = "redis"
    compose_path = "platform/02.redis/compose.yaml"
    data_path = "/data/platform/redis"
    secret_key = "password"
    env_var_name = "REDIS_PASSWORD"


# Generate tasks using make_tasks() - DRY
_tasks = make_tasks(RedisDeployer, load_shared_tasks(__file__))
status = _tasks["status"]
pre_compose = _tasks["pre_compose"]
composing = _tasks["composing"]
post_compose = _tasks["post_compose"]
setup = _tasks["setup"]
