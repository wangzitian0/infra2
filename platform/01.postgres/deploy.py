"""PostgreSQL deployment using make_tasks() for DRY"""
from libs.deployer import Deployer, make_tasks, load_shared_tasks


class PostgresDeployer(Deployer):
    service = "postgres"
    compose_path = "platform/01.postgres/compose.yaml"
    data_path = "/data/platform/postgres"
    chmod = "700"
    secret_key = "root_password"
    env_var_name = "POSTGRES_PASSWORD"


# Generate tasks using make_tasks() - DRY
_tasks = make_tasks(PostgresDeployer, load_shared_tasks(__file__))
status = _tasks["status"]
pre_compose = _tasks["pre_compose"]
composing = _tasks["composing"]
post_compose = _tasks["post_compose"]
setup = _tasks["setup"]
