"""Authentik shared tasks for API operations

Token Hierarchy (mirrors Vault):
- AUTHENTIK_ROOT_TOKEN: Full admin access, creates apps and issues app tokens
- AUTHENTIK_APP_TOKEN: Per-service, limited to own SSO configuration

Storage in Vault:
- secret/platform/production/authentik/root_token: Admin API token
- secret/platform/production/<service>/sso_*: Per-service SSO config
"""
import os
from invoke import task
from libs.common import check_service, get_env
from libs.console import header, success, error, warning, info


@task
def status(c):
    """Check Authentik status"""
    return check_service(c, "authentik", "ak healthcheck")


@task
def create_root_token(c):
    """Create Authentik Root Token for SSO administration
    
    This creates the admin API token stored as 'root_token' in Vault.
    Requires VAULT_ROOT_TOKEN to write to Vault.
    
    The Authentik Root Token is used to:
    - Create SSO applications
    - Issue per-service app tokens
    - Manage providers and policies
    
    Example:
        export VAULT_ROOT_TOKEN=<vault-admin-token>
        invoke authentik.shared.create-root-token
    """
    from libs.env import get_secrets
    
    header("Creating Authentik Root Token", "SSO Admin Setup")
    
    e = get_env()
    env_name = e.get('ENV', 'production')
    vault_root_token = os.getenv('VAULT_ROOT_TOKEN')
    
    if not vault_root_token:
        error("VAULT_ROOT_TOKEN not set")
        info("Vault admin token needed to store Authentik root token")
        info("Get from: op read 'op://Infra2/bootstrap/vault/Root Token/Root Token'")
        info("Then: export VAULT_ROOT_TOKEN=<token>")
        return False
    
    info("Running token creation on server...")
    
    script = f"""
set -e
cd /etc/dokploy/compose/platform-authentik-*/code/platform/10.authentik
docker compose run --rm -e VAULT_INIT_TOKEN={vault_root_token} -e VAULT_INIT_ADDR=https://vault.{e['INTERNAL_DOMAIN']} token-init
"""
    
    result = c.run(f"ssh root@{e['VPS_HOST']} '{script}'", warn=True)
    
    if not result.ok:
        error("Failed to create token")
        return False
    
    # Verify in Vault
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get("api_token")
    
    if root_token:
        success("Authentik Root Token created and stored in Vault")
        info(f"Vault path: secret/platform/production/authentik (key: root_token)")
        info(f"Token prefix: {root_token[:20]}...")
        info("\nYou can now create SSO apps:")
        info("  invoke authentik.shared.create-proxy-app --name=Portal --slug=portal ...")
        return True
    else:
        warning("Token creation ran but not found in Vault")
        return False


@task
def create_proxy_app(c, name, slug, external_host, internal_host, port=None):
    """Create SSO application with proxy provider
    
    Uses Authentik Root Token to create a forward-auth protected application.
    
    Args:
        name: Application name (e.g., "Portal")
        slug: Application slug (e.g., "portal")  
        external_host: External URL (e.g., "https://home.zitian.party")
        internal_host: Internal service (e.g., "platform-portal")
        port: Internal port (default: 8080)
    
    Example:
        invoke authentik.shared.create-proxy-app \\
            --name="Portal" \\
            --slug="portal" \\
            --external-host="https://home.zitian.party" \\
            --internal-host="platform-portal" \\
            --port=8080
    """
    import httpx
    from libs.env import get_secrets
    
    header(f"Creating SSO App: {name}", "Proxy Provider (Forward Auth)")
    
    e = get_env()
    env_name = e.get('ENV', 'production')
    
    # Get Authentik Root Token from Vault
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get("api_token")
    
    if not root_token:
        error("Authentik Root Token not found in Vault")
        info("\nRun first: invoke authentik.shared.create-root-token")
        return False
    
    base_url = f"https://sso.{e.get('INTERNAL_DOMAIN')}"
    port = port or 8080
    internal_url = f"http://{internal_host}:{port}"
    
    try:
        client = httpx.Client(
            verify=True,
            headers={"Authorization": f"Bearer {root_token}"}
        )
        
        # Test auth
        info("Verifying Authentik Root Token...")
        resp = client.get(f"{base_url}/api/v3/core/users/me/")
        if resp.status_code != 200:
            error(f"Root token auth failed: {resp.status_code}")
            return False
        
        user = resp.json()
        success(f"Authenticated as: {user['username']}")
        
        # Get auth flow UUID
        resp = client.get(f"{base_url}/api/v3/flows/instances/?slug=default-provider-authorization-implicit-consent")
        if resp.status_code != 200 or not resp.json()["results"]:
            error("Default authorization flow not found")
            return False
        
        auth_flow_uuid = resp.json()["results"][0]["pk"]
        
        # Create proxy provider
        info(f"Creating proxy provider for {external_host}...")
        resp = client.post(
            f"{base_url}/api/v3/providers/proxy/",
            json={
                "name": f"{slug}-proxy",
                "authorization_flow": auth_flow_uuid,
                "mode": "forward_single",
                "external_host": external_host,
                "internal_host": internal_url,
            }
        )
        
        if resp.status_code != 201:
            error(f"Failed to create provider: {resp.status_code} - {resp.text}")
            return False
        
        provider_id = resp.json()["pk"]
        success(f"Created proxy provider: {provider_id}")
        
        # Create application
        info(f"Creating application: {name}...")
        resp = client.post(
            f"{base_url}/api/v3/core/applications/",
            json={
                "name": name,
                "slug": slug,
                "provider": provider_id,
            }
        )
        
        if resp.status_code != 201:
            error(f"Failed to create application: {resp.status_code} - {resp.text}")
            return False
        
        app_slug = resp.json()["slug"]
        success(f"Created application: {name} (slug: {app_slug})")
        
        info(f"\n✨ SSO protection enabled for {external_host}")
        info(f"Admin URL: {base_url}/if/admin/#/core/applications/{app_slug}")
        
        return True
        
    except Exception as exc:
        error(f"API error: {type(exc).__name__}: {exc}")
        return False
    finally:
        client.close()


@task
def list_apps(c):
    """List all Authentik applications"""
    import httpx
    from libs.env import get_secrets
    
    header("Listing SSO Applications", "")
    
    e = get_env()
    env_name = e.get('ENV', 'production')
    
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get("api_token")
    
    if not root_token:
        error("Authentik Root Token not found")
        return False
    
    base_url = f"https://sso.{e.get('INTERNAL_DOMAIN')}"
    
    try:
        client = httpx.Client(headers={"Authorization": f"Bearer {root_token}"})
        resp = client.get(f"{base_url}/api/v3/core/applications/")
        
        if resp.status_code != 200:
            error(f"API error: {resp.status_code}")
            return False
        
        apps = resp.json()["results"]
        
        if not apps:
            info("No applications configured")
            return True
        
        info(f"Found {len(apps)} application(s):\n")
        for app in apps:
            success(f"• {app['name']} (slug: {app['slug']})")
        
        return True
        
    except Exception as exc:
        error(f"API error: {exc}")
        return False
    finally:
        client.close()
