"""PostgreSQL shared tasks"""

from invoke import task
from libs.common import check_service, get_env, with_env_suffix
from libs.console import run_with_status


@task
def status(c):
    """Check PostgreSQL status"""
    return check_service(c, "postgres", "pg_isready")


@task
def create_database(c, name):
    """Create a database"""
    e = get_env()
    container = with_env_suffix("platform-postgres", e)
    cmd = f"ssh root@{e['VPS_HOST']} \"docker exec {container} psql -U postgres -c 'CREATE DATABASE {name};'\""
    run_with_status(c, cmd, f"Create database {name}")


@task
def create_user(c, username, database, password):
    """Create a user with database access"""
    e = get_env()
    container = with_env_suffix("platform-postgres", e)
    cmd_create = f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -c \\"CREATE USER {username} WITH PASSWORD \'{password}\';\\""'
    cmd_grant = f"ssh root@{e['VPS_HOST']} \"docker exec {container} psql -U postgres -c 'GRANT ALL PRIVILEGES ON DATABASE {database} TO {username};'\""
    run_with_status(c, cmd_create, f"Create user {username}")
    run_with_status(c, cmd_grant, f"Grant {database} to {username}")
