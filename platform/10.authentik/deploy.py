"""Authentik deployment - has custom logic"""
import sys
from invoke import task
from libs.deployer import Deployer
from libs.common import get_env
from libs.console import header, success, warning, info, env_vars, run_with_status
from libs.env import generate_password, get_secrets

shared_tasks = sys.modules.get("platform.10.authentik.shared")


class AuthentikDeployer(Deployer):
    service = "authentik"
    compose_path = "platform/10.authentik/compose.yaml"
    data_path = "/data/platform/authentik"
    uid = "1000"
    gid = "1000"
    secret_key = "secret_key"
    env_var_name = "AUTHENTIK_SECRET_KEY"


@task
def pre_compose(c):
    """Authentik has custom pre_compose logic"""
    if not AuthentikDeployer._prepare_dirs(c):
        return None
    
    e = AuthentikDeployer.env()
    env_name = e.get('ENV', 'production')
    project = e.get('PROJECT', 'platform')
    
    # Check dependencies
    warning("Checking dependencies...")
    c.run("invoke postgres.shared.status", hide=True, warn=True)
    c.run("invoke redis.shared.status", hide=True, warn=True)
    
    # Read secrets from Vault
    pg_secrets = get_secrets(project, "postgres", env_name)
    redis_secrets = get_secrets(project, "redis", env_name)
    
    pg_pass = pg_secrets.get("root_password")
    redis_pass = redis_secrets.get("password")
    
    if pg_pass and redis_pass:
        success("Read passwords from Vault")
    else:
        warning("Could not read from Vault, manual entry required")
        pg_pass = pg_pass or "<get from Vault>"
        redis_pass = redis_pass or "<get from Vault>"
    
    # Create subdirs and database
    run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {AuthentikDeployer.data_path}/media {AuthentikDeployer.data_path}/certs'", "Create subdirs")
    run_with_status(c, f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c 'CREATE DATABASE authentik;'\"", "Create database", hide=True)
    
    # Store secret
    authentik_secrets = get_secrets(project, "authentik", env_name)
    secret_key = generate_password(50)
    if not authentik_secrets.set("secret_key", secret_key):
        warning("Failed to store Authentik secret key in Vault")
    
    env_vars("DOKPLOY ENV", {"AUTHENTIK_SECRET_KEY": secret_key, "PG_PASS": pg_pass, "REDIS_PASSWORD": redis_pass})
    success("pre_compose complete")
    return {"AUTHENTIK_SECRET_KEY": secret_key, "PG_PASS": pg_pass, "REDIS_PASSWORD": redis_pass}


@task
def composing(c):
    AuthentikDeployer.composing(c)


@task
def post_compose(c):
    e = get_env()
    header(f"{AuthentikDeployer.service} post_compose", "Verifying")
    if shared_tasks:
        result = shared_tasks.status(c)
        if result["is_ready"]:
            info(f"Setup: https://sso.{e['INTERNAL_DOMAIN']}/if/flow/initial-setup/")
            success(f"post_compose complete - {result['details']}")
            return True
    return False


@task(pre=[pre_compose, composing, post_compose])
def setup(c):
    success("Authentik setup complete!")
