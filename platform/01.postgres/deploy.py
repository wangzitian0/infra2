"""PostgreSQL deployment"""
import sys
from libs.deployer import Deployer, make_tasks

# Get shared_tasks from sys.modules (loaded by tools/loader.py)
shared_tasks = sys.modules.get("platform.01.postgres.shared")


class PostgresDeployer(Deployer):
    service = "postgres"
    compose_path = "platform/01.postgres/compose.yaml"
    data_path = "/data/platform/postgres"
    uid = "999"
    secret_key = "root_password"
    env_var_name = "POSTGRES_PASSWORD"


# Generate tasks
if shared_tasks:
    _tasks = make_tasks(PostgresDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
