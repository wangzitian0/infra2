"""PostgreSQL deployment - DRY version"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from invoke import task
from libs.deployer import Deployer
from libs.console import env_vars, success


class PostgresDeployer(Deployer):
    service = "postgres"
    compose_path = "platform/01.postgres/compose.yaml"
    data_path = "/data/platform/postgres"
    chmod = "700"
    secret_key = "root_password"
    env_var_name = "POSTGRES_PASSWORD"


@task
def pre_compose(c):
    return PostgresDeployer.pre_compose(c)


@task
def composing(c):
    PostgresDeployer.composing(c)


@task
def post_compose(c):
    from . import shared_tasks
    return PostgresDeployer.post_compose(c, shared_tasks)


@task(pre=[pre_compose, composing, post_compose])
def setup(c):
    success(f"{PostgresDeployer.service} setup complete!")
