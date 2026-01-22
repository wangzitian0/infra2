"""PostgreSQL shared tasks"""

import re
from invoke import task
from libs.common import check_service, get_env, with_env_suffix
from libs.console import run_with_status, error


def _validate_identifier(value: str, label: str) -> str:
    """Validate PostgreSQL identifier (database/user name) to prevent injection"""
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", value):
        error(
            f"Invalid {label}: '{value}'. Must start with letter/underscore and contain only alphanumeric/underscore."
        )
        raise ValueError(f"Invalid {label}")
    return value


@task
def status(c):
    """Check PostgreSQL status"""
    return check_service(c, "postgres", "pg_isready")


@task
def create_database(c, name):
    """Create a database"""
    _validate_identifier(name, "database name")
    e = get_env()
    container = with_env_suffix("platform-postgres", e)
    cmd = f"ssh root@{e['VPS_HOST']} \"docker exec {container} psql -U postgres -c 'CREATE DATABASE {name};'\""
    run_with_status(c, cmd, f"Create database {name}")


@task
def create_user(c, username, database, password):
    """Create a user with database access"""
    _validate_identifier(username, "username")
    _validate_identifier(database, "database name")
    e = get_env()
    container = with_env_suffix("platform-postgres", e)
    # SECURITY: Escape single quotes in password for PostgreSQL
    escaped_password = password.replace("'", "''")
    cmd_create = f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -c \\"CREATE USER {username} WITH PASSWORD \'{escaped_password}\';\\""'
    cmd_grant = f"ssh root@{e['VPS_HOST']} \"docker exec {container} psql -U postgres -c 'GRANT ALL PRIVILEGES ON DATABASE {database} TO {username};'\""
    run_with_status(c, cmd_create, f"Create user {username}")
    run_with_status(c, cmd_grant, f"Grant {database} to {username}")
