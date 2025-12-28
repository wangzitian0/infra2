"""Authentik deployment - has custom logic"""
from invoke import task
from libs.deployer import Deployer
from libs.common import generate_password, get_env
from libs.console import header, success, warning, info, env_vars, run_with_status


class AuthentikDeployer(Deployer):
    service = "authentik"
    compose_path = "platform/10.authentik/compose.yaml"
    data_path = "/data/platform/authentik"
    uid = "1000"
    gid = "1000"
    secret_key = "secret_key"
    env_var_name = "AUTHENTIK_SECRET_KEY"


def _get_shared_tasks():
    """Import shared_tasks dynamically to avoid relative import issues"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "shared_tasks",
        Path(__file__).parent / "shared_tasks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@task
def pre_compose(c):
    """Authentik has custom pre_compose logic"""
    if not AuthentikDeployer._prepare_dirs(c):
        return None
    
    e = AuthentikDeployer.env()
    
    # Check dependencies
    warning("Checking dependencies...")
    c.run("invoke postgres.shared.status", hide=True, warn=True)
    c.run("invoke redis.shared.status", hide=True, warn=True)
    
    # Read secrets from Vault
    pg_pass = AuthentikDeployer.read_secret(c, f"{e['PROJECT']}/{e['ENV']}/postgres", "root_password")
    redis_pass = AuthentikDeployer.read_secret(c, f"{e['PROJECT']}/{e['ENV']}/redis", "password")
    
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
    secret_key = generate_password(50)
    if not AuthentikDeployer.store_secret(c, "secret_key", secret_key):
        return None
    
    env_vars("DOKPLOY ENV", {"AUTHENTIK_SECRET_KEY": secret_key, "PG_PASS": pg_pass, "REDIS_PASSWORD": redis_pass})
    success("pre_compose complete")
    return {"AUTHENTIK_SECRET_KEY": secret_key, "PG_PASS": pg_pass, "REDIS_PASSWORD": redis_pass}


@task
def composing(c):
    AuthentikDeployer.composing(c, ["AUTHENTIK_SECRET_KEY", "PG_PASS", "REDIS_PASSWORD"])


@task
def post_compose(c):
    shared_tasks = _get_shared_tasks()
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
