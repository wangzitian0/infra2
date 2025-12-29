"""Authentik deployment - has custom logic"""
from invoke import task
from libs.deployer import Deployer, load_shared_tasks
from libs.common import generate_password, get_env, CONTAINER_NAMES
from libs.console import header, success, warning, info, env_vars, run_with_status
from libs.env import EnvManager


class AuthentikDeployer(Deployer):
    service = "authentik"
    compose_path = "platform/10.authentik/compose.yaml"
    data_path = "/data/platform/authentik"
    uid = "1000"
    gid = "1000"
    secret_key = "AUTHENTIK_SECRET_KEY"
    env_var_name = "AUTHENTIK_SECRET_KEY"


@task
def pre_compose(c):
    """Authentik has custom pre_compose logic"""
    if not AuthentikDeployer._prepare_dirs(c):
        return None
    
    e = AuthentikDeployer.env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    project = e.get("PROJECT", "platform")
    env_name = e.get("ENV", "production")
    
    # Check dependencies
    warning("Checking dependencies...")
    if not c.run("invoke postgres.shared.status", hide=True, warn=True).ok:
        warning("PostgreSQL status check failed")
    if not c.run("invoke redis.shared.status", hide=True, warn=True).ok:
        warning("Redis status check failed")
    
    # Read secrets from Vault
    pg_mgr = EnvManager(project, env_name, "postgres")
    redis_mgr = EnvManager(project, env_name, "redis")
    pg_pass = pg_mgr.get_secret("root_password")
    redis_pass = redis_mgr.get_secret("password")

    if not pg_pass or not redis_pass:
        warning("Missing database credentials in Vault")
        return None
    success("Read passwords from Vault")
    
    # Create subdirs and database
    run_with_status(
        c,
        f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {AuthentikDeployer.data_path}/media {AuthentikDeployer.data_path}/certs'",
        "Create subdirs",
    )
    db_cmd = (
        f"docker exec {CONTAINER_NAMES['postgres']} psql -U postgres -tc "
        "\"SELECT 1 FROM pg_database WHERE datname='authentik'\" | "
        "grep -q 1 || "
        f"docker exec {CONTAINER_NAMES['postgres']} psql -U postgres -c \"CREATE DATABASE authentik;\""
    )
    run_with_status(
        c,
        f"ssh {ssh_user}@{e['VPS_HOST']} \"{db_cmd}\"",
        "Ensure database",
        hide=True,
    )
    
    # Store secret
    auth_mgr = AuthentikDeployer.get_env_manager()
    secret_key = auth_mgr.get_secret(AuthentikDeployer.secret_key)
    if secret_key is None:
        secret_key = generate_password(50)
        if not auth_mgr.set_secret(AuthentikDeployer.secret_key, secret_key):
            warning("Failed to store AUTHENTIK_SECRET_KEY in Vault")
            return None
    
    env_vars("DOKPLOY ENV", {"AUTHENTIK_SECRET_KEY": secret_key, "PG_PASS": pg_pass, "REDIS_PASSWORD": redis_pass})
    success("pre_compose complete")
    return {"AUTHENTIK_SECRET_KEY": secret_key, "PG_PASS": pg_pass, "REDIS_PASSWORD": redis_pass}


@task
def composing(c):
    AuthentikDeployer.composing(c, ["AUTHENTIK_SECRET_KEY", "PG_PASS", "REDIS_PASSWORD"])


@task
def post_compose(c):
    shared_tasks = load_shared_tasks(__file__)
    e = get_env()
    header(f"{AuthentikDeployer.service} post_compose", "Verifying")
    result = shared_tasks.status(c)
    if result["is_ready"]:
        info(f"Setup: https://sso.{e['INTERNAL_DOMAIN']}/if/flow/initial-setup/")
        success(f"post_compose complete - {result['details']}")
        return True
    return False


@task(pre=[pre_compose, composing, post_compose])
def setup(c):
    success("Authentik setup complete!")
