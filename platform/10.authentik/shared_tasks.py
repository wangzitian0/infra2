"""Authentik shared tasks for API operations"""
from invoke import task
from libs.common import check_service, get_env
from libs.console import header, success, error, warning, info


@task
def status(c):
    """Check Authentik status"""
    return check_service(c, "authentik", "ak healthcheck")


@task
def create_proxy_app(c, name, slug, external_host, internal_host, port=None):
    """Create Authentik application with proxy provider
    
    First, create an API token in Authentik Web UI:
    1. Go to https://sso.{INTERNAL_DOMAIN}/if/admin/#/core/tokens
    2. Create token with name "automation" and all permissions
    3. Save token to Vault: vault kv put secret/platform/production/authentik api_token=<token>
    
    Args:
        name: Application name (e.g., "Portal")
        slug: Application slug (e.g., "portal")
        external_host: External URL (e.g., "https://home.zitian.party")
        internal_host: Internal service host (e.g., "platform-portal")
        port: Internal service port (default: 8080)
    
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
    
    header(f"Creating Authentik app: {name}", "Proxy Provider (Forward Auth)")
    
    e = get_env()
    env_name = e.get('ENV', 'production')
    
    # Get API token from Vault
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    api_token = authentik_secrets.get("api_token")
    
    if not api_token:
        error("API token not found in Vault")
        info("\nTo create an API token:")
        info("1. Login to https://sso.{}/if/admin/".format(e.get('INTERNAL_DOMAIN')))
        info("2. Go to Directory → Tokens → Create")
        info("3. Set intent: API Token")
        info("4. Save to Vault:")
        info("   export VAULT_ADDR=https://vault.{}".format(e.get('INTERNAL_DOMAIN')))
        info("   export VAULT_TOKEN=<your-root-token>")
        info("   vault kv patch secret/platform/production/authentik api_token=<token>")
        return False
    
    base_url = f"https://sso.{e.get('INTERNAL_DOMAIN')}"
    port = port or 8080
    internal_url = f"http://{internal_host}:{port}"
    
    try:
        # Use API token for authentication
        client = httpx.Client(
            verify=True,
            headers={"Authorization": f"Bearer {api_token}"}
        )
        
        # Test authentication
        info("Testing API token...")
        resp = client.get(f"{base_url}/api/v3/core/users/me/")
        if resp.status_code != 200:
            error(f"API token authentication failed: {resp.status_code}")
            return False
        
        user = resp.json()
        success(f"Authenticated as: {user['username']}")
        
        # Get default authorization flow UUID
        resp = client.get(f"{base_url}/api/v3/flows/instances/?slug=default-provider-authorization-implicit-consent")
        if resp.status_code != 200:
            error(f"Failed to get authorization flow: {resp.status_code}")
            return False
        
        flows = resp.json()["results"]
        if not flows:
            error("Default authorization flow not found")
            return False
        
        auth_flow_uuid = flows[0]["pk"]
        
        # Create proxy provider
        info(f"Creating proxy provider for {external_host}...")
        provider_data = {
            "name": f"{slug}-proxy",
            "authorization_flow": auth_flow_uuid,
            "mode": "forward_single",
            "external_host": external_host,
            "internal_host": internal_url,
        }
        
        resp = client.post(
            f"{base_url}/api/v3/providers/proxy/",
            json=provider_data
        )
        
        if resp.status_code != 201:
            error(f"Failed to create provider: {resp.status_code} - {resp.text}")
            return False
        
        provider_id = resp.json()["pk"]
        success(f"Created proxy provider: {provider_id}")
        
        # Create application
        info(f"Creating application: {name}...")
        app_data = {
            "name": name,
            "slug": slug,
            "provider": provider_id,
        }
        
        resp = client.post(
            f"{base_url}/api/v3/core/applications/",
            json=app_data
        )
        
        if resp.status_code != 201:
            error(f"Failed to create application: {resp.status_code} - {resp.text}")
            return False
        
        app_id = resp.json()["slug"]
        success(f"Created application: {name} (slug: {app_id})")
        
        info("\n✨ Configuration complete!")
        info(f"Application URL: {base_url}/if/admin/#/core/applications/{app_id}")
        info(f"External URL: {external_host}")
        info(f"Internal URL: {internal_url}")
        info("\nForward auth middleware should now work - test by visiting the external URL")
        
        return True
        
    except Exception as exc:
        error(f"API error: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        client.close()


@task
def list_apps(c):
    """List all Authentik applications"""
    import httpx
    from libs.env import get_secrets
    
    header("Listing Authentik applications", "API Query")
    
    e = get_env()
    env_name = e.get('ENV', 'production')
    
    # Get bootstrap credentials
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    admin_email = authentik_secrets.get("bootstrap_email")
    admin_password = authentik_secrets.get("bootstrap_password")
    
    if not admin_email or not admin_password:
        error("Bootstrap credentials not found in Vault")
        return False
    
    base_url = f"https://sso.{e.get('INTERNAL_DOMAIN')}"
    
    try:
        client = httpx.Client(verify=True)
        
        # Login (simplified - reuse from create_proxy_app)
        resp = client.post(
            f"{base_url}/api/v3/flows/executor/default-authentication-flow/",
            json={"uid_field": admin_email, "password": admin_password}
        )
        
        token = None
        for cookie in client.cookies.jar:
            if cookie.name == "authentik_session":
                token = cookie.value
                break
        
        if not token:
            error("Authentication failed")
            return False
        
        client.headers["Authorization"] = f"Bearer {token}"
        
        # List applications
        resp = client.get(f"{base_url}/api/v3/core/applications/")
        
        if resp.status_code != 200:
            error(f"Failed to list applications: {resp.status_code}")
            return False
        
        apps = resp.json()["results"]
        
        if not apps:
            info("No applications found")
            return True
        
        info(f"Found {len(apps)} application(s):\n")
        for app in apps:
            success(f"• {app['name']} (slug: {app['slug']})")
            if app.get('provider_obj'):
                provider = app['provider_obj']
                info(f"  Provider: {provider.get('name')} ({provider.get('pk')})")
        
        return True
        
    except Exception as exc:
        error(f"API error: {type(exc).__name__}: {exc}")
        return False
    finally:
        client.close()
