"""PostgreSQL shared tasks"""
from invoke import task
from libs.common import check_service, get_env


@task
def status(c):
    """Check PostgreSQL status"""
    return check_service(c, "postgres", "pg_isready")


@task
def create_database(c, name):
    """Create a database"""
    e = get_env()
    c.run(f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c 'CREATE DATABASE {name};'\"", warn=True)


@task
def create_user(c, username, database, password):
    """Create a user with database access"""
    e = get_env()
    c.run(f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c \\\"CREATE USER {username} WITH PASSWORD '{password}';\\\"\"", warn=True)
    c.run(f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c 'GRANT ALL ON DATABASE {database} TO {username};'\"", warn=True)
