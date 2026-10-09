"""PostgreSQL shared tasks"""

import re
from invoke import task
from libs.common import check_service
from libs.core.environ import get_env, with_env_suffix
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


@task
def ensure_database(c, name, owner=None):
    """Idempotently create a database if it does not already exist."""
    _validate_identifier(name, "database name")
    if owner:
        _validate_identifier(owner, "owner name")
    e = get_env()
    container = with_env_suffix("platform-postgres", e)
    owner_clause = f"OWNER {owner}" if owner else ""
    cmd = (
        f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -tc '
        f"\\\"SELECT 1 FROM pg_database WHERE datname = '{name}'\\\" | grep -q 1 || "
        f"docker exec {container} psql -U postgres -c "
        f'\\"CREATE DATABASE {name} {owner_clause};\\""'
    )
    run_with_status(c, cmd, f"Ensure database {name}")


@task
def ensure_user(c, username, database, password, connection_limit=8):
    """Idempotently create or update a user with connection limits and timeout guards."""
    _validate_identifier(username, "username")
    _validate_identifier(database, "database name")
    e = get_env()
    container = with_env_suffix("platform-postgres", e)
    escaped_password = password.replace("'", "''")
    cmd = (
        f'ssh root@{e["VPS_HOST"]} "docker exec {container} psql -U postgres -c \\"'
        f"DO \\$\\$ BEGIN "
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{username}') THEN "
        f"    CREATE USER {username} WITH PASSWORD '{escaped_password}' CONNECTION LIMIT {connection_limit}; "
        f"  ELSE "
        f"    ALTER ROLE {username} WITH CONNECTION LIMIT {connection_limit}; "
        f"  END IF; "
        f"  ALTER ROLE {username} SET idle_in_transaction_session_timeout = '60s'; "
        f"  ALTER ROLE {username} SET statement_timeout = '30s'; "
        f'END \\$\\$;\\""'
    )
    cmd_grant = f"ssh root@{e['VPS_HOST']} \"docker exec {container} psql -U postgres -c 'GRANT ALL PRIVILEGES ON DATABASE {database} TO {username};'\""
    run_with_status(c, cmd, f"Ensure user {username} with limits")
    run_with_status(c, cmd_grant, f"Grant {database} to {username}")
