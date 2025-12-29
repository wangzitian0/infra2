"""PostgreSQL shared tasks - DRY version"""
import re
from invoke import task
from libs.common import check_docker_service, get_env, CONTAINER_NAMES
from libs.console import error, info


@task
def status(c):
    """Check PostgreSQL status"""
    return check_docker_service(c, CONTAINER_NAMES["postgres"], "pg_isready", "PostgreSQL")


def _validate_identifier(value: str, label: str) -> bool:
    if not re.match(r"^[A-Za-z0-9_]+$", value):
        error(f"Invalid {label}: {value}")
        return False
    return True


@task
def create_database(c, name):
    """Create a database"""
    if not _validate_identifier(name, "database name"):
        return
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    info(f"Creating database: {name}")
    c.run(
        f"ssh {ssh_user}@{e['VPS_HOST']} "
        f"\"docker exec {CONTAINER_NAMES['postgres']} psql -U postgres -c 'CREATE DATABASE {name};'\"",
        warn=True,
    )


@task
def create_user(c, username, database, password):
    """Create a user with access to a database"""
    if not _validate_identifier(username, "username") or not _validate_identifier(database, "database name"):
        return
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    info(f"Creating user: {username}")
    c.run(
        f"ssh {ssh_user}@{e['VPS_HOST']} "
        f"\"docker exec {CONTAINER_NAMES['postgres']} psql -U postgres -c \\\"CREATE USER {username} WITH PASSWORD '{password}';\\\"\"",
        warn=True,
    )
    c.run(
        f"ssh {ssh_user}@{e['VPS_HOST']} "
        f"\"docker exec {CONTAINER_NAMES['postgres']} psql -U postgres -c 'GRANT ALL PRIVILEGES ON DATABASE {database} TO {username};'\"",
        warn=True,
    )
