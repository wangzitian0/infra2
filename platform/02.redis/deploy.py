"""Redis deployment using make_tasks() for DRY"""
from libs.deployer import Deployer, make_tasks


class RedisDeployer(Deployer):
    service = "redis"
    compose_path = "platform/02.redis/compose.yaml"
    data_path = "/data/platform/redis"
    secret_key = "password"
    env_var_name = "REDIS_PASSWORD"


# Import shared_tasks dynamically to avoid relative import issues
def _get_shared_tasks():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "shared_tasks",
        Path(__file__).parent / "shared_tasks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Generate tasks using make_tasks() - DRY
_tasks = make_tasks(RedisDeployer, _get_shared_tasks())
pre_compose = _tasks["pre_compose"]
composing = _tasks["composing"]
post_compose = _tasks["post_compose"]
setup = _tasks["setup"]
