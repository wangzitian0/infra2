"""Authentik deployment - fetches secrets from Vault and outputs for Dokploy"""
import sys
from invoke import task
from libs.deployer import Deployer
from libs.common import get_env
from libs.console import header, success, warning, error, info, env_vars, run_with_status
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
    """Ensure secrets exist in Vault for vault-init to fetch at runtime"""
    if not AuthentikDeployer._prepare_dirs(c):
        return None
    
    e = AuthentikDeployer.env()
    env_name = e.get('ENV', 'production')
    project = e.get('PROJECT', 'platform')
    
    # Check dependencies
    warning("Checking dependencies...")
    c.run("invoke postgres.shared.status", hide=True, warn=True)
    c.run("invoke redis.shared.status", hide=True, warn=True)
    
    # Verify secrets exist in Vault (vault-init will fetch at runtime)
    pg_secrets = get_secrets(project, "postgres", env_name)
    redis_secrets = get_secrets(project, "redis", env_name)
    
    if not pg_secrets.get("root_password"):
        error("Postgres password not found in Vault - run postgres.setup first")
        return None
    if not redis_secrets.get("password"):
        error("Redis password not found in Vault - run redis.setup first")
        return None
        
    success("Verified postgres and redis secrets in Vault")
    
    # Create subdirs and database
    run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {AuthentikDeployer.data_path}/media {AuthentikDeployer.data_path}/certs'", "Create subdirs")
    run_with_status(c, f"ssh root@{e['VPS_HOST']} \"docker exec platform-postgres psql -U postgres -c 'CREATE DATABASE authentik;'\"", "Create database", hide=True)
    
    # Ensure Authentik secret exists
    authentik_secrets = get_secrets(project, "authentik", env_name)
    secret_key = authentik_secrets.get("secret_key")
    if not secret_key:
        secret_key = generate_password(50)
        if not authentik_secrets.set("secret_key", secret_key):
            error("Failed to store Authentik secret key in Vault")
            return None
        warning("Generated new Authentik secret key in Vault")
    else:
        info("Authentik secret key exists in Vault")
    
    # For vault-init: only pass VAULT_ADDR (VAULT_APP_TOKEN set in Dokploy service env)
    result = {
        "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'zitian.party')}"),
    }
    
    env_vars("DOKPLOY ENV (vault-init)", result)
    success("pre_compose complete - vault-init will fetch secrets at runtime")
    info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
    return result


@task
def composing(c, env_context=None):
    if env_context is None:
        warning("Running composing manually - fetching secrets")
        env_context = pre_compose(c)
        if env_context is None:
            error("Failed to get secrets")
            return
    AuthentikDeployer.composing(c, env_context)


@task
def post_compose(c):
    e = get_env()
    header(f"{AuthentikDeployer.service} post_compose", "Verifying")
    
    # Configure domain via Dokploy API
    try:
        from libs.dokploy import get_dokploy
        client = get_dokploy()
        
        # Find compose by name
        compose = client.find_compose_by_name(AuthentikDeployer.service, "platform")
        if compose:
            compose_id = compose["composeId"]
            
            # Check if domain already exists
            existing_domains = client.list_domains(compose_id)
            domain_host = f"sso.{e['INTERNAL_DOMAIN']}"
            
            if not any(d.get("host") == domain_host for d in existing_domains):
                info(f"Configuring domain: {domain_host}")
                client.create_domain(
                    compose_id=compose_id,
                    host=domain_host,
                    port=9000,
                    https=True,
                    service_name="server"
                )
                success(f"Domain configured: https://{domain_host}")
            else:
                info(f"Domain already configured: {domain_host}")
    except Exception as exc:
        warning(f"Domain configuration failed: {exc}")
    
    if shared_tasks:
        result = shared_tasks.status(c)
        if result["is_ready"]:
            info(f"Setup: https://sso.{e['INTERNAL_DOMAIN']}/if/flow/initial-setup/")
            success(f"post_compose complete - {result['details']}")
            return True
    return False


@task
def setup(c):
    """Full setup - skips if healthy"""
    try:
        if shared_tasks:
            result = shared_tasks.status(c)
            if result.get("is_ready"):
                success(f"{AuthentikDeployer.service} already healthy - skipping")
                return
    except Exception as exc:
        warning(f"Status check failed: {exc}")
    
    warning(f"{AuthentikDeployer.service} not healthy - starting install")
    env_result = pre_compose(c)
    if env_result is None:
        error("pre_compose failed")
        return
    composing(c, env_result)
    post_compose(c)
    success("Authentik setup complete!")
